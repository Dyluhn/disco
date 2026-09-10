"""Receipt authentication and selection owners for resource context.

This module is the single bounded implementation owner for the pure
revision/coverage accounting that turns the append-only event log into trusted
prompt evidence.  The former ``resource_context`` module re-exports these
symbols so every historical import path, signature, receipt, and
context/resource result is preserved.

The module deliberately distinguishes three facts that the prior context
pipeline conflated:

* a resource revision existed on disk;
* some range was delivered in a historical turn; and
* the exact range is physically present in the request being assembled now.

Only the third fact permits a read body to be replaced by a pointer.  The event
log remains append-only; these helpers select the smallest lossless set of read
observations for rendering and never mutate persisted events.
"""

from __future__ import annotations

import hashlib
import posixpath
from collections import defaultdict
from collections.abc import Iterable
from dataclasses import dataclass

from ..effects import (
    CoverageSpan,
    CoverageUnit,
    EffectCapability,
    ObservationReceipt,
    ResourceCoverage,
    ResourceKey,
    ResourceRevision,
)
from ..events import ActionEvent, Event, EventSource, ObservationEvent
from ..workspace_paths import strip_redundant_workspace_prefix

WORKSPACE_FILE_NAMESPACE = "workspace.file"

# Exact historical bodies are a short-term working set, not an unbounded second
# filesystem.  Beyond this envelope the durable workspace remains the recovery
# surface and the rendered prompt must say which paths are omitted.  A single
# ordinary source file still fits comfortably; many unrelated reads cannot
# silently consume the entire provider window forever.
PROMPT_READ_MAX_RESOURCES = 8
PROMPT_READ_MAX_RENDERED_BYTES = 32 * 1024


def canonical_workspace_identifier(path: str) -> str:
    """Return the canonical workspace-relative identity used by context truth."""

    return posixpath.normpath(strip_redundant_workspace_prefix(path))


def workspace_file_key(path: str) -> ResourceKey:
    """Build a canonical resource key for one workspace file."""

    return ResourceKey(
        namespace=WORKSPACE_FILE_NAMESPACE,
        identifier=canonical_workspace_identifier(path),
    )


@dataclass(frozen=True)
class ReadReceiptRecord:
    """One persisted file-read observation and its provider-pair identity."""

    call_id: str
    action_id: str
    action_seq: int | None
    observation_seq: int | None
    receipt: ObservationReceipt


def _workspace_read_receipts(event: ObservationEvent) -> tuple[ObservationReceipt, ...]:
    """Exact workspace-content receipts carried by one observation."""

    return tuple(
        receipt
        for receipt in event.tool_result.effect_receipts
        if isinstance(receipt, ObservationReceipt)
        and receipt.capability == EffectCapability.WORKSPACE_CONTENT_READ
        and receipt.revision.resource.namespace == WORKSPACE_FILE_NAMESPACE
    )


def _index_agent_actions(
    events: tuple[Event, ...],
) -> tuple[dict[str, ActionEvent], set[str], dict[str, set[str]]]:
    """Index AGENT ActionEvents by id and by tool_call.call_id.

    Returns ``(actions_by_id, ambiguous_action_ids, action_ids_by_call)``.
    A duplicate action id is ambiguous and authenticates no receipt.
    """
    actions_by_id: dict[str, ActionEvent] = {}
    ambiguous_action_ids: set[str] = set()
    action_ids_by_call: dict[str, set[str]] = defaultdict(set)
    for event in events:
        if not isinstance(event, ActionEvent) or event.source != EventSource.AGENT:
            continue
        if event.id in actions_by_id:
            ambiguous_action_ids.add(event.id)
        else:
            actions_by_id[event.id] = event
        action_ids_by_call[event.tool_call.call_id].add(event.id)
    return actions_by_id, ambiguous_action_ids, action_ids_by_call


def _index_observations(
    events: tuple[Event, ...],
) -> tuple[dict[str, int], dict[str, int]]:
    """Count ENVIRONMENT ObservationEvents per action_id and per call_id.

    Returns ``(observation_count_by_action, observation_count_by_call)``.
    A cardinality != 1 on either key makes the pair ambiguous.
    """
    observation_count_by_action: dict[str, int] = defaultdict(int)
    observation_count_by_call: dict[str, int] = defaultdict(int)
    for event in events:
        if not isinstance(event, ObservationEvent) or event.source != EventSource.ENVIRONMENT:
            continue
        observation_count_by_action[event.action_id] += 1
        observation_count_by_call[event.tool_result.call_id] += 1
    return observation_count_by_action, observation_count_by_call


