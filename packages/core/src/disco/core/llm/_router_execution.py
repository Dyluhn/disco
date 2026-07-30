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


def _route(path: Path, reason: str, model_id: str, provider_name: str) -> _FailureRoute:
    return path, reason, model_id, provider_name


@dataclass
class _StreamAttemptState:
    yielded_any: bool = False
    recorded: bool = False


async def _yield_stream_attempt(
    router: Any,
    *,
    provider: Any,
    exec_req: CompletionRequest,
    model_id: str,
    decision: RoutingDecision,
    ctx: Any,
    state: _StreamAttemptState,
) -> AsyncIterator[StreamChunk]:
    async for chunk in provider.stream_complete(exec_req, model=model_id):
        if chunk.done and chunk.final is not None:
            final = chunk.final.model_copy(update={"routing": decision})
            if not state.recorded:
                router._sink.record(decision)
                router._cost.add(final.usage.cost_usd, ctx.conversation_id)
                state.recorded = True
            state.yielded_any = True
            yield chunk.model_copy(update={"final": final})
        else:
            state.yielded_any = True
            yield chunk


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
    while True:
        try:
            resp = await provider.complete(exec_req, model=model_id)
        except LLMAuthError as exc:
            if not auth_retry_used:
                auth_retry_used = True
                _LOG.warning("transient auth failure, retrying once: %s", exc)
                await asyncio.sleep(auth_retry_delay_s)
                attempt += 1
                continue
            _fail(
                router, req, entry, _route(active_path, active_reason, model_id, provider_name), exc
            )
            raise
        except (LLMContextWindowExceeded, LLMContentFiltered) as exc:
            _fail(
                router, req, entry, _route(active_path, active_reason, model_id, provider_name), exc
            )
            raise
        except LLMTransientError:
            if attempt >= max_attempts:
                fb = _try_role_fallback(
                    router,
                    req,
                    entry,
                    used_fallback,
                    fallback_max_attempts,
                )
                if fb is not None:
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
                _fail(
                    router,
                    req,
                    entry,
                    _route(active_path, active_reason, model_id, provider_name),
                    None,
                )
                raise
            attempt += 1
            continue
        else:
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
            router._cost.add(resp.usage.cost_usd, ctx.conversation_id)
            return resp.model_copy(update={"routing": decision})


async def execute_stream_complete(
    router: Any,
    *,
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
    while True:
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
                router,
                provider=provider,
                exec_req=exec_req,
                model_id=model_id,
                decision=decision,
                ctx=ctx,
                state=state,
            ):
                yield chunk
            return  # stream completed cleanly
        except LLMAuthError as exc:
            if not state.yielded_any and not auth_retry_used:
                auth_retry_used = True
                _LOG.warning("transient auth failure, retrying once: %s", exc)
                await asyncio.sleep(auth_retry_delay_s)
                attempt += 1
                continue
            _fail(
                router, req, entry, _route(active_path, active_reason, model_id, provider_name), exc
            )
            raise
        except (LLMContextWindowExceeded, LLMContentFiltered) as exc:
            _fail(
                router, req, entry, _route(active_path, active_reason, model_id, provider_name), exc
            )
            raise
        except LLMTransientError:
            if state.yielded_any:
                _fail(
                    router,
                    req,
                    entry,
                    _route(active_path, active_reason, model_id, provider_name),
                    None,
                )
                raise
            if attempt >= max_attempts:
                fb = _try_role_fallback(
                    router,
                    req,
                    entry,
                    used_fallback,
                    fallback_max_attempts,
                )
                if fb is not None:
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
                _fail(
                    router,
                    req,
                    entry,
                    _route(active_path, active_reason, model_id, provider_name),
                    None,
                )
                raise
            attempt += 1
            continue
