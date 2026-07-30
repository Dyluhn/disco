"""Per-run driver model context-window resolution (generic, target-neutral Core).

A ``DriverContextResolver`` probes a live model server once per
(base_url, model_id) (singleflight bounded), resolves a positive
context_window, and returns a frozen ``ResolvedDriverContext`` that the
runtime threads through router, condenser, file-read caps, executor, and
``AgentLoop`` composition.

This is the generic, target-neutral implementation. The agent-server
``driver_context`` module re-exports these symbols as an exact compatibility
facade so existing import paths remain stable.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any


class DriverContextResolutionError(Exception):
    """A positive driver context window could not be determined."""

    def __init__(self, model_key: str, provider: str, reason: str) -> None:
        self.model_key = model_key
        self.provider = provider
        self.reason = reason
        super().__init__(f"Driver '{model_key}' ({provider}): {reason}")


@dataclass(frozen=True)
class ResolvedDriverContext:
    """An immutable snapshot of a single run's effective driver model and its
    resolved context window.

    All fields are log-safe — no secrets, keys, or request bodies.
    """

    model_key: str
    provider: str
    model_id: str
    context_window: int
    source: str
    resolved_at: datetime

    def __post_init__(self) -> None:
        if type(self.context_window) is not int or self.context_window <= 0:
            raise ValueError(f"context_window must be positive, got {self.context_window}")


def _probe_n_ctx(probe_result: Any) -> tuple[Any, bool, bool]:
    """Extract (n_ctx, probe_miss, zero_miss) from a probe result dict."""
    if isinstance(probe_result, dict):
        n_ctx = probe_result.get("n_ctx", 0)
        probe_miss = "n_ctx" not in probe_result
    else:
        n_ctx = 0
        probe_miss = True
    zero_miss = type(n_ctx) is int and n_ctx == 0
    return n_ctx, probe_miss, zero_miss


class DriverContextResolver:
    """Resolve a model's context window with singleflight-bounded live probing.

    Only one probe runs per (base_url, model_id) at a time. Every caller has
    its own bounded wait over the shielded shared probe, so one timeout or
    cancellation cannot cancel the work or strand another caller. Clean-up is
    identity-safe: a done-callback removes the task only when ``_in_flight``
    still holds the same task object.
    """

    def __init__(self, timeout_s: float = 5.0) -> None:
        self._timeout_s = timeout_s
        self._in_flight: dict[tuple[str, str], asyncio.Task[dict[str, Any]]] = {}
        self._lock = asyncio.Lock()

    # ------------------------------------------------------------------
    # public
    # ------------------------------------------------------------------

    async def resolve(
        self,
        model_key: str,
        entry: Any,  # ModelEntry
        probe: Callable[[], Awaitable[dict[str, Any]]] | None,
        unavailable_source: str | None,
    ) -> ResolvedDriverContext:
        configured_window: int = entry.context_window

        if probe is None:
            if configured_window <= 0:
                raise DriverContextResolutionError(
                    model_key=model_key,
                    provider=entry.provider,
                    reason=(
                        "no probe is available and the configured context_window "
                        f"is non-positive ({configured_window})"
                    ),
                )
            source = (
                unavailable_source if unavailable_source is not None else "configured_unavailable"
            )
            return self._make_context(model_key, entry, configured_window, source)

        shared = await self._shared_probe(entry, probe)
        probe_result, fallback_source = await self._await_probe(
            model_key, entry, configured_window, shared
        )
        return self._context_from_probe(
            model_key, entry, configured_window, probe_result, fallback_source
        )

    # ------------------------------------------------------------------
    # helpers
    # ------------------------------------------------------------------

    async def _shared_probe(
        self,
        entry: Any,
        probe: Callable[[], Awaitable[dict[str, Any]]],
    ) -> asyncio.Task[dict[str, Any]]:
        """Get or start the singleflight shared probe task for this endpoint."""
        base_url: str = getattr(entry, "base_url", None) or ""
        model_id: str = entry.model_id
        key = (base_url, model_id)

        async with self._lock:
            shared = self._in_flight.get(key)
            if shared is None or shared.done():
                if shared is not None:
                    del self._in_flight[key]
                shared = asyncio.ensure_future(probe())
                self._in_flight[key] = shared
                shared.add_done_callback(lambda t, _k=key: self._cleanup(_k, t))
        return shared

    async def _await_probe(
        self,
        model_key: str,
        entry: Any,
        configured_window: int,
        shared: asyncio.Task[dict[str, Any]],
    ) -> tuple[dict[str, Any] | None, str | None]:
        """Await the shared probe with a bounded timeout.

        Returns ``(result, fallback_source)``. When ``result`` is not None the
        caller inspects n_ctx. When ``result`` is None, ``fallback_source`` is
        ``"configured_timeout"`` (timeout) or ``"configured_error"`` (exception).
        """
        try:
            return await asyncio.wait_for(asyncio.shield(shared), timeout=self._timeout_s), None
        except TimeoutError:
            return None, "configured_timeout"
        except Exception:  # noqa: BLE001 — any probe error deteriorates gracefully
            return None, "configured_error"

    def _context_from_probe(
        self,
        model_key: str,
        entry: Any,
        configured_window: int,
        probe_result: dict[str, Any] | None,
        fallback_source: str | None,
    ) -> ResolvedDriverContext:
        if probe_result is None:
            return self._fallback_configured(
                model_key, entry, configured_window, fallback_source or "configured_error"
            )
        n_ctx, probe_miss, zero_miss = _probe_n_ctx(probe_result)
        if type(n_ctx) is int and n_ctx > 0:
            return self._make_context(model_key, entry, n_ctx, "live")
        source = "configured_miss" if probe_miss or zero_miss else "configured_error"
        return self._fallback_configured(model_key, entry, configured_window, source)

    def _make_context(
        self,
        model_key: str,
        entry: Any,
        window: int,
        source: str,
    ) -> ResolvedDriverContext:
        return ResolvedDriverContext(
            model_key=model_key,
            provider=entry.provider,
            model_id=entry.model_id,
            context_window=window,
            source=source,
            resolved_at=datetime.now(UTC),
        )

    def _fallback_configured(
        self,
        model_key: str,
        entry: Any,
        configured_window: int,
        source: str,
    ) -> ResolvedDriverContext:
        if configured_window <= 0:
            raise DriverContextResolutionError(
                model_key=model_key,
                provider=entry.provider,
                reason=(
                    f"probe yielded no positive n_ctx ({source}) and the configured "
                    f"context_window is non-positive ({configured_window})"
                ),
            )
        return self._make_context(model_key, entry, configured_window, source)

    def _cleanup(self, key: tuple[str, str], task: asyncio.Task[Any]) -> None:
        """Identity-safe done-callback: remove only the exact task that completed."""
        current = self._in_flight.get(key)
        if current is task:
            del self._in_flight[key]
