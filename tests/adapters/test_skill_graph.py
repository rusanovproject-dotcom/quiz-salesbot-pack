"""Executable contract for one canonical skill graph and thin environment adapters."""
from __future__ import annotations

import os
from pathlib import Path
import shutil
import subprocess

import pytest


ROOT = Path(__file__).resolve().parents[2]
PHASES = (
    "preflight",
    "source-intake",
    "FunnelFit",
    "Factura",
    "Offer",
    "Meaning",
    "QuizPreview",
    "SellerPreview",
    "LocalVerify",
    "ProductionReadiness",
    "verify-funnel",
)
GATES = PHASES[2:-1]
CONTRACTS = (
    "funnel-brief/v1",
    "state/v1",
    "studio-snapshot/v1",
    "studio-event/v1",
)
ARTIFACT_ROOT = "projects/<project_slug>/funnels/<funnel_slug>/"
ARTIFACTS = (
    "FUNNEL-BRIEF.md",
    "FUNNEL-BRIEF.meta.json",
    "STATE.json",
    "events.jsonl",
    "quiz/",
    "salesbot/",
    "reports/",
)
STOP_CONDITIONS = (
    "missing-required-artifact",
    "missing-owner-approval",
    "blocked-readiness",
    "unverified-domain-claim",
)
LEGACY_SKILLS = {
    "audience-factura",
    "build-quiz",
    "build-salesbot",
    "funnel-interview",
    "leads-db",
    "offer-testing",
    "quiz-design",
    "quiz-meaning",
    "quiz-offer",
    "wordstat-mining",
}


def _frontmatter(path: Path) -> dict[str, str]:
    lines = path.read_text(encoding="utf-8").splitlines()
    assert lines and lines[0] == "---", f"frontmatter must start {path}"
    try:
        end = lines.index("---", 1)
    except ValueError as error:
        raise AssertionError(f"unterminated frontmatter: {path}") from error
    fields: dict[str, str] = {}
    for line in lines[1:end]:
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        key, separator, value = line.partition(":")
        assert separator and key.strip() and value.strip(), f"simple scalar field required: {path}: {line}"
        fields[key.strip()] = value.strip().strip('"')
    return fields


def _csv(value: str) -> tuple[str, ...]:
    return tuple(part.strip() for part in value.split(",") if part.strip())


def _skill_names(root: Path = ROOT) -> set[str]:
    return {
        _frontmatter(path)["name"]
        for path in (root / "skills").glob("*/SKILL.md")
    }


def _adapter_contract(environment: str, root: Path = ROOT) -> dict[str, object]:
    fields = _frontmatter(root / f"adapters/{environment}/quiz-funnel/SKILL.md")
    return {
        "entrypoint": fields["canonical-entrypoint"],
        "refs": _csv(fields["skill-refs"]),
        "phases": _csv(fields["public-phases"]),
        "contracts": _csv(fields["contracts"]),
        "artifact_root": fields["artifact-root"],
        "artifacts": _csv(fields["artifacts"]),
        "stop_conditions": _csv(fields["stop-conditions"]),
        "trigger_map": tuple(
            tuple(part.strip() for part in route.split("=>", 1))
            for route in fields["trigger-map"].split(";")
        ),
    }


def _run_check(root: Path) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    env["QUIZ_FUNNEL_PACK_ROOT"] = str(root)
    return subprocess.run(
        ["bash", str(root / "scripts/check-adapters.sh")],
        cwd=root,
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )


def test_all_legacy_methodology_has_one_canonical_owner_and_valid_frontmatter():
    """Catches a partial move or invalid discovery metadata in the canonical batch."""
    names = _skill_names()

    assert LEGACY_SKILLS <= names
    assert {"preflight", "source-intake", "verify-funnel"} <= names
    for path in (ROOT / "skills").glob("*/SKILL.md"):
        fields = _frontmatter(path)
        assert fields["name"].replace("-", "").isalnum()
        assert fields["name"].isascii()
        assert fields["description"].startswith("Use when ")
        assert "..." not in fields["description"]


def test_preflight_declares_the_ordered_graph_and_only_resolvable_skill_references():
    """Catches a skipped/reordered U1 gate or a graph edge to a missing skill."""
    fields = _frontmatter(ROOT / "skills/preflight/SKILL.md")

    assert _csv(fields["public-phases"]) == PHASES
    assert _csv(fields["gate-graph"]) == GATES
    assert fields["artifact-root"] == ARTIFACT_ROOT
    assert _csv(fields["artifacts"]) == ARTIFACTS
    assert _csv(fields["contracts"]) == CONTRACTS
    assert _csv(fields["stop-conditions"]) == STOP_CONDITIONS
    assert set(_csv(fields["skill-refs"])) <= _skill_names()


@pytest.mark.parametrize("phase", ["preflight", "source-intake", "verify-funnel"])
def test_each_new_phase_has_an_explicit_fail_closed_contract(phase: str):
    """Catches a placeholder entry skill that can bypass artifacts, approval or verification."""
    fields = _frontmatter(ROOT / f"skills/{phase}/SKILL.md")

    assert fields["phase"] == phase
    assert fields["artifact-root"] == ARTIFACT_ROOT
    assert _csv(fields["artifacts"]) == ARTIFACTS
    assert _csv(fields["contracts"]) == CONTRACTS
    assert _csv(fields["stop-conditions"]) == STOP_CONDITIONS
    assert fields["domain-write-policy"] == "project-artifacts-only"
    assert fields["deployment-policy"] == "never"


