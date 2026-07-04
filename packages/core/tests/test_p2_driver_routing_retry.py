"""P2 — driver routing-retry arm: LLMProviderUnavailable classification + escalation.

Tests:
1. Agent raises LLMProviderUnavailable once → driver retries with escalated
   provider_prefs; no "check your JSON" hint; run completes.
2. A plain LLMError still takes the L447 requery-with-hint path (regression).
3. LLMProviderUnavailable cap → explained user question (not ERROR).
"""

from __future__ import annotations

from disco.core import (
    ConversationStatus,
    ErrorEvent,
    LLMMessage,
    ToolCall,
    View,
)
from disco.core.llm import LLMAuthError, LLMError
from disco.core.llm.errors import LLMProviderUnavailable
from disco.core.loop.boundaries import AgentStep
from disco.core.loop.control import Disp
from loop_fakes import assert_blocked_question_landing, build_loop

CID = "conv"


def _finish_agent_step() -> AgentStep:
    return AgentStep(
        thought="done",
        tool_call=ToolCall(tool_name="finish", arguments={"summary": "ok"}),
        finished=False,
    )


class _ProviderFailingAgent:
    """Raises LLMProviderUnavailable on the first `fail_first_n` calls, then succeeds.

    Records provider_prefs from each call so the test can assert on escalation.
    """

    def __init__(self, steps, *, fail_first_n=1):
        self._steps = list(steps)
        self._fail_n = fail_first_n
        self.calls = 0
        self.seen_provider_prefs: list = []
        self.seen_messages_at_call: list = []

    async def step(
        self,
        view,
        tools,
        *,
        mode,
        overflow_signal,
        on_stream=None,
        temperature=None,
        assist=False,
        attempt: int = 1,
        provider_prefs=None,
    ):
        self.seen_provider_prefs.append(provider_prefs)
        self.seen_messages_at_call.append(list(view.messages))
        i = self.calls
        self.calls += 1
        if i < self._fail_n:
            raise LLMProviderUnavailable(
                "No cookie auth credentials found for this model", provider="openrouter"
            )
        item = self._steps[min(i - self._fail_n, len(self._steps) - 1)]
        if isinstance(item, BaseException):
            raise item
        return item


async def test_provider_unavailable_retries_with_escalated_prefs():
    """LLMProviderUnavailable → driver retries with escalated provider_prefs.
    The second call carries escalated prefs (not None)."""
    agent = _ProviderFailingAgent([_finish_agent_step()], fail_first_n=1)
    loop, store = build_loop(agent)

    await loop.send_message("do something")
    state = await loop.run()

    # Run must complete (not PAUSED/ERROR from provider rejection)
    assert state.execution_status == ConversationStatus.FINISHED

    # The first call had no prefs (normal first try)
    assert agent.seen_provider_prefs[0] is None

    # The second call had escalated prefs (routing retry)
    escalated = agent.seen_provider_prefs[1]
    assert escalated is not None
    assert escalated.get("allow_fallbacks") is True  # escalation level 1


async def test_provider_unavailable_no_json_hint_in_retry():
    """LLMProviderUnavailable retry must NOT add the 'check tool names / JSON'
    hint to messages (the model did nothing wrong)."""
    agent = _ProviderFailingAgent([_finish_agent_step()], fail_first_n=1)
    loop, store = build_loop(agent)

    await loop.send_message("do something")
    await loop.run()

    # The messages at retry call must NOT contain the JSON/model-blame hint
    if len(agent.seen_messages_at_call) >= 2:
        retry_messages = agent.seen_messages_at_call[1]
        hint_texts = [
            m.content for m in retry_messages
            if "check tool names" in (m.content or "").lower()
            or "json structure" in (m.content or "").lower()
        ]
        assert hint_texts == [], f"unexpected model-blame hints in retry messages: {hint_texts}"


async def test_plain_llm_error_still_uses_json_hint_requery():
    """Regression: a plain LLMError must still take the requery-with-hint path
    (existing DEFECT-6 behavior unchanged)."""

    class _PlainErrorAgent:
        def __init__(self):
            self.calls = 0
            self.seen_messages: list = []

        async def step(
            self,
            view,
            tools,
            *,
            mode,
            overflow_signal,
            on_stream=None,
            temperature=None,
            assist=False,
            attempt=1,
            provider_prefs=None,
        ):
            self.calls += 1
            self.seen_messages.append(list(view.messages))
            if self.calls == 1:
                raise LLMError("something specific broke", provider="fake")
            return _finish_agent_step()

    agent = _PlainErrorAgent()
    loop, store = build_loop(agent)

    await loop.send_message("go")
    await loop.run()

    # Should have had ≥2 calls (requery happened)
    assert agent.calls >= 2

    # The second call should include a user-visible hint about the provider rejection
    if len(agent.seen_messages) >= 2:
        retry_messages = agent.seen_messages[1]
        hint_present = any(
            "provider rejected" in (m.content or "").lower()
            or "check tool names" in (m.content or "").lower()
            for m in retry_messages
        )
        assert hint_present, "expected a provider-rejection hint in the requery messages"


