"""Event-log transitions driven by workflow router tools."""

from __future__ import annotations

import uuid
from collections.abc import Mapping
from typing import Any

from disco.core.loop.workflow_state import (
    abort_workflow_to_router,
    seed_approved_workflow_plan,
)
from disco.core.workflow import WorkflowDefinition, WorkflowRun


async def handle_workflow_tool_event(
    *,
    conversation_id: str,
    loops: Mapping[str, Any],
    kind: str,
    payload: dict[str, Any],
) -> None:
    loop = loops.get(conversation_id)
    if loop is None:
        return
    if kind == "enter_workflow":
        raw_definition = payload.get("definition")
        definition = (
            raw_definition
            if isinstance(raw_definition, WorkflowDefinition)
            else WorkflowDefinition.model_validate(raw_definition)
        )
        raw_params = payload.get("params")
        params = raw_params if isinstance(raw_params, dict) else {}
        workflow_run = WorkflowRun(
            run_id=f"wfrun_{uuid.uuid4().hex}",
            definition=definition,
            params=params,
        )
        await seed_approved_workflow_plan(loop, workflow_run)
    elif kind == "workflow_abort":
        await abort_workflow_to_router(loop)
