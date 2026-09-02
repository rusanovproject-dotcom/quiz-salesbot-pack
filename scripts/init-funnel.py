#!/usr/bin/env python3
"""Create one contract-complete local Funnel Studio instance."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys


REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from studio.errors import StudioStorageError  # noqa: E402
from studio.project_layout import initialize_project  # noqa: E402


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Безопасно создать локальный экземпляр Quiz Funnel Studio."
    )
    parser.add_argument("--project", required=True, help="project slug: lowercase ASCII")
    parser.add_argument("--funnel", required=True, help="funnel slug: lowercase ASCII")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    try:
        paths = initialize_project(
            projects_root=REPO_ROOT / "projects",
            templates_root=REPO_ROOT / "templates",
            project_slug=args.project,
            funnel_slug=args.funnel,
        )
    except StudioStorageError as error:
        print(
            json.dumps(
                {
                    "status": "error",
                    "error": type(error).__name__,
                    "message": str(error),
                },
                ensure_ascii=False,
            ),
            file=sys.stderr,
        )
        return 2
    print(
        json.dumps(
            {"status": "ready", "funnel_root": str(paths.funnel_root)},
            ensure_ascii=False,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
