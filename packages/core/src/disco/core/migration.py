"""Schema evolution — event-state-contract.md §4.1.

Every reader runs a persisted event dict through `migrate_event` *before*
validation, so old events stay readable forever as schemas evolve (invariant
#4). Migrations are forward-only and never lose information; an event is never
rewritten in the store (append-only, invariant #1) — it is upgraded on read.
"""

from __future__ import annotations

from typing import Any


def migrate_event(raw: dict[str, Any]) -> dict[str, Any]:
    """Upgrade a persisted event dict to the current SCHEMA_VERSION before
    validation. Pure, idempotent, append-only migrations.

    [CONTRACT] Every reader runs raw dicts through this before Event validation.

    No migrations exist at v1; the structure shows where future ones slot in::

        v = raw.get("schema_version", 1)
        if v < 2:
            raw = _v1_to_v2(raw)
            v = 2
        # ... and so on, each step forward-only and lossless.
        return raw
    """
    # Defensive copy so callers' inputs are never mutated (purity).
    raw = dict(raw)
    _ = raw.get("schema_version", 1)
    return raw
