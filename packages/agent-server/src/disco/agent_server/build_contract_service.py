"""Build-contract lifecycle extracted from runtime.py.

Owns activation, fold, kind, tracker, delivery, finalizer, and host-verify canary.

God-file decomposition (pure move, zero behavior change). The build-contract
machinery moves out of runtime.py into a `BuildContractService` collaborator
constructed once in `ConversationRuntime`:

  - activation:       activate_contract_for_brief
  - fold:             _fold_contract_from_history
  - kind resolution:  set_build_kind, _build_contract_for, _classify_build_kind_for_audit
  - tracker life:     _build_scope_guard, _build_scope_audit_guard,
                      _toolscope_audit_recorder, _emit_toolscope_audit_summary,
                      note_build_verify_result
  - delivery shape:   expected_delivery_mode
  - finalizer:        _finalizer_alias_for
  - canary:           _host_verify_canary_hook_for, _record_host_verify_canary
  - starters:         _starter_kit_for
  - audit helpers:    _conversation_user_text_sync, _AUDIT_KIND_TERMS,
                      _ToolScopeAuditRecorder

The mutable dicts (`_build_kind`, `_build_trackers`, `_build_audit_trackers`,
`_toolscope_audits`) and the registry (`_build_contract_registry`) stay declared
on `ConversationRuntime`; the service reaches them — plus `_store`, `_executors`,
`_surface_of`, `_effective_artifact_mode`, `_evict_loop_for_model_change`,
`_BUILD_LIKE_SURFACES` — via the back-reference `self._rt`. Every moved method
keeps a thin delegator on `ConversationRuntime` because internal callers
(`_compose_build_loop`, `send_user_turn`, `_finalize_clean_return`, etc.) and
tests reach them directly on the runtime.
"""

from __future__ import annotations

import json
import logging
import posixpath
import re
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING

from disco.core import (
    ActionEvent,
    ConversationStatus,
    EventSource,
    MessageEvent,
    ObservationEvent,
    VerifierStartedEvent,
    VerifierVerdictEvent,
)
from disco.core.appkit import BuildBrief
from disco.core.context.ledger import ArtifactRecord
from disco.core.context.store import ArtifactMemoryStore
from disco.core.contract import (
    BuildContract,
    BuildPhaseTracker,
    ContractKind,
    ContractScopeGuard,
    Phase,
    ScopeDecision,
)

if TYPE_CHECKING:
    from .runtime import ConversationRuntime

logger = logging.getLogger(__name__)
_log = logging.getLogger("disco.agent_server.runtime")  # preserve log identity after extraction


class _ToolScopeAuditRecorder:
    """Per-conversation REL-3 audit counters and deny-log emission."""

    def __init__(self, conversation_id: str) -> None:
        self.conversation_id = conversation_id
        self.total_tools = 0
        self.would_denies_by_phase: dict[str, int] = {}
        self._summary_emitted = False

    def record(self, tool: str, phase: Phase, decision: ScopeDecision) -> None:
        if self._summary_emitted:
            self.total_tools = 0
            self.would_denies_by_phase.clear()
            self._summary_emitted = False
        self.total_tools += 1
        if decision.allowed:
            return
        phase_value = phase.value
        self.would_denies_by_phase[phase_value] = self.would_denies_by_phase.get(phase_value, 0) + 1
        _log.info(
            "toolscope audit would-deny conversation=%s phase=%s tool=%s reason=%s",
            self.conversation_id,
            phase_value,
            tool,
            decision.reason,
            extra={
                "event": "toolscope_audit_would_deny",
                "conversation": self.conversation_id,
                "phase": phase_value,
                "tool": tool,
                "reason": decision.reason,
            },
        )

    def emit_summary(self, status: ConversationStatus) -> None:
        if self._summary_emitted:
            return
        by_phase = dict(sorted(self.would_denies_by_phase.items()))
        _log.info(
            "toolscope audit summary conversation=%s status=%s total_tools=%d "
            "would_denies_by_phase=%s",
            self.conversation_id,
            status.value,
            self.total_tools,
            by_phase,
            extra={
                "event": "toolscope_audit_summary",
                "conversation": self.conversation_id,
                "status": status.value,
                "total_tools": self.total_tools,
                "would_denies_by_phase": by_phase,
            },
        )
        self._summary_emitted = True


