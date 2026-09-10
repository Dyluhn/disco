from __future__ import annotations

import pytest
from disco.core import (
    ActionEvent,
    AgentErrorEvent,
    ConversationStatus,
    EventSource,
    FinalWorkspaceSeal,
    LLMMessage,
    MessageEvent,
    ObservationEvent,
    ResourceKey,
    SqliteEventStore,
    StatusEvent,
    ToolCall,
    ToolResult,
    WorkspaceMutationEvent,
    WorkspaceVersionEvent,
    derive_final_workspace_fence,
    event_from_json_dict,
    event_to_json_dict,
    workspace_terminal_matches_current_run,
)
from event_fakes import action, agent_error, observation, status, user_msg, with_seqs
from pydantic import ValidationError

_DIGEST = "a" * 64


async def test_host_workspace_mutation_is_part_of_the_exact_effect_fence() -> None:
    store = SqliteEventStore(":memory:")
    try:
        mutation = await store.append(
            "conversation-1",
            WorkspaceMutationEvent(operation="upload.write", paths=("uploads/a.txt",)),
        )
        terminal = await store.append(
            "conversation-1",
            StatusEvent(status=ConversationStatus.FINISHED),
        )
        assert mutation.seq == 1 and terminal.seq == 2
        committed = await store.append(
            "conversation-1",
            WorkspaceVersionEvent(
                version_seq=7,
                tree_digest=_DIGEST,
                trigger="finish",
                final_seal=_seal(terminal_seq=2, latest_effect_seq=1),
            ),
        )
        assert isinstance(committed, WorkspaceVersionEvent)
    finally:
        store.close()


async def test_host_workspace_mutation_after_finished_prevents_sealing() -> None:
    store = SqliteEventStore(":memory:")
    try:
        await store.append(
            "conversation-1",
            StatusEvent(status=ConversationStatus.FINISHED),
        )
        await store.append(
            "conversation-1",
            WorkspaceMutationEvent(operation="deck.patch", paths=("deck.html",)),
        )
        with pytest.raises(ValueError, match="effect event must precede"):
            await store.append(
                "conversation-1",
                WorkspaceVersionEvent(
                    version_seq=7,
                    tree_digest=_DIGEST,
                    trigger="finish",
                    final_seal=_seal(terminal_seq=1, latest_effect_seq=None),
                ),
                # fmt: skip
            )
    finally:
        store.close()


def _seal(**updates: object) -> FinalWorkspaceSeal:
    values: dict[str, object] = {
        "scope": ResourceKey(namespace="workspace.tree", identifier="conversation-1"),
        "terminal_seq": 41,
        "latest_effect_seq": 40,
        "version_seq": 7,
        "tree_digest": _DIGEST,
        "file_count": 3,
        "total_bytes": 128,
    }
    values.update(updates)
    return FinalWorkspaceSeal.model_validate(values)


def test_old_workspace_version_event_without_seal_remains_valid() -> None:
    event = WorkspaceVersionEvent(version_seq=7, tree_digest=_DIGEST, trigger="finish")

    restored = event_from_json_dict(event_to_json_dict(event))

    assert isinstance(restored, WorkspaceVersionEvent)
    assert restored.final_seal is None


def test_final_seal_round_trips_and_binds_the_version_event() -> None:
    event = WorkspaceVersionEvent(
        version_seq=7,
        tree_digest=_DIGEST,
        trigger="finish",
        final_seal=_seal(),
    ).model_copy(update={"seq": 42})

    restored = event_from_json_dict(event_to_json_dict(event))

    assert isinstance(restored, WorkspaceVersionEvent)
    assert restored.final_seal == _seal()


@pytest.mark.parametrize(
    ("event_updates", "message"),
    [
        ({"version_seq": 8}, "version sequence"),
        ({"tree_digest": "b" * 64}, "tree digest"),
    ],
)
def test_version_event_rejects_a_mismatched_seal(
    event_updates: dict[str, object],
    message: str,
) -> None:
    values: dict[str, object] = {
        "version_seq": 7,
        "tree_digest": _DIGEST,
        "trigger": "finish",
        "final_seal": _seal(),
    }
    values.update(event_updates)

    with pytest.raises(ValidationError, match=message):
        WorkspaceVersionEvent.model_validate(values)


def test_sealed_version_event_must_be_finish_triggered() -> None:
    with pytest.raises(ValidationError, match="requires a finish-triggered"):
        WorkspaceVersionEvent(
            version_seq=7,
            tree_digest=_DIGEST,
            trigger="turn",
            final_seal=_seal(),
        )


def test_final_seal_rejects_an_effect_after_terminal() -> None:
    with pytest.raises(ValidationError, match="must precede terminal"):
        _seal(latest_effect_seq=42)


