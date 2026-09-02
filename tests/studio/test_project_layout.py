"""Project layout: user data is isolated, validated and never overwritten."""
from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest


def _init(tmp_path: Path, project: str = "alpha", funnel: str = "main"):
    from studio.project_layout import initialize_project

    package_root = Path(__file__).resolve().parents[2]
    return initialize_project(
        projects_root=tmp_path / "projects",
        templates_root=package_root / "templates",
        project_slug=project,
        funnel_slug=funnel,
    )


def test_two_slug_pairs_create_independent_funnel_trees(tmp_path):
    """Ловит общую рабочую папку, из-за которой один проект меняет другой."""
    first = _init(tmp_path, "alpha", "primary")
    second = _init(tmp_path, "beta", "secondary")

    assert first.funnel_root == tmp_path / "projects/alpha/funnels/primary"
    assert second.funnel_root == tmp_path / "projects/beta/funnels/secondary"
    assert first.funnel_root != second.funnel_root
    assert first.state_path.is_file()
    assert second.state_path.is_file()
    assert first.events_path.is_file()
    assert second.events_path.is_file()
    snapshot = json.loads(first.state_path.read_text(encoding="utf-8"))
    assert snapshot == {
        "snapshot_version": "studio-snapshot/v1",
        "state": {
            "schema_version": "state/v1",
            "phase": "DISCOVERY",
            "gate": "FunnelFit",
            "gate_status": "NOT_STARTED",
            "technical_readiness": "prototype_only",
            "market_readiness": "NOT_READY",
        },
        "last_verified_gate": None,
        "next_gate": "FunnelFit",
        "blocked_reason": None,
        "revision": 0,
        "evidence_hashes": {},
    }


@pytest.mark.parametrize("slug", ["../escape", "/absolute", "nested/name", "nested\\name", ".", "..", ""])
def test_invalid_slugs_are_rejected_before_any_write(tmp_path, slug):
    """Ловит traversal/absolute path, создающий данные вне projects root."""
    from studio.errors import InvalidSlugError

    projects_root = tmp_path / "projects"
    with pytest.raises(InvalidSlugError):
        _init(tmp_path, slug, "safe")

    assert not projects_root.exists()


def test_symlink_escape_is_rejected_before_writing_outside(tmp_path):
    """Ловит запись через project symlink за доверенной границей."""
    from studio.errors import PathEscapeError

    outside = tmp_path / "outside"
    outside.mkdir()
    projects = tmp_path / "projects"
    projects.mkdir()
    (projects / "alpha").symlink_to(outside, target_is_directory=True)
    before = list(outside.iterdir())

    with pytest.raises(PathEscapeError):
        _init(tmp_path)

    assert list(outside.iterdir()) == before


@pytest.mark.parametrize("collision", ["project", "funnels", "funnel"])
def test_file_directory_collisions_are_rejected_without_overwrite(tmp_path, collision):
    """Ловит замену существующего файла каталогом при инициализации."""
    from studio.errors import PathCollisionError

    projects = tmp_path / "projects"
    projects.mkdir()
    if collision == "project":
        target = projects / "alpha"
    elif collision == "funnels":
        (projects / "alpha").mkdir()
        target = projects / "alpha/funnels"
    else:
        (projects / "alpha/funnels").mkdir(parents=True)
        target = projects / "alpha/funnels/main"
    target.write_bytes(b"manual-content")

    with pytest.raises(PathCollisionError):
        _init(tmp_path)

    assert target.read_bytes() == b"manual-content"


def test_unmanaged_existing_funnel_directory_is_a_collision(tmp_path):
    """Ловит присвоение студией чужого каталога с совпавшим именем."""
    from studio.errors import PathCollisionError

    target = tmp_path / "projects/alpha/funnels/main"
    target.mkdir(parents=True)
    manual = target / "manual.txt"
    manual.write_bytes(b"keep me")

    with pytest.raises(PathCollisionError):
        _init(tmp_path)

    assert manual.read_bytes() == b"keep me"
    assert sorted(path.name for path in target.iterdir()) == ["manual.txt"]


def test_reinitialization_preserves_every_manual_byte(tmp_path):
    """Ловит повторное копирование шаблонов поверх правок владельца."""
    paths = _init(tmp_path)
    paths.brief_path.write_bytes(b"manual brief\n\x00")
    paths.state_path.write_bytes(b'{"manual": "state formatting"}\n')
    paths.events_path.write_bytes(b'{"manual":true}\n')
    custom = paths.funnel_root / "custom.bin"
    custom.write_bytes(bytes(range(256)))
    before = {
        path.relative_to(paths.funnel_root): path.read_bytes()
        for path in paths.funnel_root.rglob("*")
        if path.is_file()
    }

    again = _init(tmp_path)

    after = {
        path.relative_to(again.funnel_root): path.read_bytes()
        for path in again.funnel_root.rglob("*")
        if path.is_file()
    }
    assert after == before


def test_current_installer_preserves_characterized_personal_files(tmp_path):
    """Ловит регрессию install.sh, перезаписывающую уже заполненные user files."""
    package_root = Path(__file__).resolve().parents[2]
    office = tmp_path / "office"
    office.mkdir()
    protected = {
        "PROJECT.md": b"manual project\n",
        "engine/quiz/quiz.config.js": b"manual quiz config\n",
        "engine/salesbot/persona/persona.md": b"manual persona\n",
        "engine/salesbot/persona/connections.md": b"manual connections\n",
        "engine/salesbot/config.env": b"MANUAL=1\n",
        "engine/salesbot/tools/antigeneric-custom.txt": b"manual anti\n",
        "engine/salesbot/tools/personas.json": b'{"manual":true}\n',
    }
    destination = office / "engines/quiz-funnel"
    for relative, raw in protected.items():
        path = destination / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(raw)

    subprocess.run(["bash", str(package_root / "install.sh"), str(office)], check=True, capture_output=True)

    assert {relative: (destination / relative).read_bytes() for relative in protected} == protected
