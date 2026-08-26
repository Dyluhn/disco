"""Bounded diagnostics shared by discovery adapters."""

from __future__ import annotations

import asyncio


def search_diagnostic(
    provider: str,
    outcome: str,
    *,
    started: float,
    status_code: int | None = None,
    result_count: int = 0,
    error: BaseException | None = None,
) -> dict[str, object]:
    diagnostic: dict[str, object] = {
        "provider": provider[:80],
        "outcome": outcome,
        "status_code": status_code if isinstance(status_code, int) else None,
        "result_count": max(0, int(result_count)),
        "latency_ms": max(0, int((asyncio.get_running_loop().time() - started) * 1_000)),
    }
    if error is not None:
        diagnostic["provider_error"] = type(error).__name__[:48]
    return diagnostic
