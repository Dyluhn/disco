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


def compile_tool_scopes(contract: BuildContract) -> ContractToolScopes:
    """Project a contract's tool packs onto the five execution phases.

    - bootstrap → the bootstrap ToolPack's tools (scaffold the artifact)
    - edit      → the EditContract's edit_tools (targeted, semantic edits)
    - repair    → the EditContract's repair_tools (bounded recovery set; broad/rewrite
                  tools appear here ONLY when the contract declared them, which a custom
                  contract does via rewrite_allowed + its repair pack)
    - verify    → the single host finalizer (ready_for_*_verification)
    - export    → empty for now; export TOOLS ship in P10 (the ExportContract today
                  declares a pipeline of stage names, not tool names)
    """
    return ContractToolScopes(
        bootstrap=frozenset(contract.bootstrap.tools),
        edit=frozenset(contract.edit.edit_tools),
        repair=frozenset(contract.edit.repair_tools),
        verify=frozenset({contract.verify.finalizer}),
        export=frozenset(),
    )