def _ambiguous_call_ids(action_ids_by_call: dict[str, set[str]]) -> set[str]:
    return {call_id for call_id, action_ids in action_ids_by_call.items() if len(action_ids) != 1}


def _ambiguous_observation_keys(
    observation_count_by_action: dict[str, int],
    observation_count_by_call: dict[str, int],
) -> tuple[set[str], set[str]]:
    ambiguous_observation_actions = {
        action_id for action_id, count in observation_count_by_action.items() if count != 1
    }
    ambiguous_observation_calls = {
        call_id for call_id, count in observation_count_by_call.items() if count != 1
    }
    return ambiguous_observation_actions, ambiguous_observation_calls


def _observation_is_trustworthy(
    event: ObservationEvent,
    *,
    ambiguous_action_ids: set[str],
    ambiguous_observation_actions: set[str],
    ambiguous_call_ids: set[str],
    ambiguous_observation_calls: set[str],
) -> bool:
    """Whether an observation is a candidate for authenticating a receipt."""
    return (
        event.source == EventSource.ENVIRONMENT
        and event.tool_result.success
        and event.action_id not in ambiguous_action_ids
        and event.action_id not in ambiguous_observation_actions
        and event.tool_result.call_id not in ambiguous_call_ids
        and event.tool_result.call_id not in ambiguous_observation_calls
    )


def _action_matches_observation(action: ActionEvent, event: ObservationEvent) -> bool:
    """Whether a candidate action is the exact pair of the observation."""
    return (
        action.tool_call.call_id == event.tool_result.call_id
        and action.tool_call.tool_name == event.tool_result.tool_name
        and not (action.seq is not None and event.seq is not None and action.seq >= event.seq)
    )


def read_receipt_records(events: Iterable[Event]) -> tuple[ReadReceiptRecord, ...]:
    """Return authenticated read receipts paired to their exact agent actions.

    A free-floating observation, mismatched call id/tool, failed result, or
    model-authored payload is ignored.  Receipts become trusted prompt evidence
    only through a canonical ActionEvent/ObservationEvent pair.
    """

    event_list = tuple(events)
    actions_by_id, ambiguous_action_ids, action_ids_by_call = _index_agent_actions(event_list)
    ambiguous_call_ids = _ambiguous_call_ids(action_ids_by_call)
    observation_count_by_action, observation_count_by_call = _index_observations(event_list)
    ambiguous_observation_actions, ambiguous_observation_calls = _ambiguous_observation_keys(
        observation_count_by_action, observation_count_by_call
    )

    out: list[ReadReceiptRecord] = []
    for event in event_list:
        if not isinstance(event, ObservationEvent):
            continue
        if not _observation_is_trustworthy(
            event,
            ambiguous_action_ids=ambiguous_action_ids,
            ambiguous_observation_actions=ambiguous_observation_actions,
            ambiguous_call_ids=ambiguous_call_ids,
            ambiguous_observation_calls=ambiguous_observation_calls,
        ):
            continue
        action = actions_by_id.get(event.action_id)
        if action is None or not _action_matches_observation(action, event):
            continue
        receipts = _workspace_read_receipts(event)
        if len(receipts) != 1:
            # One file_read action has one rendered body. Multiple exact
            # resource claims cannot be mapped onto that body unambiguously.
            continue
        out.append(
            ReadReceiptRecord(
                call_id=event.tool_result.call_id,
                action_id=action.id,
                action_seq=action.seq,
                observation_seq=event.seq,
                receipt=receipts[0],
            )
        )
    return tuple(out)


def merge_spans(spans: Iterable[CoverageSpan]) -> tuple[CoverageSpan, ...]:
    """Return the sorted union of half-open spans, merging overlap/adjacency."""

    ordered = sorted((span.start, span.end) for span in spans)
    if not ordered:
        return ()
    merged: list[tuple[int, int]] = [ordered[0]]
    for start, end in ordered[1:]:
        prior_start, prior_end = merged[-1]
        if start <= prior_end:
            merged[-1] = (prior_start, max(prior_end, end))
        else:
            merged.append((start, end))
    return tuple(CoverageSpan(start=start, end=end) for start, end in merged)


def coverage_is_subset(
    candidate: ResourceCoverage,
    containers: Iterable[ResourceCoverage],
) -> bool:
    """Whether exact compatible container coverage includes ``candidate``."""

    compatible = [
        coverage
        for coverage in containers
        if coverage.unit == candidate.unit and coverage.total == candidate.total
    ]
    if not compatible:
        return False
    if candidate.total == 0 and not candidate.spans:
        return any(coverage.total == 0 for coverage in compatible)
    union = merge_spans(span for coverage in compatible for span in coverage.spans)
    for wanted in candidate.spans:
        if not any(span.start <= wanted.start and span.end >= wanted.end for span in union):
            return False
    return True


