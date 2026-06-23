"""The core toolset (tool-sandbox-contract.md §9) + a default registry builder.

Ships the headless-buildable tools: file_read/write/edit/list, shell, code_exec,
search, extract, and `browser` (web reach + the prompt-injection content defense,
read/act separation — runs through the sandbox). Deferred: `deploy_preview` (a real
dev server + controlled preview boundary).
"""

from __future__ import annotations

from ..registry import ToolRegistry
from ._deck_patch import DeckPatchTool
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
from .image_gen import ImageGenTool, select_image_backend
from .plan import PlanStepTool, SubmitPlanTool, UpdatePlanProgressTool
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
from .verify_app import VerifyWebAppTool

__all__ = [
    "AudioOverviewTool",
    "BrowserTool",
    "CodeExecTool",
    "DeckPatchTool",
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
    "select_image_backend",
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
    "UpdatePlanProgressTool",
    "ThinkTool",
    "VerifyWebAppTool",
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
        VerifyWebAppTool(),  # W-45: structured web-app self-test → clean finish-gate verdict
        ServerStatusTool(),
        SubmitPlanTool(),  # plan-mode: proposed plan (intercepted by the loop)
        PlanStepTool(),  # plan-mode: capstone progress reports (legacy incremental)
        UpdatePlanProgressTool(),  # runthru-v2 #3: declarative full-state progress (capable)
        SheetsTool(),  # sheet_generate: write .xlsx with live formulas
        AudioOverviewTool(),  # audio_overview: two-voice TTS from finished report
        SlidesTool(),  # slides_generate: Marp-rendered slide decks (HTML/PDF/PPTX)
        DeckPatchTool(),  # deck_patch: C-EDIT-4 RFC-6902 JSON Patch + re-render
        ThinkTool(),  # think: NO-OP reasoning scratchpad (avoids prose-into-action degeneration)
        # image_generate: keyless/local image synthesis (PIL procedural; configurable
        # via Settings to use OpenAI-compatible or ComfyUI backends). No backend pinned
        # here — the tool re-reads the saved provider per call (config honored live).
        ImageGenTool(),
        # C20: read-only Explore/Plan helper dispatch+join (intercepted by loop)
        DelegateExploreTool(),
    ):
        registry.register(tool)
    return registry
