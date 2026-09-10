"""Bound the durable evidence envelope of a Deep Research report event.

The report prose is the product.  Retrieval evidence is supporting metadata,
and raw extractor passages can be far larger than their useful citation
excerpts.  SQLite deliberately caps every event at 1 MiB, so a long successful
run must compact that metadata before persistence instead of losing the whole
report at the final append.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

from disco.core import ReportEvent, ResearchCheckpointEvent
from disco.core.store.sqlite import estimated_payload_bytes

from ..url_policy import source_url_key

# Leave generous room for the sequence number and any future envelope fields
# added by the store.  The store's hard cap is 1 MiB; this is a producer target,
# not a second persistence limit.  Sizes here are measured with the store's own
# ruler (`estimated_payload_bytes`) — measuring the real JSON length instead let
# a "fitted" report be rejected at the append, because the gate's conservative
# estimate runs above it.
_REPORT_EVENT_TARGET_BYTES = 960 * 1024
_OPTIONAL_EVIDENCE_HEADROOM_BYTES = 8 * 1024
_CITED_EXCERPT_CHARS = 1_536
_REVIEWED_EXCERPT_CHARS = 768
_DISCOVERY_SNIPPET_CHARS = 256
_SOURCE_TITLE_CHARS = 512


def _payload_bytes(event: ReportEvent | ResearchCheckpointEvent) -> int:
    return estimated_payload_bytes(event.model_dump(mode="json"))


def _truncate(value: Any, limit: int) -> Any:
    if not isinstance(value, str) or len(value) <= limit:
        return value
    return value[: limit - 1].rstrip() + "…"


def _compact_passage(raw: dict[str, Any], *, text_chars: int) -> dict[str, Any]:
    compact = dict(raw)
    compact["text"] = _truncate(compact.get("text", ""), text_chars)
    compact["source_title"] = _truncate(compact.get("source_title", ""), _SOURCE_TITLE_CHARS)
    return compact


def _compact_hit(raw: dict[str, Any]) -> dict[str, Any]:
    compact = dict(raw)
    compact["snippet"] = _truncate(compact.get("snippet", ""), _DISCOVERY_SNIPPET_CHARS)
    compact["title"] = _truncate(compact.get("title", ""), _SOURCE_TITLE_CHARS)
    return compact


def _one_reviewed_passage_per_source(
    passages: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Keep the broadest useful resume corpus when metadata needs compaction."""
    kept: list[dict[str, Any]] = []
    seen_sources: set[str] = set()
    for passage in passages:
        source = str(passage.get("source_url", ""))
        key = source_url_key(source) if source else f"passage:{passage.get('id', '')}"
        if key in seen_sources:
            continue
        seen_sources.add(key)
        kept.append(_compact_passage(passage, text_chars=_REVIEWED_EXCERPT_CHARS))
    return kept


def _interleaved_optional_evidence(
    reviewed: list[dict[str, Any]], hits: list[dict[str, Any]]
) -> Iterator[tuple[str, dict[str, Any]]]:
    """Retain both readable evidence and discovery audit rows under pressure."""
    count = max(len(reviewed), len(hits))
    for index in range(count):
        if index < len(reviewed):
            yield "reviewed", reviewed[index]
        if index < len(hits):
            yield "hit", hits[index]


def _json_item_bytes(value: dict[str, Any]) -> int:
    return estimated_payload_bytes(value) + 1


