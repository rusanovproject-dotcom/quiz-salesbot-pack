"""Crash-consistent Funnel Studio snapshots and integrity event history.

The hash chain detects accidental corruption and binds events to one funnel. It
is not an authorization boundary against a same-UID owner who can rewrite the
entire project tree and recompute every unkeyed SHA-256 value.
"""
from __future__ import annotations

from contextlib import AbstractContextManager, contextmanager
from copy import deepcopy
from datetime import datetime, timezone
import errno
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import tempfile
import time
from typing import Any, Callable, Collection, Iterator, Mapping, Protocol
import uuid

from studio.contracts import GATE_NAMES, validate_state
from studio.errors import (
    CorruptStateError,
    InvalidEventHistoryError,
    LockUnavailableError,
    RevisionConflictError,
    StateIntegrityError,
    UnsafeFilesystemEntryError,
)


SNAPSHOT_VERSION = "studio-snapshot/v1"
EVENT_VERSION = "studio-event/v1"
FUNNEL_MARKER = ".funnel-studio-funnel.json"
_NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)
_DIRECTORY = getattr(os, "O_DIRECTORY", 0)
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
    "funnel_id",
    "recorded_at",
    "revision",
    "previous_revision",
    "previous_event_hash",
    "snapshot_hash",
    "snapshot",
    "event_hash",
}
_FUNNEL_MARKER_FIELDS = {
    "kind",
    "project_slug",
    "funnel_slug",
    "funnel_id",
    "genesis_snapshot_hash",
}
_HEX_32 = re.compile(r"^[0-9a-f]{32}$")


class LockBackend(Protocol):
    """Injectable non-blocking lock primitive used by the file lock shell."""

    def try_acquire(self, descriptor: int) -> bool: ...

    def release(self, descriptor: int) -> None: ...


class _PlatformLockBackend:
    """POSIX lock primitive; Windows support for U2 is through WSL."""

    def try_acquire(self, descriptor: int) -> bool:
        if os.name == "nt":
            raise NotImplementedError("native Windows is unsupported; run through WSL")

        import fcntl

        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            if exc.errno in (errno.EACCES, errno.EAGAIN):
                return False
            raise
        return True

    def release(self, descriptor: int) -> None:
        if os.name == "nt":
            raise NotImplementedError("native Windows is unsupported; run through WSL")

        import fcntl

        fcntl.flock(descriptor, fcntl.LOCK_UN)


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


def snapshot_digest(snapshot: Mapping[str, Any]) -> str:
    """Return the canonical integrity digest used by markers and events."""
    error = _snapshot_error(snapshot)
    if error is not None:
        raise ValueError(error)
    return _sha256(_canonical_json_bytes(snapshot))


