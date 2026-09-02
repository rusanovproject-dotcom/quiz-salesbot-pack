"""Локальные, детерминированные контракты Funnel Studio.

Модуль намеренно не читает и не переписывает legacy PROJECT.md. Валидация
работает только с переданным JSON-полезной нагрузкой или с файлом метаданных.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
import json
from pathlib import Path
from typing import Any, Mapping


BRIEF_SCHEMA_VERSION = "funnel-brief/v1"
STATE_SCHEMA_VERSION = "state/v1"

FACT_STATUSES = ("confirmed", "owner_hypothesis", "synthetic", "unknown")
FACT_STATUS_MARKERS = {
    "confirmed": "🟢",
    "owner_hypothesis": "🟡",
    "synthetic": "🔴",
    "unknown": "⚪",
}
GATE_NAMES = (
    "FunnelFit",
    "Factura",
    "Offer",
    "Meaning",
    "QuizPreview",
    "SellerPreview",
    "LocalVerify",
    "ProductionReadiness",
)
PHASES = ("DISCOVERY", "DESIGN", "BUILD", "VALIDATION", "RELEASE")
GATE_STATUSES = ("NOT_STARTED", "IN_PROGRESS", "PASS", "BLOCKED")
READINESS_STATUSES = ("NOT_READY", "IN_PROGRESS", "PASS", "BLOCKED")
REQUIRED_BRIEF_SECTIONS = ("audience", "boundaries", "business", "offer", "voice")


@dataclass(frozen=True)
class ValidationError:
    """Одна пригодная для автоматики ошибка валидации."""

    code: str
    path: str
    message: str


@dataclass
class ValidationResult:
    """Результат без побочных эффектов; ошибки всегда отсортированы."""

    errors: list[ValidationError] = field(default_factory=list)
    warnings: list[ValidationError] = field(default_factory=list)
    release_verdict: str | None = None
    release_eligible: bool | None = None


def validate_brief(payload: Any) -> ValidationResult:
    """Проверяет companion metadata для FUNNEL-BRIEF.md, не меняя payload."""
    errors: list[ValidationError] = []
    if not isinstance(payload, Mapping):
        errors.append(_error("E_INVALID_TYPE", "/", "Ожидается JSON-объект."))
        return _brief_result(errors)

    _validate_version(payload, BRIEF_SCHEMA_VERSION, errors)
    _reject_unknown_fields(payload, {"schema_version", "sections", "facts"}, "/", errors)

    sections = _required_object(payload, "sections", "/sections", errors)
    if sections is not None:
        _reject_unknown_fields(sections, set(REQUIRED_BRIEF_SECTIONS), "/sections", errors)
        for name in REQUIRED_BRIEF_SECTIONS:
            section_path = f"/sections/{name}"
            if name not in sections:
                errors.append(_error("E_REQUIRED_FIELD", section_path, "Обязательный раздел отсутствует."))
            elif not isinstance(sections[name], Mapping):
                errors.append(_error("E_INVALID_TYPE", section_path, "Раздел должен быть объектом."))

    facts = _required_list(payload, "facts", "/facts", errors)
    if facts is not None:
        for index, fact in enumerate(facts):
            _validate_fact(fact, f"/facts/{index}", errors)
    return _brief_result(errors)


def validate_state(payload: Any) -> ValidationResult:
    """Проверяет независимые оси состояния и выводит релизный вердикт."""
    errors: list[ValidationError] = []
    if not isinstance(payload, Mapping):
        return _state_result(errors + [_error("E_INVALID_TYPE", "/", "Ожидается JSON-объект.")], payload)

    _validate_version(payload, STATE_SCHEMA_VERSION, errors)
    allowed = {
        "schema_version", "phase", "gate", "gate_status",
        "technical_readiness", "market_readiness",
    }
    _reject_unknown_fields(payload, allowed, "/", errors)
    _validate_enum(payload, "phase", PHASES, "/phase", errors)
    _validate_enum(payload, "gate", GATE_NAMES, "/gate", errors)
    _validate_enum(payload, "gate_status", GATE_STATUSES, "/gate_status", errors)
    _validate_enum(payload, "technical_readiness", READINESS_STATUSES, "/technical_readiness", errors)
    _validate_enum(payload, "market_readiness", READINESS_STATUSES, "/market_readiness", errors)
    return _state_result(errors, payload)


def validate_file(path: str | Path, contract: str) -> ValidationResult:
    """Валидирует JSON-файл только на чтение.

    ``contract`` принимает ``brief`` или ``state``; неизвестный тип является
    ошибкой вызова, а не поводом менять файл или его данные.
    """
    raw = Path(path).read_bytes()
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        return _brief_result([_error("E_INVALID_JSON", "/", f"Некорректный JSON: {exc.msg if hasattr(exc, 'msg') else str(exc)}")])
    if contract == "brief":
        return validate_brief(payload)
    if contract == "state":
        return validate_state(payload)
    raise ValueError("contract must be 'brief' or 'state'")


def _validate_fact(value: Any, path: str, errors: list[ValidationError]) -> None:
    if not isinstance(value, Mapping):
        errors.append(_error("E_INVALID_TYPE", path, "Факт должен быть объектом."))
        return
    _reject_unknown_fields(value, {"id", "statement", "status", "refutations"}, path, errors)
    for field_name in ("id", "statement", "status"):
        field_path = f"{path}/{field_name}"
        if field_name not in value:
            errors.append(_error("E_REQUIRED_FIELD", field_path, "Обязательное поле отсутствует."))
        elif not isinstance(value[field_name], str) or not value[field_name]:
            errors.append(_error("E_INVALID_TYPE", field_path, "Поле должно быть непустой строкой."))
    if "status" in value and isinstance(value["status"], str) and value["status"] not in FACT_STATUSES:
        errors.append(_error("E_INVALID_ENUM", f"{path}/status", "Недопустимый статус факта."))
    if "refutations" in value:
        refutations = value["refutations"]
        if not isinstance(refutations, list):
            errors.append(_error("E_INVALID_TYPE", f"{path}/refutations", "Опровержения должны быть списком."))
        else:
            for index, refutation in enumerate(refutations):
                _validate_refutation(refutation, f"{path}/refutations/{index}", errors)


def _validate_refutation(value: Any, path: str, errors: list[ValidationError]) -> None:
    if not isinstance(value, Mapping):
        errors.append(_error("E_INVALID_TYPE", path, "Опровержение должно быть объектом."))
        return
    _reject_unknown_fields(value, {"recorded_at", "reason"}, path, errors)
    for field_name in ("recorded_at", "reason"):
        field_path = f"{path}/{field_name}"
        if field_name not in value:
            errors.append(_error("E_REQUIRED_FIELD", field_path, "Обязательное поле отсутствует."))
        elif not isinstance(value[field_name], str) or not value[field_name]:
            errors.append(_error("E_INVALID_TYPE", field_path, "Поле должно быть непустой строкой."))
    if "recorded_at" in value and isinstance(value["recorded_at"], str) and value["recorded_at"] and not _is_iso_datetime(value["recorded_at"]):
        errors.append(_error("E_INVALID_FORMAT", f"{path}/recorded_at", "Ожидается ISO 8601 date-time."))


def _validate_version(payload: Mapping[str, Any], expected: str, errors: list[ValidationError]) -> None:
    value = payload.get("schema_version")
    if value is None:
        errors.append(_error("E_REQUIRED_FIELD", "/schema_version", "Версия схемы обязательна."))
    elif value != expected:
        errors.append(_error("E_UNKNOWN_SCHEMA_VERSION", "/schema_version", f"Поддерживается только {expected}."))


def _validate_enum(payload: Mapping[str, Any], name: str, allowed: tuple[str, ...], path: str, errors: list[ValidationError]) -> None:
    if name not in payload:
        errors.append(_error("E_REQUIRED_FIELD", path, "Обязательное поле отсутствует."))
    elif payload[name] not in allowed:
        errors.append(_error("E_INVALID_ENUM", path, "Недопустимое значение."))


def _required_object(payload: Mapping[str, Any], name: str, path: str, errors: list[ValidationError]) -> Mapping[str, Any] | None:
    if name not in payload:
        errors.append(_error("E_REQUIRED_FIELD", path, "Обязательное поле отсутствует."))
        return None
    if not isinstance(payload[name], Mapping):
        errors.append(_error("E_INVALID_TYPE", path, "Ожидается объект."))
        return None
    return payload[name]


def _required_list(payload: Mapping[str, Any], name: str, path: str, errors: list[ValidationError]) -> list[Any] | None:
    if name not in payload:
        errors.append(_error("E_REQUIRED_FIELD", path, "Обязательное поле отсутствует."))
        return None
    if not isinstance(payload[name], list):
        errors.append(_error("E_INVALID_TYPE", path, "Ожидается список."))
        return None
    return payload[name]


def _reject_unknown_fields(payload: Mapping[str, Any], allowed: set[str], path: str, errors: list[ValidationError]) -> None:
    for name in payload:
        if name not in allowed:
            errors.append(_error("E_UNKNOWN_FIELD", f"{path.rstrip('/')}/{name}", "Поле не определено контрактом."))


def _state_result(errors: list[ValidationError], payload: Any) -> ValidationResult:
    verdict = "INVALID" if errors else _release_verdict(payload)
    return ValidationResult(
        errors=_sorted(errors),
        release_verdict=verdict,
        release_eligible=verdict == "PASS",
    )


def _release_verdict(payload: Mapping[str, Any]) -> str:
    if payload["market_readiness"] == "BLOCKED" or payload["technical_readiness"] == "BLOCKED" or payload["gate_status"] == "BLOCKED":
        return "BLOCKED"
    if (
        payload["phase"] == "RELEASE"
        and payload["gate"] == "ProductionReadiness"
        and payload["gate_status"] == "PASS"
        and payload["technical_readiness"] == "PASS"
        and payload["market_readiness"] == "PASS"
    ):
        return "PASS"
    return "PENDING"


def _brief_result(errors: list[ValidationError]) -> ValidationResult:
    return ValidationResult(errors=_sorted(errors))


def _sorted(errors: list[ValidationError]) -> list[ValidationError]:
    return sorted(errors, key=lambda error: (error.path, error.code, error.message))


def _error(code: str, path: str, message: str) -> ValidationError:
    return ValidationError(code=code, path=path, message=message)


def _is_iso_datetime(value: str) -> bool:
    try:
        datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return False
    return True
