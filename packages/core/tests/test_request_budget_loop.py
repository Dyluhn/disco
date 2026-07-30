"""Request budget loop integration tests — retry, compaction, and telemetry.

Extracted from ``test_request_budget.py`` so the test module stays under the
test-module logical-LOC limit. These tests cover the driver-level budget
preview integration with the agent loop.
"""

from __future__ import annotations

# The accepted pytest identities remain in test_request_budget.py.  This module
# holds the extracted implementations and is exercised through compatibility
# subclasses there; collecting it directly would duplicate the same cases.
__test__ = False

import httpx
import pytest
from disco.core import CondensationEvent, CondensationRequest, ErrorEvent, LLMMessage, View
from disco.core.llm import (
    OperatingMode,
    ToolSpec,
)
from disco.core.llm.openai_provider import OpenAIProvider
from disco.core.llm.request_budget import RequestBudgetEstimate
from disco.core.loop.control import Disp
from loop_fakes import (
    FakeCondenser,
    ScriptedAgent,
    build_loop,
    finish_step,
)
from request_budget_support import (
    PreviewScriptedAgent,
    RefRecordingAgent,
    _estimate,
    _req,
)

CID = "conv"


# ---------------------------------------------------------------------------
# F — Retry recomputation: provider-unavailable → tool list changes on retry
# ---------------------------------------------------------------------------


class TestF_RetryRecomputation:
    async def test_provider_unavailable_retry_recomputes_tools(self) -> None:
        from disco.core.llm import LLMProviderUnavailable

        alpha_spec = ToolSpec(name="alpha", description="Alpha tool", parameters_schema={})
        beta_spec = ToolSpec(name="beta", description="Beta tool", parameters_schema={})

        class VaryingExecutor:
            def __init__(self):
                self._sets = [[alpha_spec], [beta_spec]]
                self._avail_calls = 0
                self.calls: list = []

            def available_tools(self):
                i = min(self._avail_calls, len(self._sets) - 1)
                self._avail_calls += 1
                return self._sets[i]

            async def execute(self, call):
                from disco.core import ToolResult

                return ToolResult(
                    call_id=call.call_id,
                    tool_name=call.tool_name,
                    success=True,
                    content="ok",
                )

        executor = VaryingExecutor()
        est = _estimate(
            canonical_payload_bytes=100,
            messages_json_bytes=50,
            tools_json_bytes=30,
        )
        agent = RefRecordingAgent(
            [LLMProviderUnavailable("no instances"), finish_step()],
            budget_estimate=est,
        )
        cond = FakeCondenser(request=None)
        loop, store = build_loop(
            agent,
            condenser=cond,
            executor=executor,
            conversation_id=CID,
        )
        await loop.send_message("hi")
        await loop.run()

        assert agent.preview_calls == 1
        assert agent.calls == 2  # first raise, second succeed
        assert executor._avail_calls == 3  # initial + retry + success
        # First call tool names (preview + first actual) share same object
        assert agent.preview_tool_lists[0] is agent.step_tool_lists[0]
        first_names = [getattr(t, "name", None) for t in agent.preview_tool_lists[0]]
        assert "alpha" in first_names
        assert "beta" not in first_names
        # Second actual call sees beta, not alpha
        second_names = [getattr(t, "name", None) for t in agent.step_tool_lists[1]]
        assert "beta" in second_names
        assert "alpha" not in second_names


# ---------------------------------------------------------------------------
# G — LLMContextWindowExceeded still reaches hard-reset path
# ---------------------------------------------------------------------------


