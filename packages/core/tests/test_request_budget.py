"""Request budget preview tests — request-budget heuristic, provider/agent
preview integration, and driver compaction path."""

from __future__ import annotations

from typing import cast

import httpx
import pytest
import test_request_budget_loop as _loop_cases
from disco.core import CondensationEvent, CondensationRequest, LLMMessage, View
from disco.core.llm import (
    CallContext,
    CapabilityProfile,
    DefaultLLMRouter,
    Difficulty,
    ModelEntry,
    ModelRole,
    OperatingMode,
    OverflowSignal,
    RouterConfig,
    ToolSpec,
)
from disco.core.llm.openai_provider import OpenAIProvider
from disco.core.llm.request_budget import RequestBudgetEstimate
from disco.core.loop import RouterAgent
from disco.core.loop.control import Disp
from llm_fakes import FakeModelProvider, build_router, simple_config
from loop_fakes import (
    FakeCondenser,
    ScriptedAgent,
    build_loop,
    finish_step,
)
from request_budget_support import (
    BudgetAwareFakeProvider,
    PreviewScriptedAgent,
    RefRecordingAgent,
    _estimate,
    _req,
)


class TestRequestBudgetEstimate:
    def test_frozen(self) -> None:
        est = _estimate()
        with pytest.raises(AttributeError):
            est.driver_context_window = 1000  # type: ignore[misc]

    def test_rejects_bool_for_int_fields(self) -> None:
        with pytest.raises(ValueError, match="strictly positive"):
            RequestBudgetEstimate(
                driver_context_window=True,  # type: ignore[arg-type]
                max_output_tokens=None,
                canonical_payload_bytes=100,
                messages_json_bytes=50,
                tools_json_bytes=30,
                message_count=5,
                tool_count=2,
            )

    def test_rejects_negative(self) -> None:
        with pytest.raises(ValueError, match="strictly positive"):
            _estimate(driver_context_window=-1)
        with pytest.raises(ValueError, match="strictly positive"):
            _estimate(canonical_payload_bytes=-1)
        with pytest.raises(ValueError, match="nonnegative"):
            _estimate(message_count=-1)

    def test_rejects_zero_canonical(self) -> None:
        with pytest.raises(ValueError, match="strictly positive"):
            _estimate(canonical_payload_bytes=0)

    def test_rejects_zero_window(self) -> None:
        with pytest.raises(ValueError, match="strictly positive"):
            _estimate(driver_context_window=0)

    def test_payload_bytes_must_cover_sum(self) -> None:
        with pytest.raises(ValueError, match="must be"):
            _estimate(
                canonical_payload_bytes=100,
                messages_json_bytes=80,
                tools_json_bytes=30,
            )

    def test_payload_bytes_equal_to_sum_is_ok(self) -> None:
        est = _estimate(
            canonical_payload_bytes=100,
            messages_json_bytes=70,
            tools_json_bytes=30,
        )
        assert est.canonical_payload_bytes == 100

    def test_ceil_math(self) -> None:
        est = _estimate(
            canonical_payload_bytes=5000,
            messages_json_bytes=3000,
            tools_json_bytes=1500,
        )
        expected = (5000 * 4 + 12) // 13
        assert est.estimated_input_tokens == expected

    def test_ceil_math_exact(self) -> None:
        est = _estimate(
            canonical_payload_bytes=1300,
            messages_json_bytes=800,
            tools_json_bytes=500,
        )
        assert est.estimated_input_tokens == 400  # 1300*4/13 = 400 exactly

    def test_pressure_with_output_reserve(self) -> None:
        est = _estimate(max_output_tokens=4096, canonical_payload_bytes=10000)
        assert est.pressure_tokens == est.estimated_input_tokens + 4096

    def test_pressure_without_output_reserve(self) -> None:
        est = _estimate(max_output_tokens=None, canonical_payload_bytes=10000)
        assert est.pressure_tokens == est.estimated_input_tokens

    def test_rejects_invalid_max_output_tokens(self) -> None:
        with pytest.raises(ValueError, match="strictly positive"):
            _estimate(max_output_tokens=0)
        with pytest.raises(ValueError, match="strictly positive"):
            _estimate(max_output_tokens=-1)