async def test_provider_unavailable_cap_leads_to_pause():
    """After 2 LLMProviderUnavailable rejections, the driver PAUSEs (not ERROR)."""

    class _AlwaysFailsProvider:
        def __init__(self):
            self.calls = 0

        async def step(
            self,
            view,
            tools,
            *,
            mode,
            overflow_signal,
            on_stream=None,
            temperature=None,
            assist=False,
            attempt=1,
            provider_prefs=None,
        ):
            self.calls += 1
            raise LLMProviderUnavailable("No allowed providers", provider="openrouter")

    agent = _AlwaysFailsProvider()
    loop, store = build_loop(agent)

    await loop.send_message("do something")
    state = await loop.run()

    # Cap exhausted → explained user question (not ERROR)
    assert state.execution_status == ConversationStatus.AWAITING_USER_QUESTION
    # No ErrorEvent (it's a provider outage, not a model error)
    events = await store.get_events(CID)
    assert_blocked_question_landing(events, legacy_detail="driver-unavailable")
    assert not any(isinstance(e, ErrorEvent) for e in events)


async def test_terminal_auth_error_skips_requery_and_emits_auth_error():
    """R2: a TERMINAL provider auth/budget error (e.g. OpenRouter 'Key limit
    exceeded (total limit)') must NOT be requeried — asking the model to rephrase
    cannot fix a bad/capped key — and must surface a DISTINCT `auth_error` code so
    the UI shows honest key/credits guidance instead of a generic, retry-implying
    'model_error'."""

    class _AuthErrorAgent:
        def __init__(self):
            self.calls = 0

        async def step(self, view, tools, *, mode, overflow_signal, on_stream=None,
                       temperature=None, assist=False, attempt=1, provider_prefs=None):
            self.calls += 1
            raise LLMAuthError("Key limit exceeded (total limit)", provider="openrouter")

    agent = _AuthErrorAgent()
    loop, store = build_loop(agent)

    await loop.send_message("go")
    state = await loop.run()

    # Exactly ONE call — NO requery (a plain LLMError would have produced 2+).
    assert agent.calls == 1, f"terminal auth error must not requery (got {agent.calls} calls)"
    # ERROR status + a DISTINCT auth_error code carrying the provider's real message.
    assert state.execution_status == ConversationStatus.ERROR
    events = await store.get_events(CID)
    errs = [e for e in events if isinstance(e, ErrorEvent)]
    assert errs, "expected an ErrorEvent for the terminal auth failure"
    assert errs[-1].code == "auth_error", f"expected auth_error, got {errs[-1].code!r}"
    assert "Key limit exceeded" in (errs[-1].detail or "")


def _broken_tool_pair_view() -> View:
    return View(
        messages=[
            LLMMessage(role="user", content="go"),
            LLMMessage(
                role="assistant",
                content="I'll inspect it",
                tool_calls=[{"id": "call_bad", "name": "file_read", "arguments": {}}],
            ),
            LLMMessage(
                role="user",
                content="<system-reminder>keep working</system-reminder>",
            ),
            LLMMessage(role="tool", content="file content", tool_call_id="call_bad"),
        ],
        visible_seqs=[],
        total_events=0,
        forgotten_count=0,
    )


async def test_tool_result_adjacency_protocol_error_retries_with_repaired_history():
    class _ProtocolErrorOnceAgent:
        def __init__(self):
            self.calls = 0
            self.seen_messages: list[list[LLMMessage]] = []

        async def step(self, view, tools, *, mode, overflow_signal, on_stream=None,
                       temperature=None, assist=False, attempt=1, provider_prefs=None):
            self.calls += 1
            self.seen_messages.append(list(view.messages))
            if self.calls == 1:
                raise LLMError(
                    "invalid params, tool call result does not follow tool call (2013)",
                    provider="minimax",
                )
            return _finish_agent_step()

    agent = _ProtocolErrorOnceAgent()
    loop, _ = build_loop(agent)

    step, disp = await loop._driver.drive_step(_broken_tool_pair_view(), [])

    assert disp is Disp.FALLTHROUGH
    assert step is not None
    assert step.tool_call is not None
    assert step.tool_call.tool_name == "finish"
    assert agent.calls == 2
    retry_messages = agent.seen_messages[1]
    assert any("system-reminder" in m.content for m in retry_messages)
    assert all(not (m.role == "assistant" and m.tool_calls) for m in retry_messages)
    assert all(m.tool_call_id != "call_bad" for m in retry_messages)


async def test_tool_result_adjacency_protocol_error_repair_is_bounded():
    class _AlwaysProtocolErrorAgent:
        def __init__(self):
            self.calls = 0

        async def step(self, view, tools, *, mode, overflow_signal, on_stream=None,
                       temperature=None, assist=False, attempt=1, provider_prefs=None):
            self.calls += 1
            raise LLMError(
                "invalid params, tool call result does not follow tool call (2013)",
                provider="minimax",
            )

    agent = _AlwaysProtocolErrorAgent()
    loop, store = build_loop(agent)

    step, disp = await loop._driver.drive_step(_broken_tool_pair_view(), [])

    assert step is None
    assert disp is Disp.HALT
    assert agent.calls == 2
    events = await store.get_events(CID)
    errs = [e for e in events if isinstance(e, ErrorEvent)]
    assert errs and errs[-1].code == "model_error"