def _record_order(record: ReadReceiptRecord) -> tuple[int, int, str]:
    return (
        record.observation_seq if record.observation_seq is not None else -1,
        record.action_seq if record.action_seq is not None else -1,
        record.call_id,
    )


def select_essential_read_records(
    records: Iterable[ReadReceiptRecord],
) -> tuple[ReadReceiptRecord, ...]:
    """Select a lossless minimal read set for each resource's latest revision.

    A later narrow read can never evict an earlier full read.  Without a full
    read, reverse chronological selection keeps each read that adds coverage and
    drops only ranges already present in a later retained observation. Older
    revisions do not remain grounding context forever: the latest authenticated
    observation identifies the active revision, and renderers label older bodies
    stale instead of pretending they are equivalent. Units and declared totals
    remain distinct within the active revision.
    """

    record_list = tuple(records)
    latest_digest: dict[ResourceKey, str] = {}
    for record in sorted(record_list, key=_record_order):
        latest_digest[record.receipt.revision.resource] = record.receipt.revision.digest

    grouped: dict[tuple[ResourceKey, str, CoverageUnit, int | None], list[ReadReceiptRecord]] = (
        defaultdict(list)
    )
    for record in record_list:
        receipt = record.receipt
        if latest_digest.get(receipt.revision.resource) != receipt.revision.digest:
            continue
        grouped[
            (
                receipt.revision.resource,
                receipt.revision.digest,
                receipt.coverage.unit,
                receipt.coverage.total,
            )
        ].append(record)

    selected: list[ReadReceiptRecord] = []
    for group in grouped.values():
        ordered = sorted(group, key=_record_order)
        complete = [record for record in ordered if record.receipt.complete]
        if complete:
            selected.append(complete[-1])
            continue

        retained: list[ReadReceiptRecord] = []
        retained_coverage: list[ResourceCoverage] = []
        for record in reversed(ordered):
            if coverage_is_subset(record.receipt.coverage, retained_coverage):
                continue
            retained.append(record)
            retained_coverage.append(record.receipt.coverage)
        selected.extend(reversed(retained))
    return tuple(sorted(selected, key=_record_order))


def select_prompt_read_records(
    records: Iterable[ReadReceiptRecord],
    *,
    max_resources: int = PROMPT_READ_MAX_RESOURCES,
    max_rendered_bytes: int = PROMPT_READ_MAX_RENDERED_BYTES,
) -> tuple[ReadReceiptRecord, ...]:
    """Bound exact historical bodies to the most recently active resources.

    ``select_essential_read_records`` answers the lossless set-theoretic
    question.  This second fold answers the provider-request question: which
    complete resource groups fit in the bounded active working set?  It never
    keeps half of a resource's required ranges and never treats omitted bytes as
    present.  Omitted paths remain recoverable from the workspace snapshot/index.
    """

    record_list = tuple(records)
    essential = select_essential_read_records(record_list)
    grouped: dict[ResourceKey, list[ReadReceiptRecord]] = defaultdict(list)
    latest_order: dict[ResourceKey, tuple[int, int, str]] = {}
    for record in record_list:
        key = record.receipt.revision.resource
        latest_order[key] = max(latest_order.get(key, _record_order(record)), _record_order(record))
    for record in essential:
        grouped[record.receipt.revision.resource].append(record)

    selected: list[ReadReceiptRecord] = []
    used = 0
    selected_resources = 0
    for resource in sorted(grouped, key=lambda key: latest_order[key], reverse=True):
        if selected_resources >= max_resources:
            break
        group = grouped[resource]
        sizes = [record.receipt.rendered_size_bytes for record in group]
        if any(size is None for size in sizes):
            continue
        cost = sum(size for size in sizes if size is not None)
        if cost > max_rendered_bytes - used:
            continue
        selected.extend(group)
        used += cost
        selected_resources += 1
    return tuple(sorted(selected, key=_record_order))


def latest_read_revision_by_resource(
    records: Iterable[ReadReceiptRecord],
) -> dict[ResourceKey, ResourceRevision]:
    """Return the latest authenticated read revision for each resource."""

    latest: dict[ResourceKey, ResourceRevision] = {}
    for record in sorted(records, key=_record_order):
        latest[record.receipt.revision.resource] = record.receipt.revision
    return latest


def essential_read_call_ids(events: Iterable[Event]) -> frozenset[str]:
    """Call ids whose exact read body must survive request assembly."""

    return frozenset(
        record.call_id for record in select_essential_read_records(read_receipt_records(events))
    )


