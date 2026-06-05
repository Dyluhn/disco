"""The core toolset (tool-sandbox-contract.md §9) + a default registry builder.

v1 ships the headless-buildable tools: file_read/write/edit, shell, code_exec,
search, extract. Deferred (need external infra / a contract patch, documented as
GAPs): `browser` (Playwright + dual-LLM read/act split + noVNC) and
`deploy_preview` (a real dev server + controlled preview boundary).
"""

from __future__ import annotations

from ..registry import ToolRegistry
from .files import FileEditTool, FileListTool, FileReadTool, FileWriteTool
from .retrieval import ExtractTool, SearchTool
from .system import CodeExecTool, ShellTool

__all__ = [
    "CodeExecTool",
    "ExtractTool",
    "FileEditTool",
    "FileListTool",
    "FileReadTool",
    "FileWriteTool",
    "SearchTool",
    "ShellTool",
    "build_default_registry",
]


def build_default_registry() -> ToolRegistry:
    """Register the v1 core toolset. browser/deploy_preview are intentionally
    absent (deferred); scoping intersects with what's registered, so they're
    simply never offered until built."""
    registry = ToolRegistry()
    for tool in (
        FileReadTool(),
        FileWriteTool(),
        FileEditTool(),
        FileListTool(),
        ShellTool(),
        CodeExecTool(),
        SearchTool(),
        ExtractTool(),
    ):
        registry.register(tool)
    return registry
