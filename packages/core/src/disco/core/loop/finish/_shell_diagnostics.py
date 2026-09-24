"""Historical shell diagnostics, never replay permission or acceptance authority."""

from __future__ import annotations

from ...events import (
    ConversationStatus,
    Event,
    MessageEvent,
    StatusEvent,
    WorkspaceMutationEvent,
    latest_workspace_run_intent,
)
from .. import check_ledger


def _run_events(events: list[Event]) -> list[Event]:
    intent = latest_workspace_run_intent(events)
    floor = intent.seq or 0 if intent is not None else 0
    for event in events:
        if isinstance(event, StatusEvent) and event.status in {
            ConversationStatus.FINISHED,
            ConversationStatus.ERROR,
        }:
            floor = max(floor, event.seq or 0)
    return [event for event in events if (event.seq or 0) > floor]


def _record_state(record: check_ledger.CheckRecord, statuses: dict, events: list[Event]) -> str:
    status = statuses.get(record.fingerprint)
    state = status.state if status is not None else "inconclusive; source snapshot unavailable"
    observation = next((e for e in events if e.seq == record.seq), None)
    raw = observation.meta.get("check", {}) if observation is not None else {}
    if isinstance(raw, dict) and "memo_of" in raw:
        return "historical cached result; execution unverified"
    if any(
        isinstance(e, WorkspaceMutationEvent)
        and (e.seq or 0) > record.seq
        and e.operation != "agent.view-admitted"
        for e in events
    ):
        return "stale after workspace/runtime change"
    return state


def _diagnostics(events: list[Event]) -> list[str]:
    scoped = _run_events(events)
    written = check_ledger.written_paths(scoped)
    actions = check_ledger._actions_by_id(scoped)
    records = {record.fingerprint: record for record in check_ledger.check_records(scoped)}
    statuses = {
        status.fingerprint: status for status in check_ledger.check_statuses(scoped, limit=None)
    }
    rows: list[str] = []
    for record in records.values():
        action = actions.get(record.action_id)
        command = str(action.tool_call.arguments.get("command", "")) if action is not None else ""
        if not check_ledger.verifies_source(command, written):
            continue
        # A known source-mutating command is work, not a purported stable check.
        if (
            record.tree_before is not None
            and record.tree_after is not None
            and record.tree_before != record.tree_after
        ):
            continue
        state = _record_state(record, statuses, scoped)
        if state != "current":
            label = check_ledger.command_label(command)
            rows.append(f"- {state}: shell exit {record.exit_code} at step {record.seq}: `{label}`")
    return rows


def _shell_caveat(events: list[Event]) -> str | None:
    rows = _diagnostics(events)
    if not rows:
        return None
    shown = rows[-8:]
    remaining = len(rows) - len(shown)
    return "\n".join(
        [
            "⚠ Shell diagnostics remain unresolved; this is not a claim that all tests passed.",
            *shown,
            *(
                [f"{remaining} additional historical diagnostic(s) remain unresolved."]
                if remaining
                else []
            ),
            "Historical commands were not replayed. Shell exits are advisory observations; "
            "even exit 0 does not independently verify assertions inside a script or pipeline. "
            "Required host acceptance checks retain their own results and authority.",
        ]
    )


def _terminal_shell_content(
    events: list[Event], content: str, *, host_warning: bool
) -> tuple[str, bool]:
    caveat = _shell_caveat(events)
    if caveat is None:
        return content, False
    # Preserve an existing host-owned failure warning, but never let an earlier
    # model-authored "all tests passed" summary override unresolved observations.
    return (f"{content}\n\n{caveat}" if host_warning else caveat), True


def _already_reported(events: list[Event], content: str) -> bool:
    return any(
        isinstance(event, MessageEvent)
        and event.meta.get("diagnostic") == "stale_checks_gate"
        and event.message.content == content
        for event in _run_events(events)
    )
