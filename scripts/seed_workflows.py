#!/usr/bin/env python3
"""Seed built-in workflow instances into the router workflow store."""

from __future__ import annotations

import argparse

from disco.tools.workflow_seed import (
    seed_builtin_workflows,
)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Seed the built-in workflow instances."
    )
    parser.add_argument(
        "--projects-root",
        help=(
            "Project store root to seed. Defaults to the saved ConfigStore "
            "projects.projects_root, resolved the same way ProjectStore does."
        ),
    )
    return parser


def main() -> int:
    args = _parser().parse_args()
    for instance_id, path in seed_builtin_workflows(args.projects_root):
        print(f"[seed] wrote workflow {instance_id} -> {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
