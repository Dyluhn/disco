"""Prompt and evidence digest formatting for the research loop."""

from __future__ import annotations

import json
from typing import Any
from urllib.parse import urlparse

from ..models import Passage
from .depth import DepthBound

GIST_CHARS = 200
FRESH_GIST_CHARS = 600
WRAP_UP_FRACTION = 0.25


def normalize_query(query: str) -> str:
    return " ".join(query.casefold().split())


def query_history(state: Any) -> list[dict[str, Any]]:
    keys = ("turn", "query", "admitted", "result", "yield_reason", "error", "rejected")
    return [
        {key: entry[key] for key in keys if key in entry}
        for entry in state.trail
        if entry.get("kind") in {"search", "query_rejected"}
    ]


def upstream_zero_yield_warning(state: Any) -> str:
    streak = 0
    summaries = [entry for entry in state.trail if entry.get("kind") == "search_turn_summary"]
    for summary in reversed(summaries):
        if summary.get("queries", 0) <= 0 or summary.get("new_admitted", 0) != 0:
            break
        streak += 1
    if streak < 2:
        return ""
    return (
        "Two distinct research turns chasing an upstream lead yielded no new admitted evidence. "
        "Stop paraphrasing or retrying that named publisher/report; mark the claim or angle "
        "unresolved or single-source if appropriate, and pivot to a different authoritative "
        "class (primary paper, filing, regulator, official documentation, or independent "
        "reporting). If the host evidence floors are already met, proceed toward writing."
    )


def fresh_queries(state: Any, queries: tuple[str, ...]) -> tuple[list[str], list[str]]:
    seen = {
        normalize_query(str(entry["query"]))
        for entry in state.trail
        if entry.get("kind") in {"search", "query_rejected"} and entry.get("query")
    }
    fresh: list[str] = []
    repeated: list[str] = []
    for query in queries:
        normalized = normalize_query(query)
        if not normalized or normalized in seen:
            repeated.append(query)
            continue
        seen.add(normalized)
        fresh.append(query)
    return fresh, repeated


def _domain(url: str) -> str:
    return (urlparse(url).netloc if url else "") or "(user-provided)"


def _digest_lines(passages: list[Passage], fresh_ids: set[str]) -> list[str]:
    lines: list[str] = []
    for passage in passages:
        date = passage.published_at.isoformat() if passage.published_at else "date unknown"
        title = (passage.source_title or passage.source_url or passage.id)[:120]
        lines.append(f"  [{passage.id}] {title} — {passage.source_url} ({date})")
        limit = FRESH_GIST_CHARS if passage.id in fresh_ids else GIST_CHARS
        lines.append(f"    {' '.join(passage.text.split())[:limit]}")
    return lines


def evidence_digest(pool: list[Passage], fresh_ids: set[str]) -> str:
    if not pool:
        return "EVIDENCE POOL: empty — nothing admitted yet."
    by_domain: dict[str, list[Passage]] = {}
    for passage in pool:
        by_domain.setdefault(_domain(passage.source_url), []).append(passage)
    lines = [f"EVIDENCE POOL ({len(pool)} sources, grouped by domain):"]
    for domain in sorted(by_domain):
        lines.append(f"{domain}:")
        lines.extend(_digest_lines(by_domain[domain], fresh_ids))
    return "\n".join(lines)


def budget_line(
    remaining_s: float, total_s: float, bound: DepthBound, slots: int, searches: int
) -> str:
    line = (
        f"BUDGET: {int(remaining_s)}s research time remaining of {int(total_s)}s; "
        f"{slots} of {bound.max_sources} source slots remaining; "
        f"{searches} searches issued so far."
    )
    if total_s > 0 and remaining_s < WRAP_UP_FRACTION * total_s:
        line += (
            "\nWRAP-UP WARNING: less than 25% of the research budget remains — "
            "finish your current leads and mark ready_to_write only when the host "
            "readiness floor is satisfied."
        )
    return line


def depth_directive(bound: DepthBound, turn: int) -> str:
    if bound.retrieval_depth == "shallow":
        return (
            "DEPTH GUIDANCE (quick): keep the map broad, then perform one targeted "
            "counterevidence check and a brief primary-source/upstream audit before wrap-up."
        )
    if bound.retrieval_depth == "deep":
        if turn and turn % 2 == 0:
            return (
                "DEPTH GUIDANCE (exhaustive): refresh the coverage map now; pursue a "
                "missing angle or minority view, trace load-bearing claims upstream, "
                "and seek independent counterevidence before deciding readiness."
            )
        return (
            "DEPTH GUIDANCE (exhaustive): broaden the map, include minority viewpoints, "
            "and treat every named study, benchmark, filing, or dataset as an upstream lead."
        )
    if turn and turn % 2 == 0:
        return (
            "DEPTH GUIDANCE (standard): refresh coverage and audit load-bearing secondary "
            "claims to their original sources; use the next search for an independent "
            "counterexample where a claim is surprising or disputed."
        )
    return (
        "DEPTH GUIDANCE (standard): map major angles first, then follow named sources "
        "upstream and reserve a targeted search for criticism or negative results."
    )


def turn_user_message(
    query: str,
    state: Any,
    steers: list[str],
    *,
    remaining_s: float,
    total_s: float,
    bound: DepthBound,
) -> str:
    parts = [f"QUESTION: {query}"]
    if state.brief:
        parts.append(f"YOUR BRIEF: {state.brief}")
    if state.decision_summary:
        parts.append(f"YOUR LAST DECISION: {state.decision_summary}")
    if steers:
        parts.append(
            "USER STEER (priority user guidance — act on these in your next searches):\n"
            + "\n".join(f"- {steer}" for steer in steers)
        )
    parts.append(evidence_digest(state.pool, state.last_admitted))
    parts.append(
        "MODEL-OWNED COVERAGE STATE:\n"
        + json.dumps(state.coverage, ensure_ascii=False, sort_keys=True)
    )
    history = query_history(state)
    parts.append(
        "FULL QUERY OUTCOME HISTORY:\n"
        + (json.dumps(history, ensure_ascii=False) if history else "(none yet)")
    )
    if state.feedback:
        parts.append(f"HOST FEEDBACK — correct this on the next turn: {state.feedback}")
    parts.append(depth_directive(bound, state.turns_completed))
    parts.append(budget_line(remaining_s, total_s, bound, state.budget.remaining, state.searches))
    return "\n\n".join(parts)
