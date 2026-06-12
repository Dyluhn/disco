"""Planner-safety classification — the executor reports which in-scope tools are
read-only (ToolDef.read_only), and the loop scopes the PLANNING agent to exactly
those. This test pins the PRODUCTION classification: which concrete tools may a
planner be handed, and (critically) which may NOT."""

from __future__ import annotations

from disco.tools import (
    DefaultToolExecutor,
    agent_scope,
    build_default_registry,
)

# The durable contract: these tools only OBSERVE — safe to expose pre-approval.
_EXPECTED_READONLY = {"file_read", "file_list", "search", "extract", "submit_plan", "plan_step"}
# These MUTATE the workspace / sandbox / outside world — must stay off the
# planner's table until the plan is approved.
_EXPECTED_MUTATING = {"file_write", "file_append", "file_edit", "shell", "code_exec", "browser"}


def _executor():
    return DefaultToolExecutor(build_default_registry(), agent_scope())


def test_readonly_tool_names_matches_the_classification():
    ex = _executor()
    in_scope = {t.name for t in ex.available_tools()}
    readonly = ex.readonly_tool_names()
    # Every read-only name we expect (and that's in scope) is reported.
    assert (_EXPECTED_READONLY & in_scope) <= readonly
    # And NOTHING mutating is ever reported as read-only — the load-bearing half.
    assert readonly.isdisjoint(_EXPECTED_MUTATING)


def test_every_mutating_tool_is_excluded_from_readonly():
    ex = _executor()
    readonly = ex.readonly_tool_names()
    for name in _EXPECTED_MUTATING:
        assert name not in readonly, f"{name} mutates but was reported read-only"


def test_fail_safe_default_unmarked_tool_is_mutating():
    # A ToolDef that forgets `read_only` defaults to False → treated as mutating
    # → withheld from the planner. The worst case is an over-restricted planner,
    # never a write tool leaking to it.
    from disco.tools.anatomy import ToolDef
    from pydantic import BaseModel

    class _Args(BaseModel):
        pass

    d = ToolDef(name="hypothetical", description="x", args_model=_Args)
    assert d.read_only is False


def test_readonly_subset_of_in_scope():
    ex = _executor()
    in_scope = {t.name for t in ex.available_tools()}
    assert ex.readonly_tool_names() <= in_scope
