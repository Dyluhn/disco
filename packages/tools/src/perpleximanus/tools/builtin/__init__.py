"""The core toolset (tool-sandbox-contract.md §9) + a default registry builder.

Ships the headless-buildable tools: file_read/write/edit/list, shell, code_exec,
search, extract, and `browser` (web reach + the prompt-injection content defense,
read/act separation — runs through the sandbox). Deferred: `deploy_preview` (a real
dev server + controlled preview boundary).
"""

from __future__ import annotations

from ..registry import ToolRegistry
from .browser import BrowserTool
from .files import (
    FileAppendTool,
    FileEditTool,
    FileInsertLinesTool,
    FileListTool,
    FileReadTool,
    FileReplaceLinesTool,
    FileWriteTool,
)
from .plan import PlanStepTool, SubmitPlanTool
from .retrieval import ExtractTool, SearchTool
from .server import ServerStatusTool
from .shell_sessions import ShellExecTool, ShellKillTool, ShellViewTool, ShellWaitTool, ShellWriteTool
from .system import CodeExecTool, ShellTool

__all__ = [
    "BrowserTool",
    "CodeExecTool",
    "ExtractTool",
    "FileEditTool",
    "FileInsertLinesTool",
    "FileListTool",
    "FileReadTool",
    "FileReplaceLinesTool",
    "FileWriteTool",
    "PlanStepTool",
    "SearchTool",
    "ServerStatusTool",
    "ShellTool",
    "ShellExecTool",
    "ShellKillTool",
    "ShellViewTool",
    "ShellWaitTool",
    "ShellWriteTool",
    "SubmitPlanTool",
    "build_default_registry",
]

def build_default_registry() -> ToolRegistry:
    """Register the core toolset. `deploy_preview` is intentionally absent (deferred);
    scoping intersects with what's registered, so it's simply never offered until built."""
    registry = ToolRegistry()
    for tool in (
        FileReadTool(),
        FileWriteTool(),
        FileAppendTool(),
        FileEditTool(),
        FileReplaceLinesTool(),
        FileInsertLinesTool(),
        FileListTool(),
        ShellTool(),
        ShellExecTool(),
        ShellViewTool(),
        ShellWaitTool(),
        ShellWriteTool(),
        ShellKillTool(),
        CodeExecTool(),
        SearchTool(),
        ExtractTool(),
        BrowserTool(),
        ServerStatusTool(),
        SubmitPlanTool(),  # plan-mode: proposed plan (intercepted by the loop)
        PlanStepTool(),  # plan-mode: capstone progress reports
    ):
        registry.register(tool)
    return registry
