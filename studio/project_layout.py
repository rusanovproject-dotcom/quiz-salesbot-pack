"""Safe staged publication of independent Funnel Studio project instances."""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
import json
import os
from pathlib import Path
import re
import stat
from typing import Any, Callable, Iterator, Mapping
import uuid

from studio.contracts import validate_brief, validate_state
from studio.errors import (
    IncompleteProjectError,
    InvalidProjectTemplateError,
    InvalidSlugError,
    PathCollisionError,
    PathEscapeError,
    UnsafeFilesystemEntryError,
)
from studio.state import create_snapshot, snapshot_digest


_SLUG = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$")
_HEX_32 = re.compile(r"^[0-9a-f]{32}$")
_HEX_64 = re.compile(r"^[0-9a-f]{64}$")
_PROJECT_MARKER = ".funnel-studio-project.json"
_FUNNEL_MARKER = ".funnel-studio-funnel.json"
_NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)
_DIRECTORY = getattr(os, "O_DIRECTORY", 0)
_PROJECT_MARKER_FIELDS = {"kind", "project_slug", "project_id"}
_FUNNEL_MARKER_FIELDS = {
    "kind",
    "project_slug",
    "funnel_slug",
    "funnel_id",
    "genesis_snapshot_hash",
}
_REQUIRED_FUNNEL_LEAVES = (
    "FUNNEL-BRIEF.md",
    "FUNNEL-BRIEF.meta.json",
    "STATE.json",
    "events.jsonl",
    _FUNNEL_MARKER,
)


@dataclass(frozen=True)
class ProjectPaths:
    projects_root: Path
    project_root: Path
    funnel_root: Path
    brief_path: Path
    brief_metadata_path: Path
    state_path: Path
    events_path: Path


@dataclass(frozen=True)
class _Templates:
    brief: bytes
    brief_metadata: bytes
    initial_state: Mapping[str, Any]
    events: bytes


def validate_slug(value: str, *, kind: str = "slug") -> str:
    """Accept only a portable lowercase ASCII filename component."""
    if not isinstance(value, str) or _SLUG.fullmatch(value) is None:
        raise InvalidSlugError(
            f"invalid {kind}: expected lowercase letters, digits and internal hyphens"
        )
    return value


def initialize_project(
    *,
    projects_root: str | Path,
    templates_root: str | Path,
    project_slug: str,
    funnel_slug: str,
    fault_hook: Callable[[str], None] | None = None,
) -> ProjectPaths:
    """Publish a complete funnel atomically or preserve the existing instance."""
    project_slug = validate_slug(project_slug, kind="project slug")
    funnel_slug = validate_slug(funnel_slug, kind="funnel slug")
    templates = _load_templates(Path(templates_root))
    root = Path(projects_root).absolute()
    paths = _paths(root, project_slug, funnel_slug)
    hook = fault_hook or (lambda _stage: None)

    _create_or_open_root(root)
    root_identity = _path_identity(root)
    with _open_directory_path(root) as root_fd:
        project_metadata = _lstat_at(root_fd, project_slug)
        if project_metadata is None:
            return _create_new_project(
                root_fd,
                root,
                root_identity,
                paths,
                project_slug,
                funnel_slug,
                templates,
                hook,
            )
        if stat.S_ISLNK(project_metadata.st_mode):
            raise PathEscapeError(f"project path must not be a symlink: {paths.project_root}")
        if not stat.S_ISDIR(project_metadata.st_mode):
            raise PathCollisionError(f"project path is not a real directory: {paths.project_root}")

        with _open_directory_at(root_fd, project_slug) as project_fd:
            project_identity = _file_identity(os.fstat(project_fd))
            _validate_project_marker(project_fd, project_slug)
            with _open_directory_at(project_fd, "funnels") as funnels_fd:
                hook("after_preflight")
                funnel_metadata = _lstat_at(funnels_fd, funnel_slug)
                if funnel_metadata is not None:
                    if not stat.S_ISDIR(funnel_metadata.st_mode):
                        raise PathCollisionError(
                            f"funnel path is not a real directory: {paths.funnel_root}"
                        )
                    with _open_directory_at(funnels_fd, funnel_slug) as funnel_fd:
                        _validate_complete_funnel(
                            funnel_fd,
                            project_slug=project_slug,
                            funnel_slug=funnel_slug,
                        )
                    return paths
                return _create_funnel_in_existing_project(
                    funnels_fd,
                    root,
                    root_identity,
                    paths.project_root,
                    project_identity,
                    paths,
                    project_slug,
                    funnel_slug,
                    templates,
                    hook,
                )


