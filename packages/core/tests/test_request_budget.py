"""Request budget preview tests — request-budget heuristic, provider/agent
preview integration, and driver compaction path."""

from __future__ import annotations

from typing import Any, cast

import httpx
import pytest
from disco.core import CondensationEvent, CondensationRequest, ErrorEvent, LLMMessage, View
from disco.core.llm import (
    CallContext,
    CapabilityProfile,
    CompletionRequest,
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

MSGS = [
    LLMMessage(role="system", content="You are helpful."),
    LLMMessage(role="user", content="test"),
]


def _req(**kw: Any) -> CompletionRequest:
    defaults: dict[str, Any] = {
        "profile": CapabilityProfile(role=ModelRole.AGENT_DRIVER),
        "messages": list(MSGS),
        "request_id": "req_11111111111111111111111111111111",
    }
    defaults.update(kw)
    return CompletionRequest(**defaults)  # type: ignore[arg-type]


def _estimate(**kw: int | None) -> RequestBudgetEstimate:
    defaults: dict[str, int | None] = {
        "driver_context_window": 65536,
        "max_output_tokens": None,
        "canonical_payload_bytes": 5000,
        "messages_json_bytes": 3000,
        "tools_json_bytes": 1500,
        "message_count": 3,
        "tool_count": 1,
    }
    defaults.update(kw)
    return RequestBudgetEstimate(**defaults)  # type: ignore[arg-type]


class BudgetAwareFakeProvider(FakeModelProvider):
    """A FakeModelProvider that also supports request_budget_preview."""

    def __init__(self, name: str = "fake", *, budget_estimate=None, **kw: Any) -> None:
        super().__init__(name, **kw)
        self._budget_estimate = budget_estimate

    def request_budget_preview(
        self, req: CompletionRequest, model: str
    ) -> RequestBudgetEstimate | None:
        self.seen_requests.append(req)
        return self._budget_estimate


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


class PreviewScriptedAgent(ScriptedAgent):
    """A ScriptedAgent that also supports request_budget_preview."""

    def __init__(self, steps, *, budget_estimate=None, **kw):
        super().__init__(steps, **kw)
        self._budget_estimate = budget_estimate
        self.preview_calls = 0
        self.preview_tools_seen: list = []
        self.preview_views: list = []

    def request_budget_preview(
        self, view, tools, *, mode, overflow_signal, temperature=None, assist=False
    ) -> RequestBudgetEstimate | None:
        self.preview_calls += 1
        self.preview_tools_seen.append(tools)
        self.preview_views.append(view)
        return self._budget_estimate


class RefRecordingAgent(ScriptedAgent):
    """An agent that records raw tool-list references (not just names)."""

    def __init__(self, steps, *, budget_estimate=None, **kw):
        super().__init__(steps, **kw)
        self._budget_estimate = budget_estimate
        self.preview_calls = 0
        self.preview_tool_lists: list = []
        self.step_tool_lists: list = []

    def request_budget_preview(
        self, view, tools, *, mode, overflow_signal, temperature=None, assist=False
    ) -> RequestBudgetEstimate | None:
        self.preview_calls += 1
        self.preview_tool_lists.append(tools)
        return self._budget_estimate

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
        self.step_tool_lists.append(tools)
        return await super().step(
            view,
            tools,
            mode=mode,
            overflow_signal=overflow_signal,
            on_stream=on_stream,
            temperature=temperature,
            assist=assist,
            attempt=attempt,
            provider_prefs=provider_prefs,
        )


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
