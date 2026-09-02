"""State persistence: atomic snapshots, append-only proof and recovery."""
from __future__ import annotations

import hashlib
import json
import multiprocessing
import os
from pathlib import Path
from queue import Empty
import shutil

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


def _crash_at_stage(funnel_root: str, stage: str) -> None:
    from studio.state import StateStore

    def crash(current_stage: str) -> None:
        if current_stage == stage:
            os._exit(0)

    StateStore(funnel_root, fault_hook=crash).commit(
        state=_state(gate_status="IN_PROGRESS"),
        expected_revision=0,
        last_verified_gate=None,
        next_gate="FunnelFit",
        blocked_reason=None,
        evidence_hashes={},
    )


def _commit(store, *, expected_revision: int, gate_status: str = "IN_PROGRESS"):
    return store.commit(
        state=_state(gate_status=gate_status),
        expected_revision=expected_revision,
        last_verified_gate=None,
        next_gate="FunnelFit",
        blocked_reason="blocked" if gate_status == "BLOCKED" else None,
        evidence_hashes={},
    )


def _raising_hook(target_stage: str):
    def fail(stage: str) -> None:
        if stage == target_stage:
            raise OSError(f"injected at {stage}")

    return fail


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
    """Ловит truncate/in-place write и потерю durable write-ahead event при сбое."""
    store, paths = _store(tmp_path)
    before_state = paths.state_path.read_bytes()

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
    assert len(paths.events_path.read_bytes().splitlines()) == 1
    assert list(paths.funnel_root.glob(".STATE.json.*.tmp")) == []
    assert store.load()["revision"] == 1


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
    """Ловит stale lock и потерю durable write-ahead события."""
    from studio.state import StateStore

    store, paths = _store(tmp_path)
    before = paths.state_path.read_bytes()
    context = multiprocessing.get_context("spawn")
    process = context.Process(target=_crash_during_commit, args=(str(paths.funnel_root),))
    process.start()
    process.join(timeout=10)
    assert process.exitcode == 0
    assert paths.state_path.read_bytes() == before

    reconciled = StateStore(paths.funnel_root, lock_timeout=1).load()
    assert reconciled["revision"] == 1

    committed = StateStore(paths.funnel_root, lock_timeout=1).commit(
        state=_state(gate_status="BLOCKED"),
        expected_revision=1,
        last_verified_gate=None,
        next_gate="FunnelFit",
        blocked_reason="retry after process death",
        evidence_hashes={},
    )

    assert committed["revision"] == 2


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


def test_partial_event_append_is_removed_before_retry(tmp_path):
    """Ловит permanent wedge после неполной newline-незавершённой event записи."""
    from studio.state import StateStore

    store, paths = _store(tmp_path)
    before = paths.state_path.read_bytes()
    with pytest.raises(OSError, match="after_partial_event_write"):
        StateStore(
            paths.funnel_root,
            fault_hook=_raising_hook("after_partial_event_write"),
        ).commit(
            state=_state(gate_status="IN_PROGRESS"),
            expected_revision=0,
            last_verified_gate=None,
            next_gate="FunnelFit",
            blocked_reason=None,
            evidence_hashes={},
        )

    assert paths.state_path.read_bytes() == before
    assert paths.events_path.read_bytes() and not paths.events_path.read_bytes().endswith(b"\n")
    assert store.load()["revision"] == 0
    assert paths.events_path.read_bytes() == b""
    assert _commit(store, expected_revision=0)["revision"] == 1


@pytest.mark.parametrize("stage", ["after_event_fsync", "before_snapshot_replace"])
def test_journal_ahead_is_reconciled_and_remains_writable(tmp_path, stage):
    """Ловит потерю durable event или wedge, когда journal на revision впереди snapshot."""
    from studio.state import StateStore

    store, paths = _store(tmp_path)
    old_state = paths.state_path.read_bytes()
    with pytest.raises(OSError, match=stage):
        StateStore(paths.funnel_root, fault_hook=_raising_hook(stage)).commit(
            state=_state(gate_status="IN_PROGRESS"),
            expected_revision=0,
            last_verified_gate=None,
            next_gate="FunnelFit",
            blocked_reason=None,
            evidence_hashes={},
        )

    assert paths.state_path.read_bytes() == old_state
    assert len(paths.events_path.read_bytes().splitlines()) == 1
    proven = store.load()
    assert proven["revision"] == 1
    assert proven["state"]["gate_status"] == "IN_PROGRESS"
    assert _commit(store, expected_revision=1, gate_status="BLOCKED")["revision"] == 2


