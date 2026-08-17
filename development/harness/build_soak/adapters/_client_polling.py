"""Bounded client progress polling extracted behind the disco_api compatibility facade."""

from __future__ import annotations

import asyncio
import sqlite3
import time
from dataclasses import dataclass
from typing import Any

from ..events import (
    KIND_ACTION,
    KIND_AGENT_ERROR,
    KIND_OBSERVATION,
    NormalizationError,
    normalize_events,
    seq_of,
    tool_name_of,
)
from ._api_browser import (
    _payload,
)
from ._api_types import (
    _LOG,
    _PLANNING_DETAIL,
    _WORK_TERMINALS,
    GATE_STATES,
    INACTIVE_TIMEOUT,
    LIVE_THRASH_STOP,
    PAUSED_STATE,
    PROGRESSING_TIMEOUT,
    TERMINAL_STATES,
)
from ._client_base import _ClientBase


def _normalized_durable_events(
    raw_events: list[dict[str, Any]],
) -> list[dict[str, Any]] | None:
    try:
        events = normalize_events(raw_events)
    except (NormalizationError, TypeError, ValueError):
        return None
    identity_fields = ("seq", "id", "kind", "source")
    row_identity = [tuple(event.get(field) for field in identity_fields) for event in raw_events]
    payload_identity = [tuple(event.get(field) for field in identity_fields) for event in events]
    return events if row_identity == payload_identity else None


def _durable_event_shapes_are_valid(events: list[dict[str, Any]]) -> bool:
    relevant = {KIND_ACTION, KIND_OBSERVATION, KIND_AGENT_ERROR}
    for event in events:
        kind = event.get("kind")
        if kind not in relevant:
            continue
        if seq_of(event) < 0:
            return False
        if kind == KIND_ACTION and (event.get("id") is None or tool_name_of(event) is None):
            return False
    return True


def _latest_dangling_action_from(
    events: list[dict[str, Any]],
) -> tuple[str, str] | None:
    latest = next(
        (event for event in reversed(events) if event.get("kind") == KIND_ACTION),
        None,
    )
    if latest is None:
        return None
    action_id = str(latest["id"])
    action_seq = seq_of(latest)
    tool_name = tool_name_of(latest)
    if tool_name is None:
        return None
    paired = any(
        event.get("kind") in {KIND_OBSERVATION, KIND_AGENT_ERROR}
        and event.get("action_id") is not None
        and str(event["action_id"]) == action_id
        and seq_of(event) > action_seq
        for event in events
    )
    return None if paired else (action_id, tool_name)


def _latest_dangling_verifier_from(
    events: list[dict[str, Any]],
) -> tuple[str, str] | None:
    latest = next(
        (event for event in reversed(events) if event.get("kind") == "verifier_started"),
        None,
    )
    if latest is None:
        return None
    started_id = latest.get("id")
    raw_operation = latest.get("operation")
    started_seq = seq_of(latest)
    if (
        not isinstance(started_id, str)
        or not started_id
        or not isinstance(raw_operation, str)
        or latest.get("source") != "system"
        or latest.get("verifier") != "host"
        or started_seq < 0
    ):
        return None
    operation = raw_operation or "host.verify_deliverable"
    paired = any(
        event.get("kind") == "verifier_verdict"
        and event.get("requested_by_event_id") == started_id
        and seq_of(event) > started_seq
        for event in events
    )
    return None if paired else (started_id, operation)


def _latest_inflight_operation_from(
    events: list[dict[str, Any]],
) -> tuple[str, str] | None:
    candidates = tuple(
        candidate
        for candidate in (
            _latest_dangling_action_from(events),
            _latest_dangling_verifier_from(events),
        )
        if candidate is not None
    )
    if not candidates:
        return None
    candidate_ids = {candidate[0] for candidate in candidates}
    latest_id = next(
        str(event["id"])
        for event in reversed(events)
        if event.get("id") is not None and str(event["id"]) in candidate_ids
    )
    return next(candidate for candidate in candidates if candidate[0] == latest_id)


@dataclass
class _PollProgress:
    start: float
    last_progress: float
    marker: tuple[int, int]
    last_status: str | None = None
    seen_active: bool = False
    inflight_operation_id: str | None = None
    inflight_operation_deadline: float = 0.0


