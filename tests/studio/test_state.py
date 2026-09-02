"""State persistence: atomic snapshots, append-only proof and recovery."""
from __future__ import annotations

import hashlib
import json
import multiprocessing
import os
from pathlib import Path
from queue import Empty

import pytest

from studio.contracts import validate_state


def _state(**changes):
    payload = {
        "schema_version": "state/v1",
        "phase": "DISCOVERY",
        "gate": "FunnelFit",
        "gate_status": "NOT_STARTED",
        "technical_readiness": "prototype_only",
        "market_readiness": "NOT_READY",
    }
    payload.update(changes)
    return payload


def _store(tmp_path: Path, **kwargs):
    from studio.project_layout import initialize_project
    from studio.state import StateStore

    package_root = Path(__file__).resolve().parents[2]
    paths = initialize_project(
        projects_root=tmp_path / "projects",
        templates_root=package_root / "templates",
        project_slug="alpha",
        funnel_slug="main",
    )
    return StateStore(paths.funnel_root, **kwargs), paths


def _competing_writer(funnel_root: str, start, results, gate_status: str) -> None:
    from studio.errors import RevisionConflictError
    from studio.state import StateStore

    start.wait()
    try:
        snapshot = StateStore(funnel_root, lock_timeout=5).commit(
            state=_state(gate_status=gate_status),
            expected_revision=0,
            last_verified_gate=None,
            next_gate="FunnelFit",
            blocked_reason=None,
            evidence_hashes={},
        )
        results.put(("ok", snapshot["revision"]))
    except RevisionConflictError:
        results.put(("conflict", None))
    except Exception as exc:  # pragma: no cover - reported to parent for diagnosis
        results.put(("unexpected", repr(exc)))


def _hard_exit_before_replace(_temporary_path: Path) -> None:
    os._exit(0)


def _crash_during_commit(funnel_root: str) -> None:
    from studio.state import StateStore

    StateStore(funnel_root, before_replace=_hard_exit_before_replace).commit(
        state=_state(gate_status="IN_PROGRESS"),
        expected_revision=0,
        last_verified_gate=None,
        next_gate="FunnelFit",
        blocked_reason=None,
        evidence_hashes={},
    )


def test_commit_stores_metadata_and_keeps_u1_state_valid(tmp_path):
    """Ловит потерю recovery metadata или расширение закрытого state/v1 объекта."""
    store, _ = _store(tmp_path)
    digest = hashlib.sha256(b"factura evidence").hexdigest()

    snapshot = store.commit(
        state=_state(gate="Offer", gate_status="BLOCKED"),
        expected_revision=0,
        last_verified_gate="Factura",
        next_gate="Offer",
        blocked_reason="Нужно подтвердить цену.",
        evidence_hashes={"Factura": digest},
    )

    assert snapshot["revision"] == 1
    assert snapshot["last_verified_gate"] == "Factura"
    assert snapshot["next_gate"] == "Offer"
    assert snapshot["blocked_reason"] == "Нужно подтвердить цену."
    assert snapshot["evidence_hashes"] == {"Factura": digest}
    assert validate_state(snapshot["state"]).errors == []
    assert set(snapshot["state"]) == {
        "schema_version", "phase", "gate", "gate_status",
        "technical_readiness", "market_readiness",
    }
    assert store.load() == snapshot


def test_last_verified_gate_requires_its_evidence_hash():
    """Ловит переход accepted без доказательства названного гейта."""
    from studio.state import create_snapshot

    with pytest.raises(ValueError, match="last_verified_gate.*evidence"):
        create_snapshot(
            _state(gate="Factura", gate_status="PASS"),
            last_verified_gate="Factura",
            next_gate="Offer",
            evidence_hashes={},
        )


def test_fault_before_replace_preserves_previous_snapshot_and_history(tmp_path):
    """Ловит truncate/in-place write, теряющий последнее валидное состояние при сбое."""
    store, paths = _store(tmp_path)
    before_state = paths.state_path.read_bytes()
    before_events = paths.events_path.read_bytes()

    def fail_before_replace(_temporary_path: Path) -> None:
        raise OSError("injected before replace")

    faulty = type(store)(paths.funnel_root, before_replace=fail_before_replace)
    with pytest.raises(OSError, match="injected before replace"):
        faulty.commit(
            state=_state(gate_status="IN_PROGRESS"),
            expected_revision=0,
            last_verified_gate=None,
            next_gate="FunnelFit",
            blocked_reason=None,
            evidence_hashes={},
        )

    assert paths.state_path.read_bytes() == before_state
    assert paths.events_path.read_bytes() == before_events
    assert list(paths.funnel_root.glob(".STATE.json.*.tmp")) == []


