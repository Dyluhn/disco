"""Blocked-recovery, mutation-receipt, and plan-authority helpers.

The mutation-receipt and plan-authority rules MOVED to
`disco.core.receipt_currency` at 2026-08-06z (GROUNDED FEEDBACK constraint 2:
"one currency predicate, two consumers"). They are RE-EXPORTED here, never
copied — the loop's W-39 echo and this oracle must decide "is the prior result
still current?" with the same code, or the two windows drift and the drift stays
invisible until a canary fires on the gap between them. That is precisely what
F47 measured, one dimension over, and `_thrash_shell.py` re-exports
`disco.core.script_identity` for the identical reason.

`test_receipt_currency_shared.py` asserts OBJECT IDENTITY between these names and
the core owner's, so a future copy-paste cannot silently reopen the gap.
"""

from __future__ import annotations

from typing import Any

from disco.core.receipt_currency import (
    WORKSPACE_MUTATION_TOOLS as _FILE_MUTATION_TOOLS,
)
from disco.core.receipt_currency import (
    approved_plan_predicate_scope as approved_plan_predicate_scope,
)
from disco.core.receipt_currency import (
    trusted_mutation_receipt_outcome as trusted_mutation_receipt_outcome,
)

from ..events import (
    KIND_MESSAGE,
    KIND_STATUS,
    SRC_USER,
    kind_of,
    seq_of,
)

_RECEIPT_FILE_TOOLS = _FILE_MUTATION_TOOLS


def _validate_blocked_meta(meta: Any, detail: str) -> bool:
    return isinstance(meta, dict) and all(
        (
            meta.get("blocked_landing") is True,
            meta.get("superseded_by_landing") is True,
            meta.get("blocked_reason") == detail,
            meta.get("legacy_detail") == detail,
            meta.get("legacy_status") == "STUCK",
        )
    )


def _validate_landing(landing: dict[str, Any], detail: str) -> bool:
    meta = landing.get("meta")
    return (
        landing.get("status") == "AWAITING_USER_QUESTION"
        and isinstance(meta, dict)
        and all(
            (
                meta.get("blocked_landing") is True,
                meta.get("blocked_reason") == detail,
                meta.get("legacy_detail") == detail,
                meta.get("legacy_status") == "STUCK",
            )
        )
    )


def _subsequent_statuses(
    marker: dict[str, Any], events: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    marker_seq = seq_of(marker)
    return [
        event for event in events if seq_of(event) > marker_seq and kind_of(event) == KIND_STATUS
    ]


def _first_user_seq_after(landing: dict[str, Any], events: list[dict[str, Any]]) -> int | None:
    landing_seq = seq_of(landing)
    return next(
        (
            seq_of(event)
            for event in events
            if seq_of(event) > landing_seq
            and kind_of(event) == KIND_MESSAGE
            and event.get("source") == SRC_USER
        ),
        None,
    )


def _finished_after(user_seq: int, events: list[dict[str, Any]]) -> bool:
    return any(
        seq_of(event) > user_seq
        and kind_of(event) == KIND_STATUS
        and event.get("status") in {"FINISHED", "VERIFIED"}
        for event in events
    )


def recovered_blocked_marker(marker: dict[str, Any], events: list[dict[str, Any]]) -> bool:
    """Whether a historical STUCK marker was explicitly superseded and recovered."""
    detail = str(marker.get("detail") or "")
    if not _validate_blocked_meta(marker.get("meta"), detail):
        return False
    statuses = _subsequent_statuses(marker, events)
    if not statuses or not _validate_landing(statuses[0], detail):
        return False
    user_seq = _first_user_seq_after(statuses[0], events)
    return user_seq is not None and _finished_after(user_seq, events)


# `trusted_mutation_receipt_outcome` and `approved_plan_predicate_scope` are the
# re-exports at the top of this module. Their bodies — and the receipt-shape
# helpers they use — live in `disco.core.receipt_currency`.
