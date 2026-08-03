"""RED contracts for per-run driver context resolution and composition."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import Any

import pytest
from disco.agent_server import ConversationRuntime
from disco.agent_server.driver_context import (  # noqa: F401 — collection failure expected
    DriverContextResolutionError,
    DriverContextResolver,
    ResolvedDriverContext,
)
from disco.core import SqliteEventStore
from disco.core.events import EventSource, LLMMessage, MessageEvent
from disco.core.llm import (
    CompletionResponse,
    DefaultLLMRouter,
    ModelEntry,
    RouterConfig,
    StreamChunk,
    TokenUsage,
)


def _entry(**kw: Any) -> ModelEntry:
    defaults: dict[str, Any] = dict(
        model_id="test/model", provider="fake", context_window=8192, base_url="http://h/v1"
    )
    defaults.update(kw)
    return ModelEntry(**defaults)


def _fake_router(cfg: RouterConfig, text: str = "ok") -> DefaultLLMRouter:
    return DefaultLLMRouter(cfg, {"fake": _FakeProvider(text)})


def _runtime(
    text: str = "ok",
    *,
    model_key: str = "m",
    context_window: int = 8192,
) -> ConversationRuntime:
    store = SqliteEventStore(":memory:")
    cfg = RouterConfig(
        models={
            model_key: ModelEntry(
                model_id="test/model",
                provider="fake",
                context_window=context_window,
                base_url="http://h/v1",
            )
        },
        default_model=model_key,
    )
    return ConversationRuntime(store, router=_fake_router(cfg, text))


async def _seed_conversation(rt: ConversationRuntime, cid: str) -> int:
    rt._store.create_conversation(cid, owner_id="local", surface="build")
    rt.set_surface(cid, "build")
    stored = await rt._store.append(
        cid,
        MessageEvent(
            source=EventSource.USER,
            message=LLMMessage(role="user", content="build it"),
        ),
    )
    assert isinstance(stored.seq, int)
    return stored.seq


class _FakeProvider:
    name = "fake"

    def __init__(self, text: str) -> None:
        self._text = text

    async def complete(self, req, *, model):
        return CompletionResponse(
            text=self._text,
            tool_calls=[],
            usage=TokenUsage(input_tokens=1, output_tokens=1),
            finish_reason="stop",
            model_used=model,
            request_id=req.request_id,
            routing=None,
        )

    async def stream_complete(self, req, *, model):
        yield StreamChunk(delta_text=self._text)
        yield StreamChunk(done=True, final=await self.complete(req, model=model))

    def supports(self, requirement, *, model):
        return True


class TestResolvedDriverContext:
    def test_immutable(self) -> None:
        ts = datetime.now(UTC)
        ctx = ResolvedDriverContext(
            model_key="k",
            provider="p",
            model_id="m",
            context_window=8192,
            source="configured",
            resolved_at=ts,
        )
        with pytest.raises(AttributeError):
            ctx.context_window = 999  # type: ignore[misc]

    @pytest.mark.parametrize("bad", [0, -1, True, 1.5, "8192", None])
    def test_non_positive_or_non_integer_context_window_raises(self, bad: Any) -> None:
        ts = datetime.now(UTC)
        with pytest.raises(ValueError):
            ResolvedDriverContext(
                model_key="k",
                provider="p",
                model_id="m",
                context_window=bad,
                source="configured",
                resolved_at=ts,
            )


class TestDriverContextResolver:
    @pytest.mark.asyncio
    async def test_positive_live_n_ctx_wins_with_live_provenance(self) -> None:
        async def probe() -> dict[str, Any]:
            return {"n_ctx": 32768}

        resolver = DriverContextResolver(timeout_s=5.0)
        result = await resolver.resolve(
            model_key="k",
            entry=_entry(context_window=8192),
            probe=probe,
            unavailable_source=None,
        )
        assert result.context_window == 32768
        assert result.source == "live"

    @pytest.mark.asyncio
    async def test_timeout_falls_back_to_configured_with_distinct_provenance(self) -> None:
        latch = asyncio.Event()

        async def probe() -> dict[str, Any]:
            await latch.wait()
            return {"n_ctx": 999}

        resolver = DriverContextResolver(timeout_s=0.02)
        result = await resolver.resolve(
            model_key="k",
            entry=_entry(context_window=16384),
            probe=probe,
            unavailable_source=None,
        )
        latch.set()
        assert result.context_window == 16384
        assert result.source == "configured_timeout"

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("outcome", "expected_source"),
        [
            ("error", "configured_error"),
            ("zero", "configured_miss"),
            ("missing", "configured_miss"),
        ],
    )
    async def test_unsuccessful_probe_uses_configured_fallback(
        self, outcome: str, expected_source: str
    ) -> None:
        async def probe() -> dict[str, Any]:
            if outcome == "error":
                raise RuntimeError("backend unreachable")
            return {"n_ctx": 0} if outcome == "zero" else {}

        result = await DriverContextResolver(timeout_s=5.0).resolve(
            model_key="k",
            entry=_entry(context_window=4096),
            probe=probe,
            unavailable_source=None,
        )
        assert (result.context_window, result.source) == (4096, expected_source)

    @pytest.mark.asyncio
    @pytest.mark.parametrize("bad_n_ctx", [True, False, "32768", 3.14, None, [], {}])
    async def test_non_integer_live_n_ctx_never_becomes_a_window(self, bad_n_ctx: Any) -> None:
        async def probe() -> dict[str, Any]:
            return {"n_ctx": bad_n_ctx}

        result = await DriverContextResolver(timeout_s=5.0).resolve(
            model_key="k",
            entry=_entry(context_window=4096),
            probe=probe,
            unavailable_source=None,
        )
        assert result.context_window == 4096
        assert result.source == "configured_error"

    @pytest.mark.asyncio
    @pytest.mark.parametrize("configured", [0, -1])
    async def test_raises_without_any_positive_window(self, configured: int) -> None:
        async def probe() -> dict[str, Any]:
            return {"n_ctx": 0}

        with pytest.raises(DriverContextResolutionError):
            await DriverContextResolver(timeout_s=5.0).resolve(
                model_key="k",
                entry=_entry(context_window=configured),
                probe=probe,
                unavailable_source=None,
            )

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("unavailable_source", "expected_source"),
        [("unapproved_origin", "unapproved_origin"), (None, "configured_unavailable")],
    )
    async def test_no_probe_uses_configured_fallback(
        self, unavailable_source: str | None, expected_source: str
    ) -> None:
        result = await DriverContextResolver(timeout_s=5.0).resolve(
            model_key="k",
            entry=_entry(context_window=4096),
            probe=None,
            unavailable_source=unavailable_source,
        )
        assert (result.context_window, result.source) == (4096, expected_source)


class TestDriverContextResolverSingleflight:
    @pytest.mark.asyncio
    async def test_same_base_url_and_model_shares_one_probe(self) -> None:
        entered = asyncio.Event()
        release = asyncio.Event()
        calls = 0

        async def probe() -> dict[str, Any]:
            nonlocal calls
            calls += 1
            entered.set()
            await release.wait()
            return {"n_ctx": 16384}

        resolver = DriverContextResolver(timeout_s=5.0)
        entry = _entry()
        t1 = asyncio.create_task(
            resolver.resolve("ka", entry, probe=probe, unavailable_source=None)
        )
        await entered.wait()
        joined = asyncio.Event()

        async def second_waiter() -> ResolvedDriverContext:
            joined.set()
            return await resolver.resolve("kb", entry, probe=probe, unavailable_source=None)

        t2 = asyncio.create_task(second_waiter())
        await asyncio.wait_for(joined.wait(), timeout=1.0)
        release.set()
        r1, r2 = await asyncio.gather(t1, t2)
        assert calls == 1
        assert r1.context_window == r2.context_window == 16384

    @pytest.mark.asyncio
    async def test_different_models_same_base_url_run_independently(self) -> None:
        gate_a = asyncio.Event()
        gate_b = asyncio.Event()

        async def probe_a() -> dict[str, Any]:
            await gate_a.wait()
            return {"n_ctx": 100}

        async def probe_b() -> dict[str, Any]:
            await gate_b.wait()
            return {"n_ctx": 200}

        resolver = DriverContextResolver(timeout_s=5.0)
        base = "http://shared/v1"
        e_a = _entry(model_id="m1", base_url=base)
        e_b = _entry(model_id="m2", base_url=base)
        t1 = asyncio.create_task(
            resolver.resolve("ka", e_a, probe=probe_a, unavailable_source=None)
        )
        t2 = asyncio.create_task(
            resolver.resolve("kb", e_b, probe=probe_b, unavailable_source=None)
        )
        gate_a.set()
        gate_b.set()
        r1, r2 = await asyncio.gather(t1, t2)
        assert r1.context_window == 100
        assert r2.context_window == 200

    @pytest.mark.asyncio
    async def test_timeout_of_one_waiter_does_not_cancel_shared_probe(self) -> None:
        entered = asyncio.Event()
        release = asyncio.Event()
        calls = 0

        async def probe() -> dict[str, Any]:
            nonlocal calls
            calls += 1
            entered.set()
            await release.wait()
            return {"n_ctx": 32768}

        resolver = DriverContextResolver(timeout_s=0.02)
        entry = _entry(context_window=8192)
        t1 = asyncio.create_task(
            resolver.resolve("k1", entry, probe=probe, unavailable_source=None)
        )
        await entered.wait()
        r1 = await asyncio.wait_for(t1, timeout=5.0)
        assert r1.context_window == 8192
        assert r1.source == "configured_timeout"
        assert calls == 1

        joined = asyncio.Event()

        async def second_waiter() -> ResolvedDriverContext:
            joined.set()
            return await resolver.resolve("k2", entry, probe=probe, unavailable_source=None)

        t2 = asyncio.create_task(second_waiter())
        await asyncio.wait_for(joined.wait(), timeout=1.0)
        release.set()
        r2 = await asyncio.wait_for(t2, timeout=5.0)
        assert r2.context_window == 32768
        assert r2.source == "live"
        assert calls == 1

    @pytest.mark.asyncio
    async def test_every_joined_waiter_has_an_independent_bound(self) -> None:
        entered = asyncio.Event()
        release = asyncio.Event()
        calls = 0

        async def probe() -> dict[str, Any]:
            nonlocal calls
            calls += 1
            entered.set()
            await release.wait()
            return {"n_ctx": 32768}

        resolver = DriverContextResolver(timeout_s=0.02)
        entry = _entry(context_window=8192)
        first = asyncio.create_task(
            resolver.resolve("k1", entry, probe=probe, unavailable_source=None)
        )
        await entered.wait()
        second = asyncio.create_task(
            resolver.resolve("k2", entry, probe=probe, unavailable_source=None)
        )

        first_result, second_result = await asyncio.wait_for(
            asyncio.gather(first, second), timeout=1.0
        )
        assert first_result.source == second_result.source == "configured_timeout"
        assert first_result.context_window == second_result.context_window == 8192
        assert calls == 1

        shared = next(iter(resolver._in_flight.values()))
        assert not shared.cancelled()
        release.set()
        assert await asyncio.wait_for(shared, timeout=1.0) == {"n_ctx": 32768}
        await asyncio.sleep(0)
        assert not resolver._in_flight


class TestConversationRuntimeDriverContextSeams:
    """The runtime's kick(), loop composition, and workspace admission paths
    must be gated by driver-context resolution."""

    @pytest.mark.asyncio
    async def test_kick_registers_before_resolution_and_composition(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        rt = _runtime("ok")
        cid = "cold-override"
        user_seq = await _seed_conversation(rt, cid)
        resolved = ResolvedDriverContext(
            model_key="m",
            provider="fake",
            model_id="test/model",
            context_window=16384,
            source="live",
            resolved_at=datetime.now(UTC),
        )
        resolve_entered = asyncio.Event()
        release_resolution = asyncio.Event()
        composed = asyncio.Event()
        admitted = asyncio.Event()
        hold_admission = asyncio.Event()
        loop_sentinel = object()
        create_run_task_calls = 0
        stale_backend_checked = False
        original_create_run_task = rt._run_supervisor.create_task

        def create_run_task(*args: Any, **kwargs: Any) -> tuple[asyncio.Task[Any], int]:
            nonlocal create_run_task_calls
            create_run_task_calls += 1
            return original_create_run_task(*args, **kwargs)

        def evict_stale_backend(requested_cid: str) -> None:
            nonlocal stale_backend_checked
            assert requested_cid == cid
            assert rt._run_registry.task(requested_cid) is None
            stale_backend_checked = True

        async def resolve(requested_cid: str) -> ResolvedDriverContext:
            assert requested_cid == cid
            resolve_entered.set()
            await release_resolution.wait()
            return resolved

        def compose(requested_cid: str, snapshot: ResolvedDriverContext) -> object:
            assert requested_cid == cid
            assert snapshot is resolved
            composed.set()
            return loop_sentinel

        async def admit(requested_cid: str, loop: object, **_kwargs: Any) -> None:
            assert requested_cid == cid
            assert loop is loop_sentinel
            admitted.set()
            await hold_admission.wait()

        def legacy_compose(_cid: str) -> object:
            raise AssertionError("legacy synchronous composition ran before resolution")

        monkeypatch.setattr(rt.drivers, "resolve_context", resolve)
        monkeypatch.setattr(rt._loop_factory, "loop_for_resolved", compose)
        monkeypatch.setattr(rt, "_loop_for", legacy_compose)
        monkeypatch.setattr(rt._run_supervisor, "create_task", create_run_task)
        monkeypatch.setattr(rt.sandbox_resources, "evict_stale", evict_stale_backend)
        monkeypatch.setattr(rt._workspace, "run_after_admission", admit)

        rt.run_controller.kick(cid, claimed_user_seq=user_seq)
        task = rt._run_registry.task(cid)
        assert task is not None
        await asyncio.wait_for(resolve_entered.wait(), timeout=1.0)
        assert create_run_task_calls == 1
        assert stale_backend_checked
        assert not composed.is_set()
        assert not admitted.is_set()
        assert rt._loop_registry.loop(cid) is None
        assert rt._run_resources.executor(cid) is None

        release_resolution.set()
        await asyncio.wait_for(composed.wait(), timeout=1.0)
        await asyncio.wait_for(admitted.wait(), timeout=1.0)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    @pytest.mark.asyncio
    async def test_resolution_uses_injected_router_config(self) -> None:
        rt = _runtime("ok", model_key="injected", context_window=12288)
        result = await rt._resolve_driver_context("injected-config")
        assert result.model_key == "injected"
        assert result.model_id == "test/model"
        assert result.context_window == 12288

    @pytest.mark.asyncio
    async def test_invalid_driver_config_is_normalized_to_resolution_error(self) -> None:
        rt = _runtime("ok", model_key="configured", context_window=12288)
        assert rt.drivers._injected_router is not None
        broken = rt.drivers._injected_router._config.model_copy(
            update={"default_model": "missing", "assignments": {}}
        )
        rt.drivers._injected_router._config = broken
        with pytest.raises(DriverContextResolutionError, match="invalid driver configuration"):
            await rt._resolve_driver_context("invalid-config")

    @pytest.mark.asyncio
    async def test_real_resolution_to_composition_pipeline(self) -> None:
        rt = _runtime("ok", model_key="resolved", context_window=24576)
        cid = "full-pipeline"
        await _seed_conversation(rt, cid)
        snapshot = await rt._resolve_driver_context(cid)
        loop = rt._loop_factory.loop_for_resolved(cid, snapshot)
        assert loop._driver_context_window_value == 24576
        assert loop.agent._driver_context_window == 24576
        assert rt._driver_contexts.resolved_snapshot(cid) is snapshot
        assert rt._driver_contexts.compose_snapshot(cid) is None

    @pytest.mark.asyncio
    async def test_deep_research_loop_and_agent_share_resolved_context(self) -> None:
        rt = _runtime("ok", model_key="resolved", context_window=32768)
        cid = "deep-research-context"
        await _seed_conversation(rt, cid)
        rt.set_surface(cid, "deep_research")
        snapshot = await rt._resolve_driver_context(cid)

        loop = rt._loop_factory.loop_for_resolved(cid, snapshot)

        assert loop._driver_context_window_value == 32768
        assert loop.agent._driver_context_window == 32768

    @pytest.mark.asyncio
    async def test_snapshot_model_key_controls_composition(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        rt = _runtime("ok", model_key="resolved", context_window=8192)
        cid = "snapshot-model"
        await _seed_conversation(rt, cid)
        rt._settings.set_model_override(cid, "stale-or-concurrently-changed")
        snapshot = ResolvedDriverContext(
            model_key="resolved",
            provider="fake",
            model_id="test/model",
            context_window=16384,
            source="live",
            resolved_at=datetime.now(UTC),
        )
        seen_picks: list[str | None] = []
        original_router = rt.drivers.router

        def router(pick: str | None = None, **kwargs: Any) -> DefaultLLMRouter:
            seen_picks.append(pick)
            return original_router(pick=pick, **kwargs)

        monkeypatch.setattr(rt.drivers, "router", router)
        loop = rt._loop_factory.loop_for_resolved(cid, snapshot)
        assert seen_picks == ["resolved"]
        assert loop._driver_context_window_value == 16384

    @pytest.mark.asyncio
    async def test_provisional_loop_recomposes_and_preserves_session(self) -> None:
        rt = _runtime("ok")
        cid = "provisional"
        await _seed_conversation(rt, cid)
        provisional = rt._loop_for(cid)
        old_executor = rt._run_resources.executor(cid)
        assert old_executor is not None
        session = old_executor._sandbox
        resolved = ResolvedDriverContext(
            model_key="m",
            provider="fake",
            model_id="test/model",
            context_window=16384,
            source="live",
            resolved_at=datetime.now(UTC),
        )
        recomposed = rt._loop_factory.loop_for_resolved(cid, resolved)
        assert recomposed is not provisional
        assert rt._run_resources.executor(cid) is not old_executor
        executor = rt._run_resources.executor(cid)
        assert executor is not None
        assert executor._sandbox is session
        assert recomposed._driver_context_window_value == 16384

    @pytest.mark.asyncio
    async def test_admitted_run_is_not_recomposed_until_next_run(self) -> None:
        rt = _runtime("ok")
        cid = "model-switch"
        await _seed_conversation(rt, cid)
        first = ResolvedDriverContext(
            model_key="m",
            provider="fake",
            model_id="test/model",
            context_window=8192,
            source="configured_unavailable",
            resolved_at=datetime.now(UTC),
        )
        second = ResolvedDriverContext(
            model_key="m",
            provider="fake",
            model_id="test/model",
            context_window=16384,
            source="live",
            resolved_at=datetime.now(UTC),
        )
        loop_a = rt._loop_factory.loop_for_resolved(cid, first)
        executor_a = rt._run_resources.executor(cid)
        assert executor_a is not None
        session = executor_a._sandbox
        rt._workspace_fence.mark_run_admitted(cid)
        assert rt._loop_factory.loop_for_resolved(cid, second) is loop_a
        assert loop_a._driver_context_window_value == 8192

        rt._workspace_fence.clear_run_claim(cid)
        loop_b = rt._loop_factory.loop_for_resolved(cid, second)
        assert loop_b is not loop_a
        assert loop_b._driver_context_window_value == 16384
        executor_b = rt._run_resources.executor(cid)
        assert executor_b is not None
        assert executor_b._sandbox is session