def test_events_append_without_rewriting_earlier_bytes(tmp_path):
    """Ловит rewrite журнала, способный изменить уже записанное доказательство."""
    store, paths = _store(tmp_path)
    first = store.commit(
        state=_state(gate_status="IN_PROGRESS"), expected_revision=0,
        last_verified_gate=None, next_gate="FunnelFit", blocked_reason=None,
        evidence_hashes={},
    )
    prefix = paths.events_path.read_bytes()
    assert prefix.endswith(b"\n") and len(prefix.splitlines()) == 1

    store.commit(
        state=_state(gate_status="BLOCKED"), expected_revision=first["revision"],
        last_verified_gate=None, next_gate="FunnelFit", blocked_reason="Нет фактов.",
        evidence_hashes={},
    )

    current = paths.events_path.read_bytes()
    assert current.startswith(prefix)
    assert current[:len(prefix)] == prefix
    assert len(current.splitlines()) == 2


def test_corrupt_state_is_diagnosed_without_byte_changes(tmp_path):
    """Ловит silent reset битого STATE.json в пустой/default snapshot."""
    from studio.errors import CorruptStateError

    store, paths = _store(tmp_path)
    corrupt = b'{"snapshot_version": '
    paths.state_path.write_bytes(corrupt)

    with pytest.raises(CorruptStateError, match="STATE.json.*JSON"):
        store.load()

    assert paths.state_path.read_bytes() == corrupt


def test_same_expected_revision_allows_exactly_one_process_to_commit(tmp_path):
    """Ловит check-then-write race, разрешающий двум писателям один revision."""
    store, paths = _store(tmp_path)
    del store
    context = multiprocessing.get_context("spawn")
    start = context.Event()
    results = context.Queue()
    processes = [
        context.Process(
            target=_competing_writer,
            args=(str(paths.funnel_root), start, results, status),
        )
        for status in ("IN_PROGRESS", "BLOCKED")
    ]
    for process in processes:
        process.start()
    start.set()
    for process in processes:
        process.join(timeout=10)
        assert process.exitcode == 0
    try:
        outcomes = sorted(results.get(timeout=2) for _ in processes)
    except Empty as exc:  # pragma: no cover - clearer diagnostic for process hangs
        raise AssertionError("writer did not report an outcome") from exc

    assert outcomes == [("conflict", None), ("ok", 1)]
    on_disk = json.loads(paths.state_path.read_text(encoding="utf-8"))
    assert on_disk["revision"] == 1
    assert len(paths.events_path.read_bytes().splitlines()) == 1


def test_writer_lock_is_released_after_process_dies(tmp_path):
    """Ловит вечный stale lock после аварийной остановки писателя."""
    from studio.state import StateStore

    store, paths = _store(tmp_path)
    before = paths.state_path.read_bytes()
    context = multiprocessing.get_context("spawn")
    process = context.Process(target=_crash_during_commit, args=(str(paths.funnel_root),))
    process.start()
    process.join(timeout=10)
    assert process.exitcode == 0
    assert paths.state_path.read_bytes() == before

    committed = StateStore(paths.funnel_root, lock_timeout=1).commit(
        state=_state(gate_status="IN_PROGRESS"),
        expected_revision=0,
        last_verified_gate=None,
        next_gate="FunnelFit",
        blocked_reason=None,
        evidence_hashes={},
    )

    assert committed["revision"] == 1


def test_valid_event_history_recovers_corrupt_snapshot(tmp_path):
    """Ловит recovery, который сбрасывает состояние вместо доказанного event snapshot."""
    store, paths = _store(tmp_path)
    committed = store.commit(
        state=_state(gate="Factura", gate_status="PASS"), expected_revision=0,
        last_verified_gate="Factura", next_gate="Offer", blocked_reason=None,
        evidence_hashes={"Factura": hashlib.sha256(b"proof").hexdigest()},
    )
    paths.state_path.write_bytes(b"not-json")

    recovered = store.recover()

    assert recovered == committed
    assert json.loads(paths.state_path.read_text(encoding="utf-8")) == committed
    assert len(paths.events_path.read_bytes().splitlines()) == 1