class TestProviderPreview:
    def test_preview_emits_no_ledger_no_network(self) -> None:
        seen: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen.append(request)
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
        result = provider.request_budget_preview(
            _req(metadata={"driver_context_window": 65536}),
            model="m1",
        )
        assert seen == []  # no network
        assert result is not None
        assert result.driver_context_window == 65536
        assert result.canonical_payload_bytes > 0

    def test_preview_returns_none_when_window_missing(self) -> None:
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
            _req(messages=[LLMMessage(role="user", content="hi")], metadata=None),
            model="m1",
        )
        assert result is None  # no driver_context_window in metadata

    def test_preview_mirrors_payload_shape(self) -> None:
        """Preview uses exact _payload shape with system prompt, tools, prefill, cache."""
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
        preview = provider.request_budget_preview(req, model="m1")
        assert preview is not None
        assert preview.tool_count == 1
        assert preview.message_count >= 2
        assert len(captured_body) == 0  # no HTTP call was made

        payload = provider._payload(req, "m1", stream=True)
        assert payload["stream"] is True
        assert "tools" in payload
        assert payload["tools"][0]["function"]["name"] == "test_tool"

    def test_preview_shape_differs_with_tools(self) -> None:
        """Different tool surfaces produce different exact sizes."""
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

        base = _req(metadata={"driver_context_window": 65536})
        no_tools = provider.request_budget_preview(base, model="m1")
        assert no_tools is not None

        with_tools = provider.request_budget_preview(
            base.model_copy(
                update={
                    "tools": [
                        ToolSpec(
                            name="t1",
                            description="d1",
                            parameters_schema={"type": "object"},
                        ),
                        ToolSpec(
                            name="t2",
                            description="d2",
                            parameters_schema={"type": "object"},
                        ),
                    ]
                }
            ),
            model="m1",
        )
        assert with_tools is not None
        assert with_tools.tool_count == 2
        assert with_tools.canonical_payload_bytes > no_tools.canonical_payload_bytes
        assert with_tools.tools_json_bytes > 0
        assert no_tools.tools_json_bytes == 0

    def test_preview_planning_vs_execution_tools_differ(self) -> None:
        """Planning and execution tool surfaces produce different exact sizes."""
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

        plan_tools = [
            ToolSpec(
                name="submit_plan",
                description="Submit a plan",
                parameters_schema={"type": "object"},
            )
        ]
        exec_tools = [
            ToolSpec(
                name="file_write",
                description="Write a file",
                parameters_schema={
                    "type": "object",
                    "properties": {
                        "path": {"type": "string"},
                        "content": {"type": "string"},
                    },
                },
            )
        ]

        plan_req = _req(tools=plan_tools, metadata={"driver_context_window": 65536})
        exec_req = plan_req.model_copy(update={"tools": exec_tools})

        plan_est = provider.request_budget_preview(plan_req, model="m1")
        exec_est = provider.request_budget_preview(exec_req, model="m1")
        assert plan_est is not None
        assert exec_est is not None
        msg = f"plan={plan_est.canonical_payload_bytes} exec={exec_est.canonical_payload_bytes}"
        assert plan_est.canonical_payload_bytes != exec_est.canonical_payload_bytes, msg


