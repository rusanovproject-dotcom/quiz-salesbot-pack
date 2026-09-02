"""Контракты студии: только локальная проверка, без моделей и сети."""
from __future__ import annotations

import copy
import json

from studio.contracts import (
    BRIEF_SCHEMA_VERSION,
    GATE_NAMES,
    STATE_SCHEMA_VERSION,
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
        "technical_readiness": "NOT_READY",
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


def test_unknown_brief_version_rejects_without_mutating_payload_or_file(tmp_path):
    """Ловит мигратор, который незаметно переписывает неизвестный контракт."""
    payload = minimal_brief()
    payload["schema_version"] = "funnel-brief/v999"
    before = copy.deepcopy(payload)
    contract_file = tmp_path / "Funnel.meta.json"
    raw = json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8") + b"\n"
    contract_file.write_bytes(raw)

    result = validate_file(contract_file, "brief")

    assert [(error.code, error.path) for error in result.errors] == [
        ("E_UNKNOWN_SCHEMA_VERSION", "/schema_version")
    ]
    assert payload == before
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


def test_refutation_requires_a_dated_separate_record():
    """Ловит «опровержение» без даты, которое нельзя отследить во времени."""
    payload = minimal_brief()
    payload["facts"] = [
        {
            "id": "fact-1",
            "statement": "Требует проверки владельцем.",
            "status": "synthetic",
            "refutations": [{"recorded_at": "not-a-date", "reason": "Проверено."}],
        }
    ]

    result = validate_brief(payload)

    assert [(error.code, error.path) for error in result.errors] == [
        ("E_INVALID_FORMAT", "/facts/0/refutations/0/recorded_at")
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


def test_legacy_project_is_untouched_by_validation():
    """Ловит валидатор, который меняет legacy PROJECT.md вместо чтения контракта."""
    legacy_path = "examples/remont/PROJECT.md"
    before = open(legacy_path, "rb").read()

    result = validate_brief(minimal_brief())

    assert result.errors == []
    assert open(legacy_path, "rb").read() == before
