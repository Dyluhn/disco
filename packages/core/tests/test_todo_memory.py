"""CXT-6 tests: render_plan_as_todo_markdown + plan-approval seeds .disco/context/todo.md."""

from __future__ import annotations

import pytest
from _buildsoak_fakes import build_plan_loop
from disco.core.context import ArtifactMemoryStore
from disco.core.events import PlanEvent, PlanStep
from disco.core.loop.context_builder import render_plan_as_todo_markdown
from disco.core.loop.context_rendering import _TODO_PROGRESS_REDIRECT
from loop_fakes import ScriptedAgent, action_step, finish_step


class _MemFS:
    """Minimal in-memory WorkspaceFS (read of a missing path raises)."""

    def __init__(self) -> None:
        self.files: dict[str, bytes] = {}

    async def read_file(self, path: str) -> bytes:
        if path not in self.files:
            raise FileNotFoundError(path)
        return self.files[path]

    async def write_file(self, path: str, data: bytes) -> None:
        self.files[path] = data


# --- render helper ------------------------------------------------------------
def test_render_plan_as_todo_markdown() -> None:
    plan = PlanEvent(
        summary="Build a roofing lead site",
        steps=[PlanStep(title="hero", detail="big headline"), PlanStep(title="lead form")],
        context="Static site, no backend.",
    )
    md = render_plan_as_todo_markdown(plan)
    assert md.startswith("# Build a roofing lead site")
    assert "Static site, no backend." in md
    assert "## Steps" in md
    assert "- [ ] 1. hero" in md
    assert "  big headline" in md
    assert "- [ ] 2. lead form" in md


def test_render_plan_no_context_or_detail() -> None:
    plan = PlanEvent(summary="g", steps=[PlanStep(title="only step")])
    md = render_plan_as_todo_markdown(plan)
    assert "- [ ] 1. only step" in md
    assert md.endswith("\n")


def test_render_plan_as_todo_markdown_sanitizes_disco_paths() -> None:
    plan = PlanEvent(
        summary="Mark both TODO items done in .disco/context/todo.md",
        steps=[
            PlanStep(
                title="Mark both TODO items done in .disco/context/todo.md",
                detail="Do not edit .disco/context/todo.md directly.",
            )
        ],
        context="Progress lives in .disco/context/todo.md.",
    )
    md = render_plan_as_todo_markdown(plan)
    assert ".disco" not in md
    # DERIVED from the production constant that owns it (F62 / F58). The sibling
    # assertion below stays a literal ON PURPOSE: production holds
    # "harness-managed bookkeeping" only as a call argument inside
    # `_sanitize_todo_seed_text`, so no symbol owns it and there is nothing to
    # derive from — the F61 class, recorded rather than papered over.
    assert _TODO_PROGRESS_REDIRECT in md
    assert "harness-managed bookkeeping" in md


# --- approval seeds todo.md ---------------------------------------------------
def _submit_plan_step():
    return action_step("submit_plan", {"summary": "p", "steps": [{"title": "do the thing"}]})


async def _planned(cid: str):
    agent = ScriptedAgent([_submit_plan_step()])
    loop, store = build_plan_loop(agent, conversation_id=cid)
    await loop.send_message("build the thing")
    await loop.run()
    return loop, store


@pytest.mark.asyncio
async def test_approval_seeds_todo_md() -> None:
    loop, _store = await _planned("cxt6-seed")
    # inject a workspace FS so the best-effort seed has somewhere to write
    loop.executor.sandbox = _MemFS()  # type: ignore[attr-defined]
    await loop.approve_plan()
    todo = await ArtifactMemoryStore(loop.executor.sandbox).read_todo()  # type: ignore[arg-type]
    assert todo is not None
    assert "## Steps" in todo
    assert "- [ ] 1. do the thing" in todo


@pytest.mark.asyncio
async def test_approval_without_sandbox_does_not_crash() -> None:
    # no sandbox on the fake executor → seed is a best-effort no-op, approval still works
    loop, _store = await _planned("cxt6-nosbx")
    state = await loop.approve_plan()
    assert state is not None


@pytest.mark.asyncio
async def test_autonomous_approval_also_seeds_todo_md() -> None:
    # autonomous (no human) plan-approval path must seed todo.md identically to
    # interactive approve_plan() — no behavior gap between the two approval paths.
    agent = ScriptedAgent([_submit_plan_step(), finish_step()])
    loop, _store = build_plan_loop(agent, conversation_id="cxt6-auto")
    loop._autonomous = True  # type: ignore[attr-defined]
    loop.executor.sandbox = _MemFS()  # type: ignore[attr-defined]
    await loop.send_message("build the thing")
    await loop.run()
    todo = await ArtifactMemoryStore(loop.executor.sandbox).read_todo()  # type: ignore[arg-type]
    assert todo is not None and "- [ ] 1. do the thing" in todo
