"""Schema evolution — event-state-contract.md §4.1.

Every reader runs a persisted event dict through `migrate_event` *before*
validation, so old events stay readable forever as schemas evolve (invariant
#4). Migrations are forward-only and never lose information; an event is never
rewritten in the store (append-only, invariant #1) — it is upgraded on read.
"""

from __future__ import annotations

from typing import Any


class EventMigrationError(ValueError):
    """Typed error raised when a persisted event schema_version is unsupported.

    Event-payload evolution is intentionally separate from database-schema
    evolution in ``disco.core.store.schema``.
    """


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

    A persisted event with an integer ``schema_version`` ABOVE the canonical
    Event SCHEMA_VERSION (defined in ``_event_types``) is rejected with a
    typed ``EventMigrationError`` rather than mis-read under an older shape.
    The Event schema is NOT bumped here; current event bytes are unchanged.
    """
    raw = dict(raw)
    version = raw.get("schema_version", 1)
    # The canonical Event SCHEMA_VERSION lives in _event_types to avoid a
    # circular import. We import it lazily for the same reason.
    from ._event_types import SCHEMA_VERSION as CURRENT_EVENT_SCHEMA_VERSION

    _reject_future_version(version, CURRENT_EVENT_SCHEMA_VERSION)
    _migrate_deliverable(raw)
    return _migrate_stopped_report(raw)


def _reject_future_version(version: object, current: int) -> None:
    if isinstance(version, int) and version > current:
        raise EventMigrationError(
            f"event schema_version {version} is newer than the canonical "
            f"Event SCHEMA_VERSION {current}; the event cannot be read under an older shape"
        )


def _migrate_deliverable(raw: dict[str, Any]) -> None:
    if raw.get("kind") == "deliverable" and "artifact_kind" not in raw:
        raw["artifact_kind"] = "app"


def _migrate_stopped_report(raw: dict[str, Any]) -> dict[str, Any]:
    if not _is_stopped_report(raw):
        return raw
    meta = dict(raw.get("meta") or {})
    meta["legacy_stopped_report_migrated"] = True
    migrated = {
        key: value
        for key, value in raw.items()
        if key in {"id", "source", "timestamp", "schema_version", "seq", "agent_view_id"}
    }
    migrated.update(
        {
            "kind": "research_checkpoint",
            "meta": meta,
            "query": str(raw.get("query", "")),
            "passages": [
                *list(raw.get("passages") or []),
                *list(raw.get("reviewed_passages") or []),
            ],
            "all_hits": list(raw.get("all_hits") or []),
            "trail": _resumed_search_trail(raw),
            "completed_queries": list(raw.get("completed_probes") or []),
            "depth_tier": raw.get("depth_tier"),
            "recency_window": None,
        }
    )
    return migrated


def _is_stopped_report(raw: dict[str, Any]) -> bool:
    return (
        raw.get("kind") == "report"
        and raw.get("bounded_by") == "stopped"
        and not str(raw.get("summary", "")).strip()
        and not raw.get("sections")
    )


def _resumed_search_trail(raw: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        {"kind": "search", "query": query, "resumed": True}
        for query in list(raw.get("completed_probes") or [])
        if isinstance(query, str) and query.strip()
    ]
