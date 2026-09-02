from __future__ import annotations

import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile


ROOT = Path(__file__).resolve().parents[2]


def _copy_runnable_pack(tmp_path: Path) -> Path:
    pack = tmp_path / "pack"
    (pack / "scripts").mkdir(parents=True)
    shutil.copy2(ROOT / "scripts/init-funnel.py", pack / "scripts/init-funnel.py")
    shutil.copytree(ROOT / "studio", pack / "studio")
    shutil.copytree(ROOT / "templates", pack / "templates")
    return pack


def test_root_agents_routes_plain_language_request_to_canonical_preflight():
    text = (ROOT / "AGENTS.md").read_text(encoding="utf-8")

    assert "собери мне квиз-воронку" in text.lower()
    assert "adapters/codex/quiz-funnel/SKILL.md" in text
    assert "skills/preflight/SKILL.md" in text
    assert "определяет\nтолько канонический" in text.lower()
    assert "--project" not in text


def test_readme_has_copy_paste_codex_quickstart_without_placeholder():
    text = (ROOT / "README.md").read_text(encoding="utf-8")

    assert "https://github.com/rusanovproject-dotcom/quiz-salesbot-pack.git" in text
    assert "<адрес этого репозитория>" not in text
    assert "codex" in text.lower()
    assert "Собери мне квиз-воронку" in text


def test_generated_project_data_is_gitignored():
    result = subprocess.run(
        ["git", "check-ignore", "-q", "projects/example/funnels/main/STATE.json"],
        cwd=ROOT,
        check=False,
    )

    assert result.returncode == 0


def test_installer_never_copies_ignored_runtime_or_lead_data(tmp_path):
    projects_root = ROOT / "projects"
    projects_root_existed = projects_root.exists()
    projects_root.mkdir(exist_ok=True)
    private_dir = Path(tempfile.mkdtemp(prefix=".install-private-", dir=projects_root))
    sentinel = private_dir / "private-factura.txt"
    sentinel.write_text("must never be installed", encoding="utf-8")
    leads_root = ROOT / "quiz-leads"
    leads_root_existed = leads_root.exists()
    leads_root.mkdir(exist_ok=True)
    private_leads = Path(tempfile.mkdtemp(prefix=".install-private-", dir=leads_root))
    (private_leads / "lead.json").write_text("private lead", encoding="utf-8")
    lead_file = Path(
        tempfile.NamedTemporaryFile(
            prefix="install-private-", suffix=".leads.json", dir=ROOT, delete=False
        ).name
    )
    lead_file.write_text("private lead export", encoding="utf-8")
    office = tmp_path / "office"
    office.mkdir()
    try:
        result = subprocess.run(
            ["bash", str(ROOT / "install.sh"), str(office)],
            cwd=ROOT,
            text=True,
            capture_output=True,
            check=False,
        )
        assert result.returncode == 0, result.stderr
        installed = office / "engines/quiz-funnel/projects" / private_dir.name
        assert not installed.exists()
        assert not (office / "engines/quiz-funnel/quiz-leads" / private_leads.name).exists()
        assert not (office / "engines/quiz-funnel" / lead_file.name).exists()
    finally:
        shutil.rmtree(private_dir)
        shutil.rmtree(private_leads)
        lead_file.unlink()
        if not projects_root_existed:
            projects_root.rmdir()
        if not leads_root_existed:
            leads_root.rmdir()


def test_init_funnel_command_creates_valid_recoverable_instance(tmp_path):
    pack = _copy_runnable_pack(tmp_path)
    result = subprocess.run(
        [
            sys.executable,
            str(pack / "scripts/init-funnel.py"),
            "--project",
            "school",
            "--funnel",
            "diagnostic",
        ],
        cwd=pack,
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    funnel_root = pack / "projects/school/funnels/diagnostic"
    assert payload["status"] == "ready"
    assert Path(payload["funnel_root"]) == funnel_root
    assert {
        "FUNNEL-BRIEF.md",
        "FUNNEL-BRIEF.meta.json",
        "STATE.json",
        "events.jsonl",
    } <= {path.name for path in funnel_root.iterdir()}
    from studio.contracts import validate_state
    from studio.state import StateStore

    snapshot = StateStore(funnel_root).load()
    assert validate_state(snapshot["state"]).errors == []

    rerun = subprocess.run(result.args, cwd=pack, text=True, capture_output=True)
    assert rerun.returncode == 0, rerun.stderr
    assert json.loads(rerun.stdout)["funnel_root"] == str(funnel_root)


def test_init_funnel_command_reports_safe_error_without_traceback(tmp_path):
    pack = _copy_runnable_pack(tmp_path)
    result = subprocess.run(
        [
            sys.executable,
            str(pack / "scripts/init-funnel.py"),
            "--project",
            "Unsafe Project",
            "--funnel",
            "diagnostic",
        ],
        cwd=pack,
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 2
    payload = json.loads(result.stderr)
    assert payload["status"] == "error"
    assert payload["error"] == "InvalidSlugError"
    assert "Traceback" not in result.stderr


def test_init_funnel_rejects_unowned_existing_project(tmp_path):
    pack = _copy_runnable_pack(tmp_path)
    (pack / "projects/school/funnels").mkdir(parents=True)
    result = subprocess.run(
        [
            sys.executable,
            str(pack / "scripts/init-funnel.py"),
            "--project",
            "school",
            "--funnel",
            "diagnostic",
        ],
        cwd=pack,
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 2
    assert json.loads(result.stderr)["status"] == "error"
    assert "Traceback" not in result.stderr


def test_preflight_gives_codex_an_executable_initialization_step():
    text = (ROOT / "skills/preflight/SKILL.md").read_text(encoding="utf-8")

    assert "scripts/init-funnel.py" in text
    assert "--project" in text
    assert "--funnel" in text
    assert "source-intake" in text
