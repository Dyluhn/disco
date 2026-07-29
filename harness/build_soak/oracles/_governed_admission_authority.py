"""Workspace and execution authority tracking for governed admission."""

from __future__ import annotations

from typing import Any

from ..events import action_executed
from ._governed_admission_helpers import _seq

_MUTATING_TOOLS = frozenset(
    {
        "file_write",
        "file_append",
        "file_edit",
        "file_replace_lines",
        "shell",
        "code_exec",
        "app_create",
        "app_mutate",
        "app_eject",
        "workspace_restore",
    }
)
_NON_PRODUCTIVE_TOOLS = frozenset(
    {
        "submit_plan",
        "think",
        "plan_step",
        "update_plan_progress",
        "ask_user",
        "questions_v2",
        "propose_plan_update",
        "notify_user",
        "finish",
        "remember",
        "serve",
        "file_read",
        "file_list",
        "search",
        "extract",
        "server_status",
        "preview_start",
        "preview_status",
        "preview_logs",
        "preview_stop",
        "shell_view",
        "shell_wait",
        "browser",
        "verify_web_app",
        "verify_appkit_app",
        "design_lint",
        "app_snapshot_version",
    }
)


def _shared_execution_authority(
    execution: dict[str, Any],
) -> tuple[Any, Any, Any]:
    return (
        execution.get("instance_id"),
        execution.get("generation"),
        execution.get("locator"),
    )


def _has_workspace_mutation_capability(
    event_or_result: dict[str, Any],
) -> bool:
    profile = event_or_result.get("action_profile")
    capabilities = profile.get("capabilities") if isinstance(profile, dict) else None
    if isinstance(capabilities, list) and "workspace.mutate" in capabilities:
        return True
    receipts = event_or_result.get("effect_receipts")
    return any(
        isinstance(receipt, dict) and receipt.get("capability") == "workspace.mutate"
        for receipt in receipts or []
    )


def _workspace_mutation_changes_authority(event: dict[str, Any]) -> bool:
    operation = str(event.get("operation") or "")
    if operation in {"agent.run-claimed", "agent.artifact-manifest-fold"}:
        return False
    return not operation.startswith("agent.view-")


def _action_changes_authority(
    event: dict[str, Any],
    events: list[dict[str, Any]],
) -> bool:
    call = event.get("tool_call")
    if not isinstance(call, dict):
        return False
    mutates = call.get("tool_name") in _MUTATING_TOOLS or _has_workspace_mutation_capability(event)
    return mutates and action_executed(events, str(event.get("id") or ""))


def _observation_changes_authority(event: dict[str, Any]) -> bool:
    result = event.get("tool_result")
    if not isinstance(result, dict):
        return False
    if _has_workspace_mutation_capability(result):
        return True
    return result.get("success") is True and result.get("tool_name") in {
        "preview_start",
        "preview_stop",
    }


def _event_changes_authority(
    event: dict[str, Any],
    events: list[dict[str, Any]],
) -> bool:
    kind = event.get("kind")
    if kind in {
        "workspace_restored",
        "appkit_ejection",
        "deliverable",
        "build_platform_admission",
    }:
        return True
    if kind == "workspace_mutation":
        return _workspace_mutation_changes_authority(event)
    if kind == "action":
        return _action_changes_authority(event, events)
    if kind == "observation":
        return _observation_changes_authority(event)
    if kind == "agent_error":
        return _has_workspace_mutation_capability(event)
    return kind == "message" and event.get("source") == "user"


def _authority_change_between(
    events: list[dict[str, Any]],
    after_seq: int,
    through_seq: int,
) -> bool:
    return any(
        _event_changes_authority(event, events)
        for event in events
        if after_seq < _seq(event) < through_seq
    )


def _successful_action_ids(
    events: list[dict[str, Any]],
    actions: dict[str, dict[str, Any]],
    through_seq: int,
) -> set[str]:
    successful: set[str] = set()
    for event in events:
        if event.get("kind") != "observation" or _seq(event) >= through_seq:
            continue
        result = event.get("tool_result")
        action_id = str(event.get("action_id") or "")
        if action_id in actions and isinstance(result, dict) and result.get("success") is True:
            successful.add(action_id)
    return successful


def _productive_action_seq(
    action_id: str,
    action: dict[str, Any],
    successful: set[str],
) -> int:
    if action_id not in successful:
        return -1
    call = action.get("tool_call")
    if not isinstance(call, dict) or call.get("tool_name") in _NON_PRODUCTIVE_TOOLS:
        return -1
    return _seq(action)


def _last_successful_productive_action_seq(
    events: list[dict[str, Any]],
    *,
    through_seq: int,
) -> int:
    actions = {
        str(event.get("id") or ""): event
        for event in events
        if event.get("kind") == "action" and _seq(event) < through_seq
    }
    successful = _successful_action_ids(events, actions, through_seq)
    return max(
        (
            _productive_action_seq(action_id, action, successful)
            for action_id, action in actions.items()
        ),
        default=0,
    )


def _paired_preview_observation(
    event: dict[str, Any],
    actions: dict[str, dict[str, Any]],
) -> bool:
    result = event.get("tool_result")
    action = actions.get(str(event.get("action_id") or ""))
    if not isinstance(result, dict) or not isinstance(action, dict):
        return False
    call = action.get("tool_call")
    if not isinstance(call, dict):
        return False
    return (
        result.get("success") is True
        and result.get("tool_name") in {"preview_start", "preview_stop"}
        and call.get("tool_name") == result.get("tool_name")
        and call.get("call_id") == result.get("call_id")
    )


def _event_authority_seq(
    event: dict[str, Any],
    actions: dict[str, dict[str, Any]],
) -> int:
    kind = event.get("kind")
    if kind in {"deliverable", "build_platform_admission"}:
        return _seq(event)
    if kind == "message" and event.get("source") == "user":
        return _seq(event)
    if kind == "agent_error" and _has_workspace_mutation_capability(event):
        return _seq(event)
    if kind != "observation":
        return -1
    result = event.get("tool_result")
    if isinstance(result, dict) and _has_workspace_mutation_capability(result):
        return _seq(event)
    return _seq(event) if _paired_preview_observation(event, actions) else -1


def _last_authority_seq(
    events: list[dict[str, Any]],
    *,
    through_seq: int,
) -> int:
    authority = _last_successful_productive_action_seq(
        events,
        through_seq=through_seq,
    )
    actions = {
        str(event.get("id") or ""): event
        for event in events
        if event.get("kind") == "action" and _seq(event) < through_seq
    }
    for event in events:
        if 0 <= _seq(event) < through_seq:
            authority = max(authority, _event_authority_seq(event, actions))
    return authority
