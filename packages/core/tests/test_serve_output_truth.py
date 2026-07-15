"""CD-TOOLS-5 — serve output-truth. The `serve` handoff (Claude Design show_to_user /
present_fs_item_for_download) must not hand off a deliverable whose path does not exist: a false
"Open / Download" card for nothing. A real path → DeliverableEvent; a missing path → refused, no
event. No sandbox / unverifiable → fail-open (unchanged behavior). serve is the SHOW handoff, never
verification."""

from __future__ import annotations

import pytest
from disco.core import DeliverableEvent, MessageEvent
from loop_fakes import FakeExecutor, ScriptedAgent, action_step, build_loop, finish_step

pytestmark = pytest.mark.asyncio


class _FakeSbx:
    """Minimal sandbox stub exposing only file_exists — what _serve_path_missing reads."""

    def __init__(self, existing: set[str]) -> None:
        self._existing = set(existing)

    async def file_exists(self, path: str) -> bool:
        return path in self._existing


def _deliverable_paths(events) -> list[str]:
    return [e.path for e in events if isinstance(e, DeliverableEvent)]


def _feedback(events) -> str:
    return "\n".join(e.message.content or "" for e in events if isinstance(e, MessageEvent))


async def test_serve_missing_path_refused_no_deliverable():
    ex = FakeExecutor()
    ex.sandbox = _FakeSbx({"real.html"})  # type: ignore[attr-defined]
    agent = ScriptedAgent(
        [
            action_step("shell", {}),  # real work first (serve gate requires it)
            action_step(
                "serve", {"title": "Ghost", "path": "ghost.html"}
            ),  # does not exist → refused
            finish_step(),
        ]
    )
    loop, store = build_loop(agent, executor=ex)
    await loop.send_message("go")
    await loop.run()
    events = await store.get_events("conv")
    assert "ghost.html" not in _deliverable_paths(events)  # no false handoff
    msg = _feedback(events)
    assert "ghost.html" in msg
    assert "Create it" not in msg


async def test_serve_existing_path_emits_deliverable():
    ex = FakeExecutor()
    ex.sandbox = _FakeSbx({"real.html"})  # type: ignore[attr-defined]
    agent = ScriptedAgent(
        [
            action_step("shell", {}),
            action_step("serve", {"title": "Real", "path": "real.html"}),  # exists → handed off
            finish_step(),
        ]
    )
    loop, store = build_loop(agent, executor=ex)
    await loop.send_message("go")
    await loop.run()
    events = await store.get_events("conv")
    assert "real.html" in _deliverable_paths(events)


async def test_serve_workspace_root_auto_coerces_to_index_when_present():
    ex = FakeExecutor()
    ex.sandbox = _FakeSbx({"index.html"})  # type: ignore[attr-defined]
    agent = ScriptedAgent(
        [
            action_step("shell", {}),
            action_step("serve", {"title": "Site", "path": "/workspace"}),
            finish_step(),
        ]
    )
    loop, store = build_loop(agent, executor=ex)
    await loop.send_message("go")
    await loop.run()
    events = await store.get_events("conv")

    assert "index.html" in _deliverable_paths(events)
    assert "/workspace" not in _deliverable_paths(events)
    assert "serve refused" not in _feedback(events)


async def test_serve_workspace_root_without_entry_gets_entry_file_recipe():
    ex = FakeExecutor()
    ex.sandbox = _FakeSbx(set())  # type: ignore[attr-defined]
    agent = ScriptedAgent(
        [
            action_step("shell", {}),
            action_step("serve", {"title": "Site", "path": "/workspace/"}),
            finish_step(),
        ]
    )
    loop, store = build_loop(agent, executor=ex)
    await loop.send_message("go")
    await loop.run()
    events = await store.get_events("conv")

    assert not _deliverable_paths(events)
    msg = _feedback(events)
    assert "serve takes the entry FILE path, not the workspace root" in msg
    assert "index.html" in msg
    assert "Create it" not in msg
    assert "does not exist in the workspace yet" not in msg


async def test_serve_workspace_absolute_path_normalizes_and_serves():
    ex = FakeExecutor()
    ex.sandbox = _FakeSbx({"sub/file.html"})  # type: ignore[attr-defined]
    agent = ScriptedAgent(
        [
            action_step("shell", {}),
            action_step("serve", {"title": "Subpage", "path": "/workspace/sub/file.html"}),
            finish_step(),
        ]
    )
    loop, store = build_loop(agent, executor=ex)
    await loop.send_message("go")
    await loop.run()
    events = await store.get_events("conv")

    assert "sub/file.html" in _deliverable_paths(events)
    assert "/workspace/sub/file.html" not in _deliverable_paths(events)


async def test_serve_missing_workspace_absolute_path_reports_normalized_path():
    ex = FakeExecutor()
    ex.sandbox = _FakeSbx(set())  # type: ignore[attr-defined]
    agent = ScriptedAgent(
        [
            action_step("shell", {}),
            action_step("serve", {"title": "Missing", "path": "/workspace/missing.html"}),
            finish_step(),
        ]
    )
    loop, store = build_loop(agent, executor=ex)
    await loop.send_message("go")
    await loop.run()
    events = await store.get_events("conv")

    assert not _deliverable_paths(events)
    msg = _feedback(events)
    assert "missing.html" in msg
    assert "/workspace/missing.html" not in msg
    assert "Create it" not in msg


async def test_serve_no_sandbox_fails_open():
    # FakeExecutor has no sandbox → _serve_path_missing returns False → handoff proceeds (the
    # output-truth check never blocks an unverifiable environment; existing behavior preserved).
    agent = ScriptedAgent(
        [
            action_step("shell", {}),
            action_step("serve", {"title": "App", "path": "index.html"}),
            finish_step(),
        ]
    )
    loop, store = build_loop(agent)
    await loop.send_message("go")
    await loop.run()
    events = await store.get_events("conv")
    assert "index.html" in _deliverable_paths(events)