class TestRouterPreview:
    def test_image_request_skips(self) -> None:
        local = BudgetAwareFakeProvider("ollama", text="hi", budget_estimate=_estimate())
        router, sink, _ = build_router(local=local)
        result = router.request_budget_preview(
            _req(
                messages=[
                    LLMMessage(role="user", content="", images=["http://img/1.png"]),
                ]
            ),
            context=CallContext(),
        )
        assert result is None
        assert len(sink.decisions) == 0

    def test_missing_entry_returns_none(self) -> None:
        local = BudgetAwareFakeProvider("ollama", text="hi", budget_estimate=_estimate())
        config = RouterConfig(
            models={},
            default_model="missing",
        )
        router, sink, _ = build_router(config=config, local=local)
        result = router.request_budget_preview(_req(), context=CallContext())
        assert result is None
        assert len(sink.decisions) == 0

    def test_missing_provider_returns_none(self) -> None:
        config = RouterConfig(
            models={
                "m1": ModelEntry(
                    model_id="m1",
                    provider="missing_provider",
                    context_window=65536,
                ),
            },
            default_model="m1",
        )
        local = BudgetAwareFakeProvider("ollama", text="hi", budget_estimate=_estimate())
        router, sink, _ = build_router(config=config, local=local)
        result = router.request_budget_preview(_req(), context=CallContext())
        assert result is None  # provider "missing_provider" not in providers dict
        assert len(sink.decisions) == 0

    def test_valid_provider_returns_estimate(self) -> None:
        config = simple_config()
        local = BudgetAwareFakeProvider("ollama", text="hi", budget_estimate=_estimate())
        router, sink, _ = build_router(config=config, local=local)
        result = router.request_budget_preview(
            _req(
                profile=CapabilityProfile(role=ModelRole.AGENT_DRIVER),
                messages=[
                    LLMMessage(role="system", content="You are helpful."),
                    LLMMessage(role="user", content="hi"),
                ],
            ),
            context=CallContext(),
        )
        assert result is not None
        assert result.driver_context_window == 65536

    def test_provider_without_preview_returns_none(self) -> None:
        local = FakeModelProvider("ollama", text="hi")
        router, sink, _ = build_router(local=local)
        result = router.request_budget_preview(_req(), context=CallContext())
        assert result is None

    def test_no_routing_decision_on_preview(self) -> None:
        local = BudgetAwareFakeProvider("ollama", text="hi", budget_estimate=_estimate())
        router, sink, _ = build_router(local=local)
        _result = router.request_budget_preview(_req(), context=CallContext())
        assert len(sink.decisions) == 0

    def test_inject_system_prompt_on_preview(self) -> None:
        local = BudgetAwareFakeProvider("ollama", text="hi", budget_estimate=_estimate())
        router, sink, _ = build_router(local=local)
        _result = router.request_budget_preview(
            _req(messages=[LLMMessage(role="user", content="hello")]),
            context=CallContext(),
        )
        assert len(local.seen_requests) == 1
        seen = local.seen_requests[0]
        assert seen.messages[0].role == "system"
        assert len(seen.messages[0].content) > 0


class TestRouterAgentPreview:
    def test_agent_preview_returns_estimate(self) -> None:
        local = BudgetAwareFakeProvider("ollama", text="hi", budget_estimate=_estimate())
        router, sink, _ = build_router(local=local)
        agent = RouterAgent(router)
        view = View(
            messages=[LLMMessage(role="user", content="hi")],
            visible_seqs=[],
            total_events=1,
            forgotten_count=0,
        )
        result = agent.request_budget_preview(
            view,
            tools=[],
            mode=OperatingMode.INTERACTIVE,
            overflow_signal=OverflowSignal(difficulty=Difficulty.ROUTINE),
            temperature=0.0,
        )
        assert result is not None
        assert result.driver_context_window == 65536
        assert len(sink.decisions) == 0

    def test_agent_preview_no_router_support_returns_none(self) -> None:
        class RouterWithoutPreview:
            pass

        agent = RouterAgent(
            cast(DefaultLLMRouter, RouterWithoutPreview())  # type: ignore[valid-type]
        )
        view = View(
            messages=[LLMMessage(role="user", content="hi")],
            visible_seqs=[],
            total_events=1,
            forgotten_count=0,
        )
        result = agent.request_budget_preview(
            view,
            tools=[],
            mode=OperatingMode.INTERACTIVE,
            overflow_signal=OverflowSignal(difficulty=Difficulty.ROUTINE),
        )
        assert result is None

    def test_different_tool_surfaces_produce_different_previews(self) -> None:
        from disco.core.llm import InMemoryRoutingSink

        large_est = _estimate(
            canonical_payload_bytes=10000,
            tools_json_bytes=3000,
            tool_count=5,
        )
        small_est = _estimate(
            canonical_payload_bytes=5000,
            tools_json_bytes=0,
            tool_count=0,
        )

        class PreviewProvider(FakeModelProvider):
            def request_budget_preview(
                self,
                req,
                model,  # noqa: ANN001
            ) -> RequestBudgetEstimate | None:
                self.seen_requests.append(req)
                if req.tools and len(req.tools) > 0:
                    return large_est
                return small_est

        provider = PreviewProvider("ollama", text="hi")
        config = simple_config()
        sink = InMemoryRoutingSink()
        router = DefaultLLMRouter(config, {"ollama": provider}, sink=sink)
        agent = RouterAgent(router)

        view = View(
            messages=[LLMMessage(role="user", content="hi")],
            visible_seqs=[],
            total_events=1,
            forgotten_count=0,
        )

        no_tools = agent.request_budget_preview(
            view,
            tools=[],
            mode=OperatingMode.INTERACTIVE,
            overflow_signal=OverflowSignal(difficulty=Difficulty.ROUTINE),
        )
        with_tools = agent.request_budget_preview(
            view,
            tools=[
                ToolSpec(
                    name="t",
                    description="d",
                    parameters_schema={"type": "object"},
                )
            ],
            mode=OperatingMode.INTERACTIVE,
            overflow_signal=OverflowSignal(difficulty=Difficulty.ROUTINE),
        )
        assert no_tools is not None
        assert with_tools is not None
        assert with_tools.tool_count > no_tools.tool_count
        assert with_tools.tools_json_bytes > no_tools.tools_json_bytes