def test_claude_and_codex_adapters_declare_one_identical_public_contract():
    """Catches adapter drift in routing, graph, artifacts, contracts or stop behavior."""
    claude = _adapter_contract("claude")
    codex = _adapter_contract("codex")

    assert claude == codex
    assert claude == {
        "entrypoint": "preflight",
        "refs": ("preflight",),
        "phases": PHASES,
        "contracts": CONTRACTS,
        "artifact_root": ARTIFACT_ROOT,
        "artifacts": ARTIFACTS,
        "stop_conditions": STOP_CONDITIONS,
        "trigger_map": (
            ("хочу квиз", "preflight"),
            ("собери воронку", "preflight"),
            ("собери мне квиз-воронку", "preflight"),
            ("ии-продажник", "preflight"),
        ),
    }
    assert set(claude["refs"]) <= _skill_names()


def test_adapters_are_thin_discovery_and_invocation_surfaces():
    """Catches methodology copied into adapters instead of loaded from canonical skills."""
    forbidden_methodology = (
        "семь вариантов первого экрана",
        "три слоя фактуры",
        "шаги диалога продавца",
        "классы-диагнозы",
    )
    for environment in ("claude", "codex"):
        path = ROOT / f"adapters/{environment}/quiz-funnel/SKILL.md"
        text = path.read_text(encoding="utf-8").lower()
        body = text.split("---", 2)[-1]
        assert len(body.splitlines()) <= 28
        assert all(term not in body for term in forbidden_methodology)
        assert "skills/preflight/skill.md" in body


@pytest.mark.parametrize(
    "intent",
    ["хочу квиз", "собери воронку", "собери мне квиз-воронку", "ии-продажник"],
)
def test_pressure_intents_have_the_same_fail_closed_first_outcome(intent: str):
    """Catches a legacy trigger that enters Phase 0 or writes root PROJECT.md in one client."""
    outcomes = []
    for environment in ("claude", "codex"):
        contract = _adapter_contract(environment)
        routes = dict(contract["trigger_map"])
        outcomes.append(
            (
                routes[intent],
                contract["artifact_root"],
                contract["artifacts"],
                contract["contracts"],
                contract["phases"],
                contract["stop_conditions"],
            )
        )

    assert outcomes[0] == outcomes[1]
    assert outcomes[0][0] == "preflight"
    assert "PROJECT.md" not in outcomes[0][1]
    assert outcomes[0][2] == ARTIFACTS


def test_canonical_skills_never_claim_legacy_or_package_paths_as_domain_outputs():
    """Catches a skill that directs new project truth into legacy/package-owned files."""
    forbidden_outputs = (
        "output-root: PROJECT.md",
        "output-root: engine/",
        "output-root: examples/",
        "output-root: playbook/",
    )
    canonical_paths = list((ROOT / "skills").glob("*/SKILL.md"))
    assert canonical_paths
    for path in canonical_paths:
        fields = _frontmatter(path)
        assert fields["domain-write-policy"] == "project-artifacts-only"
        text = path.read_text(encoding="utf-8")
        assert all(value not in text for value in forbidden_outputs)


@pytest.mark.parametrize("skill", ["build-quiz", "build-salesbot"])
def test_materialization_skills_require_an_approved_brief_revision(skill: str):
    """Catches preview materialization from an unapproved or stale domain brief."""
    fields = _frontmatter(ROOT / f"skills/{skill}/SKILL.md")

    assert fields["input-policy"] == "approved-funnel-brief-revision-only"


def test_generated_claude_projection_is_current_and_canonical_mutation_is_detected(tmp_path):
    """Catches hand-edited/stale compatibility copies after canonical changes."""
    result = _run_check(ROOT)
    assert result.returncode == 0, result.stdout + result.stderr

    scratch = tmp_path / "pack"
    for relative in ("skills", "adapters", ".claude/skills", "scripts"):
        source = ROOT / relative
        target = scratch / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(source, target)
    canonical = scratch / "skills/quiz-offer/SKILL.md"
    canonical.write_text(canonical.read_text(encoding="utf-8") + "\nmutation\n", encoding="utf-8")

    stale = _run_check(scratch)
    assert stale.returncode != 0
    assert "stale projection" in (stale.stdout + stale.stderr).lower()


def test_wordstat_reusable_script_works_from_canonical_and_projection_paths(tmp_path):
    """Catches a nested asset omitted from the move/projection or projected with wrong mode."""
    scripts = (
        ROOT / "skills/wordstat-mining/scripts/wordstat.sh",
        ROOT / ".claude/skills/wordstat-mining/scripts/wordstat.sh",
    )
    outcomes = []
    for script in scripts:
        assert script.is_file()
        assert os.access(script, os.X_OK)
        env = os.environ.copy()
        env["HOME"] = str(tmp_path / "empty-home")
        result = subprocess.run(
            [str(script), "top", "тест"],
            env=env,
            text=True,
            capture_output=True,
            check=False,
        )
        outcomes.append((result.returncode, result.stdout, result.stderr))

    assert outcomes[0] == outcomes[1]
    assert outcomes[0][0] == 1
    assert "wordstat.json" in outcomes[0][2]


def test_codex_agents_fragment_routes_to_the_thin_codex_adapter():
    """Catches Codex workspace discovery bypassing the shared adapter and canonical entrypoint."""
    text = (ROOT / "adapters/codex/AGENTS.fragment.md").read_text(encoding="utf-8")

    assert "adapters/codex/quiz-funnel/SKILL.md" in text
    assert "skills/preflight/SKILL.md" in text
    assert "хочу квиз" in text
    assert "собери воронку" in text
