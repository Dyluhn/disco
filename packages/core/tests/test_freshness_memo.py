"""Register #5's freshness memo — deterministic acceptance (amendment A11 §1/§4).

# What is being proved

Register #5 (`DEFERRED-PRODUCT-FINDINGS.md`) fired three times on three sealed
commits and three seeds — 10-C `6606503c`, 10-D `65a9f46a`, 11-A `488741db` —
always the same way: *the model authors a verification command, runs it, gets a
usable answer, and then re-issues a semantically identical call a third time.*
Every other oracle PASSED in all three runs and the product behaved correctly.
The defect was never the model, the oracle, or the output. It was a **missing
feedback path**: the streak detector is a purely downstream observer, so the
loop's only response to repetition was terminal. It punished the third call
instead of preventing the second.

The memo is that feedback path. It informs; it never constrains.

# The acceptance A11 §4 names, and where each clause is proved

* *"deterministic memo tests (verification-shaped success → memo present in
  next-turn context)"* — `test_verification_shaped_success_puts_memo_in_context`,
  asserted against `View.of(events)`, the product's own definition of "what the
  LLM sees right now", not merely against the durable log.
* *"the threshold stays at 2; no run is exempted"* —
  `test_memo_fires_before_the_oracle_and_does_not_disarm_it`. The memo lands one
  call BEFORE the oracle's limit so the model can still act on it, and the third
  call still trips the oracle. A memo that made a thrash red disappear without
  the behaviour changing would be the one outcome this design forbids.
* A6.3 §6.2's positive control — `test_no_memo_across_workspace_mutation`, plus
  the two boundary kinds the original predicate MISSED
  (`test_no_memo_across_user_message`,
  `test_no_memo_across_blocking_environment_message`).
* A6.3 §3.4, "it does not suppress the call" — `test_memo_never_suppresses`.
* A6.3 §4, discriminating model-vs-predicate on a fourth occurrence —
  `test_memo_is_observable_in_the_event_log`. Without an observable event a
  fourth occurrence tells us nothing new, which is the situation the campaign
  was in before this boundary.

# Scope, stated so it is not overclaimed

Byte-identical repetition covers 10-D and 11-A. It does **not** cover 10-C,
whose three calls are three *different* compound commands that each embed
`node render-check.mjs`; only the oracle's semantic script-invocation class
matched those. See this boundary's receipt — the gap is measured and recorded,
not papered over.
"""

from __future__ import annotations

from disco.core import (
    ActionEvent,
    Event,
    EventSource,
    LLMMessage,
    MessageEvent,
    SqliteEventStore,
    ToolCall,
    ToolResult,
)
from disco.core.llm import ModelExecutionPolicy, OperatingMode
from disco.core.loop.dedup import _W39_REMINDER_SENTINEL, _w39_freshness_boundary_seq
from disco.core.view import View
from loop_fakes import (
    FakeAnalyzer,
    FakeExecutor,
    FakeSummarizer,
    NeverConfirm,
    ScriptedAgent,
)

CID = "conv"

# The real command from register #5's 10-D and 11-A firings, kept verbatim so
# the deterministic test exercises the same string the live defect did.
VERIFY_CMD = (
    "python3 -c \"import sys,urllib.request as U;u='http://localhost:8080/';"
    "r=U.urlopen(u,timeout=10);code=getattr(r,'status',None) or r.getcode();"
    "(code==200 or sys.exit('HTTP '+str(code)+' from '+u))\""
)


class _NoOpCondenser:
    def should_condense(self, view, *, token_count):
        return None

    async def condense(self, events, view, *, summarizer, reason="tokens", artifact_paths=None):
        return None


class _EchoExecutor(FakeExecutor):
    """Succeeds with a recognisable body so the memo's echoed prior result is
    distinguishable from the reminder's own prose."""

    async def execute(self, call):
        self.calls.append(call)
        return ToolResult(
            call_id=call.call_id,
            tool_name=call.tool_name,
            success=True,
            content="OK http://localhost:8080/ 200 bytes=1024",
        )

    def readonly_tool_names(self):
        return frozenset({"file_read"})


