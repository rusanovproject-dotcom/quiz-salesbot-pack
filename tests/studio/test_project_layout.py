"""Project layout: user data is isolated, validated and never overwritten."""
from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest


def _init(
    tmp_path: Path,
    project: str = "alpha",
    funnel: str = "main",
    *,
    templates_root: Path | None = None,
    fault_hook=None,
):
    from studio.project_layout import initialize_project

    package_root = Path(__file__).resolve().parents[2]
    return initialize_project(
        projects_root=tmp_path / "projects",
        templates_root=templates_root or package_root / "templates",
        project_slug=project,
        funnel_slug=funnel,
        fault_hook=fault_hook,
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


@pytest.mark.parametrize("mutation", ["state_json", "brief_contract", "events_nonempty", "symlink"])
def test_invalid_templates_are_diagnosed_before_project_mutation(tmp_path, mutation):
    """Ловит partial project, созданный до parse/contract validation шаблонов."""
    from studio.errors import InvalidProjectTemplateError

    package_templates = Path(__file__).resolve().parents[2] / "templates"
    templates = tmp_path / "templates"
    shutil.copytree(package_templates, templates)
    if mutation == "state_json":
        (templates / "STATE.json").write_bytes(b"not-json")
    elif mutation == "brief_contract":
        (templates / "FUNNEL-BRIEF.meta.json").write_bytes(b"{}")
    elif mutation == "events_nonempty":
        (templates / "events.jsonl").write_bytes(b"not empty\n")
    else:
        outside = tmp_path / "outside-template"
        outside.write_bytes(b"outside")
        (templates / "STATE.json").unlink()
        (templates / "STATE.json").symlink_to(outside)

    with pytest.raises(InvalidProjectTemplateError):
        _init(tmp_path, templates_root=templates)

    assert not (tmp_path / "projects").exists()


@pytest.mark.parametrize(
    "stage",
    [
        "after_preflight",
        "after_staging_directory",
        "after_brief_write",
        "after_brief_metadata_write",
        "after_state_write",
        "after_events_write",
        "after_completion_marker",
        "before_publish",
    ],
)
def test_initialization_failure_never_publishes_partial_funnel_and_retry_completes(tmp_path, stage):
    """Ловит ready target с отсутствующим required file после init failure."""
    def fail(current_stage: str) -> None:
        if current_stage == stage:
            raise OSError(f"injected at {stage}")

    with pytest.raises(OSError, match=stage):
        _init(tmp_path, fault_hook=fail)

    target = tmp_path / "projects/alpha/funnels/main"
    assert not target.exists()
    paths = _init(tmp_path)
    assert paths.state_path.is_file()
    assert paths.events_path.is_file()
    assert (paths.funnel_root / ".funnel-studio-funnel.json").is_file()


@pytest.mark.parametrize(
    "stage",
    [
        "after_preflight",
        "after_staging_directory",
        "after_brief_write",
        "after_brief_metadata_write",
        "after_state_write",
        "after_events_write",
        "after_completion_marker",
        "before_publish",
    ],
)
def test_existing_project_new_funnel_failure_stays_unpublished_and_retry_completes(
    tmp_path, stage
):
    """Ловит partial target в ветке добавления funnel в существующий project."""
    _init(tmp_path, funnel="first")

    def fail(current_stage: str) -> None:
        if current_stage == stage:
            raise OSError(f"injected at {stage}")

    with pytest.raises(OSError, match=stage):
        _init(tmp_path, funnel="second", fault_hook=fail)

    target = tmp_path / "projects/alpha/funnels/second"
    assert not target.exists()
    assert _init(tmp_path, funnel="second").funnel_root == target


def test_failure_after_atomic_publish_is_safe_to_retry_without_rewrite(tmp_path):
    """Ловит повторное копирование после crash/error сразу за directory publish."""
    def fail(stage: str) -> None:
        if stage == "after_publish":
            raise OSError("injected after_publish")

    with pytest.raises(OSError, match="after_publish"):
        _init(tmp_path, fault_hook=fail)
    target = tmp_path / "projects/alpha/funnels/main"
    before = {
        path.relative_to(target): path.read_bytes()
        for path in target.iterdir()
        if path.is_file()
    }

    paths = _init(tmp_path)

    after = {
        path.relative_to(target): path.read_bytes()
        for path in target.iterdir()
        if path.is_file()
    }
    assert paths.funnel_root == target
    assert after == before


def test_incomplete_owned_funnel_is_rejected_without_repairing_manual_bytes(tmp_path):
    """Ловит fast-path, принимающий marker без полного набора managed leaves."""
    from studio.errors import IncompleteProjectError

    paths = _init(tmp_path)
    paths.brief_path.write_bytes(b"manual bytes")
    paths.state_path.unlink()
    before = {
        path.name: path.read_bytes()
        for path in paths.funnel_root.iterdir()
        if path.is_file()
    }

    with pytest.raises(IncompleteProjectError, match="STATE.json"):
        _init(tmp_path)

    assert {
        path.name: path.read_bytes()
        for path in paths.funnel_root.iterdir()
        if path.is_file()
    } == before


def test_invalid_completion_identity_is_not_accepted_as_ready(tmp_path):
    """Ловит fast-path, принимающий malformed random funnel identity."""
    from studio.errors import IncompleteProjectError

    paths = _init(tmp_path)
    marker_path = paths.funnel_root / ".funnel-studio-funnel.json"
    marker = json.loads(marker_path.read_text(encoding="utf-8"))
    marker["funnel_id"] = "not-an-id"
    marker_path.write_text(json.dumps(marker) + "\n", encoding="utf-8")
    before = marker_path.read_bytes()

    with pytest.raises(IncompleteProjectError, match="identity"):
        _init(tmp_path)

    assert marker_path.read_bytes() == before


def test_symlink_swap_after_preflight_cannot_write_outside_projects_root(tmp_path):
    """Ловит ancestor swap между validation и actual initialization I/O."""
    _init(tmp_path, funnel="first")
    projects = tmp_path / "projects"
    project = projects / "alpha"
    moved = projects / "alpha-moved"
    outside = tmp_path / "outside"
    (outside / "funnels").mkdir(parents=True)

    def swap(stage: str) -> None:
        if stage != "after_preflight":
            return
        project.rename(moved)
        project.symlink_to(outside, target_is_directory=True)

    with pytest.raises(Exception):
        _init(tmp_path, funnel="second", fault_hook=swap)

    assert list((outside / "funnels").iterdir()) == []


def test_symlinked_funnel_marker_is_rejected_without_reading_outside(tmp_path):
    """Ловит follow-symlink marker read на re-init fast path."""
    from studio.errors import UnsafeFilesystemEntryError

    paths = _init(tmp_path)
    marker = paths.funnel_root / ".funnel-studio-funnel.json"
    outside = tmp_path / "outside-marker"
    outside.write_text(marker.read_text())
    marker.unlink()
    marker.symlink_to(outside)

    with pytest.raises(UnsafeFilesystemEntryError):
        _init(tmp_path)

    assert outside.is_file()