def test_tampered_history_cannot_replace_corrupt_snapshot(tmp_path):
    """Ловит recovery по неподтверждённому/изменённому событию."""
    from studio.errors import InvalidEventHistoryError

    store, paths = _store(tmp_path)
    store.commit(
        state=_state(gate_status="IN_PROGRESS"), expected_revision=0,
        last_verified_gate=None, next_gate="FunnelFit", blocked_reason=None,
        evidence_hashes={},
    )
    line = json.loads(paths.events_path.read_text(encoding="utf-8"))
    line["snapshot"]["blocked_reason"] = "invented"
    paths.events_path.write_text(json.dumps(line) + "\n", encoding="utf-8")
    corrupt = b"broken snapshot"
    paths.state_path.write_bytes(corrupt)

    with pytest.raises(InvalidEventHistoryError, match="hash"):
        store.recover()

    assert paths.state_path.read_bytes() == corrupt


def test_brief_digest_change_invalidates_offer_and_dependents_only():
    """Ловит глобальный reset, уничтожающий принятую Factura и независимые readiness."""
    from studio.state import create_snapshot, invalidate_for_digest_change

    old = hashlib.sha256(b"old offer brief").hexdigest()
    factura = hashlib.sha256(b"factura").hexdigest()
    meaning = hashlib.sha256(b"meaning").hexdigest()
    quiz_preview = hashlib.sha256(b"quiz preview").hexdigest()
    seller_preview = hashlib.sha256(b"seller preview").hexdigest()
    local_verify = hashlib.sha256(b"local verify").hexdigest()
    snapshot = create_snapshot(
        _state(
            phase="VALIDATION", gate="LocalVerify", gate_status="PASS",
            technical_readiness="IN_PROGRESS", market_readiness="BLOCKED",
        ),
        last_verified_gate="LocalVerify",
        next_gate="ProductionReadiness",
        blocked_reason="Рынок не подтверждён.",
        revision=7,
        evidence_hashes={
            "Factura": factura,
            "Offer": old,
            "Meaning": meaning,
            "QuizPreview": quiz_preview,
            "SellerPreview": seller_preview,
            "LocalVerify": local_verify,
        },
    )
    dependencies = {
        "Offer": {"Factura"},
        "Meaning": {"Offer"},
        "QuizPreview": {"Meaning"},
        "SellerPreview": {"Meaning"},
        "LocalVerify": {"QuizPreview", "SellerPreview"},
        "ProductionReadiness": {"LocalVerify"},
    }

    updated = invalidate_for_digest_change(
        snapshot,
        gate="Offer",
        digest=hashlib.sha256(b"manual edit").hexdigest(),
        dependencies=dependencies,
    )

    assert updated["revision"] == 7
    assert updated["last_verified_gate"] == "Factura"
    assert updated["next_gate"] == "Offer"
    assert updated["blocked_reason"] is None
    assert updated["evidence_hashes"] == {"Factura": factura}
    assert updated["state"] == {
        **snapshot["state"],
        "gate": "Offer",
        "gate_status": "NOT_STARTED",
    }
    assert updated["state"]["phase"] == "VALIDATION"
    assert updated["state"]["technical_readiness"] == "IN_PROGRESS"
    assert updated["state"]["market_readiness"] == "BLOCKED"
    assert snapshot["evidence_hashes"] == {
        "Factura": factura,
        "Offer": old,
        "Meaning": meaning,
        "QuizPreview": quiz_preview,
        "SellerPreview": seller_preview,
        "LocalVerify": local_verify,
    }


def test_unchanged_digest_does_not_invalidate_snapshot():
    """Ловит ложную инвалидацию при повторной проверке тех же байтов."""
    from studio.state import create_snapshot, invalidate_for_digest_change

    digest = hashlib.sha256(b"same").hexdigest()
    snapshot = create_snapshot(
        _state(gate="Offer", gate_status="PASS"),
        last_verified_gate="Offer", next_gate="Meaning", revision=3,
        evidence_hashes={"Offer": digest},
    )

    updated = invalidate_for_digest_change(
        snapshot, gate="Offer", digest=digest, dependencies={"Meaning": {"Offer"}},
    )

    assert updated == snapshot
    assert updated is not snapshot