def test_hard_kill_after_durable_event_reconciles_on_restart(tmp_path):
    """Ловит недоказанный rollback после гибели между event fsync и snapshot replace."""
    store, paths = _store(tmp_path)
    context = multiprocessing.get_context("spawn")
    process = context.Process(
        target=_crash_at_stage,
        args=(str(paths.funnel_root), "after_event_fsync"),
    )
    process.start()
    process.join(timeout=10)
    assert process.exitcode == 0

    assert json.loads(paths.state_path.read_text(encoding="utf-8"))["revision"] == 0
    assert store.load()["revision"] == 1
    assert _commit(store, expected_revision=1)["revision"] == 2


@pytest.mark.parametrize(
    ("stage", "proven_revision"),
    [
        ("before_event_append", 0),
        ("after_partial_event_write", 0),
        ("after_event_fsync", 1),
        ("before_snapshot_replace", 1),
        ("after_snapshot_replace", 1),
        ("after_snapshot_directory_fsync", 1),
    ],
)
def test_hard_kill_at_each_commit_boundary_converges_and_allows_next_write(
    tmp_path, stage, proven_revision
):
    """Ловит finite crash-state, из которого load не может сам выйти."""
    store, paths = _store(tmp_path)
    context = multiprocessing.get_context("spawn")
    process = context.Process(target=_crash_at_stage, args=(str(paths.funnel_root), stage))
    process.start()
    process.join(timeout=10)
    assert process.exitcode == 0

    assert store.load()["revision"] == proven_revision
    assert _commit(store, expected_revision=proven_revision)["revision"] == proven_revision + 1


def test_failure_after_snapshot_replace_exposes_only_matching_proven_state(tmp_path):
    """Ловит commit, который после replace оставляет snapshot не совпадающим с tail event."""
    from studio.state import StateStore

    store, paths = _store(tmp_path)
    with pytest.raises(OSError, match="after_snapshot_replace"):
        StateStore(
            paths.funnel_root,
            fault_hook=_raising_hook("after_snapshot_replace"),
        ).commit(
            state=_state(gate_status="IN_PROGRESS"),
            expected_revision=0,
            last_verified_gate=None,
            next_gate="FunnelFit",
            blocked_reason=None,
            evidence_hashes={},
        )

    assert store.load()["revision"] == 1
    assert len(paths.events_path.read_bytes().splitlines()) == 1
    assert _commit(store, expected_revision=1)["revision"] == 2


def test_same_revision_divergence_is_rejected_until_explicit_recovery(tmp_path):
    """Ловит принятие schema-valid STATE с revision, не совпадающим с proof tail."""
    from studio.errors import StateIntegrityError

    store, paths = _store(tmp_path)
    committed = _commit(store, expected_revision=0)
    changed = json.loads(paths.state_path.read_text(encoding="utf-8"))
    changed["state"]["gate_status"] = "BLOCKED"
    changed["blocked_reason"] = "manual divergence"
    paths.state_path.write_text(json.dumps(changed) + "\n", encoding="utf-8")

    with pytest.raises(StateIntegrityError, match="snapshot hash"):
        store.load()
    with pytest.raises(StateIntegrityError, match="snapshot hash"):
        _commit(store, expected_revision=1)

    assert store.recover() == committed
    assert _commit(store, expected_revision=1)["revision"] == 2


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("phase", "DESIGN"),
        ("gate", "Offer"),
        ("gate_status", "BLOCKED"),
        ("technical_readiness", "NOT_READY"),
        ("market_readiness", "IN_PROGRESS"),
    ],
)
def test_each_valid_u1_state_axis_is_bound_to_same_revision_event(tmp_path, field, value):
    """Ловит same-revision divergence на каждой независимой U1 оси."""
    from studio.errors import StateIntegrityError

    store, paths = _store(tmp_path)
    committed = _commit(store, expected_revision=0)
    changed = json.loads(paths.state_path.read_text(encoding="utf-8"))
    changed["state"][field] = value
    assert validate_state(changed["state"]).errors == []
    paths.state_path.write_text(json.dumps(changed) + "\n", encoding="utf-8")

    with pytest.raises(StateIntegrityError, match="snapshot hash"):
        store.load()
    assert store.recover() == committed


def test_complete_prefix_truncation_is_rejected_when_snapshot_has_newer_head(tmp_path):
    """Ловит silent rollback к валидному префиксу при сохранившемся новом snapshot head."""
    from studio.errors import StateIntegrityError

    store, paths = _store(tmp_path)
    _commit(store, expected_revision=0)
    second = _commit(store, expected_revision=1, gate_status="BLOCKED")
    lines = paths.events_path.read_bytes().splitlines(keepends=True)
    paths.events_path.write_bytes(lines[0])
    snapshot_bytes = paths.state_path.read_bytes()

    with pytest.raises(StateIntegrityError, match="ahead of event history"):
        store.load()
    with pytest.raises(StateIntegrityError, match="ahead of event history"):
        store.recover()
    assert paths.state_path.read_bytes() == snapshot_bytes
    assert second["revision"] == 2


