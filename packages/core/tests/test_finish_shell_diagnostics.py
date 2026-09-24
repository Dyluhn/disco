"""Opaque shell history is diagnostic, never replay permission or typed acceptance."""

from __future__ import annotations

import hashlib

import pytest
from disco.core import (
    AgentErrorEvent,
    ConversationStatus,
    EventSource,
    LLMMessage,
    MessageEvent,
    ObservationEvent,
    StatusEvent,
    VerifierVerdictEvent,
    WorkspaceMutationEvent,
)
from disco.core.loop import AgentStep
from disco.core.loop.control import Disp
from disco.core.loop.finish._shell_diagnostics import _shell_caveat, _terminal_shell_content
from disco.core.loop.finish.finalize_parts.verify_receipt import reuse_finish_verify_receipt
from disco.core.loop.turn_control import Valve
from event_fakes import with_seqs
from test_check_ledger import (
    CID,
    DIGEST_A,
    DIGEST_B,
    _edit,
    _GateExecutor,
    _make_loop,
    _obs,
    _pure,
    _Sandbox,
    _seed_stale_check,
    _shell_calls,
)


def _stale():
    return _pure("check", "bash smoke.sh") + _edit("edit", "src/a.js")


def test_successful_exit_is_not_a_failed_check_or_independent_acceptance():
    events = with_seqs(_pure("pipe", "bash smoke.sh | tail -5"))
    assert _shell_caveat(events) is None
    assert _terminal_shell_content(events, "Artifact ready.", host_warning=False) == (
        "Artifact ready.",
        False,
    )
    assert not any(isinstance(event, VerifierVerdictEvent) for event in events)


@pytest.mark.parametrize("case", ["stale", "failed", "flaky", "unknown", "memo", "runtime"])
def test_unresolved_history_remains_visible_even_after_browser_pass(case):
    events = _stale()
    if case == "failed":
        events = _pure("check", "bash smoke.sh", exit_code=1)
    elif case == "flaky":
        events = (
            _pure("first", "bash smoke.sh")
            + _pure("bad", "bash smoke.sh", exit_code=1)
            + _pure("ok", "bash smoke.sh")
        )
    elif case == "unknown":
        events = _obs("check", "bash smoke.sh", before=None, after=None)
    elif case == "memo":
        events = _pure("check", "bash smoke.sh")
        events[-1].meta["check"]["memo_of"] = 12
    elif case == "runtime":
        events = _pure("check", "bash smoke.sh") + [
            WorkspaceMutationEvent(operation="preview.restart")
        ]
    events.append(VerifierVerdictEvent(verified=True, verdict="pass"))
    content, caveat = _terminal_shell_content(
        with_seqs(events), "ALL TESTS PASSED", host_warning=False
    )
    assert caveat
    assert "ALL TESTS PASSED" not in content
    assert "Shell diagnostics remain unresolved" in content
    assert "Historical commands were not replayed" in content
    assert "Required host acceptance checks retain" in content


def test_current_agent_check_supersedes_stale_history_without_replay():
    events = with_seqs(_stale() + _pure("new", "bash smoke.sh"))
    assert _shell_caveat(events) is None


def test_new_check_does_not_certify_an_unrelated_historical_command():
    events = with_seqs(_stale() + _pure("new", "bash other.sh"))
    content = _shell_caveat(events)
    assert content is not None and "bash smoke.sh" in content
    assert "bash other.sh" not in content


@pytest.mark.parametrize("boundary", ["intent", "finished", "error"])
def test_old_run_diagnostics_do_not_contaminate_next_run(boundary):
    event = (
        WorkspaceMutationEvent(operation="agent.run-intent.user-turn", run_protocol_version=1)
        if boundary == "intent"
        else StatusEvent(status=ConversationStatus(boundary.upper()))
    )
    assert _shell_caveat(with_seqs(_stale() + [event])) is None


def test_host_failure_warning_is_retained_alongside_shell_caveat():
    content, warned = _terminal_shell_content(with_seqs(_stale()), "HOST FAILED", host_warning=True)
    assert warned and content.startswith("HOST FAILED\n\n")
    assert "Shell diagnostics remain unresolved" in content