def test_final_seal_rejects_an_effect_at_the_terminal_sequence() -> None:
    with pytest.raises(ValidationError, match="must precede terminal"):
        _seal(latest_effect_seq=41)


def test_version_event_sequence_must_follow_the_terminal_fence() -> None:
    event = WorkspaceVersionEvent(
        version_seq=7,
        tree_digest=_DIGEST,
        trigger="finish",
        final_seal=_seal(),
    )

    with pytest.raises(ValidationError, match="must precede version event"):
        WorkspaceVersionEvent.model_validate(event.model_dump(mode="python") | {"seq": 41})


def test_derive_final_workspace_fence_derives_terminal_and_effect_maximum() -> None:
    events = with_seqs(
        [
            action(),
            observation(),
            agent_error(),
            user_msg("non-effect event"),
            status(ConversationStatus.FINISHED),
        ]
    )

    assert derive_final_workspace_fence(events) == (5, 3)


def test_derive_final_workspace_fence_accepts_finish_with_no_effect_events() -> None:
    events = with_seqs([user_msg("answer only"), status(ConversationStatus.FINISHED)])

    assert derive_final_workspace_fence(events) == (2, None)


@pytest.mark.parametrize(
    "events",
    [
        [status(ConversationStatus.FINISHED)],
        [action(), status(ConversationStatus.FINISHED).model_copy(update={"seq": 2})],
    ],
)
def test_derive_final_workspace_fence_rejects_unsequenced_semantic_events(events) -> None:
    with pytest.raises(ValueError, match="canonical persisted event sequences"):
        derive_final_workspace_fence(events)


def test_derive_final_workspace_fence_rejects_effect_after_finish() -> None:
    events = with_seqs([status(ConversationStatus.FINISHED), action()])

    with pytest.raises(ValueError, match="effect event must precede"):
        derive_final_workspace_fence(events)


@pytest.mark.parametrize(
    "latest_status",
    [
        status(ConversationStatus.IDLE),
        StatusEvent(source=EventSource.AGENT, status=ConversationStatus.FINISHED),
    ],
)
def test_derive_final_workspace_fence_requires_latest_status_to_be_system_finished(
    latest_status: StatusEvent,
) -> None:
    events = with_seqs([status(ConversationStatus.FINISHED), latest_status])

    with pytest.raises(ValueError, match="latest status event must be a system FINISHED"):
        derive_final_workspace_fence(events)


async def test_store_revalidates_terminal_fence_after_assigning_sequence() -> None:
    store = SqliteEventStore(":memory:")
    try:
        event = WorkspaceVersionEvent(
            version_seq=7,
            tree_digest=_DIGEST,
            trigger="finish",
            final_seal=_seal(latest_effect_seq=1),
        )

        with pytest.raises(ValidationError, match="must precede version event"):
            await store.append("conversation-1", event)

        assert await store.get_events("conversation-1") == []
    finally:
        store.close()


async def test_store_accepts_a_correctly_ordered_conversation_bound_seal() -> None:
    store = SqliteEventStore(":memory:")
    try:
        await store.append("conversation-1", action())
        await store.append("conversation-1", observation())
        await store.append("conversation-1", agent_error())
        await store.append("conversation-1", status(ConversationStatus.FINISHED))
        event = WorkspaceVersionEvent(
            version_seq=7,
            tree_digest=_DIGEST,
            trigger="finish",
            final_seal=_seal(terminal_seq=4, latest_effect_seq=3),
        )

        stored = await store.append("conversation-1", event)

        assert stored.seq == 5
        assert isinstance(stored, WorkspaceVersionEvent)
        assert stored.final_seal == event.final_seal
    finally:
        store.close()


async def test_store_accepts_an_exact_no_effect_fence() -> None:
    store = SqliteEventStore(":memory:")
    try:
        await store.append("conversation-1", user_msg("answer only"))
        await store.append("conversation-1", status(ConversationStatus.FINISHED))
        event = WorkspaceVersionEvent(
            version_seq=7,
            tree_digest=_DIGEST,
            trigger="finish",
            final_seal=_seal(terminal_seq=2, latest_effect_seq=None),
        )

        stored = await store.append("conversation-1", event)

        assert stored.seq == 3
        assert isinstance(stored, WorkspaceVersionEvent)
        assert stored.final_seal == event.final_seal
    finally:
        store.close()


