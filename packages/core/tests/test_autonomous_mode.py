"""Autonomous mode (issue A) — the flag must DEFAULT OFF (interactive behavior
fully preserved) and, when ON, withhold the ask/intake gates from the model's tool set.

These tests pin the load-bearing property Dylan asked to verify: "when autonomous
mode is off, it actually turns off." We assert the tool schema directly — the
structural difference between the two modes — for both PLANNING and EXECUTION.
"""

from __future__ import annotations

from disco.core.llm import OperatingMode
from disco.core.llm.types import ToolSpec
from disco.core.loop import AgentLoop, NeverConfirm
from disco.core.store.sqlite import SqliteEventStore
from disco.core.view import NoOpCondenser
from loop_fakes import FakeAnalyzer, FakeExecutor, FakeSummarizer

_TOOLS = [
    ToolSpec(name="file_read", description="read", parameters_schema={}),
    ToolSpec(name="file_write", description="write", parameters_schema={}),
    ToolSpec(name="submit_plan", description="plan", parameters_schema={}),
]


def _loop(*, mode, autonomous):
    return AgentLoop(
        "conv",
        SqliteEventStore(":memory:"),
        object(),
        FakeExecutor(tools=_TOOLS),
        None,
        FakeAnalyzer(),
        NeverConfirm(),
        NoOpCondenser(),
        FakeSummarizer(),
        mode=mode,
        planning_tools=frozenset({"submit_plan", "file_read"}),
        autonomous=autonomous,
    )


def _names(tools):
    return {getattr(t, "name", None) for t in tools}


def test_default_is_off_and_offers_ask_gates_in_execution():
    # OFF (the default): ask_user + clarify ARE advertised — interactive unchanged.
    loop = _loop(mode=OperatingMode.LONG_HORIZON, autonomous=False)
    names = _names(loop._tools_for_step())
    assert "ask_user" in names
    assert "clarify" in names
    assert "questions_v2" not in names
    assert loop._autonomous is False  # constructed default


def test_autonomous_withholds_ask_gates_in_execution():
    loop = _loop(mode=OperatingMode.LONG_HORIZON, autonomous=True)
    names = _names(loop._tools_for_step())
    assert "ask_user" not in names
    assert "clarify" not in names
    assert "questions_v2" not in names
    # the rest of the meta-tools are still there (self-correction affordances)
    assert "finish" in names
    assert "notify_user" in names


def test_default_offers_ask_gates_in_planning():
    loop = _loop(mode=OperatingMode.PLANNING, autonomous=False)
    names = _names(loop._tools_for_step())
    assert "ask_user" in names
    assert "questions_v2" in names
    assert "clarify" in names


def test_autonomous_withholds_ask_gates_in_planning():
    loop = _loop(mode=OperatingMode.PLANNING, autonomous=True)
    names = _names(loop._tools_for_step())
    assert "ask_user" not in names
    assert "questions_v2" not in names
    assert "clarify" not in names


def test_flag_omitted_constructs_off():
    # Not passing autonomous at all → False (no silent on-by-default).
    loop = AgentLoop(
        "conv",
        SqliteEventStore(":memory:"),
        object(),
        FakeExecutor(tools=_TOOLS),
        None,
        FakeAnalyzer(),
        NeverConfirm(),
        NoOpCondenser(),
        FakeSummarizer(),
        mode=OperatingMode.LONG_HORIZON,
    )
    assert loop._autonomous is False
    assert "ask_user" in _names(loop._tools_for_step())