class TestG_WindowExceeded:
    async def test_context_window_exceeded_after_preview_still_hard_resets(
        self,
        monkeypatch,
    ) -> None:
        from disco.core.llm import LLMContextWindowExceeded

        est = _estimate(
            canonical_payload_bytes=200000,
            messages_json_bytes=50000,
            tools_json_bytes=5000,
        )

        class ExceedAgent(ScriptedAgent):
            def request_budget_preview(
                self,
                view,
                tools,
                *,
                mode,
                overflow_signal,
                temperature=None,
                assist=False,
            ):
                return est

        agent = ExceedAgent([LLMContextWindowExceeded("window exceeded")])
        cond = FakeCondenser(
            request=CondensationRequest(soft=False, reason="tokens"),
            tombstone=None,
        )
        monkeypatch.setenv("DISCO_CONTEXT_PACK", "0")
        loop, store = build_loop(agent, condenser=cond, conversation_id=CID)
        await loop.send_message("hi")
        events = await store.get_events(CID)
        view = View(
            messages=[LLMMessage(role="user", content="hi")],
            visible_seqs=[e.seq for e in events if e.seq is not None],
            total_events=len(events),
            forgotten_count=0,
        )

        # Call drive_step directly — prove LLMContextWindowExceeded
        # reaches the hard-reset path (returns HALT with ErrorEvent)
        step, disp = await loop._driver.drive_step(view, events)
        assert step is None
        assert disp == Disp.HALT

        final_events = await store.get_events(CID)
        assert any(
            isinstance(e, ErrorEvent) and e.code == "context_window" for e in final_events
        ), "Expected a context_window ErrorEvent after hard reset made no progress"


# ---------------------------------------------------------------------------
# H — Causal output-reserve proof
# ---------------------------------------------------------------------------


class TestH_OutputReserve:
    """Identical payload, identical messages/tools/window; the only difference
    is max_output_tokens.  The threshold lies BETWEEN estimated_input_tokens and
    pressure_tokens so the test proves causality and fails if the driver passes
    estimated_input_tokens instead of pressure_tokens."""

    CANONICAL_PAYLOAD = 10000
    MSGS_JSON = 5000
    TOOLS_JSON = 3000
    THRESHOLD = 5000
    OUTPUT_RESERVE = 4096

    @classmethod
    def _make_condenser(cls):
        class ThresholdCondenser(FakeCondenser):
            def __init__(self):
                super().__init__(
                    request=None,
                    tombstone=CondensationEvent(
                        forgotten_start_seq=1,
                        forgotten_end_seq=1,
                        summary="[ok]",
                    ),
                )
                self.token_count_seen: list = []

            def should_condense(self, view, *, token_count):
                self.should_calls += 1
                self.token_count_seen.append(token_count)
                if token_count is not None and token_count > cls.THRESHOLD:
                    return CondensationRequest(soft=True, reason="tokens")
                return None

        return ThresholdCondenser()

    def _estimate(self, max_output_tokens):
        return RequestBudgetEstimate(
            driver_context_window=65536,
            max_output_tokens=max_output_tokens,
            canonical_payload_bytes=self.CANONICAL_PAYLOAD,
            messages_json_bytes=self.MSGS_JSON,
            tools_json_bytes=self.TOOLS_JSON,
            message_count=4,
            tool_count=5,
        )

    async def test_no_output_reserve_below_cutoff_calls_model(self) -> None:
        est = self._estimate(max_output_tokens=None)
        assert est.estimated_input_tokens < self.THRESHOLD
        assert est.pressure_tokens == est.estimated_input_tokens

        agent = PreviewScriptedAgent(
            [finish_step()],
            budget_estimate=est,
        )
        cond = self._make_condenser()
        monkeypatch_env = pytest.MonkeyPatch()
        monkeypatch_env.setenv("DISCO_CONTEXT_PACK", "0")
        try:
            loop, store = build_loop(agent, condenser=cond, conversation_id=CID)
            await loop.send_message("hi")
            events = await store.get_events(CID)
            view = View(
                messages=[LLMMessage(role="user", content="hi")],
                visible_seqs=[e.seq for e in events if e.seq is not None],
                total_events=len(events),
                forgotten_count=0,
            )
            initial_tools = loop._driver.tools_for_step(
                suppress_meta_tools=False,
                force_submit_only=False,
                force_read_tools=None,
                blocked_tools=frozenset(),
                mode=OperatingMode.INTERACTIVE,
                available_tools=loop.executor.available_tools(),
            )

            preview_disp = await loop._driver._try_request_budget_preview(
                view,
                events,
                OperatingMode.INTERACTIVE,
                None,
                initial_tools,
            )
            assert preview_disp is None  # within_budget → proceed
            assert cond.should_calls == 1
            assert cond.token_count_seen == [est.pressure_tokens]

            step, disp = await loop._driver.drive_step(view, events)
            assert disp == Disp.FALLTHROUGH
            assert step is not None
            assert step.finished
            assert agent.calls == 1
        finally:
            monkeypatch_env.undo()

    async def test_output_reserve_crosses_cutoff_compacts_no_model_call(self) -> None:
        est = self._estimate(max_output_tokens=self.OUTPUT_RESERVE)
        assert est.estimated_input_tokens < self.THRESHOLD
        assert est.pressure_tokens > self.THRESHOLD

        agent = PreviewScriptedAgent(
            [finish_step()],
            budget_estimate=est,
        )
        cond = self._make_condenser()
        monkeypatch_env = pytest.MonkeyPatch()
        monkeypatch_env.setenv("DISCO_CONTEXT_PACK", "0")
        try:
            loop, store = build_loop(agent, condenser=cond, conversation_id=CID)
            await loop.send_message("hi")
            events = await store.get_events(CID)
            view = View(
                messages=[LLMMessage(role="user", content="hi")],
                visible_seqs=[e.seq for e in events if e.seq is not None],
                total_events=len(events),
                forgotten_count=0,
            )
            initial_tools = loop._driver.tools_for_step(
                suppress_meta_tools=False,
                force_submit_only=False,
                force_read_tools=None,
                blocked_tools=frozenset(),
                mode=OperatingMode.INTERACTIVE,
                available_tools=loop.executor.available_tools(),
            )

            result = await loop._driver._try_request_budget_preview(
                view,
                events,
                OperatingMode.INTERACTIVE,
                None,
                initial_tools,
            )
            assert result == Disp.CONTINUE
            assert agent.calls == 0
            assert cond.should_calls == 1
            assert cond.token_count_seen == [est.pressure_tokens]
        finally:
            monkeypatch_env.undo()


