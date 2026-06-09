"""The PLANNING agent is read-only — engine enforcement (planner safety).

Two independent guards in `_tools_for_step`, both fail-safe:
  (1) capability backstop — drop any tool the executor reports as NOT read-only,
      even one a misconfigured name allowlist explicitly names.
  (2) name allowlist — restrict further to the operator's curated set.

These tests exercise the backstop, which is the defense-in-depth half the name
allowlist alone never had.
"""

from __future__ import annotations

from loop_fakes import FakeAnalyzer, FakeExecutor, FakeSummarizer
from perpleximanus.core.llm import OperatingMode
from perpleximanus.core.llm.types import ToolSpec
from perpleximanus.core.loop import AgentLoop, NeverConfirm
from perpleximanus.core.store.sqlite import SqliteEventStore
from perpleximanus.core.view import NoOpCondenser

# A mixed tool surface: two read-only, two mutating.
_TOOLS = [
    ToolSpec(name="file_read", description="read", parameters_schema={}),
    ToolSpec(name="search", description="search", parameters_schema={}),
    ToolSpec(name="shell", description="run shell", parameters_schema={}),
    ToolSpec(name="file_write", description="write", parameters_schema={}),
    ToolSpec(name="submit_plan", description="plan", parameters_schema={}),
]


class ReadonlyExecutor(FakeExecutor):
    """A FakeExecutor that, like the real DefaultToolExecutor, can report which
    of its tools are read-only."""

    def __init__(self, readonly):
        super().__init__(tools=_TOOLS)
        self._readonly = frozenset(readonly)

    def readonly_tool_names(self):
        return self._readonly


def _loop(executor, *, mode, planning_tools=frozenset()):
    return AgentLoop(
        "conv",
        SqliteEventStore(":memory:"),
        object(),  # agent — unused by _tools_for_step
        executor,
        None,
        FakeAnalyzer(),
        NeverConfirm(),
        NoOpCondenser(),
        FakeSummarizer(),
        mode=mode,
        planning_tools=planning_tools,
    )


def _names(tools):
    return {getattr(t, "name", None) for t in tools}


# ---- the load-bearing property: the backstop overrides a bad allowlist ------


def test_backstop_drops_a_mutating_tool_even_when_the_allowlist_names_it():
    # Allowlist mistakenly includes "shell" (a write/exec tool). The capability
    # backstop drops it anyway — the planner CANNOT be handed shell.
    ex = ReadonlyExecutor(readonly={"file_read", "search", "submit_plan"})
    loop = _loop(
        ex,
        mode=OperatingMode.PLANNING,
        planning_tools=frozenset({"file_read", "search", "submit_plan", "shell"}),
    )
    names = _names(loop._tools_for_step())
    assert "shell" not in names  # backstop won, despite the allowlist
    assert "file_write" not in names
    # ask_user is the virtual planning escape hatch (read-only-safe; the loop
    # intercepts it, never executes it) — present alongside the readonly set.
    assert names == {"file_read", "search", "submit_plan", "ask_user"}


def test_planning_with_no_allowlist_shows_only_readonly_tools():
    ex = ReadonlyExecutor(readonly={"file_read", "search", "submit_plan"})
    loop = _loop(ex, mode=OperatingMode.PLANNING)  # no allowlist
    names = _names(loop._tools_for_step())
    assert names == {"file_read", "search", "submit_plan", "ask_user"}
    assert "shell" not in names and "file_write" not in names


def test_allowlist_restricts_further_within_readonly():
    # Both guards compose: readonly = {file_read, search, submit_plan}; allowlist
    # narrows to {submit_plan, file_read}. Intersection wins (+ the ask_user hatch).
    ex = ReadonlyExecutor(readonly={"file_read", "search", "submit_plan"})
    loop = _loop(
        ex,
        mode=OperatingMode.PLANNING,
        planning_tools=frozenset({"submit_plan", "file_read"}),
    )
    assert _names(loop._tools_for_step()) == {"submit_plan", "file_read", "ask_user"}


# ---- back-compat: an executor that can't report capabilities ----------------


def test_legacy_executor_without_readonly_falls_back_to_allowlist_only():
    # Plain FakeExecutor has no readonly_tool_names() → backstop is skipped and
    # only the name allowlist governs (unchanged legacy behavior). This is why
    # the existing fake-based loop tests keep passing.
    ex = FakeExecutor(tools=_TOOLS)
    loop = _loop(
        ex,
        mode=OperatingMode.PLANNING,
        planning_tools=frozenset({"submit_plan", "shell"}),
    )
    # No backstop → the allowlist (incl. shell) is honored verbatim (+ ask_user).
    assert _names(loop._tools_for_step()) == {"submit_plan", "shell", "ask_user"}


# ---- execution mode is untouched --------------------------------------------


def test_execution_mode_exposes_mutating_tools_plus_virtuals():
    ex = ReadonlyExecutor(readonly={"file_read", "search", "submit_plan"})
    loop = _loop(ex, mode=OperatingMode.LONG_HORIZON)
    names = _names(loop._tools_for_step())
    # Writes/exec are back on the table once we're executing...
    assert {"shell", "file_write"} <= names
    # ...and the virtual escape-hatch tools are appended.
    assert {"ask_user", "notify_user", "finish"} <= names


# ---- force-commit: ignored read-streak nudges withhold read tools -----------


def test_force_commit_in_planning_leaves_only_plan_tool_and_ask_user():
    # When read-streak nudges were ignored, _force_commit is armed for one step:
    # the planner loses every read-only tool so its only moves are submit_plan or
    # ask_user. This is the inclusive fix for a model stuck reading a large file.
    ex = ReadonlyExecutor(readonly={"file_read", "search", "submit_plan"})
    loop = _loop(
        ex,
        mode=OperatingMode.PLANNING,
        planning_tools=frozenset({"file_read", "search", "submit_plan"}),
    )
    loop._force_commit = True
    names = _names(loop._tools_for_step())
    assert names == {"submit_plan", "ask_user"}  # reads withheld, plan tool kept
    # And without the flag the planner sees the reads again (it self-clears).
    loop._force_commit = False
    assert "file_read" in _names(loop._tools_for_step())


def test_force_commit_in_execution_withholds_read_only_tools():
    ex = ReadonlyExecutor(readonly={"file_read", "search", "submit_plan"})
    loop = _loop(ex, mode=OperatingMode.LONG_HORIZON)
    loop._force_commit = True
    names = _names(loop._tools_for_step())
    # Read-only tools are gone; the editing/exec tools and ask_user remain.
    assert "file_read" not in names and "search" not in names
    assert {"shell", "file_write", "ask_user"} <= names


def test_read_nudges_since_user_counts_only_after_last_user_message():
    from perpleximanus.core.events import EventSource, MessageEvent
    from perpleximanus.core.llm.types import LLMMessage

    def env(text):
        return MessageEvent(
            source=EventSource.ENVIRONMENT,
            message=LLMMessage(role="user", content=text),
        )

    def user(text):
        return MessageEvent(
            source=EventSource.USER, message=LLMMessage(role="user", content=text)
        )

    stop = "<system-reminder>\nSTOP reading — commit now.\n</system-reminder>"
    events = [
        env(stop),  # belongs to a PRIOR turn — must NOT be counted
        user("new request"),
        env(stop),
        env(stop),
    ]
    assert AgentLoop._read_nudges_since_user(events) == 2
