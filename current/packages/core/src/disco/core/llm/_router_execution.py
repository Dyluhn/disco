"""Router execution — the retry/fallback loop for provider calls.

Extracted from ``routing.py`` so the ``DefaultLLMRouter`` class stays under
the class logical-LOC limit. These functions own the same-model transient
retry, auth-retry, and role-fallback escalation logic for both the
non-streaming (``complete``) and streaming (``stream_complete``) paths.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any, Literal

from .errors import (
    LLMAuthError,
    LLMContentFiltered,
    LLMContextWindowExceeded,
    LLMTransientError,
)
from .types import (
    CompletionRequest,
    CompletionResponse,
    ModelRole,
    Requirement,
    RoutingDecision,
    StreamChunk,
)

_LOG = logging.getLogger(__name__)

_MAX_ATTEMPTS = 5
_ROLE_FALLBACK_MAX_ATTEMPTS = 2
_AUTH_RETRY_DELAY_S = 2.0

Path = Literal["local", "overflow", "pinned", "manual", "role_fallback"]
_FailureRoute = tuple[Path, str, str, str]


def _attempt_identity(exec_req: CompletionRequest, ctx: Any) -> tuple[str | None, str]:
    metadata = exec_req.metadata or {}
    conversation_id = getattr(ctx, "conversation_id", None) or metadata.get("conversation_id")
    stage = metadata.get("inspect_stage") or "router"
    return (
        conversation_id[:256] if isinstance(conversation_id, str) else None,
        str(stage)[:128],
    )


def _record_attempt_start(
    exec_req: CompletionRequest,
    model_id: str,
    provider_name: str,
    attempt: int,
    call_ordinal: int,
    ctx: Any,
) -> float:
    conversation_id, stage = _attempt_identity(exec_req, ctx)
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
    return time.perf_counter()


def _record_attempt_end(
    exec_req: CompletionRequest,
    model_id: str,
    provider_name: str,
    attempt: int,
    call_ordinal: int,
    ctx: Any,
    started: float,
    *,
    outcome: str,
    error: BaseException | None = None,
    retry_scheduled: bool | None = None,
) -> None:
    conversation_id, stage = _attempt_identity(exec_req, ctx)
    try:
        from ..inspect import record_model_attempt

        record_model_attempt(
            conversation_id,
            stage=stage,
            attempt=attempt,
            provider=provider_name,
            model=model_id,
            outcome=outcome,
            call_ordinal=call_ordinal,
            latency_ms=max(0, int((time.perf_counter() - started) * 1_000)),
            error_class=type(error).__name__ if error is not None else None,
            retry_scheduled=retry_scheduled,
        )
    except Exception:  # noqa: BLE001 — inspect is passive and cannot gate calls
        _LOG.debug("LLM attempt-end inspection failed", exc_info=True)


def _route(path: Path, reason: str, model_id: str, provider_name: str) -> _FailureRoute:
    return path, reason, model_id, provider_name


def _notify_success(router: Any, model_key: str, decision: RoutingDecision, ctx: Any) -> None:
    observer = router._on_success
    if observer is None:
        return
    try:
        observer(model_key, decision, ctx)
    except Exception:  # noqa: BLE001 — passive observation cannot fail a completed call
        _LOG.exception("LLM success observer failed")


@dataclass
class _StreamAttemptState:
    yielded_any: bool = False
    recorded: bool = False
    started_at: float | None = None


@dataclass
class _AttemptContext:
    exec_req: CompletionRequest
    model_id: str
    provider_name: str
    attempt: int
    call_ordinal: int
    ctx: Any
    started: float


@dataclass
class _StreamErrorResult:
    action: str
    fallback: tuple[Any, str, str, str, list[str], int, bool] | None = None


def _fallback_values(result: _StreamErrorResult) -> tuple[Any, str, str, str, list[str], int, bool]:
    if result.fallback is None:
        raise RuntimeError("stream fallback action missing fallback target")
    return result.fallback


async def _stream_error(
    router,
    req,
    entry,
    state,
    exc,
    *,
    exec_req,
    model_id,
    provider_name,
    attempt,
    call_ordinal,
    ctx,
    active_path,
    active_reason,
    max_attempts,
    used_fallback,
    fallback_max_attempts,
    auth_retry_used,
    auth_retry_delay_s,
) -> _StreamErrorResult:
    if isinstance(exc, LLMAuthError) and not state.yielded_any and not auth_retry_used:
        _stream_record(
            exec_req, model_id, provider_name, attempt, call_ordinal, ctx, state, "error", exc, True
        )
        _LOG.warning("transient auth failure, retrying once: %s", exc)
        await asyncio.sleep(auth_retry_delay_s)
        return _StreamErrorResult("auth_retry")
    if isinstance(exc, LLMTransientError):
        return await _stream_transient_error(
            router,
            req,
            entry,
            state,
            exc,
            exec_req=exec_req,
            model_id=model_id,
            provider_name=provider_name,
            attempt=attempt,
            call_ordinal=call_ordinal,
            ctx=ctx,
            active_path=active_path,
            active_reason=active_reason,
            max_attempts=max_attempts,
            used_fallback=used_fallback,
            fallback_max_attempts=fallback_max_attempts,
        )
    _stream_record(
        exec_req, model_id, provider_name, attempt, call_ordinal, ctx, state, "error", exc
    )
    if isinstance(exc, (LLMAuthError, LLMContextWindowExceeded, LLMContentFiltered)):
        _fail(router, req, entry, _route(active_path, active_reason, model_id, provider_name), exc)
    return _StreamErrorResult("raise")


async def _stream_transient_error(
    router,
    req,
    entry,
    state,
    exc,
    *,
    exec_req,
    model_id,
    provider_name,
    attempt,
    call_ordinal,
    ctx,
    active_path,
    active_reason,
    max_attempts,
    used_fallback,
    fallback_max_attempts,
) -> _StreamErrorResult:
    if state.yielded_any:
        _stream_record(
            exec_req, model_id, provider_name, attempt, call_ordinal, ctx, state, "error", exc
        )
        _fail(router, req, entry, _route(active_path, active_reason, model_id, provider_name), None)
        return _StreamErrorResult("raise")
    if attempt < max_attempts:
        _stream_record(
            exec_req, model_id, provider_name, attempt, call_ordinal, ctx, state, "error", exc, True
        )
        return _StreamErrorResult("retry")
    fallback = _try_role_fallback(router, req, entry, used_fallback, fallback_max_attempts)
    _stream_record(
        exec_req,
        model_id,
        provider_name,
        attempt,
        call_ordinal,
        ctx,
        state,
        "error",
        exc,
        fallback is not None,
    )
    if fallback is not None:
        return _StreamErrorResult("fallback", fallback)
    _fail(router, req, entry, _route(active_path, active_reason, model_id, provider_name), None)
    return _StreamErrorResult("raise")


async def _yield_stream_attempt(
    router: Any,
    *,
    model_key: str,
    provider: Any,
    provider_name: str,
    exec_req: CompletionRequest,
    model_id: str,
    decision: RoutingDecision,
    ctx: Any,
    state: _StreamAttemptState,
    call_ordinal: int,
) -> AsyncIterator[StreamChunk]:
    state.started_at = _record_attempt_start(
        exec_req, model_id, provider_name, decision.attempt, call_ordinal, ctx
    )
    async for chunk in provider.stream_complete(exec_req, model=model_id):
        if chunk.done and chunk.final is not None:
            final = chunk.final.model_copy(update={"routing": decision})
            if not state.recorded:
                _record_attempt_end(
                    exec_req,
                    model_id,
                    provider_name,
                    decision.attempt,
                    call_ordinal,
                    ctx,
                    state.started_at,
                    outcome="success",
                    retry_scheduled=False,
                )
                router._sink.record(decision)
                router._cost.add(final.usage.cost_usd, ctx.conversation_id)
                _notify_success(router, model_key, decision, ctx)
                state.recorded = True
            state.yielded_any = True
            yield chunk.model_copy(update={"final": final})
        else:
            state.yielded_any = True
            yield chunk
    if not state.recorded and state.started_at is not None:
        _record_attempt_end(
            exec_req,
            model_id,
            provider_name,
            decision.attempt,
            call_ordinal,
            ctx,
            state.started_at,
            outcome="incomplete",
            retry_scheduled=False,
        )


def _role_fallback_target(
    config: Any,
    providers: dict,
    role: ModelRole,
    *,
    requirements: frozenset[Requirement],
    role_fallback_provider_key: str,
    driver_roles: frozenset[ModelRole],
    fallback_eligible_roles: frozenset[ModelRole],
) -> tuple[Any, str] | None:
    """Resolve the role-fallback (provider, model_id) or None."""
    from .types import Requirement as Req

    settings = config.role_fallback
    if role in driver_roles or role not in fallback_eligible_roles:
        return None
    if not requirements.issubset({Req.JSON_MODE}):
        return None
    if not settings.enabled or not settings.model.strip():
        return None
    provider = providers.get(role_fallback_provider_key)
    if provider is None:
        return None
    return provider, settings.model.strip()


def _record_failure(
    sink: Any,
    req: CompletionRequest,
    entry: Any | None,
    path: Path,
    reason: str,
    exc: Exception | None,
    *,
    chosen_model: str | None = None,
    provider_name: str | None = None,
) -> None:
    """Emit one terminal-failure RoutingDecision to the sink."""
    kind = type(exc).__name__ if exc else "retries exhausted"
    sink.record(
        RoutingDecision(
            profile=req.profile,
            chosen_model=chosen_model or (entry.model_id if entry else ""),
            provider=provider_name or (entry.provider if entry else ""),
            path=path,
            reason=f"terminal failure: {kind} ({reason})",
            overflow_triggers=[],
        )
    )


def _fail(
    router: Any,
    req: CompletionRequest,
    entry: Any,
    route: _FailureRoute,
    exc: Exception | None,
) -> None:
    path, reason, model_id, provider_name = route
    router._record_failure(
        req,
        entry,
        path,
        reason,
        exc,
        chosen_model=model_id,
        provider_name=provider_name,
    )


def _complete_record(meta: _AttemptContext, outcome, error=None, retry=False):
    _record_attempt_end(
        meta.exec_req,
        meta.model_id,
        meta.provider_name,
        meta.attempt,
        meta.call_ordinal,
        meta.ctx,
        meta.started,
        outcome=outcome,
        error=error,
        retry_scheduled=retry,
    )


def _completion_decision(
    req, model_id, provider_name, active_path, active_reason, active_triggers, attempt
):
    return RoutingDecision(
        profile=req.profile,
        chosen_model=model_id,
        provider=provider_name,
        path=active_path,
        reason=active_reason,
        overflow_triggers=active_triggers,
        attempt=attempt,
    )


def _stream_record(
    exec_req,
    model_id,
    provider_name,
    attempt,
    call_ordinal,
    ctx,
    state,
    outcome,
    error=None,
    retry=False,
):
    if state.recorded or state.started_at is None:
        return
    _complete_record(
        _AttemptContext(
            exec_req, model_id, provider_name, attempt, call_ordinal, ctx, state.started_at
        ),
        outcome,
        error,
        retry,
    )


def _try_role_fallback(
    router: Any,
    req: CompletionRequest,
    entry: Any,
    used_fallback: bool,
    fallback_max_attempts: int,
) -> tuple[Any, str, str, str, list[str], int, bool] | None:
    """Attempt role-fallback resolution.

    Returns ``(provider, model_id, provider_name, active_path, active_triggers,
    max_attempts, used_fallback)`` or None when no fallback is available.
    """
    if used_fallback:
        return None
    fallback = router._role_fallback_target(
        req.profile.role,
        requirements=router._effective_requirements(req),
    )
    if fallback is None:
        return None
    provider, model_id = fallback
    provider_name = provider.name
    active_reason = f"role_fallback after {entry.model_id}"
    active_triggers = [f"original_model:{entry.model_id}"]
    return (
        provider,
        model_id,
        provider_name,
        active_reason,
        active_triggers,
        fallback_max_attempts,
        True,
    )


async def execute_complete(
    router: Any,
    *,
    model_key: str,
    provider: Any,
    exec_req: CompletionRequest,
    model_id: str,
    provider_name: str,
    entry: Any,
    active_path: Path,
    active_reason: str,
    active_triggers: list[str],
    overflow_triggers: list[str],
    req: CompletionRequest,
    ctx: Any,
    max_attempts: int = _MAX_ATTEMPTS,
    fallback_max_attempts: int = _ROLE_FALLBACK_MAX_ATTEMPTS,
    auth_retry_delay_s: float = _AUTH_RETRY_DELAY_S,
) -> CompletionResponse:
    """Run the same-model retry loop for a non-streaming completion."""
    used_fallback = False
    auth_retry_used = False
    attempt = 1
    call_ordinal = 0
    while True:
        call_ordinal += 1
        attempt_started = _record_attempt_start(
            exec_req, model_id, provider_name, attempt, call_ordinal, ctx
        )
        meta = _AttemptContext(
            exec_req, model_id, provider_name, attempt, call_ordinal, ctx, attempt_started
        )
        try:
            resp = await provider.complete(exec_req, model=model_id)
        except asyncio.CancelledError as exc:
            _complete_record(meta, "cancelled", exc)
            raise
        except LLMAuthError as exc:
            if not auth_retry_used:
                _complete_record(meta, "error", exc, True)
                auth_retry_used = True
                _LOG.warning("transient auth failure, retrying once: %s", exc)
                await asyncio.sleep(auth_retry_delay_s)
                attempt += 1
                continue
            _complete_record(meta, "error", exc)
            _fail(
                router, req, entry, _route(active_path, active_reason, model_id, provider_name), exc
            )
            raise
        except (LLMContextWindowExceeded, LLMContentFiltered) as exc:
            _complete_record(meta, "error", exc)
            _fail(
                router, req, entry, _route(active_path, active_reason, model_id, provider_name), exc
            )
            raise
        except LLMTransientError as exc:
            if attempt >= max_attempts:
                fb = _try_role_fallback(router, req, entry, used_fallback, fallback_max_attempts)
                if fb is None:
                    _complete_record(meta, "error", exc)
                    _fail(
                        router,
                        req,
                        entry,
                        _route(active_path, active_reason, model_id, provider_name),
                        None,
                    )
                    raise
                _complete_record(meta, "error", exc, True)
                (
                    provider,
                    model_id,
                    provider_name,
                    active_reason,
                    active_triggers,
                    max_attempts,
                    used_fallback,
                ) = fb
                active_path, attempt = "role_fallback", 1
                continue
            _complete_record(meta, "error", exc, True)
            attempt += 1
            continue
        except Exception as exc:  # noqa: BLE001 — preserve provider error behavior
            _complete_record(meta, "error", exc)
            raise
        else:
            _complete_record(meta, "success")
            decision = _completion_decision(
                req, model_id, provider_name, active_path, active_reason, active_triggers, attempt
            )
            router._sink.record(decision)
            router._cost.add(resp.usage.cost_usd, ctx.conversation_id)
            _notify_success(router, model_key, decision, ctx)
            return resp.model_copy(update={"routing": decision})


async def execute_stream_complete(
    router: Any,
    *,
    model_key: str,
    provider: Any,
    exec_req: CompletionRequest,
    model_id: str,
    provider_name: str,
    entry: Any,
    active_path: Path,
    active_reason: str,
    active_triggers: list[str],
    req: CompletionRequest,
    ctx: Any,
    max_attempts: int = _MAX_ATTEMPTS,
    fallback_max_attempts: int = _ROLE_FALLBACK_MAX_ATTEMPTS,
    auth_retry_delay_s: float = _AUTH_RETRY_DELAY_S,
) -> AsyncIterator[StreamChunk]:
    used_fallback = False
    auth_retry_used = False
    attempt = 1
    call_ordinal = 0
    while True:
        call_ordinal += 1
        decision = _completion_decision(
            req, model_id, provider_name, active_path, active_reason, active_triggers, attempt
        )
        state = _StreamAttemptState()
        try:
            async for chunk in _yield_stream_attempt(
                router,
                model_key=model_key,
                provider=provider,
                provider_name=provider_name,
                exec_req=exec_req,
                model_id=model_id,
                decision=decision,
                ctx=ctx,
                state=state,
                call_ordinal=call_ordinal,
            ):
                yield chunk
            return  # stream completed cleanly
        except asyncio.CancelledError as exc:
            if not state.recorded:
                _stream_record(
                    exec_req,
                    model_id,
                    provider_name,
                    attempt,
                    call_ordinal,
                    ctx,
                    state,
                    "cancelled",
                    exc,
                )
            raise
        except Exception as exc:  # noqa: BLE001 — preserve provider error behavior
            outcome = await _stream_error(
                router,
                req,
                entry,
                state,
                exc,
                exec_req=exec_req,
                model_id=model_id,
                provider_name=provider_name,
                attempt=attempt,
                call_ordinal=call_ordinal,
                ctx=ctx,
                active_path=active_path,
                active_reason=active_reason,
                max_attempts=max_attempts,
                used_fallback=used_fallback,
                fallback_max_attempts=fallback_max_attempts,
                auth_retry_used=auth_retry_used,
                auth_retry_delay_s=auth_retry_delay_s,
            )
            if outcome.action == "raise":
                raise
            if outcome.action == "auth_retry":
                auth_retry_used = True
                attempt += 1
                continue
            if outcome.action == "retry":
                attempt += 1
                continue
            (
                provider,
                model_id,
                provider_name,
                active_reason,
                active_triggers,
                max_attempts,
                used_fallback,
            ) = _fallback_values(outcome)
            active_path, attempt = "role_fallback", 1