def essential_read_pair_seqs(events: Iterable[Event]) -> frozenset[int]:
    """Action+observation seqs BP-06/condensation must retain as provider pairs."""

    selected = select_prompt_read_records(read_receipt_records(events))
    return frozenset(
        seq
        for record in selected
        for seq in (record.action_seq, record.observation_seq)
        if seq is not None
    )


def snapshot_receipt(
    *,
    path: str,
    sha256: str,
    total_lines: int,
    raw_size_bytes: int,
    rendered_content: str,
) -> ObservationReceipt:
    """Build the exact full-file receipt for a CURRENT WORKSPACE body."""

    coverage = ResourceCoverage(
        unit=CoverageUnit.LINES,
        spans=(CoverageSpan(start=0, end=total_lines),) if total_lines else (),
        total=total_lines,
    )
    rendered_bytes = rendered_content.encode("utf-8")
    return ObservationReceipt(
        capability=EffectCapability.WORKSPACE_CONTENT_READ,
        revision=ResourceRevision(resource=workspace_file_key(path), digest=sha256),
        coverage=coverage,
        complete=True,
        raw_size_bytes=raw_size_bytes,
        rendered_size_bytes=len(rendered_bytes),
        rendered_sha256=hashlib.sha256(rendered_bytes).hexdigest(),
    )


def snapshot_window_receipt(
    *,
    path: str,
    sha256: str,
    raw_size_bytes: int,
    byte_spans: tuple[tuple[int, int], ...],
    rendered_content: str,
) -> ObservationReceipt:
    """Build exact byte-range evidence for a bounded current-file window."""

    rendered_bytes = rendered_content.encode("utf-8")
    return ObservationReceipt(
        capability=EffectCapability.WORKSPACE_CONTENT_READ,
        revision=ResourceRevision(resource=workspace_file_key(path), digest=sha256),
        coverage=ResourceCoverage(
            unit=CoverageUnit.BYTES,
            spans=tuple(CoverageSpan(start=start, end=end) for start, end in byte_spans),
            total=raw_size_bytes,
        ),
        complete=False,
        raw_size_bytes=raw_size_bytes,
        rendered_size_bytes=len(rendered_bytes),
        rendered_sha256=hashlib.sha256(rendered_bytes).hexdigest(),
    )


def receipt_covered_in_current_prompt(
    receipt: ObservationReceipt,
    prompt_receipts: Iterable[ObservationReceipt],
) -> bool:
    """Whether the same revision/range is physically present elsewhere now."""

    compatible = [
        current.coverage
        for current in prompt_receipts
        if current.revision == receipt.revision
        and current.capability == EffectCapability.WORKSPACE_CONTENT_READ
    ]
    return coverage_is_subset(receipt.coverage, compatible)


def rendered_content_matches_receipt(receipt: ObservationReceipt, content: str) -> bool:
    """Whether ``content`` is the exact host-bound rendered observation body."""

    if receipt.rendered_size_bytes is None or receipt.rendered_sha256 is None:
        return False
    rendered = content.encode("utf-8")
    return (
        len(rendered) == receipt.rendered_size_bytes
        and hashlib.sha256(rendered).hexdigest() == receipt.rendered_sha256
    )


def current_prompt_coverage_lines(receipts: Iterable[ObservationReceipt]) -> tuple[str, ...]:
    """Render compact truthful coverage statements for the final request."""

    lines: list[str] = []
    for receipt in sorted(
        receipts,
        key=lambda item: (
            item.revision.resource.namespace,
            item.revision.resource.identifier,
            item.revision.digest,
        ),
    ):
        resource = receipt.revision.resource
        coverage = receipt.coverage
        spans = merge_spans(coverage.spans)
        if coverage.total == 0:
            rendered = "empty file present"
        elif coverage.unit == CoverageUnit.LINES:
            rendered = ", ".join(f"{span.start + 1}-{span.end}" for span in spans)
            rendered = f"exact lines {rendered} present"
        elif coverage.unit == CoverageUnit.BYTES:
            rendered = ", ".join(f"{span.start}-{span.end - 1}" for span in spans)
            rendered = f"exact byte ranges {rendered} present"
        else:
            rendered = ", ".join(f"{span.start}-{span.end - 1}" for span in spans)
            rendered = f"exact {coverage.unit.value} ranges {rendered} present"
        complete = " (complete)" if coverage.covers_total() else ""
        lines.append(
            f"{resource.identifier} @ sha256:{receipt.revision.digest} — {rendered}{complete}"
        )
    return tuple(lines)