#!/usr/bin/env python3
"""Seed built-in workflow instances into the router workflow store."""

from __future__ import annotations

import argparse

from disco.tools.workflow_seed import (
    DAILY_EMAIL_BRIEF_INSTANCE_ID,
    GENERAL_WORKSPACE_TASK_INSTANCE_ID,
    seed_daily_email_brief,
    seed_general_workspace_task,
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
    seeded = (
        (GENERAL_WORKSPACE_TASK_INSTANCE_ID, seed_general_workspace_task(args.projects_root)),
        (DAILY_EMAIL_BRIEF_INSTANCE_ID, seed_daily_email_brief(args.projects_root)),
    )
    for instance_id, path in seeded:
        print(f"[seed] wrote workflow {instance_id} -> {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
