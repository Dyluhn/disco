"""Contract → ToolScope compiler (CONTRACT-3).

Compiles a BuildContract into HARD per-phase tool allowlists: in each phase
(bootstrap / edit / repair / verify / export) the model may call ONLY the tools the
contract declared for that phase. A tool absent from a phase's pack is hard-excluded —
so e.g. a contract whose bootstrap pack is the semantic app_* tools cannot fall back to
a generic file_write during bootstrap.

Pure projection of the contract's tool packs onto phases; no tool-runtime import. The
executor-side enforcement (intersect with the registry + deny out-of-scope calls) is a
later integration that consumes these scopes (same deferral discipline as the context
runtime's wiring).
"""

from __future__ import annotations

from enum import Enum

from pydantic import BaseModel, ConfigDict

from .models import BuildContract


class Phase(str, Enum):
    BOOTSTRAP = "bootstrap"
    EDIT = "edit"
    REPAIR = "repair"
    VERIFY = "verify"
    EXPORT = "export"


class ContractToolScopes(BaseModel):
    """The compiled, hard per-phase tool allowlists for one contract."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    bootstrap: frozenset[str] = frozenset()
    edit: frozenset[str] = frozenset()
    repair: frozenset[str] = frozenset()
    verify: frozenset[str] = frozenset()
    export: frozenset[str] = frozenset()

    def for_phase(self, phase: Phase) -> frozenset[str]:
        return {
            Phase.BOOTSTRAP: self.bootstrap,
            Phase.EDIT: self.edit,
            Phase.REPAIR: self.repair,
            Phase.VERIFY: self.verify,
            Phase.EXPORT: self.export,
        }[phase]

    def allowed(self, phase: Phase, tool: str) -> bool:
        """True iff ``tool`` is permitted in ``phase`` — a HARD allowlist check."""
        return tool in self.for_phase(phase)


# CD-TOOLS-6 — the VERIFY phase is a READ-ONLY DIAGNOSTICS phase: the verifier must be able to
# INSPECT the deliverable but must NOT mutate or DRIVE it. The verifier's inspect tool is
# verify_web_app — the STRUCTURED self-test that internally runs the read-only browser checks
# (navigate/console/screenshot) and returns a verdict; it is read_only=False (drives the sandbox)
# so it must be listed explicitly or the guard would deny the verifier itself. Raw `browser` is
# DELIBERATELY EXCLUDED: BrowserArgs admits click/fill/submit/back, and the scope guard is tool-
# name-only, so admitting raw browser would turn VERIFY into general browser AUTOMATION, not read-
# only diagnostics (codex CD-TOOLS-6 round-1). The rest are genuinely read-only (would already fall
# through to allowed); listing them makes the verify allowlist explicit + self-documenting. NO
# mutator (file_write/edit/exact_replace/safe_write_file/shell/code_exec/app_*/deck/sheets/slides/
# image/scaffold/preview_start/stop) — NOR raw browser — is here → all stay DENIED in VERIFY.
_VERIFY_DIAGNOSTICS: frozenset[str] = frozenset(
    {
        "verify_web_app",
        "file_read",
        "file_list",
        "search",
        "server_status",
        "preview_status",
        "preview_logs",
        "think",
    }
)


def compile_tool_scopes(contract: BuildContract) -> ContractToolScopes:
    """Project a contract's tool packs onto the five execution phases.

    - bootstrap → the bootstrap ToolPack's tools (scaffold the artifact)
    - edit      → the EditContract's edit_tools (targeted, semantic edits)
    - repair    → the EditContract's repair_tools (bounded recovery set; broad/rewrite
                  tools appear here ONLY when the contract declared them, which a custom
                  contract does via rewrite_allowed + its repair pack)
    - verify    → the host finalizer + the read-only DIAGNOSTICS the verifier needs to inspect
                  (CD-TOOLS-6); NO mutator is permitted here
    - export    → empty for now; export TOOLS ship in P10 (the ExportContract today
                  declares a pipeline of stage names, not tool names)
    """
    return ContractToolScopes(
        bootstrap=frozenset(contract.bootstrap.tools),
        edit=frozenset(contract.edit.edit_tools),
        repair=frozenset(contract.edit.repair_tools),
        verify=frozenset({contract.verify.finalizer}) | _VERIFY_DIAGNOSTICS,
        export=frozenset(),
    )
