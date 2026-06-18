"""The core toolset (tool-sandbox-contract.md §9) + a default registry builder.

Ships the headless-buildable tools: file_read/write/edit/list, shell, code_exec,
search, extract, and `browser` (web reach + the prompt-injection content defense,
read/act separation — runs through the sandbox). Deferred: `deploy_preview` (a real
dev server + controlled preview boundary).
"""

from __future__ import annotations

from ..registry import ToolRegistry
from .audio_overview import AudioOverviewTool
from .browser import BrowserTool
from .files import (
    FileAppendTool,
    FileEditTool,
    FileInsertLinesTool,
    FileListTool,
    FileReadTool,
    FileReplaceLinesTool,
    FileStrReplaceTool,
    FileWriteTool,
)
from .image_gen import ImageGenTool
from .plan import PlanStepTool, SubmitPlanTool
from .retrieval import ExtractTool, SearchTool
from .server import ServerStatusTool
from .sheets import SheetsTool
from .shell_sessions import (
    ShellExecTool,
    ShellKillTool,
    ShellViewTool,
    ShellWaitTool,
    ShellWriteTool,
)
from .slides import SlidesTool
from .subagent import DelegateExploreTool
from .system import CodeExecTool, ShellTool
from .think import ThinkTool

__all__ = [
    "AudioOverviewTool",
    "BrowserTool",
    "CodeExecTool",
    "DelegateExploreTool",  # C20: read-only Explore/Plan helper dispatch+join (intercepted by loop)
    "ExtractTool",
    "FileEditTool",
    "FileInsertLinesTool",
    "FileListTool",
    "FileReadTool",
    "FileReplaceLinesTool",
    "FileStrReplaceTool",
    "FileWriteTool",
    "ImageGenTool",
    "PlanStepTool",
    "SearchTool",
    "ServerStatusTool",
    "SheetsTool",
    "ShellTool",
    "SlidesTool",
    "ShellExecTool",
    "ShellKillTool",
    "ShellViewTool",
    "ShellWaitTool",
    "ShellWriteTool",
    "SubmitPlanTool",
    "ThinkTool",
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
        FileStrReplaceTool(),  # W4: anchored str-replace; withheld from weak-tier advertised set
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
        SheetsTool(),  # sheet_generate: write .xlsx with live formulas
        AudioOverviewTool(),  # audio_overview: two-voice TTS from finished report
        SlidesTool(),  # slides_generate: Marp-rendered slide decks (HTML/PDF/PPTX)
        ThinkTool(),  # think: NO-OP reasoning scratchpad (avoids prose-into-action degeneration)
        # image_generate: keyless/local image synthesis (PIL procedural; live diffusers deferred)
        ImageGenTool(),
        # C20: read-only Explore/Plan helper dispatch+join (intercepted by loop)
        DelegateExploreTool(),
    ):
        registry.register(tool)
    return registry
