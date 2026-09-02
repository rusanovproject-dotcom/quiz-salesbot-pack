"""Контракты студии: только локальная проверка, без моделей и сети."""
from __future__ import annotations

import copy
import json
from pathlib import Path

from studio.contracts import (
    BRIEF_SCHEMA_VERSION,
    FACT_STATUSES,
    GATE_NAMES,
    GATE_STATUSES,
    MARKET_READINESS_STATUSES,
    PHASES,
    REQUIRED_BRIEF_SECTIONS,
    STATE_SCHEMA_VERSION,
    TECHNICAL_READINESS_STATUSES,
    validate_brief,
    validate_file,
    validate_state,
)


def minimal_brief():
    return {
        "schema_version": BRIEF_SCHEMA_VERSION,
        "sections": {
            "business": {},
            "audience": {},
            "offer": {},
            "voice": {},
            "boundaries": {},
        },
        "facts": [],
    }


def minimal_state():
    return {
        "schema_version": STATE_SCHEMA_VERSION,
        "phase": "DISCOVERY",
        "gate": "FunnelFit",
        "gate_status": "NOT_STARTED",
        "technical_readiness": "prototype_only",
        "market_readiness": "NOT_READY",
    }


def test_valid_minimal_brief_and_state_have_no_messages():
    """Ловит регрессию, при которой пустой стартовый контракт нельзя создать."""
    brief = validate_brief(minimal_brief())
    state = validate_state(minimal_state())

    assert brief.errors == []
    assert brief.warnings == []
    assert state.errors == []
    assert state.warnings == []


def test_missing_brief_sections_are_aggregated_in_stable_order():
    """Ловит fail-fast, из-за которого владелец исправляет шаблон по одной дыре."""
    payload = {"schema_version": BRIEF_SCHEMA_VERSION, "sections": {}, "facts": []}

    result = validate_brief(payload)

    assert [(error.code, error.path) for error in result.errors] == [
        ("E_REQUIRED_FIELD", "/sections/audience"),
        ("E_REQUIRED_FIELD", "/sections/boundaries"),
        ("E_REQUIRED_FIELD", "/sections/business"),
        ("E_REQUIRED_FIELD", "/sections/offer"),
        ("E_REQUIRED_FIELD", "/sections/voice"),
    ]


def test_unknown_brief_version_rejects_without_mutating_in_memory_payload():
    """Ловит валидатор, который незаметно мигрирует переданный объект."""
    payload = minimal_brief()
    payload["schema_version"] = "funnel-brief/v999"
    before = copy.deepcopy(payload)

    result = validate_brief(payload)

    assert [(error.code, error.path) for error in result.errors] == [
        ("E_UNKNOWN_SCHEMA_VERSION", "/schema_version")
    ]
    assert payload == before


def test_unknown_brief_version_rejects_without_rewriting_file(tmp_path):
    """Ловит файловый валидатор, который записывает неизвестную версию обратно."""
    payload = minimal_brief()
    payload["schema_version"] = "funnel-brief/v999"
    contract_file = tmp_path / "Funnel.meta.json"
    raw = json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8") + b"\n"
    contract_file.write_bytes(raw)

    result = validate_file(contract_file, "brief")

    assert [(error.code, error.path) for error in result.errors] == [
        ("E_UNKNOWN_SCHEMA_VERSION", "/schema_version")
    ]
    assert contract_file.read_bytes() == raw


def test_synthetic_fact_round_trips_without_becoming_a_refutation():
    """Ловит подмену статуса synthetic на refuted, которая искажает фактуру."""
    payload = minimal_brief()
    payload["facts"] = [
        {
            "id": "fact-1",
            "statement": "Требует проверки владельцем.",
            "status": "synthetic",
            "refutations": [],
        }
    ]

    result = validate_brief(payload)

    assert result.errors == []
    assert payload["facts"][0]["status"] == "synthetic"
    assert "refuted" not in payload["facts"][0]


def test_refutation_requires_strict_rfc3339_datetime():
    """Ловит date-only, пробел и timezone-less даты, расходящиеся со schema."""
    payload = minimal_brief()
    payload["facts"] = [
        {
            "id": "fact-1",
            "statement": "Требует проверки владельцем.",
            "status": "synthetic",
            "refutations": [{"recorded_at": "2026-09-02T15:51:00Z", "reason": "Проверено."}],
        }
    ]

    assert validate_brief(payload).errors == []
    for invalid_datetime in (
        "2026-09-02",
        "2026-09-02 15:51:00Z",
        "2026-09-02T15:51:00",
        "2026-02-30T15:51:00Z",
    ):
        payload["facts"][0]["refutations"][0]["recorded_at"] = invalid_datetime
        result = validate_brief(payload)
        assert [(error.code, error.path) for error in result.errors] == [
            ("E_INVALID_FORMAT", "/facts/0/refutations/0/recorded_at")
        ]


