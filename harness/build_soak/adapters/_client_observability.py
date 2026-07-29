"""Bounded client observability extracted behind the disco_api compatibility facade."""

from __future__ import annotations

import asyncio
import json
import time
from typing import Any

from ..efficiency import LiveEfficiencyProgress, efficiency_record
from ..events import (
    NormalizationError,
    normalize_events,
)
from ..oracles.thrash import ThrashOracle
from ._api_inspect import _InspectTraceAggregation
from ._api_inspect_validation import _live_thrash_finding_is_current
from ._api_types import (
    _LOG,
    PAUSED_STATE,
    TERMINAL_STATES,
)
from ._client_base import _ClientBase


class _ObservabilityMixin(_ClientBase):
    def enable_efficiency_progress(
        self, progress: LiveEfficiencyProgress, *, ledger_path: str | None = None
    ) -> None:
        """Attach a bounded live work-cost readout to the terminal wait.

        Observability only: it reads the evidence the poll loop already writes
        (durable events + the in-memory inspect trace + the append-only provider
        ledger) and makes ZERO provider calls. Sampling is rate-limited here and
        emission is rate-limited again inside the progress object, so a
        sub-second poll cannot flood the log.
        """
        self._efficiency_progress = progress
        self._efficiency_last_sample = 0.0
        self._efficiency_started_at = time.monotonic()
        self._efficiency_ledger_path = ledger_path
        self._efficiency_ledger_offset = 0
        self._efficiency_ledger_calls = 0

    def _live_provider_calls(self, conversation_id: str) -> int | None:
        """Conversation-bound provider calls so far, read INCREMENTALLY.

        The ledger is append-only and shared by every concurrent worker, so it
        grows without bound; re-parsing it whole on each sample would make the
        readout more expensive than the run. Only the bytes appended since the
        last sample are parsed, and the running total is kept here.

        Counts ONLY records carrying this conversation's id. Unscoped records are
        deliberately excluded: under concurrency they may belong to another
        worker, and a live readout must not overstate what this run cost.
        """
        path = self._efficiency_ledger_path
        if not path:
            return None
        try:
            with open(path, "rb") as handle:
                handle.seek(self._efficiency_ledger_offset)
                chunk = handle.read()
        except OSError:
            return self._efficiency_ledger_calls or None
        # Stop at the last COMPLETE line. A record still being appended would
        # otherwise be half-parsed now and skipped forever after, permanently
        # under-counting; leaving it unconsumed picks it up on the next sample.
        cut = chunk.rfind(b"\n") + 1
        if cut <= 0:
            return self._efficiency_ledger_calls or None
        self._efficiency_ledger_offset += cut
        for raw in chunk[:cut].decode("utf-8", errors="replace").splitlines():
            line = raw.strip()
            if not line:
                continue
            try:
                record = json.loads(line)
            except ValueError:
                continue
            if isinstance(record, dict) and record.get("conversation_id") == conversation_id:
                self._efficiency_ledger_calls += 1
        return self._efficiency_ledger_calls

    async def _emit_efficiency_progress(self, conversation_id: str, *, state: str) -> None:
        progress = self._efficiency_progress
        if progress is None:
            return
        now = time.monotonic()
        if self._efficiency_last_sample and (
            now - self._efficiency_last_sample < self._efficiency_sample_interval_s
        ):
            return
        started = self._efficiency_started_at or now
        self._efficiency_last_sample = now
        try:
            events = self.collect_events(conversation_id)
            trace = await self.collect_inspect_trace(conversation_id)
        except Exception:  # noqa: BLE001 — a readout must never break the wait
            return
        record = efficiency_record(
            scenario_id="",
            conversation_id=conversation_id,
            events=events,
            trace=trace if isinstance(trace, dict) else None,
        )
        progress.update(
            now=now,
            elapsed_s=now - started,
            actions=record.get("actions"),
            planning_turns=record.get("planning_turns"),
            execution_turns=record.get("execution_turns"),
            provider_calls=self._live_provider_calls(conversation_id),
            compactions=record.get("compactions"),
            repairs=record.get("model_repairs"),
            state=state,
        )

    def enable_live_thrash_monitor(self, scenario: dict[str, Any]) -> None:
        self._live_thrash_scenario = scenario
        self._live_thrash_samples = 0
        self._live_thrash_findings = []
        self._live_thrash_candidate = ""
        self._live_thrash_candidate_count = 0
        self._live_thrash_recorded = set()
        self._live_thrash_last_sample = time.monotonic()

    async def _fetch_inspect_snapshot(self, conversation_id: str) -> tuple[str, object | None]:
        try:
            status, data = await self._t.get_json(f"/api/debug/trace/{conversation_id}")
        except Exception:  # noqa: BLE001 — the aggregate records unavailability
            return "unavailable", None
        if (
            status == 404
            and isinstance(data, dict)
            and set(data) == {"error", "conversation_id"}
            and data.get("error") == "no trace"
            and data.get("conversation_id") == conversation_id
        ):
            return "pretrace", None
        if status >= 400:
            return "unavailable", None
        return "snapshot", data

    async def _sample_inspect_trace(self, conversation_id: str) -> None:
        aggregation = self._inspect_aggregations.get(conversation_id)
        lock = self._inspect_sample_locks.get(conversation_id)
        if aggregation is None or lock is None or aggregation.finalized:
            return
        async with lock:
            if aggregation.finalized:
                return
            disposition, snapshot = await self._fetch_inspect_snapshot(conversation_id)
            if disposition == "pretrace":
                if aggregation.accepted_sample_count == 0:
                    aggregation.note_expected_pretrace_absence()
                else:
                    # The exact 404/no-trace response is provisional only before
                    # the first source snapshot. Once a trace existed, its
                    # disappearance is observable restart/loss, not pretrace.
                    aggregation.note_trace_disappeared()
            elif disposition == "unavailable":
                aggregation.note_unavailable()
            else:
                aggregation.add_snapshot(snapshot)

    async def _poll_inspect_trace(self, conversation_id: str) -> None:
        aggregation = self._inspect_aggregations[conversation_id]
        stop = self._inspect_stop_events[conversation_id]
        interval = min(max(self._poll, 0.01), 0.25)
        try:
            while not stop.is_set():
                try:
                    await asyncio.wait_for(stop.wait(), timeout=interval)
                except TimeoutError:
                    await self._sample_inspect_trace(conversation_id)
        except asyncio.CancelledError:
            aggregation.note_poller_cancelled()
            raise
        except Exception:  # noqa: BLE001 — retain evidence and fail continuity closed
            aggregation.note_unavailable()

    async def start_inspect_collection(self, conversation_id: str) -> None:
        """Idempotently begin polling before the user message kicks the model."""

        if conversation_id in self._inspect_aggregations:
            return
        self._inspect_aggregations[conversation_id] = _InspectTraceAggregation(conversation_id)
        self._inspect_stop_events[conversation_id] = asyncio.Event()
        self._inspect_sample_locks[conversation_id] = asyncio.Lock()
        # Take the earliest possible sample synchronously. A not-yet-created trace
        # is provisional: the first real dropped=0 snapshot proves nothing was lost.
        await self._sample_inspect_trace(conversation_id)
        self._inspect_poll_tasks[conversation_id] = asyncio.create_task(
            self._poll_inspect_trace(conversation_id),
            name=f"inspect-aggregate:{conversation_id}",
        )

    async def _finish_inspect_collection(self, conversation_id: str) -> dict[str, Any]:
        aggregation = self._inspect_aggregations[conversation_id]
        if aggregation.finalized:
            return aggregation.render()
        self._inspect_stop_events[conversation_id].set()
        poller = self._inspect_poll_tasks.get(conversation_id)
        if poller is not None:
            try:
                await poller
            except asyncio.CancelledError:
                # An independently cancelled poller taints continuity but never
                # deletes the canonical prefix already retained.
                aggregation.note_poller_cancelled()
        await self._sample_inspect_trace(conversation_id)
        aggregation.finish()
        return aggregation.render()

    async def finish_inspect_collection(self, conversation_id: str) -> dict[str, Any] | None:
        """Idempotently stop, take a final sample, and freeze the aggregate.

        The owned finish task is shielded so caller cancellation cannot replace an
        already-collected prefix with ``None``. A later cleanup caller awaits the
        same task before releasing the conversation.
        """

        if conversation_id not in self._inspect_aggregations:
            return None
        task = self._inspect_finish_tasks.get(conversation_id)
        if task is None:
            task = asyncio.create_task(
                self._finish_inspect_collection(conversation_id),
                name=f"inspect-finalize:{conversation_id}",
            )
            self._inspect_finish_tasks[conversation_id] = task
        return await asyncio.shield(task)

    def note_inspect_stop_unconfirmed(self, conversation_id: str) -> None:
        """Taint continuity when an ACTIVE conversation's stop was not confirmed.

        A stop that cannot be proven quiescent may leave the model emitting
        trace events after the final sample, so the aggregate must not claim
        losslessness.  The retained canonical prefix is preserved.  This is a
        no-op after finalization: a frozen aggregate was completed through the
        stop-first path and its verdict already stands.
        """

        aggregation = self._inspect_aggregations.get(conversation_id)
        if aggregation is not None and not aggregation.finalized:
            aggregation.reasons.add("active_stop_unconfirmed")

    def note_expected_stack_restart(self, conversation_id: str) -> None:
        """Declare the stack restart this run is about to perform.

        The drive knows it is killing the agent-server; the poller only sees the
        endpoint stop answering. Without the declaration the aggregate scores a
        harness-caused outage as evidence loss. Narrow by construction: see
        `_InspectTraceAggregation.note_expected_stack_restart`. A no-op after
        finalization, so a late call can never reopen a closed verdict.
        """

        aggregation = self._inspect_aggregations.get(conversation_id)
        if aggregation is not None and not aggregation.finalized:
            aggregation.note_expected_stack_restart()

    @property
    def live_thrash_monitor(self) -> dict[str, Any]:
        return {
            "enabled": self._live_thrash_scenario is not None,
            "sample_count": self._live_thrash_samples,
            "minimum_confirmation_samples": 2,
            "findings": list(self._live_thrash_findings),
        }

    def observe_live_thrash_snapshot(
        self,
        events: list[dict[str, Any]],
        inspect_trace: dict[str, Any] | None,
        *,
        terminal_status: str = "",
    ) -> bool:
        """Evaluate one in-run snapshot; require two matching live samples.

        A just-committed ActionEvent may not have its ObservationEvent yet. Two
        matching samples prevent that transient prefix from being reported as a
        tool-error finding. At a terminal state the event prefix is stable, so a
        single sample is conclusive.
        """

        scenario = self._live_thrash_scenario
        if scenario is None:
            return False
        self._live_thrash_samples += 1
        try:
            normalized_events = normalize_events(events)
        except NormalizationError as exc:
            # The frozen classifier owns INVALID_RUN adjudication for corrupt
            # evidence. A live sampler must never reinterpret a row-shaped or
            # malformed prefix as a stream of failed actions.
            _LOG.error("live thrash sampler could not normalize events: %s", exc)
            return False
        failed = [
            result.to_dict()
            for result in ThrashOracle().check(
                normalized_events,
                scenario=scenario,
                inspect_trace=inspect_trace,
            )
            if result.failed
        ]
        # k6g F2: a live KILL needs persistent CURRENT no-progress evidence. A
        # finding whose last contributing event precedes trusted progress (a
        # receipt-backed mutation, an approved plan transition, an answered
        # user turn, a typed blocking obligation) is historical: the terminal
        # classifier still adjudicates it on the frozen events, but the monitor
        # must not kill a resumed, progressing run over it.
        failed = [
            result
            for result in failed
            if _live_thrash_finding_is_current(normalized_events, result)
        ]
        if not failed:
            self._live_thrash_candidate = ""
            self._live_thrash_candidate_count = 0
            return False
        fingerprint = json.dumps(failed, sort_keys=True, separators=(",", ":"))
        if fingerprint == self._live_thrash_candidate:
            self._live_thrash_candidate_count += 1
        else:
            self._live_thrash_candidate = fingerprint
            self._live_thrash_candidate_count = 1
        terminal = terminal_status in TERMINAL_STATES or terminal_status == PAUSED_STATE
        if not terminal and self._live_thrash_candidate_count < 2:
            return False
        if fingerprint in self._live_thrash_recorded:
            return False
        self._live_thrash_recorded.add(fingerprint)
        finding = {
            "detected_at_epoch": time.time(),
            "terminal_status": terminal_status or None,
            "event_count": len(normalized_events),
            "max_event_seq": max(
                (int(event.get("seq", -1)) for event in normalized_events), default=-1
            ),
            "confirmation_samples": self._live_thrash_candidate_count,
            "oracle_results": failed,
        }
        self._live_thrash_findings.append(finding)
        _LOG.error(
            "live thrash threshold crossed conversation=%s findings=%s",
            self.last_conversation_id,
            failed,
        )
        return True

    async def _sample_live_thrash(self, conversation_id: str, *, terminal_status: str = "") -> bool:
        if self._live_thrash_scenario is None:
            return False
        now = time.monotonic()
        terminal = terminal_status in TERMINAL_STATES or terminal_status == PAUSED_STATE
        if not terminal and now - self._live_thrash_last_sample < 1.0:
            return False
        self._live_thrash_last_sample = now
        try:
            events = self.collect_events(conversation_id)
            trace = await self.collect_inspect_trace(conversation_id)
        except Exception:  # noqa: BLE001 — final evidence gate catches missing trace
            return False
        return self.observe_live_thrash_snapshot(
            events,
            trace,
            terminal_status=terminal_status,
        )
