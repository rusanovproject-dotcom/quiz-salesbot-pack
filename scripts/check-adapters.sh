#!/usr/bin/env bash
set -euo pipefail

PACK_ROOT="${QUIZ_FUNNEL_PACK_ROOT:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
MODE="${1:-check}"

if [[ "$MODE" != "check" && "$MODE" != "--write" ]]; then
  echo "usage: scripts/check-adapters.sh [--write]" >&2
  exit 2
fi

python3 - "$PACK_ROOT" "$MODE" <<'PY'
from __future__ import annotations

from pathlib import Path
import stat
import sys


root = Path(sys.argv[1]).resolve()
mode = sys.argv[2]
canonical_root = root / "skills"
projection_root = root / ".claude/skills"
errors: list[str] = []


def frontmatter(path: Path) -> dict[str, str]:
    lines = path.read_text(encoding="utf-8").splitlines()
    if not lines or lines[0] != "---":
        raise ValueError(f"missing frontmatter: {path}")
    end = lines.index("---", 1)
    fields: dict[str, str] = {}
    for line in lines[1:end]:
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        key, separator, value = line.partition(":")
        if not separator or not key.strip() or not value.strip():
            raise ValueError(f"invalid frontmatter scalar: {path}: {line}")
        fields[key.strip()] = value.strip().strip('"')
    return fields


def csv(value: str) -> tuple[str, ...]:
    return tuple(part.strip() for part in value.split(",") if part.strip())


def projected_bytes(source: Path, relative: Path) -> bytes:
    raw = source.read_bytes()
    if source.name != "SKILL.md":
        return raw
    marker = (
        f"<!-- GENERATED from skills/{relative.as_posix()} by "
        "scripts/check-adapters.sh --write. DO NOT EDIT. -->\n"
    ).encode("utf-8")
    first_end = raw.find(b"\n---\n", 4)
    if not raw.startswith(b"---\n") or first_end < 0:
        raise ValueError(f"invalid canonical frontmatter: {source}")
    insertion = first_end + len(b"\n---\n")
    return raw[:insertion] + marker + raw[insertion:]


if not canonical_root.is_dir():
    errors.append(f"missing canonical skills directory: {canonical_root}")
else:
    sources = sorted(path for path in canonical_root.rglob("*") if path.is_file())
    expected_relatives = {path.relative_to(canonical_root) for path in sources}
    for source in sources:
        relative = source.relative_to(canonical_root)
        target = projection_root / relative
        expected = projected_bytes(source, relative)
        expected_mode = stat.S_IMODE(source.stat().st_mode)
        if mode == "--write":
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(expected)
            target.chmod(expected_mode)
            continue
        if not target.is_file() or target.read_bytes() != expected:
            errors.append(f"stale projection: {target.relative_to(root)}")
            continue
        if stat.S_IMODE(target.stat().st_mode) != expected_mode:
            errors.append(f"stale projection mode: {target.relative_to(root)}")

    if projection_root.is_dir():
        actual_relatives = {
            path.relative_to(projection_root)
            for path in projection_root.rglob("*")
            if path.is_file()
        }
        for relative in sorted(actual_relatives - expected_relatives):
            errors.append(f"hidden projection source: {(projection_root / relative).relative_to(root)}")

try:
    skill_names = {
        frontmatter(path)["name"]
        for path in canonical_root.glob("*/SKILL.md")
    }
    adapters = [
        frontmatter(root / f"adapters/{environment}/quiz-funnel/SKILL.md")
        for environment in ("claude", "codex")
    ]
    parity_fields = (
        "canonical-entrypoint",
        "skill-refs",
        "public-phases",
        "contracts",
        "artifact-root",
        "artifacts",
        "stop-conditions",
        "trigger-map",
    )
    if any(adapters[0].get(field) != adapters[1].get(field) for field in parity_fields):
        errors.append("adapter contract drift")
    for adapter in adapters:
        missing = set(csv(adapter["skill-refs"])) - skill_names
        if missing:
            errors.append(f"adapter references missing canonical skills: {sorted(missing)}")
    preflight = frontmatter(canonical_root / "preflight/SKILL.md")
    missing = set(csv(preflight["skill-refs"])) - skill_names
    if missing:
        errors.append(f"graph references missing canonical skills: {sorted(missing)}")
except (KeyError, OSError, ValueError) as error:
    errors.append(str(error))

if errors:
    print("\n".join(errors), file=sys.stderr)
    raise SystemExit(1)
if mode == "--write":
    print("Claude compatibility projections regenerated")
else:
    print("Adapters and generated projections are in sync")
PY