def test_schema_and_validator_share_versions_shapes_enums_and_closed_objects():
    """Ловит drift между опубликованными JSON Schema и ручным валидатором."""
    root = Path(__file__).resolve().parents[2]
    brief_schema = json.loads((root / "schemas/funnel-brief-v1.json").read_text())
    state_schema = json.loads((root / "schemas/state-v1.json").read_text())
    brief_properties = brief_schema["properties"]
    fact_schema = brief_properties["facts"]["items"]
    state_properties = state_schema["properties"]

    assert brief_properties["schema_version"] == {
        "type": "string", "const": BRIEF_SCHEMA_VERSION,
    }
    assert tuple(brief_schema["required"]) == ("schema_version", "sections", "facts")
    assert brief_schema["additionalProperties"] is False
    assert set(brief_properties["sections"]["required"]) == set(REQUIRED_BRIEF_SECTIONS)
    assert brief_properties["sections"]["additionalProperties"] is False
    assert tuple(fact_schema["properties"]["status"]["enum"]) == FACT_STATUSES
    assert fact_schema["additionalProperties"] is False
    refutation_schema = fact_schema["properties"]["refutations"]["items"]
    assert refutation_schema["additionalProperties"] is False
    assert refutation_schema["properties"]["recorded_at"] == {"type": "string", "format": "date-time"}

    assert state_properties["schema_version"] == {
        "type": "string", "const": STATE_SCHEMA_VERSION,
    }
    assert tuple(state_schema["required"]) == (
        "schema_version", "phase", "gate", "gate_status",
        "technical_readiness", "market_readiness",
    )
    assert state_schema["additionalProperties"] is False
    assert tuple(state_properties["phase"]["enum"]) == PHASES
    assert tuple(state_properties["gate"]["enum"]) == GATE_NAMES
    assert tuple(state_properties["gate_status"]["enum"]) == GATE_STATUSES
    assert tuple(state_properties["technical_readiness"]["enum"]) == TECHNICAL_READINESS_STATUSES
    assert tuple(state_properties["market_readiness"]["enum"]) == MARKET_READINESS_STATUSES
    for name in ("phase", "gate", "gate_status", "technical_readiness", "market_readiness"):
        assert state_properties[name]["type"] == "string"


def test_validator_reports_type_before_version_or_enum_and_rejects_extra_fields():
    """Ловит диагностику enum для значений, которые schema сначала считает не-строкой."""
    brief = minimal_brief()
    brief.update(schema_version=1, unexpected=True)
    state = minimal_state()
    state.update(
        schema_version=1,
        phase=1,
        gate=1,
        gate_status=1,
        technical_readiness=1,
        market_readiness=1,
        unexpected=True,
    )

    assert [(error.code, error.path) for error in validate_brief(brief).errors] == [
        ("E_INVALID_TYPE", "/schema_version"),
        ("E_UNKNOWN_FIELD", "/unexpected"),
    ]
    assert [(error.code, error.path) for error in validate_state(state).errors] == [
        ("E_INVALID_TYPE", "/gate"),
        ("E_INVALID_TYPE", "/gate_status"),
        ("E_INVALID_TYPE", "/market_readiness"),
        ("E_INVALID_TYPE", "/phase"),
        ("E_INVALID_TYPE", "/schema_version"),
        ("E_INVALID_TYPE", "/technical_readiness"),
        ("E_UNKNOWN_FIELD", "/unexpected"),
    ]


def test_state_axes_reject_invalid_values_independently():
    """Ловит связанную валидацию, когда одна неверная ось скрывает остальные."""
    payload = minimal_state()
    payload.update(
        phase="SOMETHING_ELSE",
        gate="AnythingGoes",
        gate_status="MAYBE",
        technical_readiness="MAGIC",
        market_readiness="LATER",
    )

    result = validate_state(payload)

    assert [(error.code, error.path) for error in result.errors] == [
        ("E_INVALID_ENUM", "/gate"),
        ("E_INVALID_ENUM", "/gate_status"),
        ("E_INVALID_ENUM", "/market_readiness"),
        ("E_INVALID_ENUM", "/phase"),
        ("E_INVALID_ENUM", "/technical_readiness"),
    ]
    assert GATE_NAMES == (
        "FunnelFit", "Factura", "Offer", "Meaning", "QuizPreview",
        "SellerPreview", "LocalVerify", "ProductionReadiness",
    )


def test_market_blocked_prevents_release_pass_even_when_technical_passes():
    """Ловит ложный релиз по техготовности при заблокированной рыночной готовности."""
    payload = minimal_state()
    payload.update(
        phase="RELEASE",
        gate="ProductionReadiness",
        gate_status="PASS",
        technical_readiness="PASS",
        market_readiness="BLOCKED",
    )

    result = validate_state(payload)

    assert result.errors == []
    assert result.release_verdict == "BLOCKED"
    assert result.release_eligible is False


def test_prototype_only_is_technical_readiness_and_never_releases():
    """Ловит модель, в которой prototype_only ошибочно становится phase или PASS."""
    payload = minimal_state()
    payload.update(
        phase="RELEASE",
        gate="ProductionReadiness",
        gate_status="PASS",
        technical_readiness="prototype_only",
        market_readiness="PASS",
    )

    result = validate_state(payload)

    assert result.errors == []
    assert result.release_verdict == "PENDING"
    assert result.release_eligible is False


def test_real_templates_validate_and_markdown_exposes_human_sections():
    """Ловит поломку поставляемых companion-файлов или человекочитаемой структуры."""
    root = Path(__file__).resolve().parents[2]
    markdown = (root / "templates/FUNNEL-BRIEF.md").read_text(encoding="utf-8")
    headings = [line.removeprefix("## ") for line in markdown.splitlines() if line.startswith("## ")]

    assert "FUNNEL-BRIEF.meta.json" in markdown
    assert headings == ["Дело", "Аудитория и факты", "Оффер", "Голос", "Границы"]
    assert validate_file(root / "templates/FUNNEL-BRIEF.meta.json", "brief").errors == []
    assert validate_file(root / "templates/STATE.json", "state").errors == []


def test_legacy_project_is_untouched_by_validation():
    """Ловит валидатор, который меняет legacy PROJECT.md вместо чтения контракта."""
    legacy_path = "examples/remont/PROJECT.md"
    before = open(legacy_path, "rb").read()

    result = validate_brief(minimal_brief())

    assert result.errors == []
    assert open(legacy_path, "rb").read() == before