# ---------------------------------------------------------------------------
# I — Telemetry: monkeypatch driver module, not global inspect
# ---------------------------------------------------------------------------


class TestI_TelemetryMonkeypatch:
    def _build_loop_for_telemetry(self, agent, condenser):
        return build_loop(agent, condenser=condenser, conversation_id=CID)

    async def test_inspect_off_emits_zero_events(self, monkeypatch) -> None:
        """inspect_enabled → False → no log_event call."""
        est = _estimate(
            canonical_payload_bytes=100,
            messages_json_bytes=50,
            tools_json_bytes=30,
        )
        agent = PreviewScriptedAgent(
            [finish_step()],
            budget_estimate=est,
        )
        cond = FakeCondenser(request=None)

        log_calls: list = []
        monkeypatch.setattr(
            "disco.core.loop.driver.inspect_enabled",
            lambda: False,
        )
        monkeypatch.setattr(
            "disco.core.loop.driver.log_event",
            lambda *a, **kw: log_calls.append((a, kw)),
        )

        loop, store = self._build_loop_for_telemetry(agent, cond)
        await loop.send_message("hi")
        await loop.run()
        budget_events = [(a, kw) for a, kw in log_calls if a == ("request_budget.preview",)]
        assert len(budget_events) == 0

    async def test_within_budget_emits_scalar_event(self, monkeypatch) -> None:
        est = _estimate(
            canonical_payload_bytes=100,
            messages_json_bytes=50,
            tools_json_bytes=30,
        )
        agent = PreviewScriptedAgent(
            [finish_step()],
            budget_estimate=est,
        )
        cond = FakeCondenser(request=None)

        log_calls: list = []
        monkeypatch.setattr(
            "disco.core.loop.driver.inspect_enabled",
            lambda: True,
        )
        monkeypatch.setattr(
            "disco.core.loop.driver.log_event",
            lambda *a, **kw: log_calls.append((a, dict(kw))),
        )

        loop, store = self._build_loop_for_telemetry(agent, cond)
        await loop.send_message("hi")
        await loop.run()

        budget_events = [(a, kw) for a, kw in log_calls if a == ("request_budget.preview",)]
        assert len(budget_events) == 1
        _, ev = budget_events[0]

        assert ev["outcome"] == "within_budget"
        assert ev["cid"] == CID
        assert isinstance(ev["driver_context_window"], int)
        assert isinstance(ev["max_output_tokens"], (int, type(None)))
        assert isinstance(ev["canonical_payload_bytes"], int)
        assert isinstance(ev["messages_json_bytes"], int)
        assert isinstance(ev["tools_json_bytes"], int)
        assert isinstance(ev["estimated_input_tokens"], int)
        assert isinstance(ev["pressure_tokens"], int)
        assert isinstance(ev["message_count"], int)
        assert isinstance(ev["tool_count"], int)

        # No raw sentinel text or tool names should appear
        values_str = [str(v) for v in ev.values()]
        assert not any("finish_step" in s or "[summary]" in s for s in values_str)
        # All values are str, int, or None
        for v in ev.values():
            assert v is None or isinstance(v, (str, int)), f"unexpected type {type(v)} for {v}"

    async def test_unavailable_emits_no_scalar_fields(self, monkeypatch) -> None:
        agent = ScriptedAgent([finish_step()])
        cond = FakeCondenser(request=None)

        log_calls: list = []
        monkeypatch.setattr(
            "disco.core.loop.driver.inspect_enabled",
            lambda: True,
        )
        monkeypatch.setattr(
            "disco.core.loop.driver.log_event",
            lambda *a, **kw: log_calls.append((a, dict(kw))),
        )

        loop, store = self._build_loop_for_telemetry(agent, cond)
        await loop.send_message("hi")
        await loop.run()

        budget_events = [(a, kw) for a, kw in log_calls if a == ("request_budget.preview",)]
        assert len(budget_events) == 1
        _, ev = budget_events[0]
        assert ev["outcome"] == "unavailable"
        assert ev["cid"] == CID
        # No estimate scalars on unavailable
        assert "driver_context_window" not in ev
        assert "canonical_payload_bytes" not in ev

    async def test_compacted_context_pack_emits_scalar_event(self, monkeypatch) -> None:
        est = _estimate(
            canonical_payload_bytes=200000,
            messages_json_bytes=50000,
            tools_json_bytes=5000,
        )
        agent = PreviewScriptedAgent(
            [finish_step()],
            budget_estimate=est,
        )
        cond = FakeCondenser(
            request=CondensationRequest(soft=True, reason="tokens"),
        )
        snip = CondensationEvent(
            forgotten_start_seq=1,
            forgotten_end_seq=1,
            summary="[snip]",
        )
        monkeypatch.setattr(
            "disco.core.loop.driver.context_pack_enabled",
            lambda: True,
        )
        monkeypatch.setattr(
            "disco.core.loop.driver.context_compact_if_needed",
            lambda *a, **kw: [snip],
        )

        log_calls: list = []
        monkeypatch.setattr(
            "disco.core.loop.driver.inspect_enabled",
            lambda: True,
        )
        monkeypatch.setattr(
            "disco.core.loop.driver.log_event",
            lambda *a, **kw: log_calls.append((a, dict(kw))),
        )

        loop, store = self._build_loop_for_telemetry(agent, cond)
        await loop.send_message("hi")
        events = await store.get_events(CID)
        view = View(
            messages=[LLMMessage(role="user", content="hi")],
            visible_seqs=[e.seq for e in events if e.seq is not None],
            total_events=len(events),
            forgotten_count=0,
        )
        initial_tools = loop._driver.tools_for_step(
            suppress_meta_tools=False,
            force_submit_only=False,
            force_read_tools=None,
            blocked_tools=frozenset(),
            mode=OperatingMode.INTERACTIVE,
            available_tools=loop.executor.available_tools(),
        )

        result = await loop._driver._try_request_budget_preview(
            view,
            events,
            OperatingMode.INTERACTIVE,
            None,
            initial_tools,
        )
        assert result == Disp.CONTINUE

        budget_events = [(a, kw) for a, kw in log_calls if a == ("request_budget.preview",)]
        assert len(budget_events) == 1
        _, ev = budget_events[0]
        assert ev["outcome"] == "compacted"
        assert ev["method"] == "context_pack"
        assert ev["kind"] == "soft"
        assert ev["cid"] == CID
        assert isinstance(ev["driver_context_window"], int)
        assert isinstance(ev["max_output_tokens"], (int, type(None)))
        assert isinstance(ev["canonical_payload_bytes"], int)
        assert isinstance(ev["messages_json_bytes"], int)
        assert isinstance(ev["tools_json_bytes"], int)
        assert isinstance(ev["estimated_input_tokens"], int)
        assert isinstance(ev["pressure_tokens"], int)
        assert isinstance(ev["message_count"], int)
        assert isinstance(ev["tool_count"], int)
        values_str = [str(v) for v in ev.values()]
        assert not any("finish_step" in s or "[snip]" in s for s in values_str)
        for v in ev.values():
            assert v is None or isinstance(v, (str, int)), f"unexpected type {type(v)} for {v}"

    async def test_no_progress_emits_scalar_event(self, monkeypatch) -> None:
        est = _estimate(
            canonical_payload_bytes=200000,
            messages_json_bytes=50000,
            tools_json_bytes=5000,
        )
        agent = PreviewScriptedAgent(
            [finish_step()],
            budget_estimate=est,
        )
        cond = FakeCondenser(
            request=CondensationRequest(soft=True, reason="tokens"),
            tombstone=None,
        )
        monkeypatch.setenv("DISCO_CONTEXT_PACK", "0")

        log_calls: list = []
        monkeypatch.setattr(
            "disco.core.loop.driver.inspect_enabled",
            lambda: True,
        )
        monkeypatch.setattr(
            "disco.core.loop.driver.log_event",
            lambda *a, **kw: log_calls.append((a, dict(kw))),
        )

        loop, store = self._build_loop_for_telemetry(agent, cond)
        await loop.send_message("hi")
        events = await store.get_events(CID)
        view = View(
            messages=[LLMMessage(role="user", content="hi")],
            visible_seqs=[e.seq for e in events if e.seq is not None],
            total_events=len(events),
            forgotten_count=0,
        )
        initial_tools = loop._driver.tools_for_step(
            suppress_meta_tools=False,
            force_submit_only=False,
            force_read_tools=None,
            blocked_tools=frozenset(),
            mode=OperatingMode.INTERACTIVE,
            available_tools=loop.executor.available_tools(),
        )

        result = await loop._driver._try_request_budget_preview(
            view,
            events,
            OperatingMode.INTERACTIVE,
            None,
            initial_tools,
        )
        assert result is None  # no_progress → proceed to model call

        budget_events = [(a, kw) for a, kw in log_calls if a == ("request_budget.preview",)]
        assert len(budget_events) == 1
        _, ev = budget_events[0]
        assert ev["outcome"] == "no_progress"
        assert ev["kind"] == "soft"
        assert ev["cid"] == CID
        assert isinstance(ev["driver_context_window"], int)
        assert isinstance(ev["max_output_tokens"], (int, type(None)))
        assert isinstance(ev["canonical_payload_bytes"], int)
        assert isinstance(ev["messages_json_bytes"], int)
        assert isinstance(ev["tools_json_bytes"], int)
        assert isinstance(ev["estimated_input_tokens"], int)
        assert isinstance(ev["pressure_tokens"], int)
        assert isinstance(ev["message_count"], int)
        assert isinstance(ev["tool_count"], int)
        values_str = [str(v) for v in ev.values()]
        assert not any("finish_step" in s or "[summary]" in s for s in values_str)
        for v in ev.values():
            assert v is None or isinstance(v, (str, int)), f"unexpected type {type(v)} for {v}"

    async def test_should_condense_raises_emits_unavailable_scalar_event(
        self,
        monkeypatch,
    ) -> None:
        est = _estimate(
            canonical_payload_bytes=200000,
            messages_json_bytes=50000,
            tools_json_bytes=5000,
        )
        agent = PreviewScriptedAgent(
            [finish_step()],
            budget_estimate=est,
        )

        class RaisingShouldCondense(FakeCondenser):
            def should_condense(self, view, *, token_count):
                self.should_calls += 1
                raise RuntimeError("should_condense boom")

        cond = RaisingShouldCondense()

        log_calls: list = []
        monkeypatch.setattr(
            "disco.core.loop.driver.inspect_enabled",
            lambda: True,
        )
        monkeypatch.setattr(
            "disco.core.loop.driver.log_event",
            lambda *a, **kw: log_calls.append((a, dict(kw))),
        )

        loop, store = self._build_loop_for_telemetry(agent, cond)
        await loop.send_message("hi")
        events = await store.get_events(CID)
        view = View(
            messages=[LLMMessage(role="user", content="hi")],
            visible_seqs=[e.seq for e in events if e.seq is not None],
            total_events=len(events),
            forgotten_count=0,
        )
        initial_tools = loop._driver.tools_for_step(
            suppress_meta_tools=False,
            force_submit_only=False,
            force_read_tools=None,
            blocked_tools=frozenset(),
            mode=OperatingMode.INTERACTIVE,
            available_tools=loop.executor.available_tools(),
        )

        result = await loop._driver._try_request_budget_preview(
            view,
            events,
            OperatingMode.INTERACTIVE,
            None,
            initial_tools,
        )
        assert result is None  # unavailable → proceed to model call

        budget_events = [(a, kw) for a, kw in log_calls if a == ("request_budget.preview",)]
        assert len(budget_events) == 1
        _, ev = budget_events[0]
        assert ev["outcome"] == "unavailable"
        assert ev["cid"] == CID
        assert isinstance(ev["driver_context_window"], int)
        assert isinstance(ev["max_output_tokens"], (int, type(None)))
        assert isinstance(ev["canonical_payload_bytes"], int)
        assert isinstance(ev["messages_json_bytes"], int)
        assert isinstance(ev["tools_json_bytes"], int)
        assert isinstance(ev["estimated_input_tokens"], int)
        assert isinstance(ev["pressure_tokens"], int)
        assert isinstance(ev["message_count"], int)
        assert isinstance(ev["tool_count"], int)
        values_str = [str(v) for v in ev.values()]
        assert not any("finish_step" in s or "[summary]" in s for s in values_str)
        for v in ev.values():
            assert v is None or isinstance(v, (str, int)), f"unexpected type {type(v)} for {v}"

    async def test_logging_exception_does_not_change_behavior(self, monkeypatch) -> None:
        est = _estimate(
            canonical_payload_bytes=100,
            messages_json_bytes=50,
            tools_json_bytes=30,
        )
        agent = PreviewScriptedAgent(
            [finish_step()],
            budget_estimate=est,
        )
        cond = FakeCondenser(request=None)

        monkeypatch.setattr(
            "disco.core.loop.driver.inspect_enabled",
            lambda: True,
        )
        monkeypatch.setattr(
            "disco.core.loop.driver.log_event",
            lambda *a, **kw: (_ for _ in ()).throw(RuntimeError("logging broken")),
        )

        loop, store = self._build_loop_for_telemetry(agent, cond)
        await loop.send_message("hi")
        await loop.run()

        assert agent.calls == 1  # actual call still proceeds


