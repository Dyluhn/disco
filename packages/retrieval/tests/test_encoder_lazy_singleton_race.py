"""Cold-start singleton race for lazy encoders.

Proves (or rejects) duplicate construction in ``_embedding()`` and ``_reranker()``
when parallel first-use calls run via ``asyncio.to_thread``. ``_nli_backend`` has
a load ``Lock``; these two loaders do not (main at time of writing).

No real models, downloads, or RAM guards: constructors are faked and
``_require_ram`` is stubbed. Overlap is demonstrated with ``threading.Event``
gates owned by the test harness — never a peer barrier inside the fake
constructor (which would deadlock once a correct lock-only fix serializes
construction). Every wait is bounded; the harness release is always set in
``finally`` and workers are joined/cancelled so no thread is stuck on failure.

Contract asserted by the two cold-race tests (must also hold after a minimal
lock-only product fix): concurrent cold ``asyncio.to_thread`` calls build
exactly once and return the identical object. Warm-singleton, failure/recovery,
and option-forwarding controls below must pass both before and after the fix.
This file asserts construction singleton only — not inference serialization or
memory behavior.
"""

from __future__ import annotations

import asyncio
import sys
import threading
import types

import pytest

import disco.retrieval.local_encoders as le


@pytest.fixture(autouse=True)
def _reset_singletons(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(le, "_embedding_model", None)
    monkeypatch.setattr(le, "_cross_encoder", None)
    for var in (
        "DISCO_EMBED_MODEL",
        "PMX_EMBED_MODEL",
        "DISCO_RERANK_MODEL",
        "PMX_RERANK_MODEL",
        "DISCO_ENCODER_TIER",
        "PMX_ENCODER_TIER",
    ):
        monkeypatch.delenv(var, raising=False)


def _install_fake_cross_encoder(monkeypatch: pytest.MonkeyPatch, fake_cls: type) -> None:
    """Make ``from fastembed.rerank.cross_encoder import TextCrossEncoder`` hit
    ``fake_cls`` whether or not fastembed is installed (no network/allocation)."""
    for name in ("fastembed", "fastembed.rerank", "fastembed.rerank.cross_encoder"):
        if name not in sys.modules:
            mod = types.ModuleType(name)
            if name != "fastembed.rerank.cross_encoder":
                mod.__path__ = []  # type: ignore[attr-defined] — mark as package
            monkeypatch.setitem(sys.modules, name, mod)
    ce_mod = sys.modules["fastembed.rerank.cross_encoder"]
    monkeypatch.setattr(ce_mod, "TextCrossEncoder", fake_cls, raising=False)
    rerank_pkg = sys.modules["fastembed.rerank"]
    if not hasattr(rerank_pkg, "cross_encoder"):
        monkeypatch.setattr(rerank_pkg, "cross_encoder", ce_mod, raising=False)
    top_pkg = sys.modules["fastembed"]
    if not hasattr(top_pkg, "rerank"):
        monkeypatch.setattr(top_pkg, "rerank", rerank_pkg, raising=False)


async def _join_two(t1: asyncio.Task, t2: asyncio.Task, timeout: float = 10.0):
    try:
        return await asyncio.wait_for(asyncio.gather(t1, t2), timeout=timeout)
    except BaseException:
        for t in (t1, t2):
            if not t.done():
                t.cancel()
        await asyncio.gather(t1, t2, return_exceptions=True)
        raise


# ── cold races (fail on current main: 2 constructions, distinct objects) ─────


@pytest.mark.asyncio
async def test_embedding_cold_concurrent_builds_once(monkeypatch: pytest.MonkeyPatch):
    release = threading.Event()
    both_inside = threading.Event()
    all_started = threading.Event()
    guard = threading.Lock()
    arrivals: list[int] = []
    starts: list[int] = []
    constructions: list[str] = []

    def fake_require_ram(model_name: str) -> None:
        with guard:
            arrivals.append(threading.get_ident())
            if len(arrivals) >= 2:
                both_inside.set()

    class FakeEmbed:
        def __init__(self, *, model_name: str, **kwargs) -> None:
            with guard:
                constructions.append(model_name)
            # Harness gate, not a peer barrier: after a lock-only fix only one
            # thread ever blocks here; the test always releases (finally).
            release.wait(timeout=5)

    monkeypatch.setattr(le, "_require_ram", fake_require_ram)
    monkeypatch.setattr(le, "_embedding_factory", lambda _name: FakeEmbed)

    def _wrapped():
        with guard:
            starts.append(threading.get_ident())
            if len(starts) >= 2:
                all_started.set()
        return le._embedding()

    t1 = asyncio.create_task(asyncio.to_thread(_wrapped))
    t2 = asyncio.create_task(asyncio.to_thread(_wrapped))
    try:
        started_ok = await asyncio.to_thread(all_started.wait, 5)
        assert started_ok, "setup failed: workers never both started"
        # Gate so both loaders overlap before either constructs. Bounded: after
        # a correct fix the second arrival never comes, wait times out, release
        # below still lets the single builder finish (no deadlock).
        await asyncio.to_thread(both_inside.wait, 5)
    finally:
        release.set()

    r1, r2 = await _join_two(t1, t2)
    assert len(constructions) == 1, f"duplicate _embedding() construction: {len(constructions)}"
    assert r1 is r2, "concurrent cold _embedding() must return the same singleton"


@pytest.mark.asyncio
async def test_reranker_cold_concurrent_builds_once(monkeypatch: pytest.MonkeyPatch):
    release = threading.Event()
    both_inside = threading.Event()
    all_started = threading.Event()
    guard = threading.Lock()
    arrivals: list[int] = []
    starts: list[int] = []
    constructions: list[str] = []

    def fake_require_ram(model_name: str) -> None:
        with guard:
            arrivals.append(threading.get_ident())
            if len(arrivals) >= 2:
                both_inside.set()

    class FakeCE:
        def __init__(self, *, model_name: str, **kwargs) -> None:
            with guard:
                constructions.append(model_name)
            release.wait(timeout=5)

    monkeypatch.setattr(le, "_require_ram", fake_require_ram)
    _install_fake_cross_encoder(monkeypatch, FakeCE)

    def _wrapped():
        with guard:
            starts.append(threading.get_ident())
            if len(starts) >= 2:
                all_started.set()
        return le._reranker()

    t1 = asyncio.create_task(asyncio.to_thread(_wrapped))
    t2 = asyncio.create_task(asyncio.to_thread(_wrapped))
    try:
        started_ok = await asyncio.to_thread(all_started.wait, 5)
        assert started_ok, "setup failed: workers never both started"
        await asyncio.to_thread(both_inside.wait, 5)
    finally:
        release.set()

    r1, r2 = await _join_two(t1, t2)
    assert len(constructions) == 1, f"duplicate _reranker() construction: {len(constructions)}"
    assert r1 is r2, "concurrent cold _reranker() must return the same singleton"


# ── controls: warmed singleton (pass before and after fix) ───────────────────


@pytest.mark.asyncio
async def test_embedding_warmed_concurrent_reuses_singleton(monkeypatch: pytest.MonkeyPatch):
    constructions: list[str] = []

    class FakeEmbed:
        def __init__(self, *, model_name: str, **kwargs) -> None:
            constructions.append(model_name)

    monkeypatch.setattr(le, "_require_ram", lambda _name: None)
    monkeypatch.setattr(le, "_embedding_factory", lambda _name: FakeEmbed)
    first = le._embedding()
    assert len(constructions) == 1

    r1, r2 = await asyncio.gather(
        asyncio.to_thread(le._embedding), asyncio.to_thread(le._embedding)
    )
    assert r1 is first and r2 is first
    assert len(constructions) == 1, "warm reads must not reconstruct"


@pytest.mark.asyncio
async def test_reranker_warmed_concurrent_reuses_singleton(monkeypatch: pytest.MonkeyPatch):
    constructions: list[str] = []

    class FakeCE:
        def __init__(self, *, model_name: str, **kwargs) -> None:
            constructions.append(model_name)

    monkeypatch.setattr(le, "_require_ram", lambda _name: None)
    _install_fake_cross_encoder(monkeypatch, FakeCE)
    first = le._reranker()
    assert len(constructions) == 1

    r1, r2 = await asyncio.gather(
        asyncio.to_thread(le._reranker), asyncio.to_thread(le._reranker)
    )
    assert r1 is first and r2 is first
    assert len(constructions) == 1, "warm reads must not reconstruct"


# ── controls: failure does not poison the singleton ──────────────────────────


def test_embedding_constructor_failure_does_not_poison_singleton(
    monkeypatch: pytest.MonkeyPatch,
):
    calls: list[str] = []

    class Flaky:
        def __init__(self, *, model_name: str, **kwargs) -> None:
            calls.append(model_name)
            if len(calls) == 1:
                raise RuntimeError("boom")

    monkeypatch.setattr(le, "_require_ram", lambda _name: None)
    monkeypatch.setattr(le, "_embedding_factory", lambda _name: Flaky)

    with pytest.raises(RuntimeError, match="boom"):
        le._embedding()
    assert le._embedding_model is None

    inst = le._embedding()
    assert le._embedding_model is inst
    assert len(calls) == 2


def test_reranker_constructor_failure_does_not_poison_singleton(
    monkeypatch: pytest.MonkeyPatch,
):
    calls: list[str] = []

    class Flaky:
        def __init__(self, *, model_name: str, **kwargs) -> None:
            calls.append(model_name)
            if len(calls) == 1:
                raise RuntimeError("boom")

    monkeypatch.setattr(le, "_require_ram", lambda _name: None)
    _install_fake_cross_encoder(monkeypatch, Flaky)

    with pytest.raises(RuntimeError, match="boom"):
        le._reranker()
    assert le._cross_encoder is None

    inst = le._reranker()
    assert le._cross_encoder is inst
    assert len(calls) == 2


# ── controls: option forwarding preserved (explicit wins, arena passed) ───────


def test_embedding_forwards_model_name_and_arena_options(monkeypatch: pytest.MonkeyPatch):
    seen: dict = {}

    class FakeEmbed:
        def __init__(self, *, model_name: str, **kwargs) -> None:
            seen["model_name"] = model_name
            seen["kwargs"] = kwargs

    monkeypatch.setattr(le, "_require_ram", lambda _name: None)
    monkeypatch.setattr(le, "_embedding_factory", lambda _name: FakeEmbed)

    monkeypatch.setenv("DISCO_ENCODER_TIER", "lite")
    le._embedding()
    assert seen["model_name"] == le.EMBED_MODEL_LITE
    assert "enable_cpu_mem_arena" in seen["kwargs"]

    monkeypatch.setattr(le, "_embedding_model", None)
    monkeypatch.setenv("DISCO_EMBED_MODEL", "custom/embed-id")
    monkeypatch.setenv("DISCO_ENCODER_CPU_ARENA", "on")
    le._embedding()
    assert seen["model_name"] == "custom/embed-id"
    assert seen["kwargs"].get("enable_cpu_mem_arena") is True


def test_reranker_forwards_model_name_and_arena_options(monkeypatch: pytest.MonkeyPatch):
    seen: dict = {}

    class FakeCE:
        def __init__(self, *, model_name: str, **kwargs) -> None:
            seen["model_name"] = model_name
            seen["kwargs"] = kwargs

    monkeypatch.setattr(le, "_require_ram", lambda _name: None)
    _install_fake_cross_encoder(monkeypatch, FakeCE)

    monkeypatch.setenv("DISCO_ENCODER_TIER", "lite")
    le._reranker()
    assert seen["model_name"] == le.RERANK_MODEL_LITE
    assert "enable_cpu_mem_arena" in seen["kwargs"]

    monkeypatch.setattr(le, "_cross_encoder", None)
    monkeypatch.setenv("DISCO_RERANK_MODEL", "custom/rerank-id")
    monkeypatch.setenv("DISCO_ENCODER_CPU_ARENA", "on")
    le._reranker()
    assert seen["model_name"] == "custom/rerank-id"
    assert seen["kwargs"].get("enable_cpu_mem_arena") is True