def invalidate_for_digest_change(
    snapshot: Mapping[str, Any],
    *,
    gate: str,
    digest: str,
    dependencies: Mapping[str, Collection[str]],
) -> dict[str, Any]:
    """Invalidate one gate and its transitive dependants, preserving other axes."""
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
    """Single-funnel store with event-first commits and locked reconciliation."""

    def __init__(
        self,
        funnel_root: str | Path,
        *,
        before_replace: Callable[[Path], None] | None = None,
        fault_hook: Callable[[str], None] | None = None,
        lock_timeout: float = 5.0,
        lock_backend: LockBackend | None = None,
    ) -> None:
        self.funnel_root = Path(funnel_root).absolute()
        self.state_path = self.funnel_root / "STATE.json"
        self.events_path = self.funnel_root / "events.jsonl"
        self.before_replace = before_replace
        self.fault_hook = fault_hook
        self.lock_timeout = lock_timeout
        self.lock_backend = lock_backend or _PlatformLockBackend()
        self._ancestor_identities = _capture_ancestor_identities(self.funnel_root)
        with _open_directory_path(self.funnel_root) as descriptor:
            self._directory_identity = _file_identity(os.fstat(descriptor))
            self._marker = _read_funnel_marker(descriptor)
        _validate_funnel_location(self.funnel_root, self._marker)
        self.funnel_id = self._marker["funnel_id"]
        self.genesis_snapshot_hash = self._marker["genesis_snapshot_hash"]
        self.lock_path = _stable_lock_path(self.funnel_root, self.funnel_id)

    def load(self) -> dict[str, Any]:
        """Return only state matching the durable journal, repairing journal-ahead."""
        with self._locked_funnel() as (lock, directory):
            return self._load_proven(directory, lock)

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
        """Durably append proof first, then publish its snapshot projection."""
        with self._locked_funnel() as (lock, directory):
            current = self._load_proven(directory, lock)
            actual_revision = current["revision"]
            if actual_revision != expected_revision:
                raise RevisionConflictError(
                    f"expected revision {expected_revision}, found {actual_revision}"
                )
            snapshot = create_snapshot(
                state,
                last_verified_gate=last_verified_gate,
                next_gate=next_gate,
                blocked_reason=blocked_reason,
                revision=actual_revision + 1,
                evidence_hashes=evidence_hashes,
            )
            events = _read_events_at(directory, self.funnel_id, repair_partial=True)
            previous_hash = events[-1]["event_hash"] if events else None
            event = _create_event(snapshot, actual_revision, previous_hash, self.funnel_id)

            lock.assert_owned()
            self._fault("before_event_append")
            lock.assert_owned()
            _append_event_at(directory, event, self._fault)
            lock.assert_owned()
            appended = _read_events_at(directory, self.funnel_id, repair_partial=False)
            if not appended or appended[-1]["event_hash"] != event["event_hash"]:
                raise InvalidEventHistoryError("appended event is not the journal tail")
            _write_snapshot_at(
                directory,
                snapshot,
                fault_hook=self._fault,
                before_replace=self.before_replace,
                ownership_check=lock.assert_owned,
            )
            lock.assert_owned()
            return snapshot

    def recover(self) -> dict[str, Any]:
        """Explicitly project the valid event tail over corrupt/same-revision state."""
        with self._locked_funnel() as (lock, directory):
            events = _read_events_at(directory, self.funnel_id, repair_partial=True)
            try:
                snapshot = _read_snapshot_at(directory)
            except CorruptStateError:
                if not events:
                    raise InvalidEventHistoryError("event history has no recoverable snapshot")
                recovered = deepcopy(events[-1]["snapshot"])
                _write_snapshot_at(directory, recovered, fault_hook=self._fault)
                lock.assert_owned()
                return recovered

            if not events:
                self._validate_genesis(snapshot)
                return snapshot
            tail = events[-1]
            revision = snapshot["revision"]
            if revision > tail["revision"]:
                raise StateIntegrityError("STATE.json is ahead of event history; refusing rollback")
            if revision == tail["revision"] and snapshot_digest(snapshot) == tail["snapshot_hash"]:
                return snapshot
            recovered = deepcopy(tail["snapshot"])
            _write_snapshot_at(directory, recovered, fault_hook=self._fault)
            lock.assert_owned()
            return recovered

    @contextmanager
    def _locked_funnel(self) -> Iterator[tuple["_ExclusiveFileLock", int]]:
        with _ExclusiveFileLock(
            self.lock_path,
            timeout=self.lock_timeout,
            backend=self.lock_backend,
        ) as lock:
            lock.assert_owned()
            with self._open_funnel() as directory:
                yield lock, directory

    @contextmanager
    def _open_funnel(self) -> Iterator[int]:
        projects_path = self._ancestor_identities[0][0]
        components = (
            self._marker["project_slug"],
            "funnels",
            self._marker["funnel_slug"],
        )
        with _open_directory_path(projects_path) as projects_fd:
            _require_directory_identity(projects_fd, self._ancestor_identities[0][1])
            with _open_directory_at(projects_fd, components[0]) as project_fd:
                _require_directory_identity(project_fd, self._ancestor_identities[1][1])
                with _open_directory_at(project_fd, components[1]) as funnels_fd:
                    _require_directory_identity(funnels_fd, self._ancestor_identities[2][1])
                    with _open_directory_at(funnels_fd, components[2]) as funnel_fd:
                        _require_directory_identity(funnel_fd, self._directory_identity)
                        if _read_funnel_marker(funnel_fd) != self._marker:
                            raise StateIntegrityError("funnel identity marker changed")
                        yield funnel_fd

    def _load_proven(self, directory: int, lock: "_ExclusiveFileLock") -> dict[str, Any]:
        snapshot = _read_snapshot_at(directory)
        events = _read_events_at(directory, self.funnel_id, repair_partial=True)
        if not events:
            self._validate_genesis(snapshot)
            return snapshot

        tail = events[-1]
        revision = snapshot["revision"]
        if revision > tail["revision"]:
            raise StateIntegrityError("STATE.json is ahead of event history")
        if revision == tail["revision"]:
            if snapshot_digest(snapshot) != tail["snapshot_hash"]:
                raise StateIntegrityError("STATE.json snapshot hash differs from event tail")
            return snapshot

        lock.assert_owned()
        reconciled = deepcopy(tail["snapshot"])
        _write_snapshot_at(directory, reconciled, fault_hook=self._fault)
        lock.assert_owned()
        return reconciled

    def _validate_genesis(self, snapshot: Mapping[str, Any]) -> None:
        if snapshot["revision"] != 0:
            raise StateIntegrityError("STATE.json is ahead of empty event history")
        if snapshot_digest(snapshot) != self.genesis_snapshot_hash:
            raise StateIntegrityError("revision zero differs from genesis snapshot hash")

    def _fault(self, stage: str) -> None:
        if self.fault_hook is not None:
            self.fault_hook(stage)


