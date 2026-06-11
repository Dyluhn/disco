"""Tests for MCP tool naming — RP-05 rung A.

Covers: round-trip (qualified_name → split_qualified_name → same values),
regex enforcement, ambiguous name rejection.
"""

from __future__ import annotations

import pytest
from perpleximanus.tools.mcp.naming import is_mcp_qualified, qualified_name, split_qualified_name


class TestQualifiedName:
    def test_basic_round_trip(self):
        qn = qualified_name("filesystem", "read_file")
        assert qn == "mcp__filesystem__read_file"
        assert split_qualified_name(qn) == ("filesystem", "read_file")

    def test_round_trip_with_underscores(self):
        qn = qualified_name("my_server", "get_data")
        assert split_qualified_name(qn) == ("my_server", "get_data")

    def test_server_with_double_underscore_rejected(self):
        with pytest.raises(ValueError, match="double underscores"):
            qualified_name("my__bad", "tool")

    def test_tool_with_double_underscore_rejected(self):
        with pytest.raises(ValueError, match="double underscores"):
            qualified_name("server", "bad__tool")

    def test_not_qualified(self):
        assert split_qualified_name("not_mcp_prefix") is None
        assert split_qualified_name("mcp__") is None
        assert split_qualified_name("mcp__server") is None
        assert split_qualified_name("mcp__server__") is None

    def test_empty_parts_rejected(self):
        assert split_qualified_name("mcp____tool") is None

    def test_three_parts(self):
        qn = qualified_name("a", "b")
        assert split_qualified_name(qn) == ("a", "b")

    def test_is_mcp_qualified(self):
        qn = qualified_name("fs", "echo")
        assert is_mcp_qualified(qn) is True
        assert is_mcp_qualified("builtin_search") is False
        assert is_mcp_qualified("mcp__not_valid") is False
        assert is_mcp_qualified("mcp__x____y") is False
