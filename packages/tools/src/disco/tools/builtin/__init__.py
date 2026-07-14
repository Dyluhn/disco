"""The core toolset (tool-sandbox-contract.md §9) + a default registry builder.

Ships the headless-buildable tools: file_read/write/edit/list, shell, code_exec,
search, extract, and `browser` (web reach + the prompt-injection content defense,
read/act separation — runs through the sandbox). EPIC F: the platform-owned preview
surface (`preview_start/status/logs/stop`) supersedes the old `deploy_preview`
placeholder — the model declares intent, the platform owns the port/serving/health.
"""

from __future__ import annotations

from disco.core.flags import appkit_enabled

from ..registry import ToolRegistry
from ._deck_patch import DeckPatchTool
from .app_kit import APPKIT_V2_TOOLS
from .appkit import APP_TOOLS
from .audio_overview import AudioOverviewTool
from .browser import BrowserTool
from .context_memory import ContextMemoryTool
from .design_lint import DesignLintTool
from .document import DocExportTool, DocSetSectionTool
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
from .find_and_edit import FindAndEditTool
from .hardware_identity import HardwareIdentityTool
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
from .scaffold_starter import ScaffoldStarterTool
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
from .trusted_components import AddTrustedComponentTool, EjectTrustedComponentTool
from .verify_app import VerifyWebAppTool
from .workflow_controls import WorkflowNeedsInputTool, WorkflowSkipTool

__all__ = [
    "AudioOverviewTool",
    "BrowserTool",
    "CodeExecTool",
    "ContextMemoryTool",
    "DeckPatchTool",
    "DesignLintTool",
    "DocExportTool",
    "DocSetSectionTool",
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
    "FindAndEditTool",
    "HardwareIdentityTool",
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
    "WorkflowNeedsInputTool",
    "WorkflowSkipTool",
    "build_default_registry",
]

def _trusted_registry_nonempty() -> bool:
    from disco.core.trusted_components.registry import TrustedComponentRegistry

    return bool(TrustedComponentRegistry.default().names())


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
        FindAndEditTool(),  # H3: regex fan-out edit tool mediated by the driver LLM
        SafeWriteFileTool(),  # CD-TOOLS-3: guarded whole-file writer (shrink/governed/atomic)
        RunProjectScriptTool(),  # CD-TOOLS-7: buffered transactional batch of file transforms
        FileListTool(),
        HardwareIdentityTool(),  # F07: sourced PCI identity; unknown stays numeric
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
        DesignLintTool(),  # AppKit D3: read-only design-slop scanner (scope-gated)
        DocSetSectionTool(),  # document/report: schema-validated .disco/parts section writer
        DocExportTool(),  # document/report: host assembly + P10 render-facts stamp
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
        *(cls() for cls in APP_TOOLS),  # legacy AppKit wrappers still used by governed paths
        # Hard-replace endpoint (fix-2): with the legacy overlapping app_* retired,
        # the v2 AppKit tools own their names in the default registry; contract
        # scopes + the AppKit executor still gate where they are callable. The
        # LEGACY app_snapshot_version stays the registered owner of its name
        # (governed persisted-v1 paths depend on it) — skip the v2 duplicate here;
        # the AppKit executor registers the full v2 set for strict-mode builds.
        # KILL SWITCH: DISCO_APPKIT_ENABLED=0 drops the whole v2 set — app_*
        # becomes unknown_tool everywhere (the legacy pair above is a different,
        # governed surface and stays).
        *(
            cls()
            for cls in (APPKIT_V2_TOOLS if appkit_enabled() else ())
            if cls().definition.name != "app_snapshot_version"
        ),
        ScaffoldStarterTool(),  # P7: materialize the contract's host-owned starter frame
        # WO-TC2: the trusted-components tier — verified vendored security cores.
        # Free-form Build scope only (AGENT_TOOLS); strict AppKit/artifact/research
        # scopes never include the names. Registered ONLY when the registry ships
        # components — an installable-nothing tool is a false affordance (review #7).
        *(
            (AddTrustedComponentTool(), EjectTrustedComponentTool())
            if _trusted_registry_nonempty()
            else ()
        ),
        # image_generate: keyless/local image synthesis (PIL procedural; configurable
        # via Settings to use OpenAI-compatible or ComfyUI backends). No backend pinned
        # here — the tool re-reads the saved provider per call (config honored live).
        ImageGenTool(),
        # C20: read-only Explore/Plan helper dispatch+join (intercepted by loop)
        DelegateExploreTool(),
        # WF-4: workflow-only control tools. They are registered for workflow scopes
        # but not included in the default agent scope; the loop intercepts calls.
        WorkflowSkipTool(),
        WorkflowNeedsInputTool(),
    ):
        registry.register(tool)
    return registry