class TestInspetEvidence:
    def test_estimate_fields_are_scalar(self) -> None:
        est = _estimate()
        assert isinstance(est.driver_context_window, int)
        assert isinstance(est.canonical_payload_bytes, int)
        assert isinstance(est.messages_json_bytes, int)
        assert isinstance(est.tools_json_bytes, int)
        assert isinstance(est.message_count, int)
        assert isinstance(est.tool_count, int)
        assert isinstance(est.estimated_input_tokens, int)
        assert isinstance(est.pressure_tokens, int)
        if est.max_output_tokens is not None:
            assert isinstance(est.max_output_tokens, int)


# -----------------------------------------------------------------
# Driver-level budget preview integration tests
# -----------------------------------------------------------------


CID = "conv"


# ---------------------------------------------------------------------------
# A — Under-threshold same-tool-object proof
# ---------------------------------------------------------------------------


class TestA_UnderThresholdProof:
    async def test_preview_and_first_call_share_same_tool_list_object(self) -> None:
        est = _estimate(
            canonical_payload_bytes=100,
            messages_json_bytes=50,
            tools_json_bytes=30,
        )
        agent = RefRecordingAgent(
            [finish_step()],
            budget_estimate=est,
        )
        cond = FakeCondenser(request=None)
        loop, store = build_loop(agent, condenser=cond, conversation_id=CID)
        await loop.send_message("hi")
        await loop.run()

        assert agent.preview_calls == 1
        assert agent.calls == 1
        # prove same list object, not just same content
        assert agent.preview_tool_lists[0] is agent.step_tool_lists[0]

    async def test_should_condense_receives_pressure_tokens(self) -> None:
        est = _estimate(
            canonical_payload_bytes=200000,
            messages_json_bytes=50000,
            tools_json_bytes=5000,
        )
        agent = RefRecordingAgent(
            [finish_step()],
            budget_estimate=est,
        )
        _seen_tokens: list = []

        class TokenSpyCondenser(FakeCondenser):
            def should_condense(self, view, *, token_count):
                self.should_calls += 1
                _seen_tokens.append(token_count)
                return None

        cond = TokenSpyCondenser(request=None)
        loop, store = build_loop(agent, condenser=cond, conversation_id=CID)
        await loop.send_message("hi")
        await loop.run()

        assert est.pressure_tokens in _seen_tokens


# ---------------------------------------------------------------------------
# B — Preview absent / raises / None (each: one actual call)
# ---------------------------------------------------------------------------


class TestB_PreviewAbsentRaisesNone:
    async def test_preview_method_absent_makes_one_call(self) -> None:
        agent = ScriptedAgent([finish_step()])
        loop, store = build_loop(agent, conversation_id=CID)
        await loop.send_message("hi")
        await loop.run()
        assert agent.calls == 1

    async def test_preview_raises_makes_one_call(self, monkeypatch) -> None:
        class RaisingPreviewAgent(ScriptedAgent):
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
                raise RuntimeError("boom")

        agent = RaisingPreviewAgent([finish_step()])
        loop, store = build_loop(agent, conversation_id=CID)
        await loop.send_message("hi")
        await loop.run()
        assert agent.calls == 1

    async def test_preview_returns_none_makes_one_call(self) -> None:
        class NonePreviewAgent(ScriptedAgent):
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
                return None

        agent = NonePreviewAgent([finish_step()])
        loop, store = build_loop(agent, conversation_id=CID)
        await loop.send_message("hi")
        await loop.run()
        assert agent.calls == 1


# ---------------------------------------------------------------------------
# C — Snips first (no condenser, no model calls, CONTINUE)
# ---------------------------------------------------------------------------