# ---------------------------------------------------------------------------
# J — Provider preview proof: compare estimate scalars with _payload shape
# ---------------------------------------------------------------------------


class TestJ_ProviderPreviewProof:
    def test_preview_scalars_match_payload_shape(self) -> None:
        from disco.core.llm.openai_provider import _provider_request_shape

        captured_body: list[bytes] = []

        def handler(request: httpx.Request) -> httpx.Response:
            captured_body.append(request.read())
            return httpx.Response(
                200,
                json={
                    "model": "m1",
                    "choices": [{"message": {"content": "ok"}, "finish_reason": "stop"}],
                    "usage": {"prompt_tokens": 10, "completion_tokens": 2},
                },
            )

        provider = OpenAIProvider(
            "http://fake/v1",
            name="fake",
            transport=httpx.MockTransport(handler),
        )
        tools = [
            ToolSpec(
                name="test_tool",
                description="A test tool",
                parameters_schema={
                    "type": "object",
                    "properties": {"x": {"type": "integer"}},
                },
            )
        ]
        req = _req(
            messages=[
                LLMMessage(role="system", content="You are helpful."),
                LLMMessage(role="user", content="do it"),
            ],
            tools=tools,
            metadata={"driver_context_window": 65536},
        )

        # Get the preview estimate (zero HTTP calls)
        preview = provider.request_budget_preview(req, model="m1")
        assert preview is not None
        assert len(captured_body) == 0  # no HTTP call

        # Get the real payload and shape
        payload = provider._payload(req, "m1", stream=True)
        shape = _provider_request_shape(req, payload)

        # Compare every estimate scalar with shape
        assert preview.driver_context_window == shape.driver_context_window
        assert preview.max_output_tokens == shape.max_output_tokens
        assert preview.canonical_payload_bytes == shape.canonical_payload_bytes
        assert preview.messages_json_bytes == shape.messages_json_bytes
        assert preview.tools_json_bytes == shape.tools_json_bytes
        assert preview.message_count == shape.message_count
        assert preview.tool_count == shape.tool_count

    def test_preview_no_routing_decision_emitted(self) -> None:
        provider = OpenAIProvider(
            "http://fake/v1",
            name="fake",
            transport=httpx.MockTransport(
                lambda r: httpx.Response(
                    200,
                    json={
                        "model": "m1",
                        "choices": [{"message": {"content": "ok"}, "finish_reason": "stop"}],
                        "usage": {"prompt_tokens": 10, "completion_tokens": 2},
                    },
                )
            ),
        )
        result = provider.request_budget_preview(
            _req(metadata={"driver_context_window": 65536}),
            model="m1",
        )
        assert result is not None
        assert result.driver_context_window == 65536


