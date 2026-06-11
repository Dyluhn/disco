"""MCP result fencing — RP-05 rung B.

`fence_mcp_result()` wraps untrusted MCP tool output in an XML fence so the LLM
knows it arrived over MCP (untrusted). The executor never feeds raw MCP output
back into the tool-call parser — this block is appended as text, not parsed.

Design mirrors `builtin/browser.py:_fence` (line 131): structured but untrusted,
never parsed back into a tool call without explicit human confirmation.
"""

from __future__ import annotations

import json
from typing import Any

# The fence applies to each MCP call result. We format the result as an untrusted
# block the LLM cannot use to inject tool calls or instructions (same discipline
# as the browser's _fence for untrusted page views).
_FENCE_OPEN = '<untrusted_mcp_result server="{server}" tool="{tool}">'
_FENCE_CLOSE = "</untrusted_mcp_result>"


def fence_mcp_result(server: str, tool: str, result: Any) -> str:
    """Produce a fenced string the LLM sees as untrusted MCP output.

    The fenced block is appended to the tool result text; the executor does
    NOT feed raw MCP output back into the tool-call parser. This is the
    CyberArk/MCPTox defense: every output channel is injectable, and the
    fence ensures the model treats it as data, not instruction.

    Args:
        server: MCP server name.
        tool: MCP tool name.
        result: The raw MCP tool result (dict with "content" and "isError" keys,
                or any JSON-serializable value).

    Returns:
        A fenced XML block string.
    """
    # Serialize the result as a schema-validated-looking JSON blob.
    # For content items with .text or a "text" key, extract just the text;
    # otherwise dump the whole result.
    if isinstance(result, dict):
        text_parts = []
        for item in result.get("content", []):
            if hasattr(item, "text"):
                text_parts.append(item.text)
            elif isinstance(item, dict) and "text" in item:
                text_parts.append(item["text"])
        body = "\n".join(text_parts) if text_parts else json.dumps(
            result, ensure_ascii=False, default=str
        )
    else:
        body = json.dumps(result, ensure_ascii=False, default=str)

    open_tag = _FENCE_OPEN.format(server=server, tool=tool)
    return f"{open_tag}\n{body}\n{_FENCE_CLOSE}"