def _make_loop(executor=None):
    from disco.core.loop.engine import AgentLoop

    return AgentLoop(
        CID,
        SqliteEventStore(":memory:"),
        ScriptedAgent([]),
        executor or _EchoExecutor(),
        None,
        FakeAnalyzer(),
        NeverConfirm(),
        _NoOpCondenser(),
        FakeSummarizer(),
        mode=OperatingMode.LONG_HORIZON,
        # The CAPABLE-MODEL default: assist OFF. This is the population all
        # three of register #5's firings came from, and the reason an
        # assist-gated memo was dead code in every canary that reproduced it.
        model_policy=ModelExecutionPolicy.standard(),
    )


def _shell(call_id: str, cmd: str = VERIFY_CMD) -> ActionEvent:
    return ActionEvent(
        thought="verifying the preview responds",
        tool_call=ToolCall(tool_name="shell", call_id=call_id, arguments={"command": cmd}),
    )


def _write(call_id: str) -> ActionEvent:
    return ActionEvent(
        thought="writing the component",
        tool_call=ToolCall(
            tool_name="file_write",
            call_id=call_id,
            arguments={"path": "src/App.jsx", "content": "x"},
        ),
    )


async def _drive(loop, action: ActionEvent) -> list[Event]:
    persisted = await loop.store.append(CID, action)
    await loop._execute_and_observe(persisted)
    return await loop.store.get_events(CID)


def _memos_in_context(events: list[Event]) -> list[str]:
    """Memos visible in the NEXT TURN's context.

    `View.of` is the product's own "what the LLM sees right now" — a memo that
    is on the durable log but absent from the view would not be a feedback path
    at all, so the acceptance is asserted here rather than against the log.
    """
    return [
        m.content
        for m in View.of(events).messages
        if isinstance(m.content, str) and _W39_REMINDER_SENTINEL in m.content
    ]


# ---------------------------------------------------------------------------
# A11 §4 — verification-shaped success → memo present in next-turn context
# ---------------------------------------------------------------------------


async def test_verification_shaped_success_puts_memo_in_context():
    loop = _make_loop()
    await _drive(loop, _shell("v1"))
    events = await _drive(loop, _shell("v2"))

    in_context = _memos_in_context(events)
    assert len(in_context) == 1, "the memo must reach the model, not just the log"
    memo = in_context[0]
    assert "You already ran" in memo
    # A6.3 §2 — "here is that result", so the model is handed the answer it
    # already has rather than merely told that one exists.
    assert "That run produced:" in memo
    assert "OK http://localhost:8080/ 200 bytes=1024" in memo


async def test_memo_is_observable_in_the_event_log():
    """A6.3 §4 — a fourth occurrence must be able to discriminate model from
    predicate: emitted-and-ignored is a prompting finding, never-emitted is a
    predicate finding. That reading is only possible if the memo is an event."""
    loop = _make_loop()
    await _drive(loop, _shell("v1"))
    events = await _drive(loop, _shell("v2"))

    memo_events = [
        e
        for e in events
        if isinstance(e, MessageEvent)
        and e.message is not None
        and isinstance(e.message.content, str)
        and _W39_REMINDER_SENTINEL in e.message.content
    ]
    assert len(memo_events) == 1
    assert memo_events[0].source == EventSource.ENVIRONMENT


# ---------------------------------------------------------------------------
# A6.3 §3.3/§3.4 — fires at streak 2, and disarms nothing
# ---------------------------------------------------------------------------


async def test_memo_fires_before_the_oracle_and_does_not_disarm_it():
    """The memo lands on the SECOND call — one before `ThrashOracle`'s limit of
    2 repeats — so the model receives it while it can still act. A memo arriving
    with the terminal verdict could change nothing.

    The third call still happens and is still recorded: the memo informs, it
    does not gate. `ThrashOracle` grades whatever the model actually did, which
    is what keeps the memo from being usable to make a red disappear."""
    ex = _EchoExecutor()
    loop = _make_loop(ex)

    after_first = await _drive(loop, _shell("v1"))
    assert _memos_in_context(after_first) == [], "nothing to be fresh about yet"

    after_second = await _drive(loop, _shell("v2"))
    assert len(_memos_in_context(after_second)) == 1, "memo at streak 2, before the threshold"

    after_third = await _drive(loop, _shell("v3"))
    # Anti-spam: one memo per fresh streak, not one per repeat.
    assert len(_memos_in_context(after_third)) == 1
    # The behaviour the oracle grades is unchanged — three real executions.
    assert len([c for c in ex.calls if c.tool_name == "shell"]) == 3


