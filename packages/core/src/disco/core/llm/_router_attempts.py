"""Attempt bookkeeping shared by the router's complete and streaming paths.

This module contains passive inspection only.  It deliberately does not decide
whether a provider error is retryable; that decision remains in the execution
loop which owns the call.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Any, Literal

from .empty_completion import completion_outcome
from .types import CompletionRequest, CompletionResponse, RoutingDecision

_LOG = logging.getLogger(__name__)


@dataclass(frozen=True)
class Attempt:
    """The immutable context needed to record one provider attempt."""

    exec_req: CompletionRequest
    model_id: str
    provider_name: str
    attempt: int
    call_ordinal: int
    ctx: Any
    started: float


def _notify_success(router: Any, model_key: str, decision: RoutingDecision, ctx: Any) -> None:
    observer = router._on_success
    if observer is None:
        return
    try:
        observer(model_key, decision, ctx)
    except Exception:  # noqa: BLE001 — passive observation cannot fail a completed call
        _LOG.exception("LLM success observer failed")


def _identity(exec_req: CompletionRequest, ctx: Any) -> tuple[str | None, str]:
    metadata = exec_req.metadata or {}
    conversation_id = getattr(ctx, "conversation_id", None) or metadata.get("conversation_id")
    stage = metadata.get("inspect_stage") or "router"
    return (
        conversation_id[:256] if isinstance(conversation_id, str) else None,
        str(stage)[:128],
    )


def begin_attempt(
    exec_req: CompletionRequest,
    model_id: str,
    provider_name: str,
    attempt: int,
    call_ordinal: int,
    ctx: Any,
) -> Attempt:
    """Record the start and return the context for one provider attempt."""
    conversation_id, stage = _identity(exec_req, ctx)
    try:
        from ..inspect import record_model_attempt

        record_model_attempt(
            conversation_id,
            stage=stage,
            attempt=attempt,
            provider=provider_name,
            model=model_id,
            outcome="started",
            call_ordinal=call_ordinal,
        )
    except Exception:  # noqa: BLE001 — inspect is passive and cannot gate calls
        _LOG.debug("LLM attempt-start inspection failed", exc_info=True)
    return Attempt(
        exec_req,
        model_id,
        provider_name,
        attempt,
        call_ordinal,
        ctx,
        time.perf_counter(),
    )


def end_attempt(
    attempt: Attempt,
    *,
    outcome: str,
    error: BaseException | None = None,
    error_class: str | None = None,
    retry_scheduled: bool | None = None,
) -> None:
    """Record the end of an attempt without changing retry behavior."""
    conversation_id, stage = _identity(attempt.exec_req, attempt.ctx)
    try:
        from ..inspect import record_model_attempt

        record_model_attempt(
            conversation_id,
            stage=stage,
            attempt=attempt.attempt,
            provider=attempt.provider_name,
            model=attempt.model_id,
            outcome=outcome,
            call_ordinal=attempt.call_ordinal,
            latency_ms=max(0, int((time.perf_counter() - attempt.started) * 1_000)),
            error_class=error_class or (type(error).__name__ if error is not None else None),
            retry_scheduled=retry_scheduled,
        )
    except Exception:  # noqa: BLE001 — inspect is passive and cannot gate calls
        _LOG.debug("LLM attempt-end inspection failed", exc_info=True)


def end_stream_attempt(
    attempt: Attempt | None,
    *,
    recorded: bool,
    outcome: str,
    error: BaseException | None = None,
    error_class: str | None = None,
    retry_scheduled: bool | None = None,
) -> None:
    """Record a stream attempt only while it has an open telemetry record."""
    if recorded or attempt is None:
        return
    end_attempt(
        attempt,
        outcome=outcome,
        error=error,
        error_class=error_class,
        retry_scheduled=retry_scheduled,
    )


def finish_complete(
    router: Any,
    model_key: str,
    req: CompletionRequest,
    ctx: Any,
    response: CompletionResponse,
    model_id: str,
    provider_name: str,
    active_path: Literal["local", "overflow", "pinned", "manual", "role_fallback"],
    active_reason: str,
    active_triggers: list[str],
    attempt: int,
    attempt_context: Attempt,
) -> CompletionResponse:
    """Record and publish a completed non-streaming response in order."""
    outcome, error_class = completion_outcome(response)
    end_attempt(
        attempt_context,
        outcome=outcome,
        error_class=error_class,
        retry_scheduled=False,
    )
    decision = RoutingDecision(
        profile=req.profile,
        chosen_model=model_id,
        provider=provider_name,
        path=active_path,
        reason=active_reason,
        overflow_triggers=active_triggers,
        attempt=attempt,
    )
    router._sink.record(decision)
    router._cost.add(response.usage.cost_usd, ctx.conversation_id)
    _notify_success(router, model_key, decision, ctx)
    return response.model_copy(update={"routing": decision})


__all__ = ["Attempt", "begin_attempt", "end_attempt", "end_stream_attempt"]
