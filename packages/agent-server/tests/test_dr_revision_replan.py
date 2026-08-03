"""DR plan-revision re-propose — the live STUCK regression (2026-07-07).

Sending a plan revision to a deep-research conversation (PlanPanel "Send
revision" → request_plan → loop.enter_planning emits RUNNING/'planning') left
the DR dispatcher with NO matching phase: Phase 1 needs no plan, Phase 2 needs
detail=='plan_approved', Phase 3 needs a report. It fell through, did nothing,
and the runtime backstop marked the conversation STUCK within milliseconds
('loop ended without reaching a terminal state') — zero model calls.

These tests pin the fix: Phase 1R re-proposes (revision = prev+1, folding every
post-question user message into the decompose query as constraints) and parks at
AWAITING_PLAN_APPROVAL again. Hermetic — decompose_query and the driver
pre-flight are faked.
"""

from __future__ import annotations

from disco.agent_server import ConversationRuntime
from disco.agent_server import deep_research_service as drs
from disco.core import (
    ConversationStatus,
    EventSource,
    LLMMessage,
    MessageEvent,
    PlanEvent,
    PlanStep,
    SqliteEventStore,
    StatusEvent,
)


class _Subq:
    def __init__(self, title: str) -> None:
        self.title = title


def _fake_decompose(captured: dict):
    async def _decompose(router, query, *, max_subq, recency_window=None):
        captured["query"] = query
        return [_Subq("sub one"), _Subq("sub two")]

    return _decompose


async def _seed_revision_state(store, cid: str, *, planning_marker: bool) -> None:
    """Question → plan → AWAITING_PLAN_APPROVAL → revision message
    (+ optionally the RUNNING/'planning' marker enter_planning emits)."""
    await store.append(
        cid,
        MessageEvent(
            source=EventSource.USER,
            message=LLMMessage(role="user", content="state of local LLMs in 2026"),
        ),
    )
    plan = PlanEvent(summary="plan v1", steps=[PlanStep(title="s1")], revision=1)
    await store.append(cid, plan)
    await store.append(
        cid,
        StatusEvent(status=ConversationStatus.AWAITING_PLAN_APPROVAL, detail=plan.id),
    )
    await store.append(
        cid,
        MessageEvent(
            source=EventSource.USER,
            message=LLMMessage(role="user", content="do not include models released before 2026"),
        ),
    )
    if planning_marker:
        await store.append(
            cid,
            StatusEvent(status=ConversationStatus.RUNNING, detail="planning"),
        )


def _rt(store, monkeypatch) -> ConversationRuntime:
    rt = ConversationRuntime(store)
    rt._settings._set_surface("c1", "deep_research")

    async def _preflight_ok(cid, **kw):
        return None

    monkeypatch.setattr(rt._driver_preflight, "check", _preflight_ok)
    monkeypatch.setattr(rt.drivers, "router", lambda **kw: object())
    return rt


async def test_revision_via_request_plan_reproposes(monkeypatch):
    """The exact live trace: revision → RUNNING/'planning' → dispatcher must
    re-propose (NOT fall through to the STUCK backstop)."""
    captured: dict = {}
    monkeypatch.setattr(drs, "decompose_query", _fake_decompose(captured))
    store = SqliteEventStore(":memory:")
    rt = _rt(store, monkeypatch)
    await _seed_revision_state(store, "c1", planning_marker=True)

    await rt._dr._maybe_run_deep_research("c1")

    events = await store.get_events("c1")
    plans = [e for e in events if isinstance(e, PlanEvent)]
    assert len(plans) == 2, "revision must re-propose a new plan"
    assert plans[-1].revision == 2
    statuses = [e for e in events if isinstance(e, StatusEvent)]
    assert statuses[-1].status == ConversationStatus.AWAITING_PLAN_APPROVAL
    # The decompose query is the ORIGINAL question with the revision folded in
    # as a constraint — never the revision text alone.
    q = captured["query"]
    assert q.startswith("state of local LLMs in 2026")
    assert "do not include models released before 2026" in q


async def test_revision_via_plain_message_while_awaiting(monkeypatch):
    """A revision typed as a plain message during AWAITING_PLAN_APPROVAL (no
    planning marker) hits the same dead zone — must also re-propose."""
    captured: dict = {}
    monkeypatch.setattr(drs, "decompose_query", _fake_decompose(captured))
    store = SqliteEventStore(":memory:")
    rt = _rt(store, monkeypatch)
    await _seed_revision_state(store, "c1", planning_marker=False)

    await rt._dr._maybe_run_deep_research("c1")

    events = await store.get_events("c1")
    plans = [e for e in events if isinstance(e, PlanEvent)]
    assert len(plans) == 2 and plans[-1].revision == 2
    assert "do not include models released before 2026" in captured["query"]


async def test_awaiting_approval_without_new_message_stays_parked(monkeypatch):
    """No fresh user message → the dispatcher must remain a no-op (no re-propose
    loop; the conversation stays parked at AWAITING_PLAN_APPROVAL)."""
    captured: dict = {}
    monkeypatch.setattr(drs, "decompose_query", _fake_decompose(captured))
    store = SqliteEventStore(":memory:")
    rt = _rt(store, monkeypatch)
    await store.append(
        "c1",
        MessageEvent(
            source=EventSource.USER,
            message=LLMMessage(role="user", content="state of local LLMs in 2026"),
        ),
    )
    plan = PlanEvent(summary="plan v1", steps=[PlanStep(title="s1")], revision=1)
    await store.append("c1", plan)
    await store.append(
        "c1",
        StatusEvent(status=ConversationStatus.AWAITING_PLAN_APPROVAL, detail=plan.id),
    )
    before = len(await store.get_events("c1"))

    await rt._dr._maybe_run_deep_research("c1")

    events = await store.get_events("c1")
    assert len(events) == before, "dispatcher must not emit anything"
    assert "query" not in captured, "decompose must not run"