# ---------------------------------------------------------------------------
# K — Fail-open: CompactionPolicy.default() raising still reaches model call
# ---------------------------------------------------------------------------


class TestK_CompactionPolicyDefaultRaising:
    async def test_compaction_policy_default_raises_still_calls_model(
        self,
        monkeypatch,
    ) -> None:
        est = _estimate(
            canonical_payload_bytes=200000,
            messages_json_bytes=50000,
            tools_json_bytes=5000,
        )
        agent = PreviewScriptedAgent(
            [finish_step()],
            budget_estimate=est,
        )
        cond = FakeCondenser(
            request=CondensationRequest(soft=True, reason="tokens"),
            tombstone=None,
        )
        monkeypatch.setenv("DISCO_CONTEXT_PACK", "1")
        loop, store = build_loop(agent, condenser=cond, conversation_id=CID)
        await loop.send_message("hi")
        events = await store.get_events(CID)
        view = View(
            messages=[LLMMessage(role="user", content="hi")],
            visible_seqs=[e.seq for e in events if e.seq is not None],
            total_events=len(events),
            forgotten_count=0,
        )
        monkeypatch.setattr(
            "disco.core.loop.driver.CompactionPolicy.default",
            lambda: (_ for _ in ()).throw(RuntimeError("compaction-policy boom")),
        )
        step, disp = await loop._driver.drive_step(view, events)
        assert disp == Disp.FALLTHROUGH
        assert step is not None
        assert step.finished
        assert agent.calls == 1


