"""Helpers for seeding built-in workflow instances."""

from __future__ import annotations

import json
from pathlib import Path

from disco.core.llm import ConfigStore
from disco.core.workflow import (
    BROWSER_AUTOMATION_DEFINITION,
    DAILY_EMAIL_BRIEF_DEFINITION,
    FORM_FILL_DEFINITION,
    GENERAL_WORKSPACE_TASK_DEFINITION,
    SKILL_AUTHORING_DEFINITION,
    WorkflowApproval,
    WorkflowDefinition,
    WorkflowInstance,
)

from .projects import resolve_projects_root

GENERAL_WORKSPACE_TASK_INSTANCE_ID = "general_workspace_task"
DAILY_EMAIL_BRIEF_INSTANCE_ID = "daily_email_brief"
BROWSER_AUTOMATION_INSTANCE_ID = "browser_automation"
FORM_FILL_INSTANCE_ID = "form_fill"
SKILL_AUTHORING_INSTANCE_ID = "skill_authoring"


def _approved_enabled_instance(definition: WorkflowDefinition) -> WorkflowInstance:
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


def general_workspace_task_instance() -> WorkflowInstance:
    return _approved_enabled_instance(GENERAL_WORKSPACE_TASK_DEFINITION)


def daily_email_brief_instance() -> WorkflowInstance:
    definition = DAILY_EMAIL_BRIEF_DEFINITION
    digest = definition.digest()
    return WorkflowInstance(
        definition_digest=digest,
        definition=definition,
        params={},
        connector_bindings={},
        enabled=False,
        approval=None,
    )


def browser_automation_instance() -> WorkflowInstance:
    return _approved_enabled_instance(BROWSER_AUTOMATION_DEFINITION)


def form_fill_instance() -> WorkflowInstance:
    return _approved_enabled_instance(FORM_FILL_DEFINITION)


def skill_authoring_instance() -> WorkflowInstance:
    return _approved_enabled_instance(SKILL_AUTHORING_DEFINITION)


def _workflows_dir(projects_root: str | Path | None = None) -> Path:
    configured_root = (
        str(projects_root)
        if projects_root is not None
        else ConfigStore().load().projects.projects_root
    )
    root = Path(resolve_projects_root(configured_root)).expanduser()
    workflows_dir = root / "workflows"
    workflows_dir.mkdir(parents=True, exist_ok=True)
    return workflows_dir


def _seed_instance(
    instance_id: str,
    instance: WorkflowInstance,
    projects_root: str | Path | None = None,
) -> Path:
    path = _workflows_dir(projects_root) / f"{instance_id}.json"
    payload = instance.model_dump(mode="json")
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return path


def seed_general_workspace_task(projects_root: str | Path | None = None) -> Path:
    return _seed_instance(
        GENERAL_WORKSPACE_TASK_INSTANCE_ID,
        general_workspace_task_instance(),
        projects_root,
    )


def seed_daily_email_brief(projects_root: str | Path | None = None) -> Path:
    return _seed_instance(
        DAILY_EMAIL_BRIEF_INSTANCE_ID,
        daily_email_brief_instance(),
        projects_root,
    )


def seed_browser_automation(projects_root: str | Path | None = None) -> Path:
    return _seed_instance(
        BROWSER_AUTOMATION_INSTANCE_ID,
        browser_automation_instance(),
        projects_root,
    )


def seed_form_fill(projects_root: str | Path | None = None) -> Path:
    return _seed_instance(
        FORM_FILL_INSTANCE_ID,
        form_fill_instance(),
        projects_root,
    )


def seed_skill_authoring(projects_root: str | Path | None = None) -> Path:
    return _seed_instance(
        SKILL_AUTHORING_INSTANCE_ID,
        skill_authoring_instance(),
        projects_root,
    )


def seed_builtin_workflows(
    projects_root: str | Path | None = None,
) -> tuple[tuple[str, Path], ...]:
    return (
        (
            GENERAL_WORKSPACE_TASK_INSTANCE_ID,
            seed_general_workspace_task(projects_root),
        ),
        (DAILY_EMAIL_BRIEF_INSTANCE_ID, seed_daily_email_brief(projects_root)),
        (BROWSER_AUTOMATION_INSTANCE_ID, seed_browser_automation(projects_root)),
        (FORM_FILL_INSTANCE_ID, seed_form_fill(projects_root)),
        (SKILL_AUTHORING_INSTANCE_ID, seed_skill_authoring(projects_root)),
    )


__all__ = [
    "BROWSER_AUTOMATION_INSTANCE_ID",
    "DAILY_EMAIL_BRIEF_INSTANCE_ID",
    "FORM_FILL_INSTANCE_ID",
    "GENERAL_WORKSPACE_TASK_INSTANCE_ID",
    "SKILL_AUTHORING_INSTANCE_ID",
    "browser_automation_instance",
    "daily_email_brief_instance",
    "form_fill_instance",
    "general_workspace_task_instance",
    "seed_browser_automation",
    "seed_builtin_workflows",
    "seed_daily_email_brief",
    "seed_form_fill",
    "seed_general_workspace_task",
    "seed_skill_authoring",
    "skill_authoring_instance",
]