@pytest.mark.parametrize(
    ("prior_events", "seal_updates", "message"),
    [
        (
            [user_msg("work"), status(ConversationStatus.FINISHED)],
            {"terminal_seq": 1, "latest_effect_seq": None},
            "terminal sequence does not match",
        ),
        (
            [action(), user_msg("between"), status(ConversationStatus.FINISHED)],
            {"terminal_seq": 3, "latest_effect_seq": 2},
            "latest effect sequence does not match",
        ),
        (
            [status(ConversationStatus.FINISHED), status(ConversationStatus.IDLE)],
            {"terminal_seq": 1, "latest_effect_seq": None},
            "latest status event must be a system FINISHED",
        ),
        (
            [
                user_msg("work"),
                StatusEvent(source=EventSource.AGENT, status=ConversationStatus.FINISHED),
            ],
            {"terminal_seq": 2, "latest_effect_seq": None},
            "latest status event must be a system FINISHED",
        ),
        (
            [status(ConversationStatus.FINISHED), action()],
            {"terminal_seq": 1, "latest_effect_seq": None},
            "effect event must precede",
        ),
        (
            [user_msg("no terminal")],
            {"terminal_seq": 1, "latest_effect_seq": None},
            "requires a persisted status event",
        ),
    ],
)
async def test_store_rejects_every_stale_or_tampered_event_log_fence(
    prior_events,
    seal_updates: dict[str, object],
    message: str,
) -> None:
    store = SqliteEventStore(":memory:")
    try:
        for prior in prior_events:
            await store.append("conversation-1", prior)
        event = WorkspaceVersionEvent(
            version_seq=7,
            tree_digest=_DIGEST,
            trigger="finish",
            final_seal=_seal(**seal_updates),
        )

        with pytest.raises(ValueError, match=message):
            await store.append("conversation-1", event)

        assert len(await store.get_events("conversation-1")) == len(prior_events)
    finally:
        store.close()


async def test_store_rejects_a_seal_for_another_conversation() -> None:
    store = SqliteEventStore(":memory:")
    try:
        await store.append("conversation-1", status(ConversationStatus.FINISHED))
        event = WorkspaceVersionEvent(
            version_seq=7,
            tree_digest=_DIGEST,
            trigger="finish",
            final_seal=_seal(
                scope=ResourceKey(namespace="workspace.tree", identifier="other"),
                terminal_seq=1,
                latest_effect_seq=None,
            ),
        )

        with pytest.raises(ValueError, match="scope must match"):
            await store.append("conversation-1", event)
    finally:
        store.close()


# --- [2] stale FINISHED rejected; execution_superseded closure is safe ---------


def test_v1_stale_finished_rejected_after_newer_admission():
    """A FINISHED tagged with an old agent_view_id after a newer view was
    admitted must be rejected — it cannot seal the workspace."""
    before = _seq(
        [
            _user_v1("build it"),
            _intent_v1("i1"),
            _admission_v1("i1", "aview_1"),
            _action_v1("file_write", "aview_1"),
            _obs_v1("file_write", "aview_1"),
            _admission_v1("i1", "aview_2"),
        ]
    )
    terminal = StatusEvent(status=ConversationStatus.FINISHED, agent_view_id="aview_1")
    assert workspace_terminal_matches_current_run(before, terminal) is False


def test_v1_execution_superseded_closure_is_safe():
    """When stale actions are closed via execution_superseded, the action
    set is considered closed and _strict_workspace_actions_are_closed accepts."""
    from disco.core.events import _strict_workspace_actions_are_closed
    from event_fakes import action as _action

    act = _action(tool="file_write").model_copy(update={"agent_view_id": "aview_3"})
    events = _seq(
        [
            _user_v1("build it"),
            _intent_v1("i2"),
            _admission_v1("i2", "aview_3"),
            act,
            AgentErrorEvent(
                error="execution_superseded",
                action_id=act.id,
                tool_call_id=act.tool_call.call_id,
                agent_view_id="aview_3",
            ),
        ]
    )
    assert _strict_workspace_actions_are_closed(events) is True


def test_v1_untagged_finished_rejected_under_strict():
    """Under strict v1, a FINISHED with no agent_view_id (untagged) cannot
    resolve the current run — it is an old unattributed terminal."""
    before = _seq(
        [
            _user_v1("build it"),
            _intent_v1("i3"),
            _admission_v1("i3", "aview_4"),
            _action_v1("file_write", "aview_4"),
        ]
    )
    terminal = StatusEvent(status=ConversationStatus.FINISHED)
    assert terminal.agent_view_id is None
    assert workspace_terminal_matches_current_run(before, terminal) is False


def _user_v1(text: str = "msg") -> MessageEvent:
    return MessageEvent(source=EventSource.USER, message=LLMMessage(role="user", content=text))


def _intent_v1(id: str) -> WorkspaceMutationEvent:
    return WorkspaceMutationEvent(
        operation="agent.run-intent.user-turn",
        run_protocol_version=1,
        id=id,
    )


def _admission_v1(intent_id: str, view_id: str) -> WorkspaceMutationEvent:
    return WorkspaceMutationEvent(
        operation="agent.view-admitted",
        run_intent_id=intent_id,
        agent_view_id=view_id,
        run_protocol_version=1,
    )