# ---------------------------------------------------------------------------
# L — Fail-open: View.of(events) raising inside condenser fallback
# ---------------------------------------------------------------------------


class TestL_ViewOfRaisingInCondenserFallback:
    async def test_view_of_raises_only_when_condenser_attempted_still_calls_model(
        self,
        monkeypatch,
    ) -> None:
        est = _estimate(
            canonical_payload_bytes=200000,
            messages_json_bytes=50000,
            tools_json_bytes=5000,
        )
        agent = PreviewScriptedAgent(
            [finish_step()],
            budget_estimate=est,
        )
        cond = FakeCondenser(
            request=CondensationRequest(soft=True, reason="tokens"),
            tombstone=CondensationEvent(
                forgotten_start_seq=1,
                forgotten_end_seq=1,
                summary="[ok]",
            ),
        )
        monkeypatch.setenv("DISCO_CONTEXT_PACK", "0")
        loop, store = build_loop(agent, condenser=cond, conversation_id=CID)
        await loop.send_message("hi")
        events = await store.get_events(CID)
        view = View(
            messages=[LLMMessage(role="user", content="hi")],
            visible_seqs=[e.seq for e in events if e.seq is not None],
            total_events=len(events),
            forgotten_count=0,
        )
        monkeypatch.setattr(
            "disco.core.loop.driver.View.of",
            lambda events: (_ for _ in ()).throw(RuntimeError("view-of boom")),
        )
        step, disp = await loop._driver.drive_step(view, events)
        assert disp == Disp.FALLTHROUGH
        assert step is not None
        assert step.finished
        assert agent.calls == 1