def _create_new_project(
    root_fd: int,
    root: Path,
    root_identity: tuple[int, int],
    paths: ProjectPaths,
    project_slug: str,
    funnel_slug: str,
    templates: _Templates,
    hook: Callable[[str], None],
) -> ProjectPaths:
    hook("after_preflight")
    stage = f".{project_slug}.project-staging-{uuid.uuid4().hex}"
    os.mkdir(stage, mode=0o700, dir_fd=root_fd)
    try:
        with _open_directory_at(root_fd, stage) as project_fd:
            _write_new_file(
                project_fd,
                _PROJECT_MARKER,
                _json_bytes(
                    {
                        "kind": "funnel-studio-project",
                        "project_slug": project_slug,
                        "project_id": uuid.uuid4().hex,
                    }
                ),
            )
            os.mkdir("funnels", mode=0o700, dir_fd=project_fd)
            with _open_directory_at(project_fd, "funnels") as funnels_fd:
                os.mkdir(funnel_slug, mode=0o700, dir_fd=funnels_fd)
                with _open_directory_at(funnels_fd, funnel_slug) as funnel_fd:
                    hook("after_staging_directory")
                    _build_funnel(
                        funnel_fd,
                        project_slug,
                        funnel_slug,
                        templates,
                        hook,
                    )
                os.fsync(funnels_fd)
            os.fsync(project_fd)
        hook("before_publish")
        _assert_path_identity(root, root_identity)
        os.rename(stage, project_slug, src_dir_fd=root_fd, dst_dir_fd=root_fd)
        os.fsync(root_fd)
        hook("after_publish")
        return paths
    except BaseException:
        _cleanup_project_stage(root_fd, stage, funnel_slug)
        raise


def _create_funnel_in_existing_project(
    funnels_fd: int,
    root: Path,
    root_identity: tuple[int, int],
    project_root: Path,
    project_identity: tuple[int, int],
    paths: ProjectPaths,
    project_slug: str,
    funnel_slug: str,
    templates: _Templates,
    hook: Callable[[str], None],
) -> ProjectPaths:
    stage = f".{funnel_slug}.staging-{uuid.uuid4().hex}"
    os.mkdir(stage, mode=0o700, dir_fd=funnels_fd)
    try:
        with _open_directory_at(funnels_fd, stage) as funnel_fd:
            hook("after_staging_directory")
            _build_funnel(funnel_fd, project_slug, funnel_slug, templates, hook)
        hook("before_publish")
        _assert_path_identity(root, root_identity)
        _assert_path_identity(project_root, project_identity)
        os.rename(stage, funnel_slug, src_dir_fd=funnels_fd, dst_dir_fd=funnels_fd)
        os.fsync(funnels_fd)
        hook("after_publish")
        return paths
    except BaseException:
        _cleanup_funnel_stage(funnels_fd, stage)
        raise


def _build_funnel(
    funnel_fd: int,
    project_slug: str,
    funnel_slug: str,
    templates: _Templates,
    hook: Callable[[str], None],
) -> None:
    funnel_id = uuid.uuid4().hex
    snapshot = create_snapshot(templates.initial_state)
    marker = {
        "kind": "funnel-studio-funnel",
        "project_slug": project_slug,
        "funnel_slug": funnel_slug,
        "funnel_id": funnel_id,
        "genesis_snapshot_hash": snapshot_digest(snapshot),
    }
    _write_new_file(funnel_fd, "FUNNEL-BRIEF.md", templates.brief)
    hook("after_brief_write")
    _write_new_file(funnel_fd, "FUNNEL-BRIEF.meta.json", templates.brief_metadata)
    hook("after_brief_metadata_write")
    _write_new_file(funnel_fd, "STATE.json", _json_bytes(snapshot))
    hook("after_state_write")
    _write_new_file(funnel_fd, "events.jsonl", templates.events)
    hook("after_events_write")
    _write_new_file(funnel_fd, _FUNNEL_MARKER, _json_bytes(marker))
    hook("after_completion_marker")
    os.fsync(funnel_fd)