_AUDIT_KIND_TERMS: tuple[tuple[ContractKind, tuple[str, ...]], ...] = (
    (
        ContractKind.DECK,
        (
            "slide deck",
            "slides",
            "presentation",
            "powerpoint",
            "pptx",
            "deck",
        ),
    ),
    (
        ContractKind.DOCUMENT,
        (
            "document",
            "report",
            "white paper",
            "whitepaper",
            "pdf",
            "proposal",
            "briefing memo",
        ),
    ),
    (
        ContractKind.APPKIT_LEADGEN,
        (
            "lead gen",
            "lead-gen",
            "lead generation",
            "lead form",
            "contact form",
            "signup form",
            "sign-up form",
        ),
    ),
    (
        ContractKind.INTERACTIVE_PROTOTYPE,
        (
            "interactive prototype",
            "prototype",
            "calculator",
            "quiz",
            "game",
            "simulator",
        ),
    ),
    (
        ContractKind.STATIC_SITE,
        (
            "static site",
            "landing page",
            "website",
            "web site",
            "homepage",
            "portfolio",
            "site",
        ),
    ),
    (
        ContractKind.WORKFLOW_OUTPUT,
        (
            "workflow output",
            "workflow",
            "automation output",
        ),
    ),
)


class BuildContractService:
    """Per-conversation build-contract lifecycle.

    Reaches mutable runtime state via `self._rt` — the owning
    `ConversationRuntime`. See the module docstring for the full list of
    reached attributes.
    """

    def __init__(self, runtime: ConversationRuntime) -> None:
        self._rt = runtime

    # ---- activation ----------------------------------------------------------

    def activate_contract_for_brief(
        self, conversation_id: str, build_brief: BuildBrief | None
    ) -> None:
        """CONTRACT-ACTIVATE (2026-07-10): declare the build contract from the
        classified BuildBrief — the production caller set_build_kind never had.

        FIRST DECLARATION WINS: once a kind is set for a conversation it is never
        re-declared here, so a mid-run steer message (which also carries a brief)
        cannot reset the phase tracker or evict a live loop. Unmapped/unknown
        app_kinds leave the conversation on the CUSTOM contract exactly as before
        activation existed. What this turns on for a mapped build: the contract's
        starter-kit RECOMMENDATION (scaffold_starter's omitted-kind fallback), the
        verification finalizer alias, the delivery-mode label, and honest
        kind-resolved audit — NOT hard tool enforcement, which remains an
        artifact-mode-only wiring (see the executor guard selection)."""
        if build_brief is None or conversation_id in self._rt._build_kind:
            return
        from disco.core.contract import contract_kind_for_app_kind

        kind = contract_kind_for_app_kind(getattr(build_brief, "app_kind", None))
        self._rt.set_build_kind(conversation_id, (kind or ContractKind.CUSTOM).value)

    # ---- fold ----------------------------------------------------------------

    async def _fold_contract_from_history(self, conversation_id: str) -> None:
        """CONTRACT-DURABILITY (2026-07-10): restore a restarted process's contract
        identity + phase from the persisted event log.

        `_build_kind` / `_build_trackers` are process-local, so an agent-server
        restart mid-build silently dropped the declared contract (codex four-fix
        review, residual #4) — the resumed run continued contract-less until the
        next brief-bearing turn. The durable record already exists: every brief-
        bearing turn persists the ENVIRONMENT `<build_brief>` MessageEvent, tool
        outcomes persist as Action/Observation pairs, and host verify outcomes
        persist as `VerifierVerdictEvent`s. This fold replays them:

        * kind — the FIRST persisted brief's `app_kind`, through the SAME
          `contract_kind_for_app_kind` → CUSTOM-sentinel mapping activation uses,
          so a fold can never disagree with what activation would have declared
          (first-wins holds across restarts).
        * phase — successful tool calls through `BuildPhaseTracker.note_tool_success`
          and pass/fail verdicts through `note_verifier_result`, in event order —
          the exact transition inputs the live run feeds.

        Runs at most once per process per conversation (`_contract_fold_attempted`);
        no-ops for conversations that never declared a brief, and for build-like
        surfaces only. Never raises: a fold failure degrades to today's behavior
        (contract-less until the next brief), logged for the audit trail."""
        if (
            conversation_id in self._rt._build_kind
            or conversation_id in self._rt._contract_fold_attempted
        ):
            return
        if self._rt._surface_of(conversation_id) not in self._rt._BUILD_LIKE_SURFACES:
            return
        self._rt._contract_fold_attempted.add(conversation_id)
        try:
            events = await self._rt._store.get_events(conversation_id)
        except Exception:
            self._rt._contract_fold_attempted.discard(conversation_id)
            _log.warning(
                "contract fold: event read failed for %s; continuing contract-less",
                conversation_id,
                exc_info=True,
            )
            return

        from .build_messages import parse_build_brief_content

        app_kind: str | None = None
        brief_seq = -1
        for i, ev in enumerate(events):
            if isinstance(ev, MessageEvent) and ev.source == EventSource.ENVIRONMENT:
                payload = parse_build_brief_content(ev.message.content, meta=ev.meta)
                if payload is not None:
                    raw = payload.get("app_kind")
                    app_kind = raw if isinstance(raw, str) else None
                    brief_seq = i
                    break  # FIRST brief wins, matching activate_contract_for_brief
        if app_kind is None:
            return  # never declared — identical to today's no-brief behavior

        from disco.core.contract import contract_kind_for_app_kind

        kind = contract_kind_for_app_kind(app_kind) or ContractKind.CUSTOM
        self._rt.set_build_kind(conversation_id, kind.value)
        if not self._rt._effective_artifact_mode(conversation_id):
            _log.info(
                "contract fold: %s restored kind=%s (kind-only: not an artifact run)",
                conversation_id,
                kind.value,
            )
            return
        self._rt._build_scope_guard(conversation_id)
        entry = self._rt._build_trackers.get(conversation_id)
        if entry is None:
            return
        tracker = entry[1]
        actions: dict[str, str] = {}
        for ev in events[brief_seq + 1 :]:
            if isinstance(ev, ActionEvent):
                actions[ev.id] = ev.tool_call.tool_name
            elif isinstance(ev, ObservationEvent):
                if ev.tool_result.success:
                    aid = ev.action_id
                    if aid is not None:
                        tool = actions.get(aid)
                        if tool:
                            tracker.note_tool_success(tool)
            elif isinstance(ev, VerifierStartedEvent):
                tracker.note_finalizer_called()
            elif isinstance(ev, VerifierVerdictEvent):
                verdict = str(ev.verdict) if ev.verdict is not None else None
                if verdict is not None:
                    tracker.note_verifier_result(passed=verdict == "pass")
        _log.info(
            "contract fold: %s restored kind=%s phase=%s from %d events",
            conversation_id,
            kind.value,
            tracker.current().value,
            len(events),
        )

    # ---- kind ----------------------------------------------------------------

    def set_build_kind(self, conversation_id: str, kind: str | None) -> None:
        """Declare the build contract kind for a conversation (e.g. 'appkit.leadgen').
        Unset/None => the CUSTOM contract. Resets any existing tracker for the run."""
        if kind:
            self._rt._build_kind[conversation_id] = kind
        else:
            self._rt._build_kind.pop(conversation_id, None)
        self._rt._build_trackers.pop(conversation_id, None)
        self._rt._build_audit_trackers.pop(conversation_id, None)
        self._rt._evict_loop_for_model_change(conversation_id)

    def _build_contract_for(
        self, conversation_id: str, *, classify_default: bool = False
    ) -> BuildContract:
        kind = self._rt._build_kind.get(conversation_id)
        if kind is None and classify_default:
            kind = self._rt._classify_build_kind_for_audit(conversation_id)
        brief = {"kind": kind} if kind else None
        return self._rt._build_contract_registry.get_for_brief(brief, strict_kind=False)

    # ---- classify ------------------------------------------------------------

    def _classify_build_kind_for_audit(self, conversation_id: str) -> str | None:
        """Conservative REL-3 audit kind classifier.

        Explicit contract IDs win. Otherwise only obvious artifact terms classify;
        weak/unknown prompts fall back to the existing tightened CUSTOM contract.
        """
        text = self._rt._conversation_user_text_sync(conversation_id).lower()
        if not text:
            return None
        for kind in ContractKind:
            if kind.value in text:
                return kind.value
        compact = re.sub(r"[^a-z0-9.+-]+", " ", text)
        for kind, terms in self._rt._AUDIT_KIND_TERMS:
            if any(term in compact for term in terms):
                return kind.value
        return None

    def _conversation_user_text_sync(self, conversation_id: str) -> str:
        """Best-effort sync read of user text for build-kind audit classification.

        Loop composition is synchronous, so use the same SQLite recovery pattern as
        _surface_of. Non-SQLite test stores simply get the CUSTOM fallback.
        """
        conn = getattr(self._rt._store, "_conn", None)
        if conn is None:
            return ""
        chunks: list[str] = []
        try:
            rows = conn.execute(
                "SELECT payload FROM events WHERE conversation_id = ? "
                "AND kind = 'message' ORDER BY seq ASC LIMIT 8",
                (conversation_id,),
            ).fetchall()
        except Exception:
            return ""
        for row in rows:
            try:
                payload = json.loads(row["payload"])
            except Exception:
                continue
            if payload.get("source") != EventSource.USER.value:
                continue
            msg = payload.get("message") or {}
            content = msg.get("content")
            if isinstance(content, str):
                chunks.append(content)
        return "\n".join(chunks)

    # ---- tracker life --------------------------------------------------------

    def _build_scope_guard(
        self,
        conversation_id: str,
        *,
        observer: Callable[[str, Phase, ScopeDecision], None] | None = None,
    ) -> tuple[ContractScopeGuard | None, Callable[[str], None] | None]:
        """The (guard, on_tool_success) pair governing tools for an artifact run, or
        (None, None). Resolves+caches the conversation's contract and a fresh phase
        tracker on first use; the guard reads the tracker's LIVE phase per call."""
        entry = self._rt._build_trackers.get(conversation_id)
        if entry is None:
            contract = self._rt._build_contract_for(conversation_id)
            entry = (contract, BuildPhaseTracker(contract))
            self._rt._build_trackers[conversation_id] = entry
        contract, tracker = entry
        return (
            ContractScopeGuard.for_contract(
                contract,
                tracker.current,
                observer=observer,
            ),
            tracker.note_tool_success,
        )

    def _build_scope_audit_guard(
        self, conversation_id: str
    ) -> tuple[ContractScopeGuard, Callable[[str], None]]:
        """REL-3 audit guard for default build-like runs.

        It resolves a kind-classified contract when possible, falls back to the
        tightened CUSTOM contract, and observes would-denies without enforcing.
        """
        entry = self._rt._build_audit_trackers.get(conversation_id)
        if entry is None:
            contract = self._rt._build_contract_for(conversation_id, classify_default=True)
            entry = (contract, BuildPhaseTracker(contract))
            self._rt._build_audit_trackers[conversation_id] = entry
        contract, tracker = entry
        recorder = self._rt._toolscope_audit_recorder(conversation_id)
        return (
            ContractScopeGuard.for_contract(
                contract,
                tracker.current,
                observe=True,
                observer=recorder.record,
            ),
            tracker.note_tool_success,
        )

    def _toolscope_audit_recorder(self, conversation_id: str) -> _ToolScopeAuditRecorder:
        recorder = self._rt._toolscope_audits.get(conversation_id)
        if recorder is None:
            recorder = _ToolScopeAuditRecorder(conversation_id)
            self._rt._toolscope_audits[conversation_id] = recorder
        return recorder

    def _emit_toolscope_audit_summary(
        self, conversation_id: str, status: ConversationStatus
    ) -> None:
        recorder = self._rt._toolscope_audits.get(conversation_id)
        if recorder is not None:
            recorder.emit_summary(status)

    def note_build_verify_result(self, conversation_id: str, *, passed: bool) -> None:
        """Advance the build-phase tracker on a host VERIFY outcome (pass -> EXPORT, fail
        -> REPAIR so the model may use the repair tools to fix). The BOOTSTRAP->EDIT->VERIFY
        edges are driven automatically by tool success (on_tool_success); this is the
        entry point the verification gate calls for the VERIFY->EXPORT/REPAIR edge.
        No-op if the conversation has no active build tracker."""
        entry = self._rt._build_trackers.get(conversation_id)
        if entry is not None:
            entry[1].note_verifier_result(passed=passed)

    # ---- delivery shape ------------------------------------------------------

    def expected_delivery_mode(self, conversation_id: str) -> str | None:
        """P5: the host-owned delivery SHAPE ("app"|"files") the conversation's build
        contract declares — the agent-server deliverable surface reads this to label /
        validate a handoff (so a deck run can't be handed off as a runnable app).
        Resolves the contract on first use; None when no build contract governs the run
        (a plain chat conversation has no delivery shape)."""
        entry = self._rt._build_trackers.get(conversation_id)
        if entry is None:
            if not (
                self._rt._effective_artifact_mode(conversation_id)
                or conversation_id in self._rt._build_kind
            ):
                return None
            self._rt._build_scope_guard(conversation_id)
            entry = self._rt._build_trackers.get(conversation_id)
        return entry[0].artifact.delivery_mode if entry is not None else None

    # ---- finalizer -----------------------------------------------------------

    def _finalizer_alias_for(self, conversation_id: str) -> str | None:
        """P6: the contract's verification finalizer to advertise as a `finish` alias —
        ONLY for a RESOLVED, NON-CUSTOM contract. A plain build, a declared "custom" kind,
        or an unknown kind that falls back to CUSTOM gets None (never fabricate the generic
        ready_for_artifact_verification finalizer)."""
        if conversation_id not in self._rt._build_kind:
            return None
        self._rt._build_scope_guard(conversation_id)
        resolved = self._rt._build_trackers[conversation_id][0]
        if resolved.kind is ContractKind.CUSTOM:
            return None
        return resolved.verify.finalizer

    # ---- starter kit ---------------------------------------------------------

    def _starter_kit_for(self, conversation_id: str) -> str | None:
        """P7: the active contract's starter_kit name (app_shell / lead_form), stamped on
        the executor's ToolContext so scaffold_starter materializes THIS build's starter.
        None for a non-build run or a contract with no starter_kit."""
        if conversation_id not in self._rt._build_kind:
            return None
        self._rt._build_scope_guard(conversation_id)
        return self._rt._build_trackers[conversation_id][0].artifact.starter_kit

    # ---- host-verify canary --------------------------------------------------

    def _host_verify_canary_hook_for(
        self, conversation_id: str
    ) -> Callable[[VerifierVerdictEvent], Awaitable[None]] | None:
        if not self._rt._host_verify_flags_enabled():
            return None

        async def _hook(event: VerifierVerdictEvent) -> None:
            await self._rt._record_host_verify_canary(conversation_id, event)

        return _hook

    async def _record_host_verify_canary(
        self, conversation_id: str, event: VerifierVerdictEvent
    ) -> None:
        """REL-1d canary bookkeeping for a persisted host verifier verdict.

        This is deliberately non-authoritative: it advances phase telemetry and
        stamps the REL-2a manifest, but the finish outcome remains owned by the
        existing gate path.
        """
        if self._rt._surface_of(conversation_id) in self._rt._BUILD_LIKE_SURFACES:
            self._rt._build_scope_guard(conversation_id)
        passed = event.verdict == "pass"
        self._rt.note_build_verify_result(conversation_id, passed=passed)

        path = posixpath.normpath(str(event.artifact_path or "").strip())
        if path in ("", ".") or path.startswith("/") or path == ".." or path.startswith("../"):
            _log.info(
                "host verify canary skipped manifest stamp for %s: invalid artifact path %r",
                conversation_id,
                event.artifact_path,
            )
            return

        sbx = getattr(self._rt._executors.get(conversation_id), "sandbox", None)
        if sbx is None:
            _log.info(
                "host verify canary skipped manifest stamp for %s: sandbox unavailable",
                conversation_id,
            )
            return

        store = ArtifactMemoryStore(sbx)
        existing = next((r for r in await store.read_artifacts() if r.path == path), None)
        base = existing or ArtifactRecord(path=path, kind=event.artifact_kind or "files")
        kind = event.artifact_kind or base.kind
        await store.upsert_artifact(
            base.model_copy(
                update={
                    "path": path,
                    "kind": kind,
                    "verified": passed,
                    "verify_verdict": event.verdict,
                }
            )
        )


def host_verify_canary_enabled() -> bool:
    """True iff DISCO_HOST_VERIFY_CANARY is truthy (default OFF)."""
    from disco.core.env import disco_env

    return str(disco_env("HOST_VERIFY_CANARY") or "").strip().lower() in frozenset(
        {"1", "true", "yes", "on"}
    )