async def _live_thrash_stop(
    owner: Any,
    conversation_id: str,
    status: str,
) -> str | None:
    crossed = await owner._sample_live_thrash(
        conversation_id,
        terminal_status=status,
    )
    if not crossed or status in TERMINAL_STATES:
        return None
    try:
        killed = await owner.kill(conversation_id)
        _LOG.error(
            "stopped conversation=%s after live thrash threshold (http=%s)",
            conversation_id,
            killed.get("http_status"),
        )
    except Exception as exc:  # noqa: BLE001 - retain the hard stop
        _LOG.error(
            "failed to kill conversation=%s after live thrash threshold: %s",
            conversation_id,
            type(exc).__name__,
        )
    return LIVE_THRASH_STOP


def _ordinary_poll_stop(
    owner: Any,
    conversation_id: str,
    state: dict[str, Any],
    status: str,
    progress: _PollProgress,
    *,
    stop_on_gate: bool,
    min_terminal_seq: int | None,
) -> str | None:
    if stop_on_gate and status in GATE_STATES:
        return status
    if status in _WORK_TERMINALS and owner._terminal_is_new(
        conversation_id,
        min_terminal_seq,
    ):
        return status
    if status == PAUSED_STATE:
        return status
    if status != "IDLE":
        progress.seen_active = True
        return None
    parked = progress.seen_active or owner._idle_parked_durably(
        conversation_id,
        state,
    )
    if parked and owner._terminal_is_new(conversation_id, min_terminal_seq):
        return status
    return None


def _record_poll_progress(
    owner: Any,
    conversation_id: str,
    status: str,
    now: float,
    progress: _PollProgress,
) -> None:
    marker = owner._progress_marker(conversation_id)
    if marker == progress.marker and status == progress.last_status:
        return
    progress.marker = marker
    progress.last_status = status
    progress.last_progress = now


def _record_running_work(
    owner: Any,
    conversation_id: str,
    now: float,
    progress: _PollProgress,
) -> None:
    inflight_state = owner._durable_inflight_state(conversation_id)
    if inflight_state is not None:
        inflight_marker, inflight = inflight_state
        if inflight_marker != progress.marker:
            progress.marker = inflight_marker
            progress.last_progress = now
        if inflight is None:
            progress.inflight_operation_id = None
            progress.inflight_operation_deadline = 0.0
        else:
            operation_id, operation_name = inflight
            if operation_id != progress.inflight_operation_id:
                progress.inflight_operation_id = operation_id
                progress.inflight_operation_deadline = (
                    now
                    + owner._action_timeout_s(operation_name)
                    + owner._action_result_persistence_grace_s()
                )
    if owner._active_agent_step_request_id(conversation_id) is not None:
        progress.last_progress = now


def _poll_timeout(
    now: float,
    progress: _PollProgress,
    *,
    inactivity_s: float,
    hard_cap_s: float,
) -> str | None:
    inactive_for = now - progress.last_progress
    if now - progress.start >= hard_cap_s:
        return PROGRESSING_TIMEOUT if inactive_for < inactivity_s else INACTIVE_TIMEOUT
    if (
        progress.inflight_operation_id is not None
        and now >= progress.inflight_operation_deadline
    ):
        return INACTIVE_TIMEOUT
    if progress.inflight_operation_id is None and inactive_for >= inactivity_s:
        return INACTIVE_TIMEOUT
    return None


