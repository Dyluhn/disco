"""The POST `/api/leads` route + in-region Drizzle-insert contract cluster that
`worker_inspect.inspect_worker` delegates to. This is a single cohesive decision
cluster — route presence, public-ness, and in-region parameterized-insert
presence — extracted verbatim (same regexes, same order, same reason strings) so
`inspect_worker` itself stays a thin orchestrator.

Calls back into a handful of pure ``worker_inspect`` helpers (`_route_handler`,
`_post_region`, `_region_has_run_insert`). That import is performed INSIDE the
function body (never at this module's top level) so the reference is
re-resolved on every call — if a test ever monkeypatches one of those names on
`worker_inspect`, this caller observes the patch exactly as a caller still
living in `worker_inspect.py` would.
"""

from __future__ import annotations

import re


def _post_contract_signals(src: str) -> tuple[bool, bool, bool, bool, list[str]]:
    """The POST `/api/leads` route-presence + in-region Drizzle-insert checks.

    Returns ``(has_post_route, post_public, post_region_has_insert,
    insert_parameterized, reasons)`` where ``reasons`` carries every POST-contract
    gap found — in the same order `inspect_worker` used to append them inline: the
    route/public-ness reason (if any), then the insert-presence/parameterization
    reason (if any)."""
    from disco.core.appkit.worker_inspect import (
        _post_region,
        _region_has_run_insert,
        _route_handler,
    )

    reasons: list[str] = []
    post_block = _route_handler(
        src, r'url\.pathname\s*===\s*"/api/leads"\s*&&\s*request\.method\s*===\s*"POST"'
    )
    has_post_route = post_block is not None
    post_public = has_post_route and re.search(r"isAuthorized\s*\(", post_block or "") is None
    # Region-scope the insert proof to the POST handler's in-region code (the block +
    # any helper it calls), so an insert that merely exists ELSEWHERE in the file — or
    # sits in a dead/unreachable position — does not satisfy the POST contract. This is
    # STRUCTURAL presence + non-dead position, NOT a runtime-reachability proof.
    post_region = _post_region(src, post_block) if post_block is not None else ""
    post_region_has_insert, insert_parameterized = _region_has_run_insert(post_region)
    if not has_post_route:
        reasons.append("no public POST /api/leads route")
    elif not post_public:
        reasons.append("POST /api/leads must be public — it is gated behind an auth check")
    if not post_region_has_insert:
        reasons.append(
            "the POST /api/leads handler region does not contain a Drizzle insert in a "
            "reachable (non-dead) position (db.insert(leads).values(...).run() in the "
            "POST handler or a helper it calls, not after an early return / inside a dead branch)"
        )
    elif not insert_parameterized:
        reasons.append(
            "the lead insert is not the generated Drizzle data plane "
            "(db.insert(leads).values(...).run(); raw SQL INSERT strings are not allowed)"
        )
    return has_post_route, post_public, post_region_has_insert, insert_parameterized, reasons
