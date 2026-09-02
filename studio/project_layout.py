"""Safe, idempotent filesystem layout for independent funnel projects."""
from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
import re
from typing import Any

from studio.errors import InvalidSlugError, PathCollisionError, PathEscapeError
from studio.state import create_snapshot, write_snapshot_atomic


_SLUG = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$")
_PROJECT_MARKER = ".funnel-studio-project.json"
_FUNNEL_MARKER = ".funnel-studio-funnel.json"


@dataclass(frozen=True)
class ProjectPaths:
    projects_root: Path
    project_root: Path
    funnel_root: Path
    brief_path: Path
    brief_metadata_path: Path
    state_path: Path
    events_path: Path


def validate_slug(value: str, *, kind: str = "slug") -> str:
    """Accept only a portable, lowercase ASCII filename component."""
    if not isinstance(value, str) or _SLUG.fullmatch(value) is None:
        raise InvalidSlugError(f"invalid {kind}: expected lowercase letters, digits and internal hyphens")
    return value


def initialize_project(
    *,
    projects_root: str | Path,
    templates_root: str | Path,
    project_slug: str,
    funnel_slug: str,
) -> ProjectPaths:
    """Create one managed funnel without ever rewriting an existing instance."""
    project_slug = validate_slug(project_slug, kind="project slug")
    funnel_slug = validate_slug(funnel_slug, kind="funnel slug")
    root = Path(projects_root)
    templates = Path(templates_root)
    paths = _paths(root, project_slug, funnel_slug)

    _preflight_root(root)
    _assert_inside(root, paths.project_root)
    _assert_inside(root, paths.funnel_root)
    _preflight_managed_tree(paths, project_slug, funnel_slug)
    template_files = _preflight_templates(templates)

    if paths.funnel_root.exists():
        return paths

    root.mkdir(parents=True, exist_ok=True)
    paths.project_root.mkdir(exist_ok=True)
    _write_marker_if_missing(
        paths.project_root / _PROJECT_MARKER,
        {"kind": "funnel-studio-project", "project_slug": project_slug},
    )
    (paths.project_root / "funnels").mkdir(exist_ok=True)
    paths.funnel_root.mkdir()
    _write_marker_if_missing(
        paths.funnel_root / _FUNNEL_MARKER,
        {
            "kind": "funnel-studio-funnel",
            "project_slug": project_slug,
            "funnel_slug": funnel_slug,
        },
    )
    paths.brief_path.write_bytes(template_files["brief"].read_bytes())
    paths.brief_metadata_path.write_bytes(template_files["brief_metadata"].read_bytes())
    initial_state = json.loads(template_files["state"].read_text(encoding="utf-8"))
    write_snapshot_atomic(paths.state_path, create_snapshot(initial_state))
    paths.events_path.write_bytes(template_files["events"].read_bytes())
    return paths


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


def _preflight_root(root: Path) -> None:
    if root.exists() and not root.is_dir():
        raise PathCollisionError(f"projects root is not a directory: {root}")


def _assert_inside(root: Path, candidate: Path) -> None:
    boundary = root.resolve(strict=False)
    resolved = candidate.resolve(strict=False)
    if not resolved.is_relative_to(boundary):
        raise PathEscapeError(f"path escapes projects root: {candidate}")


def _preflight_managed_tree(paths: ProjectPaths, project_slug: str, funnel_slug: str) -> None:
    for expected_directory in (
        paths.project_root,
        paths.project_root / "funnels",
        paths.funnel_root,
    ):
        if expected_directory.is_symlink():
            _assert_inside(paths.projects_root, expected_directory)
            raise PathCollisionError(f"managed directories cannot be symlinks: {expected_directory}")
        if expected_directory.exists() and not expected_directory.is_dir():
            raise PathCollisionError(f"expected directory collides with file: {expected_directory}")

    project_marker = paths.project_root / _PROJECT_MARKER
    if paths.project_root.exists():
        _require_marker(
            project_marker,
            {"kind": "funnel-studio-project", "project_slug": project_slug},
        )

    funnel_marker = paths.funnel_root / _FUNNEL_MARKER
    if paths.funnel_root.exists():
        _require_marker(
            funnel_marker,
            {
                "kind": "funnel-studio-funnel",
                "project_slug": project_slug,
                "funnel_slug": funnel_slug,
            },
        )


def _require_marker(path: Path, expected: dict[str, str]) -> None:
    if path.is_symlink() or not path.is_file():
        raise PathCollisionError(f"existing directory is not managed by Funnel Studio: {path.parent}")
    try:
        actual: Any = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise PathCollisionError(f"invalid ownership marker: {path}") from exc
    if actual != expected:
        raise PathCollisionError(f"ownership marker does not match requested project: {path}")


def _preflight_templates(root: Path) -> dict[str, Path]:
    files = {
        "brief": root / "FUNNEL-BRIEF.md",
        "brief_metadata": root / "FUNNEL-BRIEF.meta.json",
        "state": root / "STATE.json",
        "events": root / "events.jsonl",
    }
    for path in files.values():
        if path.is_symlink() or not path.is_file():
            raise PathCollisionError(f"required template is not a regular file: {path}")
    return files


def _write_marker_if_missing(path: Path, payload: dict[str, str]) -> None:
    if not path.exists():
        path.write_bytes(_json_bytes(payload))


def _json_bytes(payload: Any) -> bytes:
    return (json.dumps(payload, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
