"""CXT-4 tests: build_context_pack (event-log overlay) + render_context_pack
(deterministic, byte-stable, history-free `<context-pack>` block)."""

from __future__ import annotations

from disco.core.context import (
    ArtifactMemoryKind,
    CompactionPolicy,
    ContextLedger,
    ResourceRef,
    Severity,
    VerifierFailureRef,
)
from disco.core.context.compaction import context_mark_resolved, context_write_summary
from disco.core.events import EventSource, LLMMessage, MessageEvent, PlanEvent, PlanStep
from disco.core.loop.context_builder import build_context_pack, render_context_pack


def _plan(summary: str, revision: int = 1) -> PlanEvent:
    return PlanEvent(summary=summary, steps=[PlanStep(title="s1")], revision=revision)


def _user(text: str) -> MessageEvent:
    return MessageEvent(source=EventSource.USER, message=LLMMessage(role="user", content=text))


# --- build_context_pack -------------------------------------------------------
def test_goal_and_version_from_latest_plan() -> None:
    events = [_user("make a site"), _plan("a landing page", revision=2)]
    pack = build_context_pack(events)
    assert pack.active_goal == "a landing page"
    assert pack.current_version == 2


def test_goal_falls_back_to_head_user_message() -> None:
    pack = build_context_pack([_user("build me a dashboard")])
    assert pack.active_goal == "build me a dashboard"
    assert pack.current_version == 0


def test_resolved_summary_refs_folded_into_recoverable() -> None:
    events = [
        _plan("g"),
        context_mark_resolved(2, 3, range_id="r1"),
        context_write_summary("r1", ".disco/context/summary/r1.md", "explored, dead end"),
    ]
    pack = build_context_pack(events)
    paths = {r.rel_path for r in pack.recoverable_refs}
    assert ".disco/context/summary/r1.md" in paths
    assert any(r.kind is ArtifactMemoryKind.SUMMARY for r in pack.recoverable_refs)


def test_duplicate_legacy_summary_refs_are_collapsed_by_path() -> None:
    events = [
        _plan("g"),
        context_mark_resolved(2, 3, range_id="legacy-1"),
        context_write_summary("legacy-1", ".disco/context/summary/shared.md", "first"),
        context_mark_resolved(2, 3, range_id="legacy-2"),
        context_write_summary("legacy-2", ".disco/context/summary/shared.md", "second"),
    ]
    pack = build_context_pack(events)
    assert [
        ref.rel_path
        for ref in pack.recoverable_refs
        if ref.rel_path == ".disco/context/summary/shared.md"
    ] == [".disco/context/summary/shared.md"]


def test_failures_arg_surfaces_unresolved_only() -> None:
    failures = (
        VerifierFailureRef(kind="console_error", message="boom", severity=Severity.BLOCKER),
        VerifierFailureRef(kind="render", message="blank", resolved=True),
    )
    pack = build_context_pack([_plan("g")], failures=failures)
    msgs = {f.message for f in pack.latest_failures}
    assert msgs == {"boom"}  # resolved one excluded by from_ledger


def test_explicit_empty_failures_clears_ledger_failures() -> None:
    led = ContextLedger.empty("c").model_copy(
        update={"latest_verifier_failures": (VerifierFailureRef(kind="k", message="stale"),)}
    )
    # failures=() must CLEAR (not fall back to the ledger's stale failures)
    cleared = build_context_pack([_plan("g")], base_ledger=led, failures=())
    assert cleared.latest_failures == ()
    # failures=None (default) keeps the ledger's
    kept = build_context_pack([_plan("g")], base_ledger=led)
    assert {f.message for f in kept.latest_failures} == {"stale"}


def test_base_ledger_resources_preserved() -> None:
    led = ContextLedger.empty("c").model_copy(
        update={"resource_manifest": (ResourceRef(rel_path="logo.svg", source="u://x"),)}
    )
    pack = build_context_pack([_plan("g")], base_ledger=led)
    assert any(r.rel_path == "logo.svg" for r in pack.resource_refs)