def test_multi_event_history_recovers_tail_and_accepts_next_commit(tmp_path):
    """Ловит recovery, игнорирующий previous hash/revision link после первого event."""
    store, paths = _store(tmp_path)
    _commit(store, expected_revision=0)
    second = _commit(store, expected_revision=1, gate_status="BLOCKED")
    paths.state_path.write_bytes(b"corrupt")

    assert store.recover() == second
    assert _commit(store, expected_revision=2)["revision"] == 3
    assert len(paths.events_path.read_bytes().splitlines()) == 3


@pytest.mark.parametrize("mutation", ["delete_middle", "reorder", "bad_link"])
def test_broken_multi_event_chain_is_rejected(tmp_path, mutation):
    """Ловит принятие удалённого, переставленного или неверно связанного event."""
    from studio.errors import InvalidEventHistoryError

    store, paths = _store(tmp_path)
    _commit(store, expected_revision=0)
    _commit(store, expected_revision=1)
    _commit(store, expected_revision=2)
    events = [json.loads(line) for line in paths.events_path.read_text().splitlines()]
    if mutation == "delete_middle":
        events.pop(1)
    elif mutation == "reorder":
        events[1], events[2] = events[2], events[1]
    else:
        events[1]["previous_event_hash"] = "0" * 64
    paths.events_path.write_text("".join(json.dumps(item) + "\n" for item in events))

    with pytest.raises(InvalidEventHistoryError):
        store.load()


def test_partial_tail_after_valid_events_is_discarded_without_losing_head(tmp_path):
    """Ловит отказ от valid prefix из-за недописанного следующего event."""
    store, paths = _store(tmp_path)
    _commit(store, expected_revision=0)
    second = _commit(store, expected_revision=1)
    complete = paths.events_path.read_bytes()
    with paths.events_path.open("ab") as stream:
        stream.write(b'{"partial":')

    assert store.load() == second
    assert paths.events_path.read_bytes() == complete


def test_cross_funnel_snapshot_and_history_replay_is_rejected(tmp_path):
    """Ловит перенос валидной state/history пары в funnel с другой identity."""
    from studio.errors import StateIntegrityError
    from studio.project_layout import initialize_project
    from studio.state import StateStore

    first_store, first_paths = _store(tmp_path)
    _commit(first_store, expected_revision=0)
    package_root = Path(__file__).resolve().parents[2]
    second_paths = initialize_project(
        projects_root=tmp_path / "projects",
        templates_root=package_root / "templates",
        project_slug="beta",
        funnel_slug="main",
    )
    shutil.copyfile(first_paths.state_path, second_paths.state_path)
    shutil.copyfile(first_paths.events_path, second_paths.events_path)

    with pytest.raises(StateIntegrityError, match="funnel identity"):
        StateStore(second_paths.funnel_root).load()


@pytest.mark.parametrize("operation", ["load", "commit", "recover"])
@pytest.mark.parametrize("leaf", ["STATE.json", "events.jsonl", ".funnel-studio-funnel.json"])
def test_symlinked_managed_leaf_is_rejected_without_outside_io(tmp_path, leaf, operation):
    """Ловит follow-symlink чтение/append для state, history или identity marker."""
    from studio.errors import UnsafeFilesystemEntryError
    from studio.state import StateStore

    store, paths = _store(tmp_path)
    outside = tmp_path / "outside"
    outside.write_bytes(b"outside")
    target = paths.funnel_root / leaf
    target.unlink()
    target.symlink_to(outside)

    with pytest.raises(UnsafeFilesystemEntryError):
        if operation == "load":
            store.load()
        elif operation == "commit":
            _commit(store, expected_revision=0)
        else:
            store.recover()
    assert outside.read_bytes() == b"outside"


@pytest.mark.parametrize("operation", ["load", "commit", "recover"])
def test_managed_ancestor_symlink_swap_is_rejected_without_outside_io(tmp_path, operation):
    """Ловит pathname ancestor swap между StateStore construction и I/O."""
    from studio.errors import UnsafeFilesystemEntryError

    store, paths = _store(tmp_path)
    project = paths.project_root
    moved = paths.projects_root / "alpha-moved"
    outside = tmp_path / "outside-project"
    outside.mkdir()
    sentinel = outside / "sentinel"
    sentinel.write_bytes(b"outside")
    project.rename(moved)
    project.symlink_to(outside, target_is_directory=True)

    with pytest.raises(UnsafeFilesystemEntryError):
        if operation == "load":
            store.load()
        elif operation == "commit":
            _commit(store, expected_revision=0)
        else:
            store.recover()
    assert sentinel.read_bytes() == b"outside"


