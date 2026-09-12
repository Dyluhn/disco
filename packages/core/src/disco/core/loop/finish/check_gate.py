"""Finish gate: re-run the recorded checks that source changes staled (ledger phase 4).

A check that passed earlier and has not run since the last source change is the one
piece of evidence the agent is most likely to skip. At finish the host runs those
checks itself — as tagged host probes, through the ordinary observation path, so the
ledger records them — and refuses finish while one of them fails on the current
source. One retry absorbs a flake. Three refusals for the same failing set let finish
through with the failure recorded, so the gate is a hard, visible budget rather than a
wall the run can never pass.
"""

from __future__ import annotations

from typing import cast

from ...events import ActionEvent, AgentErrorEvent, Event, EventSource, LLMMessage, MessageEvent, ObservationEvent, ToolCall
from .. import check_ledger
from .common import _FinishGateComponent, signals
from .verify_gate_parts.probe_events import latest_event_for_action

STALE_RERUN_CAP = 8
RERUN_ATTEMPTS = 2  # the run plus one retry for a flake
REFUSAL_CAP = 3
OUTPUT_TAIL = 400


class _StaleChecksGateService(_FinishGateComponent):
    async def stale_checks_gate_passed(self, events: list[Event]) -> bool:
        sandbox = getattr(self._loop.executor, "sandbox", None)
        records = check_ledger.check_records(events)
        stale = _stale_agent_checks(events, records)
        if not stale:
            self._loop._stale_check_refusals = 0
            return True
        actions = check_ledger._actions_by_id(events)
        now = await check_ledger.snapshot(sandbox)
        digest = now.tree_digest if now is not None else None
        failures: list[tuple[check_ledger.CheckStatus, int | None, str]] = []
        for status, record in stale:
            if record.tree_after == digest:
                continue  # the agent's own run is on the current source after all
            probe = _latest_probe_on(records, status.fingerprint, digest)
            if probe is not None and probe.passed:
                continue
            if probe is not None:
                failures.append((status, probe.exit_code, _probe_output(events, probe)))
                continue
            original = actions.get(record.action_id)
            arguments = dict(original.tool_call.arguments) if original and original.tool_call else {}
            arguments["command"] = status.command
            exit_code, output = await self._rerun(arguments)
            if exit_code != 0:
                exit_code, output = await self._rerun(arguments)
            if exit_code != 0:
                failures.append((status, exit_code, output))
        if not failures:
            self._loop._stale_check_refusals = 0
            return True
        refusals = int(getattr(self._loop, "_stale_check_refusals", 0)) + 1
        self._loop._stale_check_refusals = refusals
        await self._loop._emit(_refusal_message(failures, refusals))
        if refusals >= REFUSAL_CAP:
            self._loop._stale_check_refusals = 0
            return True
        return False

    async def _rerun(self, arguments: dict[str, object]) -> tuple[int | None, str]:
        """Run one recorded check as a host probe with the agent's own arguments (the command
        exactly as written plus whatever else the tool took), forced past the memo."""
        action = ActionEvent(
            thought="Finish gate: re-running a recorded check that source changes staled",
            tool_call=ToolCall(
                tool_name="shell", arguments={**arguments, check_ledger.FORCE_ARG: True}
            ),
            meta={"verify_probe": True},
        )
        if signals.hard_deny_reason(action) is not None:
            return 0, ""
        action = cast(ActionEvent, await self._loop._emit(action))
        await self._loop._execute_and_observe(action)
        event = await latest_event_for_action(self._loop, action.id)
        exit_code = check_ledger.exit_code_of(
            (event.meta.get("check") or {}) if event is not None else None
        )
        if isinstance(event, ObservationEvent):
            return exit_code, event.tool_result.content or ""
        if isinstance(event, AgentErrorEvent):
            return exit_code if exit_code is not None else 1, event.detail or event.error
        return 1, ""


def _without_probes(events: list[Event]) -> list[Event]:
    """The agent's own view: host probe actions and their results removed."""
    probe_ids = {e.id for e in events if isinstance(e, ActionEvent) and e.meta.get("verify_probe")}
    kept: list[Event] = []
    for event in events:
        if isinstance(event, ActionEvent) and event.id in probe_ids:
            continue
        if isinstance(event, ObservationEvent | AgentErrorEvent) and event.action_id in probe_ids:
            continue
        kept.append(event)
    return kept


def _stale_agent_checks(
    events: list[Event], records: list[check_ledger.CheckRecord]
) -> list[tuple[check_ledger.CheckStatus, check_ledger.CheckRecord]]:
    """Stale, previously passing one-shot checks that can depend on the source, judged by
    the agent's own latest run. Setup and discovery commands stay stale in the table but
    are not re-run (check_ledger.verifies_source)."""
    latest_agent = {r.fingerprint: r for r in records if not r.probe}
    written = check_ledger.written_paths(events)
    out = [
        (status, latest_agent[status.fingerprint])
        for status in check_ledger.check_statuses(_without_probes(events), limit=None)
        if status.state == "stale"
        and status.exit_code == 0
        and status.tool == "shell"
        and status.fingerprint in latest_agent
        and check_ledger.verifies_source(status.command, written)
    ]
    return out[-STALE_RERUN_CAP:]


def _latest_probe_on(
    records: list[check_ledger.CheckRecord], fingerprint: str, digest: str | None
) -> check_ledger.CheckRecord | None:
    if digest is None:
        return None
    for record in reversed(records):
        if record.probe and record.fingerprint == fingerprint and record.tree_before == digest:
            return record
    return None


def _probe_output(events: list[Event], probe: check_ledger.CheckRecord) -> str:
    for event in events:
        if event.seq != probe.seq:
            continue
        if isinstance(event, ObservationEvent):
            return event.tool_result.content or ""
        if isinstance(event, AgentErrorEvent):
            return event.detail or event.error
    return ""


def _refusal_message(
    failures: list[tuple[check_ledger.CheckStatus, int | None, str]], refusals: int
) -> MessageEvent:
    lines = [
        "<system-reminder>",
        f"[finish gate] {len(failures)} recorded check{'s' if len(failures) != 1 else ''} that "
        "passed earlier fail on the current source (each was run twice just now):",
    ]
    for status, exit_code, output in failures:
        tail = (output or "").strip()[-OUTPUT_TAIL:]
        lines.append(f"- `{status.label}` passed at step {status.seq}; now exit {exit_code}.")
        if tail:
            lines.append("  " + tail.replace("\n", "\n  "))
    if refusals >= REFUSAL_CAP:
        lines.append(
            f"This is refusal {refusals} of {REFUSAL_CAP} for these checks, so finish proceeds "
            "with the failures recorded; the finish summary should say they fail."
        )
    else:
        lines.append(
            f"Finish refused ({refusals} of {REFUSAL_CAP}). Fix the code or the check, then call "
            "`finish` again; checks that pass are current in the Checks section."
        )
    lines.append("</system-reminder>")
    return MessageEvent(
        source=EventSource.ENVIRONMENT,
        message=LLMMessage(role="user", content="\n".join(lines)),
        meta={"diagnostic": "stale_checks_gate"},
    )