def _load_templates(root: Path) -> _Templates:
    if root.is_symlink() or not root.is_dir():
        raise InvalidProjectTemplateError(f"templates root is not a real directory: {root}")
    try:
        brief = _read_regular_path(root / "FUNNEL-BRIEF.md")
        brief_metadata = _read_regular_path(root / "FUNNEL-BRIEF.meta.json")
        state_raw = _read_regular_path(root / "STATE.json")
        events = _read_regular_path(root / "events.jsonl")
    except (OSError, UnsafeFilesystemEntryError) as exc:
        raise InvalidProjectTemplateError(f"required template is invalid: {exc}") from exc
    try:
        metadata_payload = json.loads(brief_metadata.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise InvalidProjectTemplateError("FUNNEL-BRIEF.meta.json is invalid JSON") from exc
    brief_result = validate_brief(metadata_payload)
    if brief_result.errors:
        raise InvalidProjectTemplateError("FUNNEL-BRIEF.meta.json violates funnel-brief/v1")
    try:
        state_payload = json.loads(state_raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise InvalidProjectTemplateError("STATE.json is invalid JSON") from exc
    if validate_state(state_payload).errors:
        raise InvalidProjectTemplateError("STATE.json violates state/v1")
    if events != b"":
        raise InvalidProjectTemplateError("events.jsonl template must be empty")
    return _Templates(brief, brief_metadata, state_payload, events)


def _validate_complete_funnel(
    funnel_fd: int,
    *,
    project_slug: str,
    funnel_slug: str,
) -> None:
    for name in _REQUIRED_FUNNEL_LEAVES:
        metadata = _lstat_at(funnel_fd, name)
        if metadata is None:
            raise IncompleteProjectError(f"managed funnel is incomplete: missing {name}")
        if not stat.S_ISREG(metadata.st_mode):
            raise UnsafeFilesystemEntryError(f"managed funnel leaf is not regular: {name}")
    marker = _read_json_at(funnel_fd, _FUNNEL_MARKER)
    if not isinstance(marker, dict) or set(marker) != _FUNNEL_MARKER_FIELDS:
        raise IncompleteProjectError("managed funnel completion marker is invalid")
    if (
        marker["kind"] != "funnel-studio-funnel"
        or marker["project_slug"] != project_slug
        or marker["funnel_slug"] != funnel_slug
    ):
        raise PathCollisionError("funnel ownership marker does not match requested path")
    if (
        not isinstance(marker["funnel_id"], str)
        or _HEX_32.fullmatch(marker["funnel_id"]) is None
        or not isinstance(marker["genesis_snapshot_hash"], str)
        or _HEX_64.fullmatch(marker["genesis_snapshot_hash"]) is None
    ):
        raise IncompleteProjectError("managed funnel completion identity is invalid")


def _validate_project_marker(project_fd: int, project_slug: str) -> None:
    marker = _read_json_at(project_fd, _PROJECT_MARKER)
    if not isinstance(marker, dict) or set(marker) != _PROJECT_MARKER_FIELDS:
        raise PathCollisionError("existing project is not a completed managed project")
    if marker["kind"] != "funnel-studio-project" or marker["project_slug"] != project_slug:
        raise PathCollisionError("project ownership marker does not match requested project")
    if (
        not isinstance(marker["project_id"], str)
        or _HEX_32.fullmatch(marker["project_id"]) is None
    ):
        raise PathCollisionError("project ownership marker has invalid identity")


def _paths(root: Path, project_slug: str, funnel_slug: str) -> ProjectPaths:
    project = root / project_slug
    funnel = project / "funnels" / funnel_slug
    return ProjectPaths(
        projects_root=root,
        project_root=project,
        funnel_root=funnel,
        brief_path=funnel / "FUNNEL-BRIEF.md",
        brief_metadata_path=funnel / "FUNNEL-BRIEF.meta.json",
        state_path=funnel / "STATE.json",
        events_path=funnel / "events.jsonl",
    )


def _create_or_open_root(root: Path) -> None:
    if root.exists() and (root.is_symlink() or not root.is_dir()):
        raise PathCollisionError(f"projects root is not a real directory: {root}")
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    if root.is_symlink():
        raise UnsafeFilesystemEntryError(f"projects root cannot be a symlink: {root}")


@contextmanager
def _open_directory_path(path: Path) -> Iterator[int]:
    try:
        descriptor = os.open(path, os.O_RDONLY | _DIRECTORY | _NOFOLLOW)
    except OSError as exc:
        raise UnsafeFilesystemEntryError(f"unsafe directory: {path}") from exc
    try:
        if not stat.S_ISDIR(os.fstat(descriptor).st_mode):
            raise UnsafeFilesystemEntryError(f"path is not a directory: {path}")
        yield descriptor
    finally:
        os.close(descriptor)


@contextmanager
def _open_directory_at(parent_fd: int, name: str) -> Iterator[int]:
    try:
        descriptor = os.open(name, os.O_RDONLY | _DIRECTORY | _NOFOLLOW, dir_fd=parent_fd)
    except OSError as exc:
        raise UnsafeFilesystemEntryError(f"unsafe managed directory: {name}") from exc
    try:
        if not stat.S_ISDIR(os.fstat(descriptor).st_mode):
            raise UnsafeFilesystemEntryError(f"managed entry is not a directory: {name}")
        yield descriptor
    finally:
        os.close(descriptor)


def _write_new_file(directory: int, name: str, raw: bytes) -> None:
    flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY | _NOFOLLOW
    try:
        descriptor = os.open(name, flags, 0o600, dir_fd=directory)
    except OSError as exc:
        raise UnsafeFilesystemEntryError(f"cannot create managed leaf safely: {name}") from exc
    try:
        view = memoryview(raw)
        while view:
            written = os.write(descriptor, view)
            if written <= 0:
                raise OSError("short filesystem write")
            view = view[written:]
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _read_regular_path(path: Path) -> bytes:
    try:
        descriptor = os.open(path, os.O_RDONLY | _NOFOLLOW)
    except OSError as exc:
        raise UnsafeFilesystemEntryError(f"template is not safely readable: {path}") from exc
    try:
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            raise UnsafeFilesystemEntryError(f"template is not regular: {path}")
        chunks = []
        while True:
            chunk = os.read(descriptor, 64 * 1024)
            if not chunk:
                return b"".join(chunks)
            chunks.append(chunk)
    finally:
        os.close(descriptor)


def _read_json_at(directory: int, name: str) -> Any:
    metadata = _lstat_at(directory, name)
    if metadata is None:
        raise PathCollisionError(f"managed marker is missing: {name}")
    if not stat.S_ISREG(metadata.st_mode):
        raise UnsafeFilesystemEntryError(f"managed marker is not regular: {name}")
    try:
        descriptor = os.open(name, os.O_RDONLY | _NOFOLLOW, dir_fd=directory)
    except OSError as exc:
        raise UnsafeFilesystemEntryError(f"managed marker is unsafe: {name}") from exc
    try:
        chunks = []
        while True:
            chunk = os.read(descriptor, 64 * 1024)
            if not chunk:
                break
            chunks.append(chunk)
    finally:
        os.close(descriptor)
    try:
        return json.loads(b"".join(chunks).decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise PathCollisionError(f"managed marker is invalid JSON: {name}") from exc


def _lstat_at(directory: int, name: str) -> os.stat_result | None:
    try:
        return os.stat(name, dir_fd=directory, follow_symlinks=False)
    except FileNotFoundError:
        return None


def _path_identity(path: Path) -> tuple[int, int]:
    metadata = os.lstat(path)
    if not stat.S_ISDIR(metadata.st_mode):
        raise UnsafeFilesystemEntryError(f"managed path is not a real directory: {path}")
    return _file_identity(metadata)


def _assert_path_identity(path: Path, expected: tuple[int, int]) -> None:
    try:
        actual = _path_identity(path)
    except OSError as exc:
        raise UnsafeFilesystemEntryError(f"managed directory identity changed: {path}") from exc
    if actual != expected:
        raise UnsafeFilesystemEntryError(f"managed directory identity changed: {path}")


def _file_identity(metadata: os.stat_result) -> tuple[int, int]:
    return metadata.st_dev, metadata.st_ino


def _cleanup_funnel_stage(parent_fd: int, stage: str) -> None:
    try:
        with _open_directory_at(parent_fd, stage) as stage_fd:
            for name in _REQUIRED_FUNNEL_LEAVES:
                try:
                    os.unlink(name, dir_fd=stage_fd)
                except FileNotFoundError:
                    pass
        os.rmdir(stage, dir_fd=parent_fd)
    except (FileNotFoundError, OSError, UnsafeFilesystemEntryError):
        pass


def _cleanup_project_stage(root_fd: int, stage: str, funnel_slug: str) -> None:
    try:
        with _open_directory_at(root_fd, stage) as project_fd:
            with _open_directory_at(project_fd, "funnels") as funnels_fd:
                _cleanup_funnel_stage(funnels_fd, funnel_slug)
            try:
                os.rmdir("funnels", dir_fd=project_fd)
            except FileNotFoundError:
                pass
            try:
                os.unlink(_PROJECT_MARKER, dir_fd=project_fd)
            except FileNotFoundError:
                pass
        os.rmdir(stage, dir_fd=root_fd)
    except (FileNotFoundError, OSError, UnsafeFilesystemEntryError):
        pass


def _json_bytes(payload: Mapping[str, Any]) -> bytes:
    return (json.dumps(payload, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