def _action_v1(tool: str, view_id: str) -> ActionEvent:
    return ActionEvent(
        thought="act",
        tool_call=ToolCall(tool_name=tool, arguments={"path": "x", "content": "y"}),
        agent_view_id=view_id,
    )


def _obs_v1(tool: str, view_id: str) -> ObservationEvent:
    return ObservationEvent(
        tool_result=ToolResult(call_id="c", tool_name=tool, success=True, content="ok"),
        action_id="a",
        agent_view_id=view_id,
    )


def _seq(events: list) -> list:
    return [e.model_copy(update={"seq": i + 1}) for i, e in enumerate(events)]


# --- [3] full legacy pre-v1 sealed history still resolves ----------------------


async def test_legacy_pre_v1_sealed_history_resolves():
    """A full legacy pre-v1 sealed conversation (agent.run-admitted, FINISHED,
    WorkspaceVersionEvent with final_seal) must still resolve correctly through
    derive_final_workspace_fence and store — no v1 protocol required."""
    store = SqliteEventStore(":memory:")
    cid = "legacy-sealed"
    store.create_conversation(cid)
    try:
        seal_scope = ResourceKey(namespace="workspace.tree", identifier=cid)
        await store.append_many(
            cid,
            [
                MessageEvent(
                    source=EventSource.USER, message=LLMMessage(role="user", content="build")
                ),
                WorkspaceMutationEvent(operation="agent.run-intent.user-turn"),
                WorkspaceMutationEvent(operation="agent.run-admitted"),
                ActionEvent(
                    thought="write",
                    tool_call=ToolCall(
                        tool_name="file_write", arguments={"path": "x", "content": "y"}
                    ),
                ),
                ObservationEvent(
                    tool_result=ToolResult(
                        call_id="c1", tool_name="file_write", success=True, content="ok"
                    ),
                    action_id="evt_placeholder",
                ),
                StatusEvent(status=ConversationStatus.FINISHED),
            ],
        )
        events = await store.get_events(cid)
        terminal_seq, latest_effect_seq = derive_final_workspace_fence(events)
        assert terminal_seq == 6
        assert latest_effect_seq == 5
        await store.append(
            cid,
            WorkspaceVersionEvent(
                version_seq=1,
                tree_digest="a" * 64,
                trigger="finish",
                final_seal=FinalWorkspaceSeal(
                    scope=seal_scope,
                    terminal_seq=terminal_seq,
                    latest_effect_seq=latest_effect_seq,
                    version_seq=1,
                    tree_digest="a" * 64,
                    file_count=1,
                    total_bytes=1,
                ),
            ),
        )
        resolved = await store.get_events(cid)
        assert resolved[-1].final_seal is not None
        assert resolved[-1].final_seal.terminal_seq == 6
    finally:
        store.close()


# --- [10] host wrong/missing mutation terminal changes zero events -------------


async def test_host_mutation_terminal_without_matching_mutation_rejected():
    """A terminal FINISHED with host_mutation_id that does NOT match any
    prior mutation event must be rejected — zero events changed."""
    store = SqliteEventStore(":memory:")
    cid = "host-mut-wrong"
    store.create_conversation(cid)
    try:
        await store.append_many(
            cid,
            [
                MessageEvent(
                    source=EventSource.USER, message=LLMMessage(role="user", content="build")
                ),
                StatusEvent(status=ConversationStatus.FINISHED),
            ],
        )
        before = await store.get_events(cid)
        terminal = StatusEvent(
            status=ConversationStatus.FINISHED,
            host_mutation_id="nonexistent-mutation",
        )
        assert workspace_terminal_matches_current_run(before, terminal) is False
        assert len(await store.get_events(cid)) == len(before)
    finally:
        store.close()


async def test_host_mutation_matching_terminal_accepted():
    """A terminal FINISHED whose host_mutation_id matches a prior mutation
    event must be accepted as matching."""
    store = SqliteEventStore(":memory:")
    cid = "host-mut-match"
    store.create_conversation(cid)
    try:
        await store.append_many(
            cid,
            [
                MessageEvent(
                    source=EventSource.USER, message=LLMMessage(role="user", content="build")
                ),
                StatusEvent(status=ConversationStatus.FINISHED),
            ],
        )
        mutation_evt = await store.append(
            cid,
            WorkspaceMutationEvent(
                operation="deck.patch",
                paths=("index.html",),
            ),
        )
        events = await store.get_events(cid)
        terminal = StatusEvent(
            status=ConversationStatus.FINISHED,
            host_mutation_id=mutation_evt.id,
        )
        assert workspace_terminal_matches_current_run(events, terminal) is True
    finally:
        store.close()