async def test_memo_never_suppresses():
    ex = _EchoExecutor()
    loop = _make_loop(ex)
    for i in range(4):
        await _drive(loop, _shell(f"v{i}"))
    assert len([c for c in ex.calls if c.tool_name == "shell"]) == 4


# ---------------------------------------------------------------------------
# A6.3 §3.2 — the freshness predicate. Fail-safe: its failure mode is silence.
# ---------------------------------------------------------------------------


async def test_no_memo_across_workspace_mutation():
    """A6.3 §6.2's named positive control: a write between the two identical
    verifies makes re-verifying legitimate, so no memo is emitted."""
    loop = _make_loop()
    await _drive(loop, _shell("v1"))
    await _drive(loop, _write("w1"))
    events = await _drive(loop, _shell("v2"))
    assert _memos_in_context(events) == []


async def test_no_memo_across_user_message():
    """A boundary the ORIGINAL predicate missed.

    `_w39_latest_mutation_seq` counted only workspace mutations, so before this
    boundary the loop would tell a model "nothing has changed since" immediately
    after a user turn that changed the goal. The oracle has always counted a
    user message as a progress-epoch boundary; the loop now does too."""
    loop = _make_loop()
    await _drive(loop, _shell("v1"))
    await loop.store.append(
        CID,
        MessageEvent(
            source=EventSource.USER,
            message=LLMMessage(role="user", content="actually, target port 9090 instead"),
        ),
    )
    events = await _drive(loop, _shell("v2"))
    assert _memos_in_context(events) == []


async def test_no_memo_across_blocking_environment_message():
    """The second boundary kind the original predicate missed: the environment
    interrupted with a blocking condition."""
    loop = _make_loop()
    await _drive(loop, _shell("v1"))
    await loop.store.append(
        CID,
        MessageEvent(
            source=EventSource.ENVIRONMENT,
            message=LLMMessage(role="user", content="sandbox restarted"),
            meta={"blocking": "sandbox_restart"},
        ),
    )
    events = await _drive(loop, _shell("v2"))
    assert _memos_in_context(events) == []


async def test_failed_prior_run_produces_no_memo():
    """"You already ran this and it passed" must never be said about a failure —
    a failed verify is exactly the case where re-running is correct."""

    class _FailFirst(FakeExecutor):
        async def execute(self, call):
            self.calls.append(call)
            failed = call.call_id == "v1"
            return ToolResult(
                call_id=call.call_id,
                tool_name=call.tool_name,
                success=not failed,
                content="" if failed else "OK",
                error="connection refused" if failed else None,
            )

        def readonly_tool_names(self):
            return frozenset({"file_read"})

    loop = _make_loop(_FailFirst())
    await _drive(loop, _shell("v1"))
    events = await _drive(loop, _shell("v2"))
    assert _memos_in_context(events) == []


# ---------------------------------------------------------------------------
# The freshness predicate as a pure function
# ---------------------------------------------------------------------------


def _msg(seq: int, source: EventSource, *, blocking: str | None = None) -> MessageEvent:
    # `seq` is set at construction: events are frozen once built, which is the
    # append-only log's invariant showing up in the type system.
    return MessageEvent(
        source=source,
        seq=seq,
        message=LLMMessage(role="user", content="hello"),
        meta={"blocking": blocking} if blocking else {},
    )


def test_freshness_boundary_counts_user_and_blocking_environment_messages():
    assert _w39_freshness_boundary_seq([_msg(5, EventSource.USER)]) == 5
    assert _w39_freshness_boundary_seq([_msg(7, EventSource.ENVIRONMENT, blocking="restart")]) == 7
    # A NON-blocking environment message is not a boundary — tool results flow
    # through this source constantly and would otherwise reset every streak,
    # silencing the memo entirely.
    assert _w39_freshness_boundary_seq([_msg(9, EventSource.ENVIRONMENT)]) == 0
    assert _w39_freshness_boundary_seq([_msg(3, EventSource.AGENT)]) == 0


def test_freshness_boundary_takes_the_latest_boundary():
    events: list[Event] = [
        _msg(2, EventSource.USER),
        _msg(11, EventSource.ENVIRONMENT, blocking="restart"),
        _msg(4, EventSource.USER),
    ]
    assert _w39_freshness_boundary_seq(events) == 11
    assert _w39_freshness_boundary_seq(events, before_seq=11) == 4
