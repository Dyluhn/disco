"""Helpers for seeding built-in workflow instances."""

from __future__ import annotations

import json
from pathlib import Path

from disco.core.llm import ConfigStore
from disco.core.workflow import (
    GENERAL_WORKSPACE_TASK_DEFINITION,
    WorkflowApproval,
    WorkflowInstance,
)

from .projects import resolve_projects_root

GENERAL_WORKSPACE_TASK_INSTANCE_ID = "general_workspace_task"


def general_workspace_task_instance() -> WorkflowInstance:
    definition = GENERAL_WORKSPACE_TASK_DEFINITION
    digest = definition.digest()
    return WorkflowInstance(
        definition_digest=digest,
        definition=definition,
        params={},
        connector_bindings={},
        enabled=True,
        approval=WorkflowApproval(
            approved_at="2026-07-04T00:00:00Z",
            approved_by="disco_builtin_seed",
            surface_shown_digest=digest,
        ),
    )


def seed_general_workspace_task(projects_root: str | Path | None = None) -> Path:
    configured_root = (
        str(projects_root)
        if projects_root is not None
        else ConfigStore().load().projects.projects_root
    )
    root = Path(resolve_projects_root(configured_root)).expanduser()
    workflows_dir = root / "workflows"
    workflows_dir.mkdir(parents=True, exist_ok=True)
    path = workflows_dir / f"{GENERAL_WORKSPACE_TASK_INSTANCE_ID}.json"
    payload = general_workspace_task_instance().model_dump(mode="json")
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return path


__all__ = [
    "GENERAL_WORKSPACE_TASK_INSTANCE_ID",
    "general_workspace_task_instance",
    "seed_general_workspace_task",
]