class _ExclusiveFileLock(AbstractContextManager["_ExclusiveFileLock"]):
    """Advisory lock shell with injectable backend and inode identity checks."""

    def __init__(
        self,
        path: str | Path,
        *,
        timeout: float,
        backend: LockBackend | None = None,
    ) -> None:
        self.path = Path(path)
        self.timeout = timeout
        self.backend = backend or _PlatformLockBackend()
        self._fd: int | None = None
        self._identity: tuple[int, int] | None = None

    def __enter__(self) -> "_ExclusiveFileLock":
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        flags = os.O_CREAT | os.O_RDWR | _NOFOLLOW
        try:
            self._fd = os.open(self.path, flags, 0o600)
        except OSError as exc:
            raise UnsafeFilesystemEntryError(f"unsafe lock entry: {self.path}") from exc
        metadata = os.fstat(self._fd)
        if not stat.S_ISREG(metadata.st_mode):
            os.close(self._fd)
            self._fd = None
            raise UnsafeFilesystemEntryError(f"lock is not a regular file: {self.path}")
        self._identity = _file_identity(metadata)
        deadline = time.monotonic() + self.timeout
        try:
            while True:
                if self.backend.try_acquire(self._fd):
                    self.assert_owned()
                    return self
                if time.monotonic() >= deadline:
                    raise LockUnavailableError(f"state lock is busy: {self.path}")
                time.sleep(0.01)
        except BaseException:
            os.close(self._fd)
            self._fd = None
            raise

    def assert_owned(self) -> None:
        if self._fd is None or self._identity is None:
            raise LockUnavailableError("state lock is not held")
        try:
            metadata = os.lstat(self.path)
        except OSError as exc:
            raise LockUnavailableError("state lock identity changed") from exc
        if not stat.S_ISREG(metadata.st_mode):
            raise LockUnavailableError("state lock identity changed")
        if _file_identity(metadata) != self._identity:
            raise LockUnavailableError("state lock identity changed")

    def __exit__(self, exc_type, exc_value, traceback) -> None:
        if self._fd is None:
            return
        release_error: BaseException | None = None
        try:
            self.backend.release(self._fd)
        except BaseException as error:
            release_error = error
        finally:
            os.close(self._fd)
            self._fd = None
        if release_error is not None and exc_type is None:
            raise release_error


def write_snapshot_atomic(
    path: str | Path,
    snapshot: Mapping[str, Any],
    *,
    before_replace: Callable[[Path], None] | None = None,
) -> None:
    """Durably replace a regular snapshot through a temporary sibling."""
    destination = Path(path)
    with _open_directory_path(destination.parent) as directory:
        _write_snapshot_at(
            directory,
            snapshot,
            before_replace=before_replace,
            temporary_parent=destination.parent,
        )


