"""Aggregation helpers for the multi-source search adapter."""

from __future__ import annotations

from collections.abc import Sequence
from typing import cast

from .models import SearchHit
from .providers import SearchProvider


def provider_row_result(
    provider: SearchProvider, result: object
) -> tuple[list[SearchHit], dict[str, object], bool, str | None]:
    name = str(getattr(provider, "name", type(provider).__name__))[:80]
    if isinstance(result, BaseException):
        return (
            [],
            {
                "provider": name,
                "outcome": "upstream",
                "provider_error": type(result).__name__[:48],
                "result_count": 0,
            },
            True,
            "upstream",
        )
    rows, diagnostic = cast(tuple[list[SearchHit], dict[str, object]], result)
    outcome = (
        str(diagnostic["outcome"])
        if isinstance(diagnostic, dict) and "outcome" in diagnostic
        else None
    )
    return list(rows), diagnostic, outcome is not None and outcome not in {"ok", "empty"}, outcome


def summarize_provider_rows(
    providers: tuple[SearchProvider, ...], gathered: Sequence[object]
) -> tuple[list[list[SearchHit]], dict[str, object]]:
    rows_by_provider: list[list[SearchHit]] = []
    diagnostics: dict[str, object] = {}
    explicit_outcomes: list[str] = []
    failed_providers = 0
    for provider, result in zip(providers, gathered, strict=True):
        rows, diagnostic, failed, outcome = provider_row_result(provider, result)
        name = str(getattr(provider, "name", type(provider).__name__))[:80]
        if diagnostic:
            diagnostics[name] = diagnostic
        if outcome is not None:
            explicit_outcomes.append(outcome)
        failed_providers += failed
        if rows:
            rows_by_provider.append(rows)
    if not explicit_outcomes and not failed_providers:
        return rows_by_provider, {"providers": diagnostics} if diagnostics else {}
    aggregate = provider_aggregate(len(providers), failed_providers, bool(rows_by_provider))
    return rows_by_provider, {"provider_aggregate": aggregate, "providers": diagnostics}


def provider_aggregate(total: int, failed: int, has_rows: bool) -> str:
    if failed and failed == total:
        return "all_failed"
    if failed:
        return "partial_outage"
    return "success" if has_rows else "empty"
