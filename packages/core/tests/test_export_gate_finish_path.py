"""[P10] Integration: the export render gate is CONSULTED inside the real finish
path (handle_finish_path), not merely callable.

Drives the actual AgentLoop through plan → approve → execute where the agent
generates a deck and then calls finish. The deck's render facts are injected at
the tool boundary (a real model won't emit a blank deck on demand, and the facts
are stamped from in-memory bytes at generation time). Same scenario, two decks:
a GOOD deck reaches FINISHED; a BLANK deck is refused with the EXPORT RENDER CHECK
steer and never finishes. The A/B isolates the gate as the cause.
"""

from __future__ import annotations

import pytest
from disco.core import ConversationStatus, StatusEvent, ToolResult
from disco.core.contract.export_render import (
    EXPORT_GATE_TOKEN,
    EXPORT_RENDER_KEY,
    check_export_render,
)
from disco.core.events import MessageEvent
from disco.core.llm import OperatingMode, ToolSpec
from loop_fakes import FakeExecutor, ScriptedAgent, action_step, build_loop, finish_step

CID = "conv"

_GOOD_HTML = (
    "<html><body>"
    + "".join(
        f'<section data-slide-id="slide-{i}"><h1>Real slide content number {i}</h1>'
        f"<p>Body copy with genuine words for slide {i}.</p></section>"
        for i in range(4)
    )
    + "</body></html>"
)
_BLANK_HTML = (
    "<html><body>"
    + "".join(f'<section data-slide-id="slide-{i}"></section>' for i in range(4))
    + "</body></html>"
)


class _DeckExecutor(FakeExecutor):
    """A backend whose slides_generate stamps injected render facts (good/blank),
    exactly as the real producer does — so the finish gate sees them on the log."""

    def __init__(self, *, html: str) -> None:
        tools = [
            ToolSpec(name=n, description=n, parameters_schema={})
            for n in ("file_write", "slides_generate", "serve")
        ]
        super().__init__(tools=tools)
        self._facts = check_export_render("html", text=html, declared_units=4)

    async def execute(self, call):
        self.calls.append(call)
        if call.tool_name == "slides_generate":
            return ToolResult(
                call_id=call.call_id,
                tool_name="slides_generate",
                success=True,
                content="deck written to deck.html",
                structured={
                    "filename": "deck.html",
                    "format": "html",
                    "slide_count": 4,
                    EXPORT_RENDER_KEY: self._facts.model_dump(mode="json"),
                },
            )
        return ToolResult(
            call_id=call.call_id, tool_name=call.tool_name, success=True, content="ok"
        )


_PLAN = {"summary": "make a deck", "steps": [{"title": "Generate the slide deck"}]}


def _steps():
    return [
        action_step("submit_plan", _PLAN),
        action_step("slides_generate", {"goal": "quarterly review deck", "filename": "deck"}),
        finish_step(),
        # tail finishes in case the gate refuses and re-enters the loop
        finish_step(),
        finish_step(),
    ]


async def _run(executor):
    loop, store = build_loop(
        executor=executor, agent=ScriptedAgent(_steps()), planning_tools=frozenset(["file_read"])
    )
    loop.mode = OperatingMode.PLANNING
    await loop.send_message("Make a quarterly review slide deck")
    await loop.run()  # submit_plan → AWAITING approval
    await loop.approve_plan()
    state = await loop.run()  # execute
    events = await store.get_events(CID)
    return state, events


def _statuses(events):
    return [(e.status, e.detail) for e in events if isinstance(e, StatusEvent)]


def _export_steer(events) -> list[str]:
    return [
        e.message.content
        for e in events
        if isinstance(e, MessageEvent) and EXPORT_GATE_TOKEN in (e.message.content or "")
    ]


@pytest.mark.asyncio
async def test_blank_deck_refused_in_finish_path() -> None:
    """A blank deck → the export gate inside handle_finish_path refuses every finish
    attempt up to the cap, emitting the concrete steer each time, then HONESTLY
    releases with a loud unverified warning (never a silent clean finish). The
    scripted agent repeats finish, so this exercises the full refuse→cap→release
    arc through the real loop."""
    state, events = await _run(_DeckExecutor(html=_BLANK_HTML))
    steers = _export_steer(events)
    # refused exactly the cap-limit times before releasing
    assert len(steers) == 3, _statuses(events)
    assert "content-empty" in steers[0]
    sts = _statuses(events)
    # it only reached a terminal state via the honest release valve — NOT a clean
    # gate-pass (which would be FINISHED with no unverified_export marker before it).
    assert any(d == "unverified_export" for _, d in sts), sts


@pytest.mark.asyncio
async def test_good_deck_finishes_through_finish_path() -> None:
    """Same scenario, a GOOD deck → no export refusal, the run FINISHES. Proves the
    gate is the cause of the blank refusal (A/B), not some unrelated block."""
    state, events = await _run(_DeckExecutor(html=_GOOD_HTML))
    assert not _export_steer(events), _statuses(events)
    assert state.execution_status == ConversationStatus.FINISHED, _statuses(events)
