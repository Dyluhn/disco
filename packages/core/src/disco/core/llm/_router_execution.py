"""Router execution — the retry/fallback loop for provider calls.

Extracted from ``routing.py`` so the ``DefaultLLMRouter`` class stays under
the class logical-LOC limit. These functions own the same-model transient
retry, auth-retry, and role-fallback escalation logic for both the
non-streaming (``complete``) and streaming (``stream_complete``) paths.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any, Literal

from ._router_attempts import (
    Attempt,
    _notify_success,
    begin_attempt,
    end_attempt,
    end_stream_attempt,
    finish_complete,
)
from ._router_backoff import _RETRY_BACKOFF_BASE_S, sleep_before_retry
from ._router_stream_retry import (
    handle_stream_auth,
    handle_stream_terminal,
    handle_stream_transient,
)
from .empty_completion import completion_outcome
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

# Eight, not five: a research run that died after 40 s of provider 400s
# (2026-09-04, a ~2-minute burst) lost an hour of research to a blip the next
# minute would have ridden out. With the 45 s cap the ladder waits ~2.5 min
# in total, still bounded, still only for transient errors.
_MAX_ATTEMPTS = 8
_ROLE_FALLBACK_MAX_ATTEMPTS = 2
_AUTH_RETRY_DELAY_S = 2.0

Path = Literal["local", "overflow", "pinned", "manual", "role_fallback"]
_FailureRoute = tuple[Path, str, str, str]


def _route(path: Path, reason: str, model_id: str, provider_name: str) -> _FailureRoute:
    return path, reason, model_id, provider_name


@dataclass
class _StreamAttemptState:
    yielded_any: bool = False
    recorded: bool = False
    attempt: Attempt | None = None


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
    state.attempt = begin_attempt(
        exec_req, model_id, provider_name, decision.attempt, call_ordinal, ctx
    )
    async for chunk in provider.stream_complete(exec_req, model=model_id):
        if chunk.done and chunk.final is not None:
            final = chunk.final.model_copy(update={"routing": decision})
            if not state.recorded:
                outcome, error_class = completion_outcome(final)
                end_stream_attempt(
                    state.attempt,
                    recorded=state.recorded,
                    outcome=outcome,
                    error_class=error_class,
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
    end_stream_attempt(
        state.attempt,
        recorded=state.recorded,
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
    backoff_base_s: float = _RETRY_BACKOFF_BASE_S,
) -> CompletionResponse:
    """Run the same-model retry loop for a non-streaming completion."""
    used_fallback = False
    auth_retry_used = False
    attempt = 1
    call_ordinal = 0
    while True:
        call_ordinal += 1
        attempt_context = begin_attempt(
            exec_req, model_id, provider_name, attempt, call_ordinal, ctx
        )
        try:
            resp = await provider.complete(exec_req, model=model_id)
        except asyncio.CancelledError as exc:
            end_attempt(attempt_context, outcome="cancelled", error=exc, retry_scheduled=False)
            raise
        except LLMAuthError as exc:
            if not auth_retry_used:
                end_attempt(attempt_context, outcome="error", error=exc, retry_scheduled=True)
                auth_retry_used = True
                _LOG.warning("transient auth failure, retrying once: %s", exc)
                await asyncio.sleep(auth_retry_delay_s)
                attempt += 1
                continue
            end_attempt(attempt_context, outcome="error", error=exc, retry_scheduled=False)
            _fail(
                router, req, entry, _route(active_path, active_reason, model_id, provider_name), exc
            )
            raise
        except (LLMContextWindowExceeded, LLMContentFiltered) as exc:
            end_attempt(attempt_context, outcome="error", error=exc, retry_scheduled=False)
            _fail(
                router, req, entry, _route(active_path, active_reason, model_id, provider_name), exc
            )
            raise
        except LLMTransientError as exc:
            if attempt >= max_attempts:
                fb = _try_role_fallback(
                    router,
                    req,
                    entry,
                    used_fallback,
                    fallback_max_attempts,
                )
                if fb is not None:
                    end_attempt(attempt_context, outcome="error", error=exc, retry_scheduled=True)
                    (
                        provider,
                        model_id,
                        provider_name,
                        active_reason,
                        active_triggers,
                        max_attempts,
                        used_fallback,
                    ) = fb
                    active_path = "role_fallback"
                    attempt = 1
                    continue
                end_attempt(attempt_context, outcome="error", error=exc, retry_scheduled=False)
                _fail(
                    router,
                    req,
                    entry,
                    _route(active_path, active_reason, model_id, provider_name),
                    None,
                )
                raise
            end_attempt(attempt_context, outcome="error", error=exc, retry_scheduled=True)
            await sleep_before_retry(attempt, backoff_base_s, error=exc, metadata=req.metadata)
            attempt += 1
            continue
        except Exception as exc:  # noqa: BLE001 — preserve provider error behavior
            end_attempt(attempt_context, outcome="error", error=exc, retry_scheduled=False)
            raise
        else:
            return finish_complete(
                router, model_key, req, ctx, resp, model_id, provider_name,
                active_path, active_reason, active_triggers, attempt, attempt_context,
            )


async def execute_stream_complete(
    router: Any, *, model_key: str, provider: Any,
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
    backoff_base_s: float = _RETRY_BACKOFF_BASE_S,
) -> AsyncIterator[StreamChunk]:
    used_fallback = False
    auth_retry_used = False
    attempt = 1
    call_ordinal = 0
    while True:
        call_ordinal += 1
        decision = RoutingDecision(
            profile=req.profile,
            chosen_model=model_id,
            provider=provider_name,
            path=active_path,
            reason=active_reason,
            overflow_triggers=active_triggers,
            attempt=attempt,
        )
        state = _StreamAttemptState()
        try:
            async for chunk in _yield_stream_attempt(
                router, model_key=model_key, provider=provider,
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
            end_stream_attempt(
                state.attempt, recorded=state.recorded,
                outcome="cancelled", error=exc, retry_scheduled=False,
            )
            raise
        except LLMAuthError as exc:
            if await handle_stream_auth(
                router, req, entry, state, exc, auth_retry_used, auth_retry_delay_s,
                _route(active_path, active_reason, model_id, provider_name), _fail,
            ):
                auth_retry_used = True
                attempt += 1
                continue
        except (LLMContextWindowExceeded, LLMContentFiltered) as exc:
            handle_stream_terminal(
                router, req, entry, state, exc,
                _route(active_path, active_reason, model_id, provider_name), _fail,
            )
        except LLMTransientError as exc:
            fallback = await handle_stream_transient(
                router, req, entry, state, exc, attempt, max_attempts,
                used_fallback, fallback_max_attempts, active_path, active_reason,
                model_id, provider_name, backoff_base_s, _try_role_fallback, _fail,
            )
            if fallback is not None:
                (
                    provider,
                    model_id,
                    provider_name,
                    active_reason,
                    active_triggers,
                    max_attempts,
                    used_fallback,
                ) = fallback
                active_path = "role_fallback"
                attempt = 1
                continue
            attempt += 1
            continue
        except Exception as exc:  # noqa: BLE001 — preserve provider error behavior
            end_stream_attempt(
                state.attempt, recorded=state.recorded,
                outcome="error", error=exc, retry_scheduled=False,
            )
            raise
