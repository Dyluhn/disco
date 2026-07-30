"""W2 render-time read-collapse projection helpers.

Extracted from ``dedup.py`` as bounded pure collaborators.  The projection
builds the receipt/coverage index that ``collapse_superseded_reads`` uses to
decide which historical reads are superseded, redundant, or stale.  One
implementation owner (this module); ``dedup.py`` imports and delegates.
"""

from __future__ import annotations

from typing import Any

from ..effects import ObservationReceipt
from ..events import (
    ActionEvent,
    Event,
    LLMMessage,
    WorkspaceMutationEvent,
)
from .resource_context import (
    canonical_workspace_identifier,
    latest_read_revision_by_resource,
    read_receipt_records,
    receipt_covered_in_current_prompt,
    rendered_content_matches_receipt,
    select_prompt_read_records,
)

_SUPERSEDED_READ_NOTICE = (
    "[superseded file_read of {path} — current content is in the "
    "CURRENT WORKSPACE block in this prompt]"
)

_SUPERSEDED_READ_NOTICE_NO_PIN = (
    "[historical file_read of {path} — no exact current revision is proven elsewhere; "
    "the original body must remain in the prompt]"
)

_REDUNDANT_READ_NOTICE = (
    "[redundant file_read of {path} — the same resource revision and exact "
    "range are retained elsewhere in this prompt]"
)

_STALE_READ_NOTICE = (
    "[stale file_read of {path} @ sha256:{digest} — a newer authenticated revision "
    "of this resource is retained in this prompt; this body is not current grounding]"
)


def file_read_call_paths(events: list[Event]) -> dict[str, str]:
    """Build call_id → canonical path for file_read actions from the event log."""
    paths: dict[str, str] = {}
    for e in events:
        if (
            isinstance(e, ActionEvent)
            and e.tool_call is not None
            and e.tool_call.tool_name == "file_read"
        ):
            p = e.tool_call.arguments.get("path")
            if isinstance(p, str) and p:
                paths[e.tool_call.call_id] = canonical_workspace_identifier(p)
    return paths


def _latest_run_intent_seq(events: list[Event]) -> int | None:
    return max(
        (
            event.seq
            for event in events
            if isinstance(event, WorkspaceMutationEvent)
            and event.operation.startswith("agent.run-intent.")
            and type(event.seq) is int
        ),
        default=None,
    )


def _active_run_visible_calls(
    records: list[Any], latest_run_intent_seq: int | None
) -> set[str]:
    if latest_run_intent_seq is None:
        return set()
    active = [
        record
        for record in records
        if record.observation_seq is not None
        and record.observation_seq > latest_run_intent_seq
    ]
    selected = {record.call_id for record in select_prompt_read_records(records)}
    visible = {record.call_id for record in active if record.call_id in selected}
    if active:
        latest_active = max(
            active,
            key=lambda record: (
                record.observation_seq if record.observation_seq is not None else -1,
                record.action_seq if record.action_seq is not None else -1,
                record.call_id,
            ),
        )
        visible.add(latest_active.call_id)
    return visible


def _messages_by_call(messages: list[LLMMessage]) -> dict[str, list[LLMMessage]]:
    out: dict[str, list[LLMMessage]] = {}
    for message in messages:
        if message.role == "tool" and message.tool_call_id is not None:
            out.setdefault(message.tool_call_id, []).append(message)
    return out


def _exact_body_is_present(
    record_by_call: dict[str, Any],
    messages_by_call: dict[str, list[LLMMessage]],
    record_call_id: str,
) -> bool:
    record = record_by_call.get(record_call_id)
    candidates = messages_by_call.get(record_call_id, [])
    if record is None or len(candidates) != 1:
        return False
    content = candidates[0].content
    receipt = record.receipt
    if rendered_content_matches_receipt(receipt, content):
        return True
    seq = record.observation_seq
    prefix = ("", "Observation: ", "Output: ")[seq % 3] if seq is not None else ""
    return bool(
        prefix
        and content.startswith(prefix)
        and rendered_content_matches_receipt(receipt, content[len(prefix):])
    )


class _ReadProjection:
    """Builds and holds the receipt/coverage index for collapse_superseded_reads."""

    def __init__(
        self,
        events: list[Event],
        messages: list[LLMMessage],
        prompt_receipts: tuple[ObservationReceipt, ...],
    ) -> None:
        self.file_read_calls = file_read_call_paths(events)
        self.prompt_receipts = prompt_receipts
        if not self.file_read_calls:
            self.records = []
            self.record_by_call = {}
            self.latest_revisions = {}
            self.selected_calls = set()
            self.active_run_visible = set()
            self.physically_present = set()
            self.retained_read_receipts: tuple[ObservationReceipt, ...] = ()
            self.messages_by_call: dict[str, list[LLMMessage]] = {}
            return
        self.records = list(read_receipt_records(events))
        self.record_by_call = {record.call_id: record for record in self.records}
        self.latest_revisions = latest_read_revision_by_resource(self.records)
        for receipt in prompt_receipts:
            self.latest_revisions[receipt.revision.resource] = receipt.revision
        self.selected_calls = {
            record.call_id for record in select_prompt_read_records(self.records)
        }
        self.active_run_visible = _active_run_visible_calls(
            self.records, _latest_run_intent_seq(events)
        )
        self.messages_by_call = _messages_by_call(messages)
        self.physically_present = {
            call_id
            for call_id in self.selected_calls
            if _exact_body_is_present(self.record_by_call, self.messages_by_call, call_id)
        }
        self.retained_read_receipts = (
            *prompt_receipts,
            *(
                record.receipt
                for record in self.records
                if record.call_id in self.selected_calls
                and record.call_id in self.physically_present
            ),
        )


def collapse_one_message(
    msg: LLMMessage,
    projection: _ReadProjection,
) -> LLMMessage:
    """Return the collapsed or original message for a single tool message."""
    if msg.role != "tool" or msg.tool_call_id not in projection.file_read_calls:
        return msg
    path = projection.file_read_calls[msg.tool_call_id]
    record = projection.record_by_call.get(msg.tool_call_id or "")
    if record is None:
        return msg
    current_revision = projection.latest_revisions.get(record.receipt.revision.resource)
    if (
        record.call_id in projection.active_run_visible
        and _exact_body_is_present(
            projection.record_by_call, projection.messages_by_call, record.call_id
        )
        and current_revision == record.receipt.revision
    ):
        return msg
    if receipt_covered_in_current_prompt(record.receipt, projection.prompt_receipts):
        return msg.model_copy(
            update={"content": _SUPERSEDED_READ_NOTICE.format(path=path)}
        )
    latest = projection.latest_revisions.get(record.receipt.revision.resource)
    if latest is not None and latest != record.receipt.revision:
        if any(retained.revision == latest for retained in projection.retained_read_receipts):
            return msg.model_copy(
                update={
                    "content": _STALE_READ_NOTICE.format(
                        path=path,
                        digest=record.receipt.revision.digest[:12],
                    )
                }
            )
        return msg
    if record.call_id in projection.selected_calls:
        return msg
    if receipt_covered_in_current_prompt(record.receipt, projection.retained_read_receipts):
        return msg.model_copy(update={"content": _REDUNDANT_READ_NOTICE.format(path=path)})
    return msg