class _PollingMixin(_ClientBase):
    async def get_state(self, conversation_id: str) -> dict[str, Any]:
        status, data = await self._t.get_json(f"/conversations/{conversation_id}/state")
        if status >= 400:
            raise RuntimeError(f"get_state failed: HTTP {status}")
        return data

    @staticmethod
    def _status_of(state: dict[str, Any]) -> str:
        return str(state.get("execution_status") or state.get("status") or "")

    def _progress_marker(self, conversation_id: str) -> tuple[int, int]:
        """A cheap PROGRESS fingerprint for the conversation: (event_count, max_seq) from
        the durable event log. Either advancing means the build is STILL PRODUCING new
        events — the signal the progress-aware wait resets its inactivity timer on (Bug 15).
        A missing/locked DB reads as no-progress (-1) rather than crashing the poll."""
        uri = f"file:{self._db_path}?mode=ro"
        try:
            conn = sqlite3.connect(uri, uri=True)
        except sqlite3.Error:
            return (0, -1)
        try:
            row = conn.execute(
                "SELECT COUNT(*), COALESCE(MAX(seq), -1) FROM events WHERE conversation_id = ?",
                (conversation_id,),
            ).fetchone()
        except sqlite3.Error:
            return (0, -1)
        finally:
            conn.close()
        return (int(row[0]), int(row[1])) if row else (0, -1)

    def _durable_events_state(
        self, conversation_id: str
    ) -> tuple[tuple[int, int], list[dict[str, Any]]] | None:
        """Read one normalized durable progress snapshot, or fail closed.

        The marker and operation pairing are derived from the SAME SQLite read. This
        prevents a completion persisted between separate marker/pairing reads from being
        mistaken for inactivity.
        """
        try:
            raw_events = self._read_events(conversation_id)
        except sqlite3.Error:
            return None
        events = _normalized_durable_events(raw_events)
        if events is None or not _durable_event_shapes_are_valid(events):
            return None
        marker = (
            len(raw_events),
            max((int(event["seq"]) for event in raw_events), default=-1),
        )
        return marker, events

    def _durable_action_state(
        self, conversation_id: str
    ) -> tuple[tuple[int, int], tuple[str, str] | None] | None:
        state = self._durable_events_state(conversation_id)
        if state is None:
            return None
        marker, events = state
        return marker, _latest_dangling_action_from(events)

    def _durable_inflight_state(
        self, conversation_id: str
    ) -> tuple[tuple[int, int], tuple[str, str] | None] | None:
        state = self._durable_events_state(conversation_id)
        if state is None:
            return None
        marker, events = state
        return marker, _latest_inflight_operation_from(events)

    def _latest_dangling_action(self, conversation_id: str) -> tuple[str, str] | None:
        """Return the most recent durable action iff it lacks a later typed response."""
        state = self._durable_action_state(conversation_id)
        return None if state is None else state[1]

    def _latest_dangling_verifier(self, conversation_id: str) -> tuple[str, str] | None:
        """Return the latest host-verifier start iff its exact verdict is absent."""
        state = self._durable_events_state(conversation_id)
        return None if state is None else _latest_dangling_verifier_from(state[1])

    def _active_agent_step_request_id(self, conversation_id: str) -> str | None:
        """Return a current same-conversation model request from trusted inspect state."""

        aggregation = self._inspect_aggregations.get(conversation_id)
        if aggregation is None or aggregation.conversation_id != conversation_id:
            return None
        return aggregation.active_agent_step_request_id()

    def _action_timeout_s(self, tool_name: str) -> float:
        return self._tool_timeout_overrides_s().get(tool_name, self._default_tool_timeout_s())

    async def _poll_progress_aware(
        self,
        conversation_id: str,
        *,
        stop_on_gate: bool,
        inactivity_s: float,
        hard_cap_s: float,
        min_terminal_seq: int | None = None,
    ) -> str:
        """The shared PROGRESS-AWARE terminal wait (Bug 15). Poll GET /state until EITHER:
          (a) a genuine terminal / gate / pause status is reached → return it; OR
          (b) the build goes GENUINELY INACTIVE — no NEW events/status change for
              `inactivity_s`, and no durable action or host verification remains legally
              in flight through its product timeout + persistence grace → return
              INACTIVE_TIMEOUT; OR
          (c) the generous `hard_cap_s` ceiling is hit. If the build was STILL progressing
              within the last inactivity window when the cap hit → PROGRESSING_TIMEOUT
              (inconclusive, NOT a product fail); otherwise → INACTIVE_TIMEOUT.

        A still-actively-progressing build is NEVER cut off by a mere wall-clock elapsing:
        the inactivity timer RESETS whenever the event count / max seq advances OR the status
        transitions. A durable unpaired action or host-verifier start suppresses only the
        ordinary inactivity window; its own bounded deadline and the unchanged hard ceiling
        still end the wait.

        IDLE RACE (live-surfaced): POST /messages KICKS the loop ASYNCHRONOUSLY, so a freshly
        -created conversation reads IDLE for a beat before the kick stamps RUNNING. IDLE is
        terminal for adjudication but ambiguous as a DRIVE-STOP — pre-kick "not started yet"
        vs parked "nothing left". We only stop on IDLE once the run has gone active at least
        once (`seen_active`); a pre-kick IDLE is ignored so we don't bail before it plans."""
        start = time.monotonic()
        progress = _PollProgress(
            start=start,
            last_progress=start,
            marker=self._progress_marker(conversation_id),
        )
        while True:
            state = await self.get_state(conversation_id)
            last = self._status_of(state)
            await self._emit_efficiency_progress(conversation_id, state=last)
            stop = await _live_thrash_stop(self, conversation_id, last)
            if stop is None:
                stop = _ordinary_poll_stop(
                    self,
                    conversation_id,
                    state,
                    last,
                    progress,
                    stop_on_gate=stop_on_gate,
                    min_terminal_seq=min_terminal_seq,
                )
            if stop is not None:
                return stop

            now = time.monotonic()
            _record_poll_progress(self, conversation_id, last, now, progress)
            if last == "RUNNING":
                _record_running_work(self, conversation_id, now, progress)
            else:
                progress.inflight_operation_id = None
                progress.inflight_operation_deadline = 0.0
            timeout = _poll_timeout(
                now,
                progress,
                inactivity_s=inactivity_s,
                hard_cap_s=hard_cap_s,
            )
            if timeout is not None:
                return timeout
            await asyncio.sleep(self._poll)

    async def poll_until_terminal_or_gate(
        self,
        conversation_id: str,
        *,
        inactivity_s: float,
        hard_cap_s: float,
        min_terminal_seq: int | None = None,
    ) -> str:
        """Progress-aware wait that ALSO stops on a GATE the runner must act on (e.g.
        AWAITING_PLAN_APPROVAL). Returns the terminal/gate/pause status, or a Bug-15
        timeout sentinel (INACTIVE_TIMEOUT / PROGRESSING_TIMEOUT). `min_terminal_seq`
        (H1/V2): when set, a terminal whose durable event seq <= it is the STALE
        pre-follow-up terminal and does NOT end the wait — only the follow-up's OWN new
        terminal (seq > min_terminal_seq) does."""
        return await self._poll_progress_aware(
            conversation_id,
            stop_on_gate=True,
            inactivity_s=inactivity_s,
            hard_cap_s=hard_cap_s,
            min_terminal_seq=min_terminal_seq,
        )

    async def poll_until_terminal(
        self,
        conversation_id: str,
        *,
        inactivity_s: float,
        hard_cap_s: float,
        min_terminal_seq: int | None = None,
    ) -> str:
        """Progress-aware wait to a strictly TERMINAL state (no gate stop). Used after a
        plan is approved / for autonomous runs with no interactive gate. `min_terminal_seq`
        (H1/V2): only a terminal whose durable event seq > it ends the wait (the follow-up's
        OWN new terminal), never the stale pre-follow-up one."""
        return await self._poll_progress_aware(
            conversation_id,
            stop_on_gate=False,
            inactivity_s=inactivity_s,
            hard_cap_s=hard_cap_s,
            min_terminal_seq=min_terminal_seq,
        )

    async def wait_until_status_leaves(
        self, conversation_id: str, status: str, *, timeout_s: float
    ) -> str:
        """After acting on a gate (e.g. approving a plan), wait for the status to move
        OFF that gate before driving again — so a not-yet-processed approval isn't
        re-read as the same gate and double-approved. Returns the new status (or the
        unchanged one on timeout)."""
        deadline = time.monotonic() + timeout_s
        last = status
        while time.monotonic() < deadline:
            last = self._status_of(await self.get_state(conversation_id))
            if last != status:
                return last
            await asyncio.sleep(self._poll)
        return last

    def _latest_plan_revision(self, conversation_id: str) -> int:
        """Highest plan-event revision in the durable log (0 when no plan exists yet).
        Mirrors events.latest_plan_revision but reads the DB directly so the adapter
        stays oracle-import-free. A re-plan appends a NEW PlanEvent whose `revision`
        increments, so a bump here is hard evidence a follow-up was PICKED UP + re-planned."""
        best = 0
        for e in self._read_events(conversation_id):
            if e.get("kind") != "plan":
                continue
            rev = _payload(e).get("revision", 1)
            try:
                best = max(best, int(rev))
            except (TypeError, ValueError):
                continue
        return best

    def _planning_reentry_since(self, conversation_id: str, after_seq: int) -> bool:
        """True when a RE-PLAN re-entry status (detail == `planning`) appears at seq >
        after_seq. enter_planning stamps this on a re-plan but NOT on the first build turn
        (a plain RUNNING with no detail) — so it is a genuine pickup-of-follow-up signal,
        not the initial plan. `after_seq` is the baseline max seq captured BEFORE the send."""
        for e in self._read_events(conversation_id):
            if e.get("kind") != "status" or int(e.get("seq", -1)) <= after_seq:
                continue
            if (_payload(e).get("detail") or None) == _PLANNING_DETAIL:
                return True
        return False

    def _progress_event_since(self, conversation_id: str, after_seq: int) -> bool:
        """True when the FIRST genuine NON-USER PROGRESS event appears at seq > after_seq —
        the EVENT-SEQUENCED pickup signal (V2). The engine actually processing the follow-up
        produces a new durable event: an action, an observation, a plan, an agent_error, an
        AGENT/assistant message, or a RUNNING / gate / `planning` status. Detectable EVEN WHEN
        the conversation STATUS stays at the prior terminal (FINISHED) — a follow-up appended
        during run finalization causes NO status change, the exact dead-window V1 missed.

        A bare USER-MESSAGE append (kind=message, source=user) is explicitly NOT progress: the
        append itself bumps the event seq but is not processing — that is precisely the
        stale-terminal red herring this guards against, so it is skipped, never a pickup."""
        for e in self._read_events(conversation_id):
            if int(e.get("seq", -1)) <= after_seq:
                continue
            kind = e.get("kind")
            if kind == "message":
                if (e.get("source") or "") == "user":
                    continue  # the user-message APPEND itself is NOT pickup (the red herring)
                return True  # an agent/assistant message = the engine produced output
            if kind in ("action", "observation", "plan", "agent_error"):
                return True
            if kind == "status":
                p = _payload(e)
                st = p.get("status")
                if (
                    st == "RUNNING"
                    or st in GATE_STATES
                    or (p.get("detail") or None) == _PLANNING_DETAIL
                ):
                    return True
        return False

    def _latest_terminal_seq(self, conversation_id: str) -> int:
        """The seq of the MOST RECENT terminal status event in the durable log (-1 when none).
        Used to require a follow-up's OWN NEW terminal (a terminal EVENT with seq > the
        baseline) before the drive returns — so the STALE pre-follow-up terminal can never be
        mistaken for the follow-up's completion (the piece V1 missed)."""
        best = -1
        for e in self._read_events(conversation_id):
            if e.get("kind") != "status":
                continue
            if _payload(e).get("status") in TERMINAL_STATES:
                best = max(best, int(e.get("seq", -1)))
        return best

    def _terminal_is_new(self, conversation_id: str, min_terminal_seq: int | None) -> bool:
        """Whether a terminal status reached NOW is the follow-up's OWN new terminal — its
        durable terminal event has seq > `min_terminal_seq` — rather than the STALE
        pre-follow-up terminal. None ⇒ no baseline guard (any terminal counts, the default)."""
        if min_terminal_seq is None:
            return True
        return self._latest_terminal_seq(conversation_id) > min_terminal_seq

    def _idle_parked_durably(self, conversation_id: str, state: dict[str, Any]) -> bool:
        """Whether an observed IDLE is a durably PARKED terminal rather than the
        pre-kick beat. `seen_active` is invocation-local, so a poll invocation that
        STARTS only after a kill parked the run (live-surfaced at pilot seed 405414:
        a delayed approve_plan WS ack held the driver past the scenario kill) would
        otherwise wait out the whole inactivity window against a stable IDLE and
        never send the scenario's recovery follow-up. The durable log is the
        authority: a terminal status event within the live state's own seq horizon
        proves this IDLE is parked, not pre-kick. Bounding by the state's `last_seq`
        keeps a fixture DB seeded ahead of its scripted states from short-circuiting
        the genuine pre-kick wait."""
        try:
            last_seq = int(state.get("last_seq") or 0)
        except (TypeError, ValueError):
            return False
        if last_seq <= 0:
            return False
        for e in self._read_events(conversation_id):
            if e.get("kind") != "status" or int(e.get("seq", -1)) > last_seq:
                continue
            if _payload(e).get("status") in TERMINAL_STATES:
                return True
        return False