def _write_snapshot_at(
    directory: int,
    snapshot: Mapping[str, Any],
    *,
    fault_hook: Callable[[str], None] | None = None,
    before_replace: Callable[[Path], None] | None = None,
    temporary_parent: Path | None = None,
    ownership_check: Callable[[], None] | None = None,
) -> None:
    error = _snapshot_error(snapshot)
    if error is not None:
        raise ValueError(error)
    _ensure_regular_at(directory, "STATE.json", allow_missing=True)
    temporary_name = f".STATE.json.{uuid.uuid4().hex}.tmp"
    descriptor = _open_new_regular_at(directory, temporary_name)
    try:
        _write_all(descriptor, _pretty_json_bytes(snapshot))
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    try:
        if before_replace is not None:
            before_replace((temporary_parent or Path(".")) / temporary_name)
        if fault_hook is not None:
            fault_hook("before_snapshot_replace")
        if ownership_check is not None:
            ownership_check()
        os.replace(
            temporary_name,
            "STATE.json",
            src_dir_fd=directory,
            dst_dir_fd=directory,
        )
        if fault_hook is not None:
            fault_hook("after_snapshot_replace")
        _fsync_descriptor(directory)
        if fault_hook is not None:
            fault_hook("after_snapshot_directory_fsync")
    except BaseException:
        try:
            os.unlink(temporary_name, dir_fd=directory)
        except FileNotFoundError:
            pass
        raise


