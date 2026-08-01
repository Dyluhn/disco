"""`VersionRecord` JSON (de)serialization and exact-shape proof.

Leaf module: no dependency on any other `store_parts` sibling.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from ..store import VersionRecord


def _version_to_dict(record: VersionRecord) -> dict[str, Any]:
    return {
        "seq": record.seq,
        "ts": record.ts,
        "label": record.label,
        "trigger": record.trigger,
        "file_count": record.file_count,
        "total_bytes": record.total_bytes,
        "tree_digest": record.tree_digest,
        "pinned": record.pinned,
    }


def _version_from_dict(data: dict[str, Any]) -> VersionRecord:
    from disco.tools.projects import store

    return store.VersionRecord(
        seq=int(data["seq"]),
        ts=str(data["ts"]),
        label=str(data.get("label") or ""),
        trigger=str(data.get("trigger") or ""),
        file_count=int(data.get("file_count") or 0),
        total_bytes=int(data.get("total_bytes") or 0),
        tree_digest=str(data["tree_digest"]),
        pinned=bool(data.get("pinned") or False),
    )


def _payload_matches_record(data: dict[str, Any], record: VersionRecord) -> bool:
    """Exact JSON-shape comparison (``True`` must not compare equal to ``1``)."""

    expected = _version_to_dict(record)
    return data.keys() == expected.keys() and all(
        type(data[key]) is type(value) and data[key] == value for key, value in expected.items()
    )
