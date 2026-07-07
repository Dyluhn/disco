"""MCP tool approval tests — RP-05 rung A.

Covers: hash computation determinism, hash mismatch detection, hash pin,
re-approval required path.
"""

from __future__ import annotations

from disco.tools.mcp.approval import (
    ApprovalRecord,
    ApprovalRequired,
    compute_description_hash,
)


class TestComputeDescriptionHash:
    def test_deterministic(self):
        """Same tool descriptions, different order → same hash."""
        tools_a = [
            {"name": "echo", "description": "Echo back a message"},
            {"name": "add", "description": "Add two numbers"},
        ]
        tools_b = [
            {"name": "add", "description": "Add two numbers"},
            {"name": "echo", "description": "Echo back a message"},
        ]
        assert compute_description_hash(tools_a) == compute_description_hash(tools_b)

    def test_different_tools_produce_different_hash(self):
        """Different tool sets → different hash."""
        h1 = compute_description_hash([
            {"name": "echo", "description": "Echo back a message"},
        ])
        h2 = compute_description_hash([
            {"name": "add", "description": "Add two numbers"},
        ])
        assert h1 != h2

    def test_same_name_different_description_produces_different_hash(self):
        """A changed description → different hash."""
        h1 = compute_description_hash([
            {"name": "echo", "description": "Echo back a message"},
        ])
        h2 = compute_description_hash([
            {"name": "echo", "description": "Echo with formatting"},
        ])
        assert h1 != h2

    def test_same_description_different_schema_produces_different_hash(self):
        """A changed inputSchema → different hash."""
        h1 = compute_description_hash([
            {
                "name": "echo",
                "description": "Echo back a message",
                "inputSchema": {
                    "type": "object",
                    "properties": {"message": {"type": "string"}},
                    "required": ["message"],
                },
            },
        ])
        h2 = compute_description_hash([
            {
                "name": "echo",
                "description": "Echo back a message",
                "inputSchema": {
                    "type": "object",
                    "properties": {"path": {"type": "string"}},
                    "required": ["path"],
                },
            },
        ])
        assert h1 != h2

    def test_empty_tool_list_produces_valid_hash(self):
        """An empty tool list produces a valid SHA-256 hex string."""
        h = compute_description_hash([])
        assert len(h) == 64
        assert all(c in "0123456789abcdef" for c in h)

    def test_hash_is_hex_string(self):
        """Hash is a 64-char hex string."""
        h = compute_description_hash([
            {"name": "t", "description": "d"},
        ])
        assert isinstance(h, str)
        assert len(h) == 64


class TestApprovalRequired:
    def test_exception_contains_server_and_hashes(self):
        """ApprovalRequired carries the server name and both hashes."""
        exc = ApprovalRequired("my_server", "abc123def456", "789012abc345")
        assert exc.server == "my_server"
        assert exc.old_hash == "abc123def456"
        assert exc.new_hash == "789012abc345"
        assert "my_server" in str(exc)
        assert "abc123def456"[:12] in str(exc)


class TestApprovalRecord:
    def test_approval_record_fields(self):
        """ApprovalRecord carries all required fields."""
        record = ApprovalRecord(
            server="test_srv",
            tool_names=["echo", "add"],
            description_hash="abcdef123456",
            approved_at="2026-06-11T00:00:00+00:00",
            approved_by="operator",
        )
        assert record.server == "test_srv"
        assert record.tool_names == ["echo", "add"]
        assert record.description_hash == "abcdef123456"
        assert record.approved_at == "2026-06-11T00:00:00+00:00"
        assert record.approved_by == "operator"

    def test_approval_record_tool_names_is_copy(self):
        """tool_names is a copy, not the original list."""
        names = ["echo", "add"]
        record = ApprovalRecord(
            server="srv",
            tool_names=names,
            description_hash="hash",
            approved_at="time",
            approved_by="op",
        )
        names.append("extra")
        assert record.tool_names == ["echo", "add"]
