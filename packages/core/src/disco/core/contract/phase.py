"""Build-phase state machine (CONTRACT-ACTIVATE).

CONTRACT-ENFORCE gates tools per phase but needs a live phase signal. This tracker is
that signal: a small, deterministic state machine advanced by OBSERVABLE build events
(a bootstrap tool succeeded, the finalizer was called, a verifier passed/failed). The
executor's ContractScopeGuard reads ``current()`` to decide each call.

Transitions (conservative — it never wrongly broadens; the worst case keeps the raw
escape-hatch tools denied, never grants them early):

    BOOTSTRAP --(a bootstrap-scoped tool succeeds)--> EDIT
    *         --(the verify finalizer is called)----> VERIFY
    VERIFY    --(verifier passed)-------------------> EXPORT
    VERIFY    --(verifier failed)-------------------> REPAIR
    REPAIR    --(a repair/edit tool succeeds)-------> REPAIR   (stays until re-finalized)

Pure; no runtime imports.
"""

from __future__ import annotations

from .models import BuildContract
from .scopes import Phase


class BuildPhaseTracker:
    """Tracks the current build phase for one run, advanced by observable signals."""

    def __init__(self, contract: BuildContract) -> None:
        self._bootstrap_tools = frozenset(contract.bootstrap.tools)
        self._finalizer = contract.verify.finalizer
        self._phase = Phase.BOOTSTRAP

    def current(self) -> Phase:
        return self._phase

    def note_tool_success(self, tool_name: str) -> None:
        """A tool ran successfully — advance the phase if this is a transition signal."""
        if tool_name == self._finalizer:
            self._phase = Phase.VERIFY
            return
        if self._phase is Phase.BOOTSTRAP and tool_name in self._bootstrap_tools:
            self._phase = Phase.EDIT

    def note_finalizer_called(self) -> None:
        """The model requested verification (the finalizer fired)."""
        self._phase = Phase.VERIFY

    def note_verifier_result(self, *, passed: bool) -> None:
        """The host verifier adjudicated: pass → EXPORT, fail → REPAIR (so the model can
        use the repair tools to fix it; without this a failed verify would strand)."""
        self._phase = Phase.EXPORT if passed else Phase.REPAIR