def _append_event_at(
    directory: int,
    event: Mapping[str, Any],
    fault_hook: Callable[[str], None],
) -> None:
    descriptor = _open_regular_at(directory, "events.jsonl", os.O_WRONLY | os.O_APPEND)
    raw = _compact_json_bytes(event) + b"\n"
    split = max(1, len(raw) // 2)
    try:
        _write_all(descriptor, raw[:split])
        fault_hook("after_partial_event_write")
        _write_all(descriptor, raw[split:])
        os.fsync(descriptor)
        fault_hook("after_event_fsync")
    finally:
        os.close(descriptor)


def _read_snapshot_at(directory: int) -> dict[str, Any]:
    try:
        descriptor = _open_regular_at(directory, "STATE.json", os.O_RDONLY)
    except FileNotFoundError as exc:
        raise CorruptStateError("STATE.json is missing") from exc
    try:
        raw = _read_all(descriptor)
    finally:
        os.close(descriptor)
    try:
        payload = json.loads(raw.decode("utf-8"))
    except UnicodeDecodeError as exc:
        raise CorruptStateError(f"STATE.json is not valid UTF-8 JSON: {exc}") from exc
    except json.JSONDecodeError as exc:
        raise CorruptStateError(f"STATE.json contains invalid JSON: {exc.msg}") from exc
    error = _snapshot_error(payload)
    if error is not None:
        raise CorruptStateError(f"STATE.json violates snapshot contract: {error}")
    return payload


def _read_events_at(
    directory: int,
    funnel_id: str,
    *,
    repair_partial: bool,
) -> list[dict[str, Any]]:
    flags = os.O_RDWR if repair_partial else os.O_RDONLY
    try:
        descriptor = _open_regular_at(directory, "events.jsonl", flags)
    except FileNotFoundError as exc:
        raise InvalidEventHistoryError("events.jsonl is missing") from exc
    try:
        raw = _read_all(descriptor)
        if raw and not raw.endswith(b"\n"):
            if not repair_partial:
                raise InvalidEventHistoryError("events.jsonl has a partial trailing event")
            boundary = raw.rfind(b"\n") + 1
            os.ftruncate(descriptor, boundary)
            os.fsync(descriptor)
            raw = raw[:boundary]
    finally:
        os.close(descriptor)
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
        if isinstance(event, Mapping) and event.get("funnel_id") != funnel_id:
            raise StateIntegrityError("event funnel identity does not match managed funnel")
        error = _event_error(event, previous_revision, previous_hash, funnel_id)
        if error is not None:
            raise InvalidEventHistoryError(f"events.jsonl line {line_number}: {error}")
        events.append(event)
        previous_hash = event["event_hash"]
        previous_revision = event["revision"]
    return events


def _create_event(
    snapshot: Mapping[str, Any],
    previous_revision: int,
    previous_event_hash: str | None,
    funnel_id: str,
) -> dict[str, Any]:
    snapshot_copy = deepcopy(dict(snapshot))
    event: dict[str, Any] = {
        "event_version": EVENT_VERSION,
        "kind": "state_committed",
        "funnel_id": funnel_id,
        "recorded_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "revision": snapshot_copy["revision"],
        "previous_revision": previous_revision,
        "previous_event_hash": previous_event_hash,
        "snapshot_hash": snapshot_digest(snapshot_copy),
        "snapshot": snapshot_copy,
    }
    event["event_hash"] = _sha256(_canonical_json_bytes(event))
    return event


def _event_error(
    event: Any,
    expected_previous_revision: int,
    expected_previous_hash: str | None,
    funnel_id: str,
) -> str | None:
    if not isinstance(event, Mapping) or set(event) != _EVENT_FIELDS:
        return "event fields do not match studio-event/v1"
    if event["event_version"] != EVENT_VERSION or event["kind"] != "state_committed":
        return "unsupported event contract"
    if event["funnel_id"] != funnel_id:
        return "event belongs to another funnel"
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
    if event["snapshot_hash"] != snapshot_digest(event["snapshot"]):
        return "snapshot hash does not match"
    unsigned = {key: value for key, value in event.items() if key != "event_hash"}
    if event["event_hash"] != _sha256(_canonical_json_bytes(unsigned)):
        return "event hash does not match"
    recorded_at = event["recorded_at"]
    if not isinstance(recorded_at, str) or not recorded_at.endswith(("Z", "+00:00")):
        return "recorded_at must be an RFC3339 string"
    try:
        datetime.fromisoformat(recorded_at.replace("Z", "+00:00"))
    except ValueError:
        return "recorded_at must be an RFC3339 string"
    return None


def _read_funnel_marker(directory: int) -> dict[str, str]:
    try:
        descriptor = _open_regular_at(directory, FUNNEL_MARKER, os.O_RDONLY)
    except FileNotFoundError as exc:
        raise UnsafeFilesystemEntryError("managed funnel marker is missing") from exc
    try:
        raw = _read_all(descriptor)
    finally:
        os.close(descriptor)
    try:
        marker = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise StateIntegrityError("funnel identity marker is invalid JSON") from exc
    if not isinstance(marker, dict) or set(marker) != _FUNNEL_MARKER_FIELDS:
        raise StateIntegrityError("funnel identity marker has invalid fields")
    if marker["kind"] != "funnel-studio-funnel":
        raise StateIntegrityError("funnel identity marker has invalid kind")
    if not isinstance(marker["funnel_id"], str) or _HEX_32.fullmatch(marker["funnel_id"]) is None:
        raise StateIntegrityError("funnel identity marker has invalid funnel_id")
    try:
        _require_digest(marker["genesis_snapshot_hash"], "genesis_snapshot_hash")
    except ValueError as exc:
        raise StateIntegrityError("funnel identity marker has invalid genesis hash") from exc
    return marker


def _validate_funnel_location(path: Path, marker: Mapping[str, str]) -> None:
    if path.name != marker["funnel_slug"] or path.parent.name != "funnels":
        raise StateIntegrityError("funnel identity does not match its path")
    if path.parent.parent.name != marker["project_slug"]:
        raise StateIntegrityError("funnel identity does not match its project path")


def _stable_lock_path(funnel_root: Path, funnel_id: str) -> Path:
    user = str(os.getuid()) if hasattr(os, "getuid") else "native"
    root = Path(tempfile.gettempdir()) / f"quiz-funnel-studio-locks-{user}"
    try:
        root.mkdir(mode=0o700, exist_ok=True)
        metadata = os.lstat(root)
    except OSError as exc:
        raise UnsafeFilesystemEntryError(f"unsafe lock root: {root}") from exc
    if not stat.S_ISDIR(metadata.st_mode) or root.is_symlink():
        raise UnsafeFilesystemEntryError(f"unsafe lock root: {root}")
    if hasattr(os, "getuid"):
        if metadata.st_uid != os.getuid() or stat.S_IMODE(metadata.st_mode) & 0o077:
            raise UnsafeFilesystemEntryError(f"lock root is not private to this user: {root}")
    key = _sha256(funnel_id.encode("ascii"))
    return root / f"{key}.lock"


def _capture_ancestor_identities(path: Path) -> tuple[tuple[Path, tuple[int, int]], ...]:
    ancestors = (path.parent.parent.parent, path.parent.parent, path.parent, path)
    captured = []
    for ancestor in ancestors:
        try:
            metadata = os.lstat(ancestor)
        except OSError as exc:
            raise UnsafeFilesystemEntryError(f"managed ancestor is missing: {ancestor}") from exc
        if not stat.S_ISDIR(metadata.st_mode):
            raise UnsafeFilesystemEntryError(f"managed ancestor is not a real directory: {ancestor}")
        captured.append((ancestor, _file_identity(metadata)))
    return tuple(captured)


@contextmanager
def _open_directory_path(path: str | Path) -> Iterator[int]:
    try:
        descriptor = os.open(path, os.O_RDONLY | _DIRECTORY | _NOFOLLOW)
    except OSError as exc:
        raise UnsafeFilesystemEntryError(f"unsafe managed directory: {path}") from exc
    try:
        if not stat.S_ISDIR(os.fstat(descriptor).st_mode):
            raise UnsafeFilesystemEntryError(f"managed path is not a directory: {path}")
        yield descriptor
    finally:
        os.close(descriptor)


@contextmanager
def _open_directory_at(parent: int, name: str) -> Iterator[int]:
    try:
        descriptor = os.open(name, os.O_RDONLY | _DIRECTORY | _NOFOLLOW, dir_fd=parent)
    except OSError as exc:
        raise UnsafeFilesystemEntryError(f"unsafe managed directory: {name}") from exc
    try:
        if not stat.S_ISDIR(os.fstat(descriptor).st_mode):
            raise UnsafeFilesystemEntryError(f"managed entry is not a directory: {name}")
        yield descriptor
    finally:
        os.close(descriptor)


def _require_directory_identity(descriptor: int, expected: tuple[int, int]) -> None:
    if _file_identity(os.fstat(descriptor)) != expected:
        raise UnsafeFilesystemEntryError("managed directory identity changed")


def _open_regular_at(directory: int, name: str, flags: int) -> int:
    try:
        descriptor = os.open(name, flags | _NOFOLLOW, 0o600, dir_fd=directory)
    except FileNotFoundError:
        raise
    except OSError as exc:
        raise UnsafeFilesystemEntryError(f"unsafe managed leaf: {name}") from exc
    if not stat.S_ISREG(os.fstat(descriptor).st_mode):
        os.close(descriptor)
        raise UnsafeFilesystemEntryError(f"managed leaf is not regular: {name}")
    return descriptor


def _open_new_regular_at(directory: int, name: str) -> int:
    return _open_regular_at(directory, name, os.O_CREAT | os.O_EXCL | os.O_WRONLY)


def _ensure_regular_at(directory: int, name: str, *, allow_missing: bool) -> None:
    try:
        descriptor = _open_regular_at(directory, name, os.O_RDONLY)
    except FileNotFoundError:
        if allow_missing:
            return
        raise
    else:
        os.close(descriptor)


def _read_all(descriptor: int) -> bytes:
    os.lseek(descriptor, 0, os.SEEK_SET)
    chunks = []
    while True:
        chunk = os.read(descriptor, 64 * 1024)
        if not chunk:
            return b"".join(chunks)
        chunks.append(chunk)


def _write_all(descriptor: int, raw: bytes) -> None:
    view = memoryview(raw)
    while view:
        written = os.write(descriptor, view)
        if written <= 0:
            raise OSError("short filesystem write")
        view = view[written:]


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


def _json_bytes(payload: Mapping[str, Any]) -> bytes:
    return (json.dumps(payload, ensure_ascii=False, indent=2) + "\n").encode("utf-8")


def _sha256(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _file_identity(metadata: os.stat_result) -> tuple[int, int]:
    return metadata.st_dev, metadata.st_ino


def _fsync_descriptor(descriptor: int) -> None:
    try:
        os.fsync(descriptor)
    except OSError:
        pass
