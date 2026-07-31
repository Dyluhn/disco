"""The sealed-workflow output-contract gate.

Owns: checking whether a workflow run's declared output path exists
(sandbox-aware, falling back to a workspace-relative filesystem check), and
refusing finish until it does — bounded by `_FINISH_VERIFY_CAP` so a model
that never lands the contract path still releases with an honest warning.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any, cast

from ..common import (
    _FINISH_VERIFY_CAP,
    _LOG,
    AgentStep,
    ConversationStatus,
    Disp,
    Event,
    EventSource,
    LLMMessage,
    MessageEvent,
    StatusEvent,
    _safe_deliverable_file_path,
)
from .workflow_paths import render_workflow_output_path


async def workflow_output_path_exists(gate: Any, path: str) -> tuple[bool, str]:
    safe = _safe_deliverable_file_path(path)
    if safe is None:
        return False, path

    sbx = getattr(gate._loop.executor, "sandbox", None)
    file_exists = getattr(sbx, "file_exists", None) if sbx is not None else None
    if callable(file_exists):
        file_exists_fn = cast(Callable[[str], Awaitable[object]], file_exists)
        try:
            return bool(await file_exists_fn(safe)), safe
        except Exception as exc:  # noqa: BLE001 — cannot confirm output existence
            _LOG.warning(
                "workflow output existence check failed for %s:%s: %s",
                gate._loop.conversation_id,
                safe,
                exc,
            )
            return False, safe

    workspace = getattr(sbx, "workspace_path", None) if sbx is not None else None
    if not workspace:
        return False, safe

    from pathlib import Path

    try:
        root = Path(workspace).resolve()
        candidate = (root / safe).resolve()
        root_s = str(root)
        cand_s = str(candidate)
        if not (cand_s == root_s or cand_s.startswith(root_s.rstrip("/") + "/")):
            return False, safe
        return candidate.is_file(), safe
    except OSError as exc:
        _LOG.warning(
            "workflow output existence check failed for %s:%s from workspace: %s",
            gate._loop.conversation_id,
            safe,
            exc,
        )
        return False, safe


def _workflow_output_meta(workflow_run: Any, contract: Any, checked_path: str) -> dict[str, Any]:
    return {
        "workflow_run_id": str(getattr(workflow_run, "run_id", "")),
        "workflow_output_path": checked_path,
        "workflow_output_format": contract.format,
    }


async def _workflow_output_contract_release(
    gate: Any, *, meta: dict[str, Any], checked_path: str, contract: Any
) -> Disp:
    await gate._loop._emit(
        StatusEvent(
            status=ConversationStatus.RUNNING,
            detail="workflow_output_contract_release",
            meta={**meta, "verdict": "release"},
        )
    )
    await gate._loop._emit(
        MessageEvent(
            source=EventSource.ENVIRONMENT,
            message=LLMMessage(
                role="user",
                content=(
                    "⚠ Finished despite the workflow output contract not being "
                    f"satisfied after {gate._loop._workflow_output_contract_refusals} "
                    f"refusals. Expected `{checked_path}` ({contract.format}) in the "
                    "workspace. The workflow output is missing; note this clearly in "
                    "the summary."
                ),
            ),
        )
    )
    gate._loop._workflow_output_contract_refusals = 0
    return Disp.FALLTHROUGH


async def gate_workflow_output_contract(
    gate: Any,
    step: AgentStep,
    events: list[Event],
) -> Disp:
    workflow_run = getattr(gate._loop, "_workflow_run", None)
    if workflow_run is None:
        return Disp.FALLTHROUGH
    contract = getattr(getattr(workflow_run, "definition", None), "output_contract", None)
    if contract is None:
        return Disp.FALLTHROUGH

    params = getattr(workflow_run, "params", {})
    if not isinstance(params, dict):
        params = {}
    output_path = render_workflow_output_path(contract.path_template, params)
    exists, checked_path = await workflow_output_path_exists(gate, output_path)
    meta = _workflow_output_meta(workflow_run, contract, checked_path)
    if exists:
        gate._loop._workflow_output_contract_refusals = 0
        await gate._loop._emit(
            StatusEvent(
                status=ConversationStatus.RUNNING,
                detail="workflow_output_contract_passed",
                meta={**meta, "verdict": "pass"},
            )
        )
        return Disp.FALLTHROUGH

    # RELEASE VALVE — an uncapped refusal is the sealed done-trap wearing a
    # new mask: a model that never lands the contract path would be refused
    # finish forever. After the cap, release with an HONEST warning instead.
    if gate._loop._workflow_output_contract_refusals >= _FINISH_VERIFY_CAP:
        return await _workflow_output_contract_release(
            gate, meta=meta, checked_path=checked_path, contract=contract
        )

    gate._loop._workflow_output_contract_refusals += 1
    await gate._loop._emit(
        StatusEvent(
            status=ConversationStatus.RUNNING,
            detail="workflow_output_contract_refused",
            meta={**meta, "verdict": "fail"},
        )
    )
    await gate._loop._emit(
        MessageEvent(
            source=EventSource.ENVIRONMENT,
            message=LLMMessage(
                role="user",
                content=(
                    "<system-reminder>\n"
                    "finish refused: this workflow completes by writing "
                    f"{checked_path}. Write it (file_write), then call finish. "
                    "If the workflow should not produce output, use `skip` with "
                    "a reason instead of `finish`.\n"
                    "</system-reminder>"
                ),
            ),
        )
    )
    return Disp.CONTINUE
