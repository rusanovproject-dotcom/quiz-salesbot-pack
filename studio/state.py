"""Atomic snapshots, optimistic revisions and proven state recovery."""
from __future__ import annotations

from contextlib import AbstractContextManager
from copy import deepcopy
from datetime import datetime, timezone
import errno
import hashlib
import json
import os
from pathlib import Path
import tempfile
import time
from typing import Any, Callable, Collection, Mapping

from studio.contracts import GATE_NAMES, validate_state
from studio.errors import (
    CorruptStateError,
    InvalidEventHistoryError,
    LockUnavailableError,
    RevisionConflictError,
)


SNAPSHOT_VERSION = "studio-snapshot/v1"
EVENT_VERSION = "studio-event/v1"
_SNAPSHOT_FIELDS = {
    "snapshot_version",
    "state",
    "last_verified_gate",
    "next_gate",
    "blocked_reason",
    "revision",
    "evidence_hashes",
}
_EVENT_FIELDS = {
    "event_version",
    "kind",
    "recorded_at",
    "revision",
    "previous_revision",
    "previous_event_hash",
    "snapshot_hash",
    "snapshot",
    "event_hash",
}


def create_snapshot(
    state: Mapping[str, Any],
    *,
    last_verified_gate: str | None = None,
    next_gate: str | None = GATE_NAMES[0],
    blocked_reason: str | None = None,
    revision: int = 0,
    evidence_hashes: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    """Wrap an unchanged valid U1 state in U2 persistence metadata."""
    snapshot = {
        "snapshot_version": SNAPSHOT_VERSION,
        "state": deepcopy(dict(state)),
        "last_verified_gate": last_verified_gate,
        "next_gate": next_gate,
        "blocked_reason": blocked_reason,
        "revision": revision,
        "evidence_hashes": deepcopy(dict(evidence_hashes or {})),
    }
    error = _snapshot_error(snapshot)
    if error is not None:
        raise ValueError(error)
    return snapshot


def invalidate_for_digest_change(
    snapshot: Mapping[str, Any],
    *,
    gate: str,
    digest: str,
    dependencies: Mapping[str, Collection[str]],
) -> dict[str, Any]:
    """Invalidate one gate and transitive dependants when its evidence changed.

    ``dependencies`` maps each gate to its direct prerequisites. The caller owns
    the product-specific graph; persistence owns the lossless invalidation rule.
    """
    copied = deepcopy(dict(snapshot))
    error = _snapshot_error(copied)
    if error is not None:
        raise ValueError(error)
    _require_gate(gate, "gate")
    _require_digest(digest, "digest")
    _validate_dependencies(dependencies)
    if copied["evidence_hashes"].get(gate) == digest:
        return copied

    affected = {gate}
    changed = True
    while changed:
        changed = False
        for candidate, prerequisites in dependencies.items():
            if candidate not in affected and affected.intersection(prerequisites):
                affected.add(candidate)
                changed = True

    copied["evidence_hashes"] = {
        name: value
        for name, value in copied["evidence_hashes"].items()
        if name not in affected
    }
    proven = [name for name in GATE_NAMES if name in copied["evidence_hashes"]]
    copied["last_verified_gate"] = proven[-1] if proven else None
    copied["next_gate"] = gate
    copied["blocked_reason"] = None
    copied["state"]["gate"] = gate
    copied["state"]["gate_status"] = "NOT_STARTED"
    return copied


class StateStore:
    """Single-writer storage rooted in one already isolated funnel tree."""

    def __init__(
        self,
        funnel_root: str | Path,
        *,
        before_replace: Callable[[Path], None] | None = None,
        lock_timeout: float = 5.0,
    ) -> None:
        self.funnel_root = Path(funnel_root)
        self.state_path = self.funnel_root / "STATE.json"
        self.events_path = self.funnel_root / "events.jsonl"
        self.lock_path = self.funnel_root / ".state.lock"
        self.before_replace = before_replace
        self.lock_timeout = lock_timeout

    def load(self) -> dict[str, Any]:
        """Read and validate without repairing, resetting or rewriting bytes."""
        try:
            raw = self.state_path.read_bytes()
            payload = json.loads(raw.decode("utf-8"))
        except FileNotFoundError as exc:
            raise CorruptStateError(f"STATE.json is missing: {self.state_path}") from exc
        except UnicodeDecodeError as exc:
            raise CorruptStateError(f"STATE.json is not valid UTF-8 JSON: {exc}") from exc
        except json.JSONDecodeError as exc:
            raise CorruptStateError(f"STATE.json contains invalid JSON: {exc.msg}") from exc
        error = _snapshot_error(payload)
        if error is not None:
            raise CorruptStateError(f"STATE.json violates snapshot contract: {error}")
        return payload

    def commit(
        self,
        *,
        state: Mapping[str, Any],
        expected_revision: int,
        last_verified_gate: str | None,
        next_gate: str | None,
        blocked_reason: str | None,
        evidence_hashes: Mapping[str, str],
    ) -> dict[str, Any]:
        """Commit exactly one optimistic revision and append its proof event."""
        with _ExclusiveFileLock(self.lock_path, timeout=self.lock_timeout):
            current = self.load()
            actual_revision = current["revision"]
            if actual_revision != expected_revision:
                raise RevisionConflictError(
                    f"expected revision {expected_revision}, found {actual_revision}"
                )
            events = self._validated_events()
            if events and events[-1]["revision"] != actual_revision:
                raise InvalidEventHistoryError(
                    "event history revision does not match current STATE.json"
                )
            if not events and actual_revision != 0:
                raise InvalidEventHistoryError("event history is missing committed revisions")

            snapshot = create_snapshot(
                state,
                last_verified_gate=last_verified_gate,
                next_gate=next_gate,
                blocked_reason=blocked_reason,
                revision=actual_revision + 1,
                evidence_hashes=evidence_hashes,
            )
            previous_hash = events[-1]["event_hash"] if events else None
            event = _create_event(snapshot, actual_revision, previous_hash)
            self._write_snapshot(snapshot)
            self._append_event(event)
            return snapshot

    def recover(self) -> dict[str, Any]:
        """Replace only a corrupt snapshot, using the last fully proven event."""
        with _ExclusiveFileLock(self.lock_path, timeout=self.lock_timeout):
            try:
                return self.load()
            except CorruptStateError:
                pass
            events = self._validated_events()
            if not events:
                raise InvalidEventHistoryError("event history has no proven snapshot")
            snapshot = deepcopy(events[-1]["snapshot"])
            self._write_snapshot(snapshot)
            return snapshot

    def _write_snapshot(self, snapshot: Mapping[str, Any]) -> None:
        write_snapshot_atomic(
            self.state_path,
            snapshot,
            before_replace=self.before_replace,
        )

    def _append_event(self, event: Mapping[str, Any]) -> None:
        raw = _compact_json_bytes(event) + b"\n"
        with self.events_path.open("ab") as stream:
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())

    def _validated_events(self) -> list[dict[str, Any]]:
        try:
            raw = self.events_path.read_bytes()
        except FileNotFoundError as exc:
            raise InvalidEventHistoryError("events.jsonl is missing") from exc
        if not raw:
            return []
        if not raw.endswith(b"\n"):
            raise InvalidEventHistoryError("events.jsonl has a partial trailing event")

        events: list[dict[str, Any]] = []
        previous_hash: str | None = None
        previous_revision = 0
        for line_number, line in enumerate(raw.splitlines(), start=1):
            try:
                event = json.loads(line.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise InvalidEventHistoryError(
                    f"events.jsonl line {line_number} is invalid JSON"
                ) from exc
            error = _event_error(event, previous_revision, previous_hash)
            if error is not None:
                raise InvalidEventHistoryError(f"events.jsonl line {line_number}: {error}")
            events.append(event)
            previous_hash = event["event_hash"]
            previous_revision = event["revision"]
        return events


class _ExclusiveFileLock(AbstractContextManager["_ExclusiveFileLock"]):
    """Narrow cross-platform advisory lock released by the OS on process exit."""

    def __init__(self, path: Path, *, timeout: float) -> None:
        self.path = path
        self.timeout = timeout
        self._fd: int | None = None

    def __enter__(self) -> "_ExclusiveFileLock":
        deadline = time.monotonic() + self.timeout
        self._fd = os.open(self.path, os.O_CREAT | os.O_RDWR, 0o600)
        try:
            while True:
                if _try_platform_lock(self._fd):
                    return self
                if time.monotonic() >= deadline:
                    raise LockUnavailableError(f"state lock is busy: {self.path}")
                time.sleep(0.01)
        except BaseException:
            os.close(self._fd)
            self._fd = None
            raise

    def __exit__(self, exc_type, exc_value, traceback) -> None:
        if self._fd is not None:
            try:
                _release_platform_lock(self._fd)
            finally:
                os.close(self._fd)
                self._fd = None


def write_snapshot_atomic(
    path: str | Path,
    snapshot: Mapping[str, Any],
    *,
    before_replace: Callable[[Path], None] | None = None,
) -> None:
    """Durably replace a snapshot through a flushed temporary sibling."""
    destination = Path(path)
    error = _snapshot_error(snapshot)
    if error is not None:
        raise ValueError(error)
    raw = _pretty_json_bytes(snapshot)
    fd, temporary_name = tempfile.mkstemp(
        dir=destination.parent,
        prefix=f".{destination.name}.",
        suffix=".tmp",
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
        if before_replace is not None:
            before_replace(temporary)
        os.replace(temporary, destination)
        _fsync_directory(destination.parent)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise


def _try_platform_lock(descriptor: int) -> bool:
    """Hide platform-specific primitives behind one non-blocking operation."""
    if os.name == "nt":  # pragma: no cover - exercised on Windows CI
        import msvcrt

        if os.fstat(descriptor).st_size == 0:
            os.write(descriptor, b"\0")
            os.fsync(descriptor)
        os.lseek(descriptor, 0, os.SEEK_SET)
        try:
            msvcrt.locking(descriptor, msvcrt.LK_NBLCK, 1)
        except OSError as exc:
            if exc.errno in (errno.EACCES, errno.EAGAIN, errno.EDEADLK):
                return False
            raise
        return True

    import fcntl  # POSIX backend; deliberately isolated from StateStore.

    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError as exc:
        if exc.errno in (errno.EACCES, errno.EAGAIN):
            return False
        raise
    return True


def _release_platform_lock(descriptor: int) -> None:
    if os.name == "nt":  # pragma: no cover - exercised on Windows CI
        import msvcrt

        os.lseek(descriptor, 0, os.SEEK_SET)
        msvcrt.locking(descriptor, msvcrt.LK_UNLCK, 1)
        return

    import fcntl  # POSIX backend; deliberately isolated from StateStore.

    fcntl.flock(descriptor, fcntl.LOCK_UN)


def _snapshot_error(payload: Any) -> str | None:
    if not isinstance(payload, Mapping):
        return "snapshot must be an object"
    if set(payload) != _SNAPSHOT_FIELDS:
        return "snapshot fields do not match studio-snapshot/v1"
    if payload.get("snapshot_version") != SNAPSHOT_VERSION:
        return f"unsupported snapshot_version: {payload.get('snapshot_version')!r}"
    state_result = validate_state(payload.get("state"))
    if state_result.errors:
        first = state_result.errors[0]
        return f"state/v1 {first.code} at {first.path}"
    for field in ("last_verified_gate", "next_gate"):
        value = payload[field]
        if value is not None and value not in GATE_NAMES:
            return f"{field} must be a known gate or null"
    reason = payload["blocked_reason"]
    if reason is not None and (not isinstance(reason, str) or not reason):
        return "blocked_reason must be a non-empty string or null"
    revision = payload["revision"]
    if isinstance(revision, bool) or not isinstance(revision, int) or revision < 0:
        return "revision must be a non-negative integer"
    hashes = payload["evidence_hashes"]
    if not isinstance(hashes, Mapping):
        return "evidence_hashes must be an object"
    for gate, digest in hashes.items():
        if gate not in GATE_NAMES:
            return f"evidence_hashes key is not a known gate: {gate!r}"
        try:
            _require_digest(digest, f"evidence_hashes[{gate!r}]")
        except ValueError as exc:
            return str(exc)
    last_verified_gate = payload["last_verified_gate"]
    if last_verified_gate is not None and last_verified_gate not in hashes:
        return "last_verified_gate must have a matching evidence hash"
    return None


def _create_event(
    snapshot: Mapping[str, Any],
    previous_revision: int,
    previous_event_hash: str | None,
) -> dict[str, Any]:
    snapshot_copy = deepcopy(dict(snapshot))
    event: dict[str, Any] = {
        "event_version": EVENT_VERSION,
        "kind": "state_committed",
        "recorded_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "revision": snapshot_copy["revision"],
        "previous_revision": previous_revision,
        "previous_event_hash": previous_event_hash,
        "snapshot_hash": _sha256(_canonical_json_bytes(snapshot_copy)),
        "snapshot": snapshot_copy,
    }
    event["event_hash"] = _sha256(_canonical_json_bytes(event))
    return event


def _event_error(
    event: Any,
    expected_previous_revision: int,
    expected_previous_hash: str | None,
) -> str | None:
    if not isinstance(event, Mapping) or set(event) != _EVENT_FIELDS:
        return "event fields do not match studio-event/v1"
    if event["event_version"] != EVENT_VERSION or event["kind"] != "state_committed":
        return "unsupported event contract"
    if event["previous_revision"] != expected_previous_revision:
        return "revision chain is discontinuous"
    if event["revision"] != expected_previous_revision + 1:
        return "event revision is not monotonic"
    if event["previous_event_hash"] != expected_previous_hash:
        return "previous event hash chain is discontinuous"
    snapshot_error = _snapshot_error(event["snapshot"])
    if snapshot_error is not None:
        return f"snapshot is invalid: {snapshot_error}"
    if event["snapshot"]["revision"] != event["revision"]:
        return "snapshot revision differs from event revision"
    expected_snapshot_hash = _sha256(_canonical_json_bytes(event["snapshot"]))
    if event["snapshot_hash"] != expected_snapshot_hash:
        return "snapshot hash does not match"
    unsigned = {key: value for key, value in event.items() if key != "event_hash"}
    expected_event_hash = _sha256(_canonical_json_bytes(unsigned))
    if event["event_hash"] != expected_event_hash:
        return "event hash does not match"
    recorded_at = event["recorded_at"]
    if not isinstance(recorded_at, str):
        return "recorded_at must be an RFC3339 string"
    try:
        datetime.fromisoformat(recorded_at.replace("Z", "+00:00"))
    except ValueError:
        return "recorded_at must be an RFC3339 string"
    return None


def _validate_dependencies(dependencies: Mapping[str, Collection[str]]) -> None:
    for gate, prerequisites in dependencies.items():
        _require_gate(gate, "dependency gate")
        if isinstance(prerequisites, (str, bytes)):
            raise ValueError("gate prerequisites must be a collection of gate names")
        for prerequisite in prerequisites:
            _require_gate(prerequisite, "dependency prerequisite")


def _require_gate(value: str, label: str) -> None:
    if value not in GATE_NAMES:
        raise ValueError(f"{label} must be a known gate")


def _require_digest(value: Any, label: str) -> None:
    if (
        not isinstance(value, str)
        or len(value) != 64
        or any(character not in "0123456789abcdef" for character in value)
    ):
        raise ValueError(f"{label} must be a lowercase SHA-256 hex digest")


def _canonical_json_bytes(payload: Mapping[str, Any]) -> bytes:
    return json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _compact_json_bytes(payload: Mapping[str, Any]) -> bytes:
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


def _pretty_json_bytes(payload: Mapping[str, Any]) -> bytes:
    return (json.dumps(payload, ensure_ascii=False, indent=2) + "\n").encode("utf-8")


def _sha256(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _fsync_directory(path: Path) -> None:
    try:
        descriptor = os.open(path, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(descriptor)
    except OSError:
        pass
    finally:
        os.close(descriptor)