@pytest.mark.parametrize("path", ["normal", "synthetic", "notify"])
async def test_all_completion_paths_retain_stale_caveat_without_executing_history(path):
    executor = _GateExecutor(_Sandbox([DIGEST_A, DIGEST_A] + [DIGEST_B] * 8))
    loop = _make_loop(executor)
    await _seed_stale_check(loop)
    await loop._emit(
        MessageEvent(
            source=EventSource.AGENT,
            message=LLMMessage(role="assistant", content="ALL TESTS PASSED"),
        )
    )
    events = await loop._events()
    state = await loop.store.get_state(CID)
    if path == "normal":
        disposition = await loop._finish.handle_finish_path(
            AgentStep(finished=True, thought="ALL TESTS PASSED"), state, events
        )
        assert disposition is Disp.HALT
    elif path == "synthetic":
        assert (
            await loop._finish.synthetic_finish_after_actionless_pauses(state, events) is Disp.HALT
        )
    else:
        assert await Valve(loop)._completed_via_notify_finish(events)
    events = await loop._events()
    final_message = [e for e in events if isinstance(e, MessageEvent)][-1]
    assert final_message.source is EventSource.AGENT
    assert final_message.message.role == "assistant"
    assert (
        final_message
        == [e for e in events if isinstance(e, MessageEvent) and e.source is EventSource.AGENT][-1]
    )
    assert final_message.meta["host_owned_terminal_warning"] is True
    assert final_message.meta["shell_diagnostics_advisory"] is True
    assert "Shell diagnostics remain unresolved" in final_message.message.content
    assert "ALL TESTS PASSED" not in final_message.message.content
    assert events[-1].status is ConversationStatus.FINISHED
    assert len(_shell_calls(executor)) == 1


@pytest.mark.parametrize(
    "case",
    [
        "memo",
        "structured_memo",
        "agent_receipt",
        "duplicate",
        "earlier_duplicate",
        "error_pair",
        "new_intent",
        "workspace",
        "runtime",
        "status",
        "user",
        "timeout",
        "exit",
        "foreign_view",
    ],
)
async def test_finish_receipt_reuse_rejects_cached_ambiguous_or_superseded_evidence(case):
    events = _pure("check", "bash smoke.sh")
    action, observation = events
    assert isinstance(observation, ObservationEvent)
    if case == "memo":
        observation.meta["check"]["memo_of"] = 1
    elif case == "structured_memo":
        events[1] = observation.model_copy(
            update={
                "tool_result": observation.tool_result.model_copy(
                    update={"structured": {"memo_of_seq": 1}}
                )
            }
        )
    elif case == "agent_receipt":
        events[1] = observation.model_copy(update={"source": EventSource.AGENT})
    elif case == "duplicate":
        events.append(observation.model_copy(update={"id": "duplicate"}))
    elif case == "earlier_duplicate":
        events.insert(0, observation.model_copy(update={"id": "earlier"}))
    elif case == "error_pair":
        events.insert(1, AgentErrorEvent(action_id=action.id, error="executor failed"))
    elif case in {"new_intent", "workspace", "runtime"}:
        operation = {
            "new_intent": "agent.run-intent.user-turn",
            "workspace": "editor.write",
            "runtime": "preview.restart",
        }[case]
        events.append(WorkspaceMutationEvent(operation=operation))
    elif case == "foreign_view":
        events.append(
            WorkspaceMutationEvent(
                operation="agent.view-admitted",
                run_protocol_version=1,
                run_intent_id="foreign-intent",
                agent_view_id="new-view",
            )
        )
    elif case == "status":
        events.append(StatusEvent(status=ConversationStatus.RUNNING))
    elif case == "user":
        events.append(
            MessageEvent(source=EventSource.USER, message=LLMMessage(role="user", content="Again"))
        )
    elif case == "timeout":
        events[1] = observation.model_copy(
            update={
                "tool_result": observation.tool_result.model_copy(
                    update={"structured": {"timed_out": True}}
                )
            }
        )
    elif case == "exit":
        events[1] = observation.model_copy(
            update={
                "tool_result": observation.tool_result.model_copy(
                    update={"structured": {"exit_code": 1}}
                )
            }
        )
    loop = _make_loop(_GateExecutor(None))
    assert not await reuse_finish_verify_receipt(loop, "bash smoke.sh", with_seqs(events))
    assert await loop._events() == []


