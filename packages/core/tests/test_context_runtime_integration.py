"""CXT-7 — context runtime integration: the P0 gate. Proves the durable context
runtime works across a real-ish Build lifecycle (create → resume → assemble) and
that the live wiring (plan approval seeds goal+todo; verify failure records to
context) is in place. NO P2+ pillar may proceed until this is green."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from disco.core.context import (
    ArtifactMemoryKind,
    ArtifactMemoryStore,
    CompactionPolicy,
    DirectEditKind,
    DirectEditRef,
    ResourceRef,
    Severity,
    VerifierFailureRef,
    context_compact_if_needed,
    context_mark_resolved,
    context_write_summary,
)
from disco.core.events import PlanEvent, PlanStep
from disco.core.loop.context_builder import build_context_pack
from disco.core.loop.finish import FinishGate
from disco.core.observations import scan_for_destructive_elision
from disco.core.view import View

from _buildsoak_fakes import build_plan_loop
from event_fakes import user_msg, with_seqs
from loop_fakes import ScriptedAgent, action_step


class _MemFS:
    def __init__(self) -> None:
        self.files: dict[str, bytes] = {}

    async def read_file(self, path: str) -> bytes:
        if path not in self.files:
            raise FileNotFoundError(path)
        return self.files[path]

    async def write_file(self, path: str, data: bytes) -> None:
        self.files[path] = data


def _submit_plan_step():
    return action_step("submit_plan", {"summary": "ship a landing page", "steps": [{"title": "hero"}]})


# --- 1. fresh build creates context (goal + todo) via LIVE approval ------------
@pytest.mark.asyncio
async def test_fresh_build_creates_goal_and_todo() -> None:
    agent = ScriptedAgent([_submit_plan_step()])
    loop, _store = build_plan_loop(agent, conversation_id="cxt7-fresh")
    loop.executor.sandbox = _MemFS()  # type: ignore[attr-defined]
    await loop.send_message("build me a landing page")
    await loop.run()
    await loop.approve_plan()
    store = ArtifactMemoryStore(loop.executor.sandbox)  # type: ignore[arg-type]
    assert await store.read_goal() == "ship a landing page"
    todo = await store.read_todo()
    assert todo is not None and "- [ ] 1. hero" in todo


# --- 2. resume reconstructs the ContextPack from durable files -----------------
@pytest.mark.asyncio
async def test_resume_reconstructs_context_pack() -> None:
    fs = _MemFS()
    store = ArtifactMemoryStore(fs)
    # a prior build populated durable context, then the transcript was truncated
    await store.write_goal("ship a landing page")
    await store.seed_todo("- [ ] 1. hero")
    await store.write_markdown(ArtifactMemoryKind.DECISIONS, "static site, no backend")
    failures = (VerifierFailureRef(kind="verify_web_app", message="blank render", severity=Severity.BLOCKER),)
    await store.record_verifier_failures(failures)
    await store.record_resources((ResourceRef(rel_path="logo.svg", source="u://x"),))
    await store.record_direct_edits((DirectEditRef(target_id="hero", rel_path="index.html", kind=DirectEditKind.TEXT),))

    # resume: rebuild the ledger from files (no chat replay)
    res = await store.reconstruct("cxt7-resume", "/ws")
    assert res.recovery_errors == ()
    led = res.ledger
    assert led.active_goal == "ship a landing page"
    assert led.latest_verifier_failures == failures
    assert any(r.rel_path == "logo.svg" for r in led.resource_manifest)
    assert any(d.target_id == "hero" for d in led.direct_edits)

    # assemble the model-facing pack from the reconstructed ledger
    pack = build_context_pack([PlanEvent(summary="ship a landing page", steps=[PlanStep(title="hero")])], base_ledger=led)
    assert pack.active_goal == "ship a landing page"
    assert {f.message for f in pack.latest_failures} == {"blank render"}
    assert any(r.rel_path == "logo.svg" for r in pack.resource_refs)


# --- 3. large logs compacted recoverably (CXT-3 + CXT-5 compose) --------------
def test_large_logs_compacted_recoverably() -> None:
    events = with_seqs([user_msg("A"), user_msg("B"), user_msg("C"), user_msg("D")])
    events = [
        *events,
        context_mark_resolved(2, 3, range_id="r1"),
        context_write_summary("r1", ".disco/context/summary/r1.md", "explored B/C — dead end; file_read it"),
    ]
    conds = context_compact_if_needed(events, CompactionPolicy.default())
    assert len(conds) == 1
    all_events = [*events, conds[0].model_copy(update={"seq": 9})]
    contents = [m.content for m in View.of(all_events).messages]
    assert "B" not in contents and "C" not in contents  # omitted from model view
    # recoverable: original events still in the audit log
    assert {e.seq for e in View.recover_span(all_events, conds[0])} == {2, 3}
    # the summary that replaced them carries a recover cue (not destructive)
    assert scan_for_destructive_elision("\n".join(c for c in contents)) == []


# --- 4. verify_web_app failure records to durable context (live hook) ----------
@pytest.mark.asyncio
async def test_verifier_failure_writes_context() -> None:
    fs = _MemFS()
    fake_loop = SimpleNamespace(executor=SimpleNamespace(sandbox=fs), conversation_id="cxt7-vf")
    gate = FinishGate(fake_loop)  # type: ignore[arg-type]
    await gate._record_verifier_failure_to_context(
        message="ReferenceError: x is not defined @ app.js:12", rel_path=".pmx/screenshots/0001.png"
    )
    failures = await ArtifactMemoryStore(fs).read_verifier_failures()
    assert len(failures) == 1
    assert failures[0].kind == "verify_web_app"
    assert "ReferenceError" in failures[0].message
    assert failures[0].rel_path == ".pmx/screenshots/0001.png"