class TestC_SnipsFirst:
    async def test_snips_first_assert_emit_sequence(self, monkeypatch) -> None:
        trace: list[str] = []

        class TracingLoopWrapper:
            def __init__(self, loop):
                self._loop = loop
                self.conversation_id = loop.conversation_id
                self.store = loop.store
                self.agent = loop.agent
                self.executor = loop.executor
                self.condenser = loop.condenser
                self.summarizer = loop.summarizer
                self._assist = loop._assist

            async def _assert_current_agent_view(self):
                trace.append("assert")
                await self._loop._assert_current_agent_view()

            async def _emit(self, event):
                trace.append("emit")
                return await self._loop._emit(event)

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
        loop, store = build_loop(agent, condenser=cond, conversation_id=CID)
        await loop.send_message("hi")

        events = await store.get_events(CID)
        view = View(
            messages=[LLMMessage(role="user", content="hi")],
            visible_seqs=[e.seq for e in events if e.seq is not None],
            total_events=len(events),
            forgotten_count=0,
        )

        # Two local snips to emit
        snip_a = CondensationEvent(forgotten_start_seq=1, forgotten_end_seq=1, summary="[snip-a]")
        snip_b = CondensationEvent(forgotten_start_seq=2, forgotten_end_seq=2, summary="[snip-b]")

        monkeypatch.setattr(
            "disco.core.loop.driver.context_pack_enabled",
            lambda: True,
        )
        monkeypatch.setattr(
            "disco.core.loop.driver.context_compact_if_needed",
            lambda events, policy, *, protected_seqs=frozenset(), pressure_chars=None: [
                snip_a,
                snip_b,
            ],
        )

        wrapper = TracingLoopWrapper(loop)
        loop._driver._loop = wrapper  # type: ignore[assignment]

        trace.clear()
        result = await loop._driver._try_request_budget_preview(
            view,
            events,
            OperatingMode.INTERACTIVE,
            None,
            [],
        )
        assert result == Disp.CONTINUE
        assert trace == ["assert", "emit", "assert", "emit"]
        assert cond.condense_calls == 0

        # Verify canonical_payload_bytes was passed as pressure_chars
        _pressure_chars: list = []

        def _spy_compact(events, policy, *, protected_seqs=frozenset(), pressure_chars=None):
            _pressure_chars.append(pressure_chars)
            return [snip_a, snip_b]

        monkeypatch.setattr(
            "disco.core.loop.driver.context_compact_if_needed",
            _spy_compact,
        )
        result = await loop._driver._try_request_budget_preview(
            view,
            events,
            OperatingMode.INTERACTIVE,
            None,
            [],
        )
        assert _pressure_chars == [est.canonical_payload_bytes]


# ---------------------------------------------------------------------------
# D — Condenser fallback (receives original events + View.of(events))
# ---------------------------------------------------------------------------


class TestD_CondenserFallback:
    async def test_condenser_receives_events_and_fresh_view(self) -> None:
        est = _estimate(
            canonical_payload_bytes=200000,
            messages_json_bytes=50000,
            tools_json_bytes=5000,
        )
        agent = PreviewScriptedAgent(
            [finish_step()],
            budget_estimate=est,
        )

        _condense_events: list = []
        _condense_view: View | None = None
        _trace: list[str] = []

        class TracingWrapper:
            def __init__(self, loop):
                self._loop = loop
                self.conversation_id = loop.conversation_id
                self.store = loop.store
                self.agent = loop.agent
                self.executor = loop.executor
                self.condenser = loop.condenser
                self.summarizer = loop.summarizer
                self._assist = loop._assist

            async def _assert_current_agent_view(self):
                _trace.append("assert")

            async def _emit(self, event):
                _trace.append("emit")
                return await self._loop._emit(event)

        class RecordingCondenser(FakeCondenser):
            def should_condense(self, view, *, token_count):
                self.should_calls += 1
                if token_count is not None and token_count > 50000:
                    return self._request
                return None

            async def condense(
                self,
                events,
                view,
                *,
                summarizer,
                reason="tokens",
                artifact_paths=None,
            ) -> CondensationEvent | None:
                nonlocal _condense_events, _condense_view
                _trace.append("condense")
                if self.condense_calls > 0:
                    return None
                self.condense_calls += 1
                _condense_events = list(events)
                _condense_view = view
                return CondensationEvent(
                    forgotten_start_seq=1, forgotten_end_seq=1, summary="[summary]"
                )

        cond = RecordingCondenser(
            request=CondensationRequest(soft=True, reason="tokens"),
        )
        # Disable context_pack so we take the condenser path
        monkeypatch = pytest.MonkeyPatch()
        monkeypatch.setenv("DISCO_CONTEXT_PACK", "0")
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

            wrapper = TracingWrapper(loop)
            loop._driver._loop = wrapper  # type: ignore[assignment]

            _trace.clear()
            result = await loop._driver._try_request_budget_preview(
                view,
                events,
                OperatingMode.INTERACTIVE,
                None,
                [],
            )
        finally:
            monkeypatch.undo()

        assert result == Disp.CONTINUE
        assert _trace == ["assert", "condense", "assert", "emit"]
        assert len(_condense_events) > 0
        view_of = View.of(_condense_events)
        assert _condense_view is not None
        assert _condense_view.messages == view_of.messages


