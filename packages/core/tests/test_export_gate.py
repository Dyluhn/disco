"""[P10] Unit tests for FinishGate.gate_export_render.

Drives the gate directly (it reads the passed `events` for facts + refusal count
and emits via the loop store) so each branch is checked in isolation: a good
export falls through, a blank/truncated/corrupt one refuses with a concrete steer,
an app-deliverable build is never blocked by a stale deck, and the refusal cap
releases with a loud UNVERIFIED warning rather than trapping the run.
"""

from __future__ import annotations

import pytest
from disco.core.context import ArtifactMemoryStore
from disco.core.context.ledger import ArtifactRecord
from disco.core.contract.export_render import (
    EXPORT_GATE_TOKEN as _EXPORT_GATE_TOKEN,
)
from disco.core.contract.export_render import (
    EXPORT_RENDER_KEY,
    check_export_render,
)
from disco.core.events import (
    DeliverableEvent,
    EventSource,
    LLMMessage,
    MessageEvent,
    ObservationEvent,
    StatusEvent,
    ToolResult,
)
from disco.core.loop.control import Disp
from loop_fakes import ScriptedAgent, build_loop, finish_step


class _ManifestFS:
    def __init__(self) -> None:
        self.files: dict[str, bytes] = {}

    async def read_file(self, path: str) -> bytes:
        if path not in self.files:
            raise FileNotFoundError(path)
        return self.files[path]

    async def write_file(self, path: str, data: bytes) -> None:
        self.files[path] = data


def _deck_obs(facts_fmt: str, **kw) -> ObservationEvent:
    facts = check_export_render(facts_fmt, **kw)
    return ObservationEvent(
        action_id="a1",
        tool_result=ToolResult(
            call_id="c1",
            tool_name="slides_generate",
            success=True,
            content="deck written",
            structured={"filename": "deck." + facts_fmt, EXPORT_RENDER_KEY: facts.model_dump(mode="json")},
        ),
    )


def _files_deliverable() -> DeliverableEvent:
    return DeliverableEvent(title="Deck", path="deck.pptx", artifact_kind="files")


def _good_html(n: int = 4) -> dict:
    sections = "".join(
        f'<section data-slide-id="slide-{i}"><h1>Real slide content number {i}</h1></section>'
        for i in range(n)
    )
    return {"text": f"<html><body>{sections}</body></html>", "declared_units": n}


def _blank_html(n: int = 4) -> dict:
    sections = "".join(f'<section data-slide-id="slide-{i}"></section>' for i in range(n))
    return {"text": f"<html><body>{sections}</body></html>", "declared_units": n}


def _gate(loop):
    return loop._finish.gate_export_render


@pytest.mark.asyncio
async def test_good_export_falls_through() -> None:
    loop, store = build_loop(ScriptedAgent([]))
    events = [_deck_obs("html", **_good_html()), _files_deliverable()]
    disp = await _gate(loop)(finish_step(), events)
    assert disp is Disp.FALLTHROUGH
    # nothing emitted
    assert not any(
        isinstance(e, MessageEvent) and _EXPORT_GATE_TOKEN in (e.message.content or "")
        for e in await loop._events()
    )


@pytest.mark.asyncio
async def test_no_export_falls_through() -> None:
    loop, _ = build_loop(ScriptedAgent([]))
    obs = ObservationEvent(
        action_id="a1",
        tool_result=ToolResult(call_id="c1", tool_name="file_write", success=True, content="", structured={"path": "notes.txt"}),
    )
    disp = await _gate(loop)(finish_step(), [obs])
    assert disp is Disp.FALLTHROUGH


@pytest.mark.asyncio
async def test_blank_export_refuses_with_steer() -> None:
    loop, _ = build_loop(ScriptedAgent([]))
    events = [_deck_obs("html", **_blank_html()), _files_deliverable()]
    disp = await _gate(loop)(finish_step(), events)
    assert disp is Disp.CONTINUE
    emitted = await loop._events()
    steer = [
        e for e in emitted
        if isinstance(e, MessageEvent) and _EXPORT_GATE_TOKEN in (e.message.content or "")
    ]
    assert len(steer) == 1
    assert "content-empty" in steer[0].message.content
    assert "NOT complete" in steer[0].message.content


@pytest.mark.asyncio
async def test_truncated_export_refuses() -> None:
    loop, _ = build_loop(ScriptedAgent([]))
    # declared 8 slides, only 2 rendered
    events = [_deck_obs("html", **{"text": _good_html(2)["text"], "declared_units": 8}), _files_deliverable()]
    disp = await _gate(loop)(finish_step(), events)
    assert disp is Disp.CONTINUE
    steer = [e for e in await loop._events() if isinstance(e, MessageEvent) and _EXPORT_GATE_TOKEN in (e.message.content or "")]
    assert steer and "truncated" in steer[0].message.content


@pytest.mark.asyncio
async def test_corrupt_pptx_refuses() -> None:
    loop, _ = build_loop(ScriptedAgent([]))
    events = [_deck_obs("pptx", data=b"not a zip", declared_units=3), _files_deliverable()]
    disp = await _gate(loop)(finish_step(), events)
    assert disp is Disp.CONTINUE


@pytest.mark.asyncio
async def test_app_deliverable_after_deck_not_blocked() -> None:
    """A blank deck followed by a NEWER app deliverable must NOT block — the app is
    the current handoff; the deck is superseded."""
    loop, _ = build_loop(ScriptedAgent([]))
    events = [
        _deck_obs("html", **_blank_html()),
        DeliverableEvent(title="App", path="site", artifact_kind="app"),
    ]
    disp = await _gate(loop)(finish_step(), events)
    assert disp is Disp.FALLTHROUGH


