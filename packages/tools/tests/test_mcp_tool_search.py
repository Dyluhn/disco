"""MCP tool_search meta-tool tests — RP-05 rung A.

Covers: cap behavior (keyword search over full tool set), meta-tool invocation
shape, no-schema-mutation guarantee.
"""

from __future__ import annotations

import pytest
from perpleximanus.core import SecurityRisk
from perpleximanus.tools.mcp.tool_search import (
    _ToolSearchArgs,
    _match_score,
    _tool_search_handler,
    meta_tool_search,
)


def test_meta_tool_search_definition():
    """The meta-tool returns a valid ToolDef."""
    tool_def = meta_tool_search()
    assert tool_def.name == "tool_search"
    assert tool_def.base_risk == SecurityRisk.LOW
    assert tool_def.runs_in == "in_process"
    assert tool_def.read_only is True
    assert tool_def.uses_capabilities == frozenset()
    assert "Return up to N" in tool_def.description
    assert tool_def.args_model is _ToolSearchArgs


def test_tool_search_args_model():
    """The args model accepts valid arguments."""
    args = _ToolSearchArgs(query="file reader", limit=3)
    assert args.query == "file reader"
    assert args.limit == 3

    # default limit
    args = _ToolSearchArgs(query="test")
    assert args.limit == 5


def test_match_score_keyword_match():
    """Keywords match against tool name and description."""
    tool = {"name": "read_file", "description": "Read a file from disk"}
    # Match on name
    assert _match_score(["read"], tool) > 0
    # Match on description
    assert _match_score(["file"], tool) > 0
    # No match
    assert _match_score(["pizza"], tool) == 0


def test_match_score_case_insensitive():
    """Keyword matching is case-insensitive."""
    tool = {"name": "Echo", "description": "ECHO back the message"}
    assert _match_score(["echo"], tool) > 0


@pytest.mark.asyncio
async def test_tool_search_handler_basic():
    """Search returns matching tools sorted by score."""
    tools = [
        {"name": "echo", "description": "Echo back"},
        {"name": "read_file", "description": "Read a file"},
        {"name": "add", "description": "Add two numbers"},
        {"name": "list_dir", "description": "List directory contents"},
    ]
    results = await _tool_search_handler("file read", 5, tools)
    assert len(results) > 0
    # read_file should be first (matches both "file" and "read")
    assert results[0]["name"] == "read_file"


@pytest.mark.asyncio
async def test_tool_search_handler_limit():
    """Search respects the limit parameter."""
    tools = [
        {"name": f"tool_{i}", "description": f"Description {i}"}
        for i in range(10)
    ]
    results = await _tool_search_handler("tool", 3, tools)
    assert len(results) == 3


@pytest.mark.asyncio
async def test_tool_search_handler_empty_query():
    """Empty query returns empty results."""
    tools = [{"name": "echo", "description": "Echo back"}]
    results = await _tool_search_handler("  ", 5, tools)
    assert results == []


@pytest.mark.asyncio
async def test_tool_search_handler_no_match():
    """No match returns empty results."""
    tools = [{"name": "echo", "description": "Echo back"}]
    results = await _tool_search_handler("pizza", 5, tools)
    assert results == []


@pytest.mark.asyncio
async def test_tool_search_does_not_mutate_input():
    """The search does NOT mutate the input all_tools list."""
    tools = [
        {"name": "echo", "description": "Echo back"},
        {"name": "add", "description": "Add two numbers"},
    ]
    original = list(tools)
    await _tool_search_handler("echo", 2, tools)
    assert tools == original


@pytest.mark.asyncio
async def test_tool_search_handler_multi_keyword():
    """Multiple keywords in query are all matched."""
    tools = [
        {"name": "echo", "description": "Echo back"},
        {"name": "read_file", "description": "Read a file from disk"},
        {"name": "write_file", "description": "Write to disk"},
    ]
    results = await _tool_search_handler("read file disk", 5, tools)
    # read_file matches all 3 keywords → highest score
    assert results[0]["name"] == "read_file"