# ---------------------------------------------------------------------------
# E — Fail-open parameterization
# ---------------------------------------------------------------------------


class TestE_FailOpen:
    async def test_should_condense_raises_proceeds_to_model_call(self) -> None:
        est = _estimate(
            canonical_payload_bytes=200000,
            messages_json_bytes=50000,
            tools_json_bytes=5000,
        )

        class RaisingCondenser(FakeCondenser):
            def should_condense(self, view, *, token_count):
                self.should_calls += 1
                if token_count is not None and token_count > 50000:
                    raise RuntimeError("should_condense boom")
                return None

        agent = PreviewScriptedAgent(
            [finish_step()],
            budget_estimate=est,
        )
        cond = RaisingCondenser()
        monkeypatch_env = pytest.MonkeyPatch()
        monkeypatch_env.setenv("DISCO_CONTEXT_PACK", "0")
        try:
            loop, store = build_loop(agent, condenser=cond, conversation_id=CID)
            await loop.send_message("hi")
            await loop.run()
        finally:
            monkeypatch_env.undo()

        assert agent.calls == 1

    async def test_context_pack_raises_then_condenser_returns_none_calls_model(
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
        monkeypatch.setattr(
            "disco.core.loop.driver.context_compact_if_needed",
            lambda *a, **kw: (_ for _ in ()).throw(RuntimeError("context-pack boom")),
        )
        loop, store = build_loop(agent, condenser=cond, conversation_id=CID)
        await loop.send_message("hi")
        await loop.run()

        assert agent.calls == 1  # proceeded to actual call

    async def test_condenser_raises_proceeds_to_model_call(self) -> None:
        est = _estimate(
            canonical_payload_bytes=200000,
            messages_json_bytes=50000,
            tools_json_bytes=5000,
        )

        class RaisingCondenseCondenser(FakeCondenser):
            def should_condense(self, view, *, token_count):
                self.should_calls += 1
                if token_count is not None and token_count > 50000:
                    return CondensationRequest(soft=True, reason="tokens")
                return None

            async def condense(
                self,
                events,
                view,
                *,
                summarizer,
                reason="tokens",
                artifact_paths=None,
            ):
                raise RuntimeError("condense boom")

        agent = PreviewScriptedAgent(
            [finish_step()],
            budget_estimate=est,
        )
        cond = RaisingCondenseCondenser()
        monkeypatch_env = pytest.MonkeyPatch()
        monkeypatch_env.setenv("DISCO_CONTEXT_PACK", "0")
        try:
            loop, store = build_loop(agent, condenser=cond, conversation_id=CID)
            await loop.send_message("hi")
            await loop.run()
        finally:
            monkeypatch_env.undo()

        assert agent.calls == 1

    async def test_agent_view_superseded_at_snip_assert_propagates(
        self,
        monkeypatch,
    ) -> None:
        from disco.core.loop.engine import AgentViewSuperseded

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

        class RaisingAssertWrapper:
            def __init__(self, loop):
                self._loop = loop
                self.conversation_id = loop.conversation_id
                self.store = loop.store
                self.agent = loop.agent
                self.executor = loop.executor
                self.condenser = loop.condenser
                self.summarizer = loop.summarizer
                self._assist = loop._assist

            async def _assert_current_agent_view(self):
                raise AgentViewSuperseded("view superseded in snip")

            async def _emit(self, event):
                return await self._loop._emit(event)

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
        loop, store = build_loop(agent, condenser=cond, conversation_id=CID)
        await loop.send_message("hi")

        events = await store.get_events(CID)
        view = View(
            messages=[LLMMessage(role="user", content="hi")],
            visible_seqs=[e.seq for e in events if e.seq is not None],
            total_events=len(events),
            forgotten_count=0,
        )

        wrapper = RaisingAssertWrapper(loop)
        loop._driver._loop = wrapper  # type: ignore[assignment]

        with pytest.raises(AgentViewSuperseded):
            await loop._driver._try_request_budget_preview(
                view,
                events,
                OperatingMode.INTERACTIVE,
                None,
                [],
            )

        events_after = await store.get_events(CID)
        assert not any(
            isinstance(e, CondensationEvent) and e.summary == "[snip]" for e in events_after
        )


# Preserve the accepted pytest/static identities while the implementation
# bodies live in the bounded companion module.  Each compatibility method
# delegates to the exact extracted case and therefore exercises the same
# assertions and fixtures under its historical node ID.
class TestF_RetryRecomputation(_loop_cases.TestF_RetryRecomputation):
    async def test_provider_unavailable_retry_recomputes_tools(self) -> None:
        await super().test_provider_unavailable_retry_recomputes_tools()


class TestG_WindowExceeded(_loop_cases.TestG_WindowExceeded):
    async def test_context_window_exceeded_after_preview_still_hard_resets(
        self,
        monkeypatch,
    ) -> None:
        await super().test_context_window_exceeded_after_preview_still_hard_resets(monkeypatch)


class TestH_OutputReserve(_loop_cases.TestH_OutputReserve):
    async def test_no_output_reserve_below_cutoff_calls_model(self) -> None:
        await super().test_no_output_reserve_below_cutoff_calls_model()

    async def test_output_reserve_crosses_cutoff_compacts_no_model_call(self) -> None:
        await super().test_output_reserve_crosses_cutoff_compacts_no_model_call()


class TestI_TelemetryMonkeypatch(_loop_cases.TestI_TelemetryMonkeypatch):
    async def test_inspect_off_emits_zero_events(self, monkeypatch) -> None:
        await super().test_inspect_off_emits_zero_events(monkeypatch)

    async def test_within_budget_emits_scalar_event(self, monkeypatch) -> None:
        await super().test_within_budget_emits_scalar_event(monkeypatch)

    async def test_unavailable_emits_no_scalar_fields(self, monkeypatch) -> None:
        await super().test_unavailable_emits_no_scalar_fields(monkeypatch)

    async def test_compacted_context_pack_emits_scalar_event(self, monkeypatch) -> None:
        await super().test_compacted_context_pack_emits_scalar_event(monkeypatch)

    async def test_no_progress_emits_scalar_event(self, monkeypatch) -> None:
        await super().test_no_progress_emits_scalar_event(monkeypatch)

    async def test_should_condense_raises_emits_unavailable_scalar_event(
        self,
        monkeypatch,
    ) -> None:
        await super().test_should_condense_raises_emits_unavailable_scalar_event(monkeypatch)

    async def test_logging_exception_does_not_change_behavior(self, monkeypatch) -> None:
        await super().test_logging_exception_does_not_change_behavior(monkeypatch)


class TestJ_ProviderPreviewProof(_loop_cases.TestJ_ProviderPreviewProof):
    def test_preview_scalars_match_payload_shape(self) -> None:
        super().test_preview_scalars_match_payload_shape()

    def test_preview_no_routing_decision_emitted(self) -> None:
        super().test_preview_no_routing_decision_emitted()


class TestK_CompactionPolicyDefaultRaising(_loop_cases.TestK_CompactionPolicyDefaultRaising):
    async def test_compaction_policy_default_raises_still_calls_model(
        self,
        monkeypatch,
    ) -> None:
        await super().test_compaction_policy_default_raises_still_calls_model(monkeypatch)


class TestL_ViewOfRaisingInCondenserFallback(_loop_cases.TestL_ViewOfRaisingInCondenserFallback):
    async def test_view_of_raises_only_when_condenser_attempted_still_calls_model(
        self,
        monkeypatch,
    ) -> None:
        await super().test_view_of_raises_only_when_condenser_attempted_still_calls_model(
            monkeypatch
        )