def fit_report_event(event: ReportEvent) -> ReportEvent:
    """Return a persistable event without changing reader-facing report data.

    Events already below the producer target are returned byte-for-byte at the
    model-field level.  Oversized events retain the complete summary, sections,
    cited ids, verification ledger, and checkpoint probes.  Only raw evidence
    payloads are excerpted; if necessary, optional reviewed/discovery rows are
    retained in stable interleaved order until the budget is full.
    """
    if _payload_bytes(event) <= _REPORT_EVENT_TARGET_BYTES:
        return event

    cited = [
        _compact_passage(passage, text_chars=_CITED_EXCERPT_CHARS) for passage in event.passages
    ]
    reviewed = _one_reviewed_passage_per_source(event.reviewed_passages)
    hits = [_compact_hit(hit) for hit in event.all_hits]
    compact_meta = dict(event.meta)
    compact_meta.update(
        {
            "report_evidence_compacted": True,
            "reviewed_passages_total": len(event.reviewed_passages),
            "all_hits_total": len(event.all_hits),
        }
    )
    compact = event.model_copy(
        update={
            "passages": cited,
            "reviewed_passages": reviewed,
            "all_hits": hits,
            "meta": compact_meta,
        }
    )
    if _payload_bytes(compact) <= _REPORT_EVENT_TARGET_BYTES:
        return compact

    # Cited evidence is not optional: every inline citation must still resolve.
    # Reserve it in full (with bounded excerpts), then spend the remaining
    # envelope on a balanced prefix of resume evidence and discovery audit rows.
    base = compact.model_copy(update={"reviewed_passages": [], "all_hits": []})
    remaining = (
        _REPORT_EVENT_TARGET_BYTES - _OPTIONAL_EVIDENCE_HEADROOM_BYTES - _payload_bytes(base)
    )
    if remaining <= 0:
        return base

    kept_reviewed: list[dict[str, Any]] = []
    kept_hits: list[dict[str, Any]] = []
    for kind, item in _interleaved_optional_evidence(reviewed, hits):
        cost = _json_item_bytes(item)
        if cost > remaining:
            continue
        remaining -= cost
        if kind == "reviewed":
            kept_reviewed.append(item)
        else:
            kept_hits.append(item)
    bounded = base.model_copy(update={"reviewed_passages": kept_reviewed, "all_hits": kept_hits})
    # The estimate above intentionally holds 8 KiB in reserve, but retain a
    # final exact guard so serializer overhead can never leak past the target.
    while _payload_bytes(bounded) > _REPORT_EVENT_TARGET_BYTES:
        if kept_hits:
            kept_hits.pop()
        elif kept_reviewed:
            kept_reviewed.pop()
        else:
            break
        bounded = base.model_copy(
            update={"reviewed_passages": kept_reviewed, "all_hits": kept_hits}
        )
    return bounded


def fit_research_checkpoint_event(
    event: ResearchCheckpointEvent,
) -> ResearchCheckpointEvent:
    """Bound a resumable checkpoint without pretending it is a report."""
    if _payload_bytes(event) <= _REPORT_EVENT_TARGET_BYTES:
        return event

    passages = _one_reviewed_passage_per_source(event.passages)
    hits = [_compact_hit(hit) for hit in event.all_hits]
    compact = event.model_copy(
        update={
            "passages": passages,
            "all_hits": hits,
            "meta": {
                **event.meta,
                "checkpoint_evidence_compacted": True,
                "passages_total": len(event.passages),
                "all_hits_total": len(event.all_hits),
            },
        }
    )
    if _payload_bytes(compact) <= _REPORT_EVENT_TARGET_BYTES:
        return compact

    base = compact.model_copy(update={"passages": [], "all_hits": []})
    remaining = (
        _REPORT_EVENT_TARGET_BYTES - _OPTIONAL_EVIDENCE_HEADROOM_BYTES - _payload_bytes(base)
    )
    if remaining <= 0:
        return base

    kept_passages: list[dict[str, Any]] = []
    kept_hits: list[dict[str, Any]] = []
    for kind, item in _interleaved_optional_evidence(passages, hits):
        cost = _json_item_bytes(item)
        if cost > remaining:
            continue
        remaining -= cost
        if kind == "reviewed":
            kept_passages.append(item)
        else:
            kept_hits.append(item)
    bounded = base.model_copy(update={"passages": kept_passages, "all_hits": kept_hits})
    while _payload_bytes(bounded) > _REPORT_EVENT_TARGET_BYTES:
        if kept_hits:
            kept_hits.pop()
        elif kept_passages:
            kept_passages.pop()
        else:
            break
        bounded = base.model_copy(update={"passages": kept_passages, "all_hits": kept_hits})
    return bounded


__all__ = ["fit_report_event", "fit_research_checkpoint_event"]