class _FakeLockBackend:
    def __init__(self, outcomes):
        self.outcomes = iter(outcomes)
        self.acquired = []
        self.released = []

    def try_acquire(self, descriptor: int) -> bool:
        self.acquired.append(descriptor)
        outcome = next(self.outcomes)
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome

    def release(self, descriptor: int) -> None:
        self.released.append(descriptor)


def test_injected_lock_backend_contract_covers_acquire_busy_release_and_error(tmp_path):
    """Ловит неисполняемый platform branch и потерю backend ошибок за abstraction."""
    from studio.errors import LockUnavailableError
    from studio.state import _ExclusiveFileLock

    available = _FakeLockBackend([True])
    with _ExclusiveFileLock(tmp_path / "available.lock", timeout=0, backend=available):
        pass
    assert len(available.acquired) == 1
    assert available.released == available.acquired

    busy = _FakeLockBackend([False])
    with pytest.raises(LockUnavailableError):
        with _ExclusiveFileLock(tmp_path / "busy.lock", timeout=0, backend=busy):
            pass
    assert busy.released == []

    broken = _FakeLockBackend([OSError("backend failed")])
    with pytest.raises(OSError, match="backend failed"):
        with _ExclusiveFileLock(tmp_path / "broken.lock", timeout=0, backend=broken):
            pass


def test_unlock_error_does_not_mask_body_error(tmp_path):
    """Ловит подмену StateStore/domain failure вторичной unlock ошибкой."""
    from studio.state import _ExclusiveFileLock

    class BrokenRelease(_FakeLockBackend):
        def release(self, descriptor: int) -> None:
            raise OSError("unlock failed")

    with pytest.raises(ValueError, match="body failed"):
        with _ExclusiveFileLock(
            tmp_path / "unlock.lock",
            timeout=0,
            backend=BrokenRelease([True]),
        ):
            raise ValueError("body failed")


def test_lock_identity_detects_pathname_replacement_and_symlink(tmp_path):
    """Ловит второй lock inode, созданный заменой pathname удерживаемого lock."""
    from studio.errors import LockUnavailableError, UnsafeFilesystemEntryError
    from studio.state import _ExclusiveFileLock

    path = tmp_path / "stable.lock"
    with _ExclusiveFileLock(path, timeout=0) as held:
        path.unlink()
        path.write_bytes(b"replacement")
        with pytest.raises(LockUnavailableError, match="identity changed"):
            held.assert_owned()

    path.unlink()
    outside = tmp_path / "outside-lock"
    outside.write_bytes(b"outside")
    path.symlink_to(outside)
    with pytest.raises(UnsafeFilesystemEntryError):
        with _ExclusiveFileLock(path, timeout=0):
            pass
    assert outside.read_bytes() == b"outside"


def test_lock_replacement_at_commit_boundary_prevents_event_io(tmp_path):
    """Ловит append после lock pathname swap между ownership check и I/O."""
    from studio.errors import LockUnavailableError
    from studio.state import StateStore

    store, paths = _store(tmp_path)

    def replace_lock(stage: str) -> None:
        if stage == "before_event_append":
            store.lock_path.unlink()
            store.lock_path.write_bytes(b"replacement")

    faulty = StateStore(paths.funnel_root, fault_hook=replace_lock)
    with pytest.raises(LockUnavailableError, match="identity changed"):
        _commit(faulty, expected_revision=0)

    assert paths.events_path.read_bytes() == b""
    assert json.loads(paths.state_path.read_text(encoding="utf-8"))["revision"] == 0


def test_lock_replacement_before_snapshot_publish_cannot_publish_snapshot(tmp_path):
    """Ловит STATE replace после loss of lock ownership на второй I/O boundary."""
    from studio.errors import LockUnavailableError
    from studio.state import StateStore

    store, paths = _store(tmp_path)

    def replace_lock(stage: str) -> None:
        if stage == "before_snapshot_replace":
            store.lock_path.unlink()
            store.lock_path.write_bytes(b"replacement")

    faulty = StateStore(paths.funnel_root, fault_hook=replace_lock)
    with pytest.raises(LockUnavailableError, match="identity changed"):
        _commit(faulty, expected_revision=0)

    assert json.loads(paths.state_path.read_text(encoding="utf-8"))["revision"] == 0
    assert len(paths.events_path.read_bytes().splitlines()) == 1
    assert StateStore(paths.funnel_root).load()["revision"] == 1
