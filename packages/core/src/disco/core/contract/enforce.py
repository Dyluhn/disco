"""Contract scope ENFORCEMENT at the tool-dispatch boundary (CONTRACT-ENFORCE).

CONTRACT-3 compiled a contract into per-phase hard allowlists; this is the piece that
DENIES an out-of-scope tool call before it executes. The executor (the universal tool
chokepoint) holds an optional ``ContractScopeGuard``; when a build run is governed by a
contract, the guard rejects a call whose tool is not permitted in the current phase —
so e.g. an ``appkit.leadgen`` run cannot ``file_write`` during the EDIT phase (it must
use the semantic app_* tools); raw rewrite is reachable only in REPAIR.

Governed surface: a tool is enforced if the contract scopes it to SOME phase (a build
tool the contract knows) OR it is a known escape-hatch mutator (file_write/shell/…).
Neutral control/inspection tools are scoped into every phase by the compiler, so
enforcement constrains the BUILD mutation surface without breaking bookkeeping,
preview, verifier, or read tools. Pure decision logic + a thin guard; no runtime imports.
"""

from __future__ import annotations

from collections.abc import Callable

from pydantic import BaseModel, ConfigDict

from .models import BuildContract
from .scopes import ContractToolScopes, Phase, compile_tool_scopes

# Fallback escape-hatch mutators, used ONLY when a caller can't supply tool metadata
# (pure unit tests). The real dispatch path passes ``is_mutating`` derived from the
# tool's read_only flag, so enforcement does NOT depend on this list staying in sync
# with the registry (no bypass via an unlisted writer/exec like file_append/shell_exec).
DANGEROUS_TOOLS: frozenset[str] = frozenset(
    {"file_write", "file_edit", "file_replace_lines", "file_delete", "file_append",
     "file_str_replace", "shell", "shell_exec", "code_exec", "bash", "exec"}
)


class ScopeDecision(BaseModel):
    """The allow/deny outcome for one tool call under a contract + phase."""

    model_config = ConfigDict(frozen=True)

    allowed: bool
    reason: str = ""
    # CD-TOOLS-6 — a machine-readable denial code. Set to VERIFIER_ONLY_TOOL_BLOCKED when a
    # MUTATING tool is denied because the run is in the VERIFY (read-only diagnostics) phase.
    code: str = ""


def _governed(tool: str, scopes: ContractToolScopes, is_mutating: bool | None) -> bool:
    """Is this tool subject to contract enforcement at all?

    Governed = a MUTATING tool (derived from the registry's read_only flag at the
    dispatch boundary — so a writer/exec is gated whatever its name), OR a build tool
    the contract scopes to any phase. When ``is_mutating`` is None
    (a caller with no tool metadata, e.g. a pure unit test) it falls back to the
    best-effort DANGEROUS_TOOLS name list. Phase-neutral controls, including the
    active finalizer, are compiled into every phase before this governed check runs.
    """
    mutating = is_mutating if is_mutating is not None else (tool in DANGEROUS_TOOLS)
    if mutating:
        return True
    return tool in (
        scopes.bootstrap | scopes.edit | scopes.repair | scopes.verify | scopes.export
    )


def decide_tool_in_scope(
    scopes: ContractToolScopes, phase: Phase, tool: str, *, is_mutating: bool | None = None
) -> ScopeDecision:
    """Allow/deny ``tool`` in ``phase`` under compiled ``scopes`` (HARD allowlist).

    - permitted in this phase → allow
    - governed (a mutating tool, or one scoped to another phase) but NOT in this phase → DENY
    - ungoverned read-only utility not named by the contract → allow

    ``is_mutating`` should be passed by the dispatch boundary (``not read_only``) so
    enforcement is metadata-driven, not dependent on a hand-maintained name list.
    """
    if scopes.allowed(phase, tool):
        return ScopeDecision(allowed=True)
    if _governed(tool, scopes, is_mutating):
        permitted = ", ".join(sorted(scopes.for_phase(phase))) or "(none)"
        mutating = is_mutating if is_mutating is not None else (tool in DANGEROUS_TOOLS)
        hint = (
            "Use the contract's semantic edit tools — raw file rewrite is reachable "
            "only in the repair phase."
            if mutating
            else "This tool belongs to a different build phase."
        )
        # CD-TOOLS-6 — a mutator denied because we are in the read-only VERIFY phase gets a
        # machine-readable code so callers/harness can distinguish "verifier may not mutate" from
        # a generic wrong-phase denial.
        code = "VERIFIER_ONLY_TOOL_BLOCKED" if (mutating and phase is Phase.VERIFY) else ""
        return ScopeDecision(
            allowed=False,
            code=code,
            reason=(
                f"out of contract scope: tool {tool!r} is not permitted in the "
                f"{phase.value} phase of this contract; permitted here: {permitted}. " + hint
            ),
        )
    return ScopeDecision(allowed=True)


ScopeAuditObserver = Callable[[str, Phase, ScopeDecision], None]


class ContractScopeGuard:
    """The executor-side guard: holds the compiled scopes + a live phase provider, and
    checks a tool name against the current phase. Constructed by whoever runs a build
    under a contract; when absent, the executor enforces nothing (a plain agent run)."""

    def __init__(
        self,
        scopes: ContractToolScopes,
        phase_provider: Callable[[], Phase],
        *,
        observe: bool = False,
        observer: ScopeAuditObserver | None = None,
    ) -> None:
        self._scopes = scopes
        self._phase = phase_provider
        self._observe = observe
        self._observer = observer

    @classmethod
    def for_contract(
        cls,
        contract: BuildContract,
        phase_provider: Callable[[], Phase],
        *,
        observe: bool = False,
        observer: ScopeAuditObserver | None = None,
    ) -> ContractScopeGuard:
        return cls(
            compile_tool_scopes(contract),
            phase_provider,
            observe=observe,
            observer=observer,
        )

    def check(self, tool_name: str, *, is_mutating: bool | None = None) -> ScopeDecision:
        phase = self._phase()
        decision = decide_tool_in_scope(
            self._scopes, phase, tool_name, is_mutating=is_mutating
        )
        if self._observer is not None:
            self._observer(tool_name, phase, decision)
        if self._observe:
            # Audit/observe mode must never alter executor behavior. The observer
            # receives the real decision above; callers see an allow so dispatch
            # proceeds exactly as it would without a guard.
            return ScopeDecision(allowed=True)
        return decision
