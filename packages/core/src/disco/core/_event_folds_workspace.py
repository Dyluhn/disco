"""Workspace folds — run-intent, view-admission, and final-fence projections.

Pure folds over the ordered event log that derive the current workspace
run-intent state, view-admission consistency, action closure, and the final
workspace fence.  These import concrete private event modules, not
``events.py``, so there is exactly one fold implementation.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field

from ._event_control import StatusEvent, WorkspaceMutationEvent
from ._event_interaction import (
    ActionEvent,
    AgentErrorEvent,
    ErrorEvent,
    MessageEvent,
    ObservationEvent,
)
from ._event_outputs import PlanEvent
from ._event_types import BaseEvent, ConversationStatus, EventSource

# The discriminated serialization union remains solely in events.py. Folds need
# only the common event envelope while retaining their historical annotations.
Event = BaseEvent


@dataclass
class _WorkspaceRunState:
    latest_intent: WorkspaceMutationEvent | None = None
    latest_view_id: str | None = None
    latest_admission_seq: int = -1
    latest_progress_seq: int = -1
    strict: bool = False
    protocol_v1_seen: bool = False
    view_action_ids: set[str] = field(default_factory=set)

    def _reset_for_intent(self, event: WorkspaceMutationEvent) -> None:
        self.protocol_v1_seen = self.protocol_v1_seen or event.run_protocol_version == 1
        self.latest_intent = event
        self.latest_view_id = None
        self.latest_admission_seq = -1
        self.latest_progress_seq = -1
        self.strict = self.protocol_v1_seen
        self.view_action_ids.clear()

    def _admit_view(self, event: WorkspaceMutationEvent, seq: int) -> None:
        self.protocol_v1_seen = True
        self.latest_view_id = event.agent_view_id
        self.latest_admission_seq = seq
        self.latest_progress_seq = -1
        self.strict = True
        self.view_action_ids.clear()

    def _consume_mutation(self, event: WorkspaceMutationEvent, seq: int) -> None:
        if event.operation.startswith("agent.run-intent."):
            if self.latest_intent is None or seq > (self.latest_intent.seq or -1):
                self._reset_for_intent(event)
            return
        if (
            self.latest_intent is not None
            and event.operation == "agent.view-admitted"
            and event.run_intent_id == self.latest_intent.id
            and event.agent_view_id is not None
            and event.run_protocol_version == 1
        ):
            self._admit_view(event, seq)
        elif (
            self.latest_intent is not None
            and not self.strict
            and event.operation == "agent.run-admitted"
        ):
            self.latest_admission_seq = seq

    def _record_progress(self, event: Event, seq: int) -> None:
        if (
            self.strict
            and self.latest_view_id is not None
            and event.agent_view_id == self.latest_view_id
        ):
            self.latest_progress_seq = max(self.latest_progress_seq, seq)
        elif not self.strict and self.latest_admission_seq >= 0:
            self.latest_progress_seq = max(self.latest_progress_seq, seq)

    def _consume_action(self, event: ActionEvent, seq: int) -> None:
        if (
            self.strict
            and self.latest_view_id is not None
            and event.agent_view_id == self.latest_view_id
        ):
            self.view_action_ids.add(event.id)
        self._record_progress(event, seq)

    def _consume_observation(self, event: ObservationEvent, seq: int) -> None:
        if self.strict and event.action_id not in self.view_action_ids:
            return
        self._record_progress(event, seq)

    def _consume_finished(self, event: StatusEvent, seq: int) -> None:
        if event.status is ConversationStatus.FINISHED and self.strict:
            self._record_progress(event, seq)

    def consume(self, event: Event, seq: int) -> None:
        if isinstance(event, WorkspaceMutationEvent):
            self._consume_mutation(event, seq)
        elif isinstance(event, ActionEvent):
            self._consume_action(event, seq)
        elif isinstance(event, ObservationEvent):
            self._consume_observation(event, seq)
        elif isinstance(event, PlanEvent) or (
            isinstance(event, MessageEvent) and event.source is EventSource.AGENT
        ):
            self._record_progress(event, seq)
        elif isinstance(event, StatusEvent):
            self._consume_finished(event, seq)

    def result(
        self,
    ) -> tuple[WorkspaceMutationEvent | None, str | None, int, int, bool]:
        return (
            self.latest_intent,
            self.latest_view_id,
            self.latest_admission_seq,
            self.latest_progress_seq,
            self.strict,
        )


def _workspace_run_intent_state(
    events: Iterable[Event],
) -> tuple[WorkspaceMutationEvent | None, str | None, int, int, bool]:
    """Newest intent, latest view, progress, and whether strict v1 is active."""

    state = _WorkspaceRunState()
    for event in events:
        seq = event.seq
        if type(seq) is not int or seq < 1:
            continue
        state.consume(event, seq)
    return state.result()


def latest_workspace_run_intent(events: Iterable[Event]) -> WorkspaceMutationEvent | None:
    """Return the newest durable workspace execution intent, if any."""

    latest, _view_id, _admission_seq, _progress_seq, _strict = _workspace_run_intent_state(events)
    return latest


def current_workspace_agent_view_id(events: Iterable[Event]) -> str | None:
    """Return the latest model-view generation bound to the newest intent."""

    _latest, view_id, _admission_seq, _progress_seq, _strict = _workspace_run_intent_state(events)
    return view_id


def current_workspace_agent_view_seq(events: Iterable[Event]) -> int | None:
    """Return the canonical sequence of the current strict view admission."""

    _latest, view_id, admission_seq, _progress_seq, _strict = _workspace_run_intent_state(events)
    return admission_seq if view_id is not None and admission_seq >= 1 else None


def event_matches_current_workspace_view(events: Iterable[Event], event: Event) -> bool:
    """Whether a persisted control target belongs to the latest strict view."""

    materialized = list(events)
    latest, view_id, _admission_seq, _progress_seq, strict = _workspace_run_intent_state(
        materialized
    )
    if latest is None or not strict:
        return True
    return view_id is not None and event.agent_view_id == view_id


def event_matches_current_workspace_intent(events: Iterable[Event], event: Event) -> bool:
    """Whether durable output was produced by a view admitted for the newest intent.

    Unlike :func:`event_matches_current_workspace_view`, this deliberately keeps
    output authoritative across later model views for the same run intent.  It
    still rejects output from a superseded intent and late output from an older
    in-flight view after a newer view for the current intent was admitted.
    """

    materialized = list(events)
    latest, _view_id, _admission_seq, _progress_seq, strict = _workspace_run_intent_state(
        materialized
    )
    if latest is None or not strict:
        return True
    if (
        type(latest.seq) is not int
        or type(event.seq) is not int
        or event.seq <= latest.seq
        or event.agent_view_id is None
    ):
        return False
    producing_admission = max(
        (
            candidate
            for candidate in materialized
            if isinstance(candidate, WorkspaceMutationEvent)
            and candidate.operation == "agent.view-admitted"
            and candidate.run_intent_id == latest.id
            and type(candidate.seq) is int
            and latest.seq < candidate.seq < event.seq
        ),
        key=lambda candidate: candidate.seq or -1,
        default=None,
    )
    return (
        producing_admission is not None and producing_admission.agent_view_id == event.agent_view_id
    )


def workspace_run_intent_admission_required(events: Iterable[Event]) -> bool:
    """Whether a fresh model-view boundary must acknowledge the newest intent."""

    latest, view_id, admission_seq, _progress_seq, strict = _workspace_run_intent_state(events)
    if latest is None:
        return False
    return view_id is None if strict else admission_seq <= (latest.seq or -1)


def pending_workspace_run_intent(events: Iterable[Event]) -> WorkspaceMutationEvent | None:
    """Return an intent not yet followed by an admitted view and real progress.

    Output from an older in-flight model request may land after a new user turn.
    It cannot consume that turn: admission must occur after the intent, at a
    model-view boundary that includes it, and progress must occur after admission.
    """

    latest, view_id, admission_seq, progress_seq, strict = _workspace_run_intent_state(events)
    if latest is None or (strict and view_id is None):
        return latest
    if not strict and admission_seq <= (latest.seq or -1):
        return latest
    return latest if progress_seq <= admission_seq else None


def _host_mutation_terminal_matches(
    events: list[Event],
    terminal: StatusEvent,
) -> bool:
    """Whether a host-mutation FINISHED terminal completes the current run."""
    completed_seq = max(
        (
            event.seq
            for event in events
            if isinstance(event, StatusEvent)
            and event.status is ConversationStatus.FINISHED
            and type(event.seq) is int
        ),
        default=-1,
    )
    if completed_seq < 1:
        return False
    later_mutations = [
        event
        for event in events
        if isinstance(event, WorkspaceMutationEvent)
        and type(event.seq) is int
        and event.seq > completed_seq
        and not event.operation.startswith("agent.")
    ]
    if not later_mutations:
        return False
    latest_mutation = max(later_mutations, key=lambda event: event.seq or -1)
    return terminal.agent_view_id is None and terminal.host_mutation_id == latest_mutation.id


def workspace_terminal_matches_current_run(
    events_before_terminal: Iterable[Event],
    terminal: StatusEvent,
) -> bool:
    """Whether *terminal* can complete the newest admitted workspace run.

    In strict v1 histories, the terminal must come from the exact latest view
    generation and that generation must have made real progress. Historical
    pre-v1 histories retain their sequence-only behavior.
    """

    events = list(events_before_terminal)
    if terminal.host_mutation_id is not None:
        return _host_mutation_terminal_matches(events, terminal)

    latest, view_id, admission_seq, progress_seq, strict = _workspace_run_intent_state(events)
    if latest is None:
        return terminal.agent_view_id is None
    if not strict:
        return progress_seq > admission_seq > (latest.seq or -1)
    if view_id is None:
        return False
    return terminal.agent_view_id == view_id and progress_seq > admission_seq > (latest.seq or -1)


class AgentViewProjection:
    """Incremental semantic projection for strict run/view histories.

    Stores and evidence retain every raw event. Model, state, and transport
    consumers share this reducer so reconnect replay cannot invent a different
    winner from the server's authoritative projection.
    """

    def __init__(self) -> None:
        self._latest_intent_id: str | None = None
        self._current_view_id: str | None = None
        self._strict = False
        self._protocol_v1_seen = False

    def _update_intent(self, event: WorkspaceMutationEvent) -> None:
        self._protocol_v1_seen = self._protocol_v1_seen or event.run_protocol_version == 1
        self._latest_intent_id = event.id
        self._current_view_id = None
        self._strict = self._protocol_v1_seen

    def _update_admission(self, event: WorkspaceMutationEvent) -> None:
        self._protocol_v1_seen = True
        self._current_view_id = event.agent_view_id
        self._strict = True

    def _is_stale_view(self, event: Event) -> bool:
        return self._strict and (
            event.agent_view_id is not None and event.agent_view_id != self._current_view_id
        )

    def _is_stale_run_status(self, event: Event) -> bool:
        if not self._strict:
            return False
        status_run_intent_id = event.run_intent_id if isinstance(event, StatusEvent) else None
        run_owned_status = status_run_intent_id is not None
        if not run_owned_status:
            return False
        return status_run_intent_id != self._latest_intent_id or self._current_view_id is not None

    def _is_unowned_agent_output(self, event: Event) -> bool:
        if not self._strict:
            return False
        if event.agent_view_id is not None:
            return False
        status_run_intent_id = event.run_intent_id if isinstance(event, StatusEvent) else None
        if status_run_intent_id is not None:
            return False
        if isinstance(event, StatusEvent):
            return event.host_mutation_id is None and event.status not in {
                ConversationStatus.RUNNING,
                ConversationStatus.IDLE,
            }
        return event.source is EventSource.AGENT or isinstance(
            event, (ObservationEvent, AgentErrorEvent, ErrorEvent)
        )

    def accept(self, event: Event) -> bool:
        """Advance through *event* and return whether it is semantically visible."""

        if isinstance(event, WorkspaceMutationEvent):
            if event.operation.startswith("agent.run-intent."):
                self._update_intent(event)
            elif (
                self._latest_intent_id is not None
                and event.operation == "agent.view-admitted"
                and event.run_intent_id == self._latest_intent_id
                and event.run_protocol_version == 1
                and event.agent_view_id is not None
            ):
                self._update_admission(event)
        stale = (
            self._is_stale_view(event)
            or self._is_stale_run_status(event)
            or self._is_unowned_agent_output(event)
        )
        return not stale


def agent_view_consistent_events[EventT: Event](
    events: Iterable[EventT],
) -> list[EventT]:
    """Keep audit history while quarantining output that lost a later view race."""

    projection = AgentViewProjection()
    return [event for event in events if projection.accept(event)]


def _strict_workspace_actions_are_closed(events: Iterable[Event]) -> bool:
    """Prove every post-admission action has a generation-matched outcome."""

    materialized = sorted(
        events,
        key=lambda event: event.seq if type(event.seq) is int else -1,
    )
    _intent, view_id, admission_seq, _progress_seq, strict = _workspace_run_intent_state(
        materialized
    )
    if not strict or view_id is None:
        return True
    actions = {
        event.id: event
        for event in materialized
        if isinstance(event, ActionEvent) and type(event.seq) is int
    }
    open_actions: set[str] = set()
    for event in materialized:
        if not _event_after_admission(event, admission_seq):
            continue
        if isinstance(event, ActionEvent):
            open_actions.add(event.id)
            continue
        if not isinstance(event, (ObservationEvent, AgentErrorEvent)):
            continue
        if event.action_id is None:
            continue
        action = actions.get(event.action_id)
        if _superseded_action_is_closed(action, event):
            assert action is not None
            open_actions.discard(action.id)
            continue
        if _current_view_action_is_closed(action, event, view_id, open_actions):
            assert action is not None
            open_actions.discard(action.id)
            continue
        return False
    return not open_actions


def _event_after_admission(event: Event, admission_seq: int) -> bool:
    return type(event.seq) is int and event.seq > admission_seq


def _superseded_action_is_closed(
    action: ActionEvent | None,
    outcome: ObservationEvent | AgentErrorEvent,
) -> bool:
    return (
        action is not None
        and isinstance(outcome, AgentErrorEvent)
        and outcome.error == "execution_superseded"
        and outcome.agent_view_id == action.agent_view_id
    )


def _current_view_action_is_closed(
    action: ActionEvent | None,
    outcome: ObservationEvent | AgentErrorEvent,
    view_id: str,
    open_actions: set[str],
) -> bool:
    return (
        action is not None
        and action.id in open_actions
        and action.agent_view_id == view_id
        and outcome.agent_view_id == view_id
    )


def _latest_fence_events(
    events: list[Event],
) -> tuple[StatusEvent | None, int | None, int | None]:
    latest_status: StatusEvent | None = None
    latest_status_seq: int | None = None
    latest_effect_seq: int | None = None
    semantic_types = (
        StatusEvent,
        ActionEvent,
        ObservationEvent,
        AgentErrorEvent,
        WorkspaceMutationEvent,
    )
    for event in events:
        if not isinstance(event, semantic_types):
            continue
        seq = event.seq
        if type(seq) is not int or seq < 1:
            raise ValueError("final workspace fence requires canonical persisted event sequences")
        if isinstance(event, StatusEvent):
            if latest_status_seq is None or seq > latest_status_seq:
                latest_status = event
                latest_status_seq = seq
        elif latest_effect_seq is None or seq > latest_effect_seq:
            latest_effect_seq = seq
    return latest_status, latest_status_seq, latest_effect_seq


def _validate_final_terminal(
    latest_status: StatusEvent | None,
    latest_status_seq: int | None,
) -> tuple[StatusEvent, int]:
    if latest_status is None or latest_status_seq is None:
        raise ValueError("final workspace fence requires a persisted status event")
    if (
        latest_status.source is not EventSource.SYSTEM
        or latest_status.status is not ConversationStatus.FINISHED
    ):
        raise ValueError("latest status event must be a system FINISHED status")
    return latest_status, latest_status_seq


def _validate_final_run(
    before_terminal: list[Event],
    terminal: StatusEvent,
) -> None:
    if not workspace_terminal_matches_current_run(before_terminal, terminal):
        raise ValueError("latest FINISHED status is not attributable to the current agent run")
    if not _strict_workspace_actions_are_closed(before_terminal):
        raise ValueError("strict workspace actions are not generation-matched and closed")


def derive_final_workspace_fence(events: Iterable[Event]) -> tuple[int, int | None]:
    """Derive the only event-log fence a final workspace seal may claim.

    The terminal is the latest persisted status event, not merely the latest
    ``FINISHED`` found while scanning backwards.  This distinction prevents an
    older completion from blessing bytes after a later RUNNING/IDLE transition.
    Action requests and their observation/error outcomes form the effect
    envelope: any such event at or after the terminal means the workspace cut
    cannot be attributed to that completion.

    The helper deliberately accepts the generic :class:`Event` iterable used by
    both lifecycle code and stores.  It fails closed on unsequenced semantic
    events so callers can never substitute list position for the durable store's
    canonical sequence.
    """

    materialized = list(events)
    latest_status, latest_status_seq, latest_effect_seq = _latest_fence_events(materialized)
    latest_status, latest_status_seq = _validate_final_terminal(
        latest_status,
        latest_status_seq,
    )
    before_terminal = [
        event for event in materialized if type(event.seq) is int and event.seq < latest_status_seq
    ]
    _validate_final_run(before_terminal, latest_status)
    if latest_effect_seq is not None and latest_effect_seq >= latest_status_seq:
        raise ValueError("latest effect event must precede the terminal FINISHED status")
    return latest_status_seq, latest_effect_seq


__all__ = [
    "AgentViewProjection",
    "agent_view_consistent_events",
    "current_workspace_agent_view_id",
    "current_workspace_agent_view_seq",
    "derive_final_workspace_fence",
    "event_matches_current_workspace_intent",
    "event_matches_current_workspace_view",
    "latest_workspace_run_intent",
    "pending_workspace_run_intent",
    "workspace_run_intent_admission_required",
    "workspace_terminal_matches_current_run",
]
