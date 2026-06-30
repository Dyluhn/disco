"""The core toolset (tool-sandbox-contract.md §9) + a default registry builder.

Ships the headless-buildable tools: file_read/write/edit/list, shell, code_exec,
search, extract, and `browser` (web reach + the prompt-injection content defense,
read/act separation — runs through the sandbox). EPIC F: the platform-owned preview
surface (`preview_start/status/logs/stop`) supersedes the old `deploy_preview`
placeholder — the model declares intent, the platform owns the port/serving/health.
"""

from __future__ import annotations

from ..registry import ToolRegistry
from ._deck_patch import DeckPatchTool
from .appkit import APP_TOOLS
from .audio_overview import AudioOverviewTool
from .scaffold_starter import ScaffoldStarterTool
from .browser import BrowserTool
from .context_memory import ContextMemoryTool
from .files import (
    ExactReplaceTool,
    FileAppendTool,
    FileEditTool,
    FileInsertLinesTool,
    FileListTool,
    FileReadTool,
    FileReplaceLinesTool,
    FileStrReplaceTool,
    FileWriteTool,
    SafeWriteFileTool,
)
from .image_gen import ImageGenTool, select_image_backend
from .plan import PlanStepTool, SubmitPlanTool, UpdatePlanProgressTool
from .preview import (
    PreviewLogsTool,
    PreviewStartTool,
    PreviewStatusTool,
    PreviewStopTool,
)
from .retrieval import ExtractTool, SearchTool
from .run_script import RunProjectScriptTool
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
    "ContextMemoryTool",
    "DeckPatchTool",
    "ExactReplaceTool",
    "DelegateExploreTool",  # C20: read-only Explore/Plan helper dispatch+join (intercepted by loop)
    "ExtractTool",
    "FileEditTool",
    "FileInsertLinesTool",
    "FileListTool",
    "FileReadTool",
    "FileReplaceLinesTool",
    "FileStrReplaceTool",
    "FileWriteTool",
    "RunProjectScriptTool",
    "SafeWriteFileTool",
    "ImageGenTool",
    "select_image_backend",
    "PlanStepTool",
    "PreviewStartTool",
    "PreviewStatusTool",
    "PreviewLogsTool",
    "PreviewStopTool",
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
    """Register the core toolset. The EPIC F preview surface (preview_start/status/
    logs/stop) is registered below and supersedes the old `deploy_preview` placeholder;
    scoping intersects with what's registered, so only registered tools are ever offered."""
    registry = ToolRegistry()
    for tool in (
        FileReadTool(),
        FileWriteTool(),
        FileAppendTool(),
        FileEditTool(),
        FileReplaceLinesTool(),
        FileInsertLinesTool(),
        FileStrReplaceTool(),  # W4: anchored str-replace; withheld from weak-tier advertised set
        ExactReplaceTool(),  # CD-TOOLS-2: atomic exact-match batch replace; anchored-edit tier
        SafeWriteFileTool(),  # CD-TOOLS-3: guarded whole-file writer (shrink/governed/atomic)
        RunProjectScriptTool(),  # CD-TOOLS-7: buffered transactional batch of file transforms
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
        # EPIC F: platform-owned preview — the model declares intent, never a port.
        PreviewStartTool(),
        PreviewStatusTool(),
        PreviewLogsTool(),
        PreviewStopTool(),
        SubmitPlanTool(),  # plan-mode: proposed plan (intercepted by the loop)
        PlanStepTool(),  # plan-mode: capstone progress reports (legacy incremental)
        UpdatePlanProgressTool(),  # runthru-v2 #3: declarative full-state progress (capable)
        SheetsTool(),  # sheet_generate: write .xlsx with live formulas
        AudioOverviewTool(),  # audio_overview: two-voice TTS from finished report
        SlidesTool(),  # slides_generate: Marp-rendered slide decks (HTML/PDF/PPTX)
        DeckPatchTool(),  # deck_patch: C-EDIT-4 RFC-6902 JSON Patch + re-render
        ThinkTool(),  # think: NO-OP reasoning scratchpad (avoids prose-into-action degeneration)
        ContextMemoryTool(),  # CXT-2: durable .disco/context/* read + narrative write
        *(cls() for cls in APP_TOOLS),  # P4/TOOL-1: AppKit semantic mutation tools
        ScaffoldStarterTool(),  # P7: materialize the contract's host-owned starter frame
        # image_generate: keyless/local image synthesis (PIL procedural; configurable
        # via Settings to use OpenAI-compatible or ComfyUI backends). No backend pinned
        # here — the tool re-reads the saved provider per call (config honored live).
        ImageGenTool(),
        # C20: read-only Explore/Plan helper dispatch+join (intercepted by loop)
        DelegateExploreTool(),
    ):
        registry.register(tool)
    return registry