@pytest.mark.asyncio
async def test_files_deliverable_after_app_after_deck_is_gated() -> None:
    """A newer files handoff after the app means the bad deck is current again."""
    loop, _ = build_loop(ScriptedAgent([]))
    events = [
        _deck_obs("html", **_blank_html()),
        DeliverableEvent(title="App", path="site", artifact_kind="app"),
        _files_deliverable(),
    ]
    disp = await _gate(loop)(finish_step(), events)
    assert disp is Disp.CONTINUE
    steer = [
        e for e in await loop._events()
        if isinstance(e, MessageEvent) and _EXPORT_GATE_TOKEN in (e.message.content or "")
    ]
    assert steer, "newer files deliverable wrongly bypassed the bad deck"


@pytest.mark.asyncio
async def test_stale_app_deliverable_does_not_mask_later_bad_deck() -> None:
    """P10-1: an app deliverable from EARLIER in the conversation must NOT disable
    the gate for a freshly-produced broken deck (the deck is newer → gate it)."""
    loop, _ = build_loop(ScriptedAgent([]))
    events = [
        DeliverableEvent(title="Old app", path="site", artifact_kind="app"),
        _deck_obs("html", **_blank_html()),  # produced AFTER the app handoff
        _files_deliverable(),
    ]
    disp = await _gate(loop)(finish_step(), events)
    assert disp is Disp.CONTINUE
    steer = [
        e for e in await loop._events()
        if isinstance(e, MessageEvent) and _EXPORT_GATE_TOKEN in (e.message.content or "")
    ]
    assert steer, "stale app deliverable wrongly masked the bad deck"


def _refusal() -> MessageEvent:
    return MessageEvent(
        source=EventSource.ENVIRONMENT,
        message=LLMMessage(
            role="user",
            content=f"<system-reminder>\n{_EXPORT_GATE_TOKEN}: prior\n</system-reminder>",
        ),
    )


def _deck_obs_named(filename: str, facts_fmt: str, **kw) -> ObservationEvent:
    facts = check_export_render(facts_fmt, **kw)
    return ObservationEvent(
        action_id="a1",
        tool_result=ToolResult(
            call_id="c1",
            tool_name="slides_generate",
            success=True,
            content="deck written",
            structured={"filename": filename, EXPORT_RENDER_KEY: facts.model_dump(mode="json")},
        ),
    )


@pytest.mark.asyncio
async def test_refusal_cap_releases_with_warning() -> None:
    # 3 refusals AFTER the current export (the gate's own emissions across 3 finish
    # attempts) hit the per-export cap → release with the UNVERIFIED warning.
    loop, _ = build_loop(ScriptedAgent([]))
    events = [
        _deck_obs("html", **_blank_html()),
        _refusal(),
        _refusal(),
        _refusal(),
        _files_deliverable(),
    ]
    disp = await _gate(loop)(finish_step(), events)
    assert disp is Disp.FALLTHROUGH
    emitted = await loop._events()
    assert any(isinstance(e, StatusEvent) and e.detail == "unverified_export" for e in emitted)
    assert any(
        isinstance(e, MessageEvent) and "UNVERIFIED" in (e.message.content or "")
        for e in emitted
    )


@pytest.mark.asyncio
async def test_stale_refusals_do_not_release_a_fresh_deck() -> None:
    # [G2] 3 refusals for an EARLIER deck must NOT spend a freshly generated blank
    # deck's budget — the new deck gets its own attempts and is refused, not released.
    loop, _ = build_loop(ScriptedAgent([]))
    events = [
        _refusal(),
        _refusal(),
        _refusal(),
        _deck_obs("html", **_blank_html()),  # fresh export AFTER the stale refusals
        _files_deliverable(),
    ]
    disp = await _gate(loop)(finish_step(), events)
    assert disp is Disp.CONTINUE


@pytest.mark.asyncio
async def test_delivering_bad_file_gates_on_that_file_not_newer_good() -> None:
    # [G4] generate bad.pptx, then good.pptx, then deliver bad.pptx: the gate must
    # check bad.pptx's OWN facts, not the newer good sibling's, and refuse.
    loop, _ = build_loop(ScriptedAgent([]))
    events = [
        _deck_obs_named("bad.html", "html", **_blank_html()),
        _deck_obs_named("good.html", "html", **_good_html()),
        DeliverableEvent(title="Deck", path="bad.html", artifact_kind="files"),
    ]
    disp = await _gate(loop)(finish_step(), events)
    assert disp is Disp.CONTINUE


@pytest.mark.asyncio
async def test_manifest_shown_export_path_gates_that_file_without_deliverable() -> None:
    # REL-2a reader promote: when the manifest has a shown export, P10 gates that
    # artifact's facts instead of silently accepting a newer sibling's good facts.
    loop, _ = build_loop(ScriptedAgent([]))
    fs = _ManifestFS()
    loop.executor.sandbox = fs  # type: ignore[attr-defined]
    await ArtifactMemoryStore(fs).upsert_artifact(
        ArtifactRecord(path="bad.html", kind="files", shown=True)
    )
    await ArtifactMemoryStore(fs).upsert_artifact(
        ArtifactRecord(path="good.html", kind="files", shown=False)
    )
    events = [
        _deck_obs_named("bad.html", "html", **_blank_html()),
        _deck_obs_named("good.html", "html", **_good_html()),
    ]

    disp = await _gate(loop)(finish_step(), events)

    assert disp is Disp.CONTINUE
    steer = [
        e for e in await loop._events()
        if isinstance(e, MessageEvent) and _EXPORT_GATE_TOKEN in (e.message.content or "")
    ]
    assert steer and "content-empty" in steer[0].message.content