# --- render_context_pack ------------------------------------------------------
def test_render_included_once_and_no_history() -> None:
    events = [_user("ignored raw history"), _plan("ship the page", revision=1)]
    design = "## Design Direction: Dark Glass\n- ID: dark-glass"
    out = render_context_pack(
        build_context_pack(events, todo_text="- [ ] hero", design_direction=design)
    )
    assert out.count("<context-pack>") == 1
    assert out.count("</context-pack>") == 1
    assert out.startswith("<context-pack>") and out.endswith("</context-pack>")
    # the pack carries the GOAL, not the raw user-history line
    assert "ship the page" in out
    assert "ignored raw history" not in out
    assert out.count("## Design Direction: Dark Glass") == 1
    assert out.count("- ID: dark-glass") == 1


def test_render_is_byte_stable() -> None:
    events = [_plan("g", revision=3)]
    failures = (VerifierFailureRef(kind="k", message="m"),)
    a = render_context_pack(build_context_pack(events, failures=failures, todo_text="t"))
    b = render_context_pack(build_context_pack(events, failures=failures, todo_text="t"))
    assert a == b


def test_render_sections_in_priority_order() -> None:
    events = [_plan("the goal")]
    failures = (VerifierFailureRef(kind="console_error", message="boom"),)
    out = render_context_pack(build_context_pack(events, failures=failures, todo_text="- [ ] do"))
    # priority: GOAL < VERIFIER_FAILURE < TODO
    i_goal = out.index("Goal (")
    i_fail = out.index("Unresolved verifier failures")
    i_todo = out.index("Todo:")
    assert i_goal < i_fail < i_todo


def test_render_omits_empty_sections() -> None:
    out = render_context_pack(build_context_pack([_plan("g")]))
    assert "Unresolved verifier failures" not in out
    assert "Resources:" not in out
    assert "Todo:" not in out
    assert "Design Direction:" not in out


def test_render_caps_via_policy() -> None:
    led = ContextLedger.empty("c").model_copy(
        update={
            "resource_manifest": tuple(
                ResourceRef(rel_path=f"a{i}.png", source="u") for i in range(10)
            )
        }
    )
    pol = CompactionPolicy(max_resource_refs=2)
    out = render_context_pack(build_context_pack([_plan("g")], base_ledger=led, policy=pol))
    assert out.count("- a") == 2  # capped to policy limit


# --- Checks section (verification ledger, phase 2) ---------------------------------------


def test_checks_section_renders_after_todo_from_recorded_shell_runs() -> None:
    from disco.core.events import ActionEvent, ObservationEvent, ToolCall, ToolResult
    from disco.core.loop import check_ledger

    action = ActionEvent(
        thought="t",
        tool_call=ToolCall(tool_name="shell", call_id="c1", arguments={"command": "bash smoke.sh"}),
    )
    meta = check_ledger.check_meta(
        action,
        check_ledger.CheckSnapshot("a" * 64, ()),
        check_ledger.CheckSnapshot("a" * 64, ()),
        0,
    )
    obs = ObservationEvent(
        tool_result=ToolResult(call_id="c1", tool_name="shell", success=True, content="ok"),
        action_id=action.id,
        meta={"check": meta},
    )
    events = [e.model_copy(update={"seq": i + 1}) for i, e in enumerate([_plan("g"), action, obs])]
    out = render_context_pack(build_context_pack(events, todo_text="- [ ] s1"))
    assert out.index("Todo:") < out.index(check_ledger.CHECKS_HEADER)
    assert "- [current] exit 0 at step 3  bash smoke.sh" in out


def test_no_shell_runs_means_no_checks_section() -> None:
    out = render_context_pack(build_context_pack([_plan("g")], todo_text="- [ ] s1"))
    assert "Checks (" not in out