async def test_real_preceding_receipt_allows_view_bookkeeping_but_grants_no_acceptance():
    intent = WorkspaceMutationEvent(operation="agent.run-intent.user-turn", run_protocol_version=1)
    events = (
        [intent]
        + _pure("check", "bash smoke.sh")
        + [
            WorkspaceMutationEvent(
                operation="agent.view-admitted",
                run_protocol_version=1,
                run_intent_id=intent.id,
                agent_view_id="fresh-view",
            )
        ]
    )
    loop = _make_loop(_GateExecutor(None))
    assert await reuse_finish_verify_receipt(loop, "bash smoke.sh", with_seqs(events))
    emitted = await loop._events()
    assert len(emitted) == 1
    assert emitted[0].meta["finish_verify_receipt_reused"] is True
    assert (
        "does not certify current runtime state or required acceptance"
        in emitted[0].message.content
    )
    assert not any(isinstance(event, VerifierVerdictEvent) for event in emitted)


async def test_receipt_reuse_count_does_not_inherit_an_older_execution():
    old_marker = MessageEvent(
        source=EventSource.ENVIRONMENT,
        message=LLMMessage(role="user", content="old reuse"),
        meta={
            "finish_verify_receipt_reused": True,
            "command_fingerprint_sha256": hashlib.sha256(b"bash smoke.sh").hexdigest(),
        },
    )
    loop = _make_loop(_GateExecutor(None))
    events = with_seqs([old_marker] + _pure("new", "bash smoke.sh"))
    assert await reuse_finish_verify_receipt(loop, "bash smoke.sh", events)
    assert (await loop._events())[-1].meta["receipt_reuses"] == 1


@pytest.mark.parametrize("command", ["false | tail -5", "false; echo done"])
async def test_wrapper_exit_zero_cannot_satisfy_missing_required_deliverable(tmp_path, command):
    from disco.core import ToolCall
    from disco.core.dod import DoDSpec, FileExistsPredicate
    from disco.core.dod_evaluator import DoDEvaluator
    from loop_fakes import FakeExecutor, ScriptedAgent, build_loop

    executor = FakeExecutor()
    loop, store = build_loop(
        ScriptedAgent([]),
        executor=executor,
        dod_evaluator_factory=lambda: DoDEvaluator(tmp_path),
    )
    await store.set_dod_spec(
        CID,
        DoDSpec(predicates=[FileExistsPredicate(path="required.txt")]),
        set_by="harness",
    )
    for event in _pure("wrapper", command):
        await loop._emit(event)
    step, disposition = await loop._finish.normalize_finish_step(
        AgentStep(
            tool_call=ToolCall(
                tool_name="finish", arguments={"summary": "ALL TESTS PASSED", "verify": command}
            )
        ),
        await loop._events(),
    )
    assert disposition is Disp.CONTINUE
    assert not step.finished
    assert executor.calls == []  # Exact preceding receipt prevents duplicate explicit effects.
    events = await loop._events()
    assert any(e.meta.get("finish_verify_receipt_reused") for e in events)
    assert not any(
        isinstance(e, StatusEvent) and e.status is ConversationStatus.FINISHED for e in events
    )


async def test_real_sandbox_restart_notice_invalidates_receipt_and_shell_diagnostics():
    from types import SimpleNamespace

    from disco.core.loop.observation_execution import maybe_emit_sandbox_restart

    loop = _make_loop(_GateExecutor(None))
    for event in _pure("check", "bash smoke.sh"):
        await loop._emit(event)
    await maybe_emit_sandbox_restart(loop, SimpleNamespace(generation=2), 1)
    events = await loop._events()
    assert events[-1].meta["sandbox_restart"] == {"generation_before": 1, "generation_after": 2}
    assert not await reuse_finish_verify_receipt(loop, "bash smoke.sh", events)
    caveat = _shell_caveat(events)
    assert caveat is not None and "stale after sandbox restart" in caveat


async def test_unchanged_sandbox_and_same_receipt_audit_do_not_invalidate_reuse():
    from types import SimpleNamespace

    from disco.core.loop.observation_execution import maybe_emit_sandbox_restart

    loop = _make_loop(_GateExecutor(None))
    for event in _pure("check", "bash smoke.sh"):
        await loop._emit(event)
    await maybe_emit_sandbox_restart(loop, SimpleNamespace(generation=1), 1)
    assert await reuse_finish_verify_receipt(loop, "bash smoke.sh", await loop._events())
    assert await reuse_finish_verify_receipt(loop, "bash smoke.sh", await loop._events())
    assert (await loop._events())[-1].meta["receipt_reuses"] == 2
    assert _shell_caveat(await loop._events()) is None
