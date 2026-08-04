"""Browser, host, export, and render-verification gates for FinishGate."""

from __future__ import annotations

from typing import Any, cast

from ...events import ActionEvent, Event, ObservationEvent, ToolCall, VerifierStartedEvent
from ...verification import HostVerificationClaim, HostVerificationResult
from ...verify_medium import VerifierMediumHint
from ..boundaries import AgentStep, HostVerificationDeliverable, VerifierContextSeed
from ..control import Disp
from .common import _FinishGateComponent, signals
from .verify_gate_parts import appkit_typed as _pt_appkit_typed
from .verify_gate_parts import browser_gate as _pt_browser_gate
from .verify_gate_parts import export_gate as _pt_export_gate
from .verify_gate_parts import finish_probe as _pt_finish_probe
from .verify_gate_parts import host_authority as _pt_host_authority
from .verify_gate_parts import host_check as _pt_host_check
from .verify_gate_parts import host_claims as _pt_host_claims
from .verify_gate_parts import host_disposition as _pt_host_disposition
from .verify_gate_parts import render_sequence as _pt_render_sequence
from .verify_gate_parts import semantic_claims as _pt_semantic_claims
from .verify_gate_parts import workflow_gate as _pt_workflow_gate
from .verify_gate_parts import workflow_paths as _pt_workflow_paths
from .verify_gate_parts.browser_probe import (
    browser_verification_unavailable,
    detect_preview_url,
    drive_verify_web_app,
    maybe_honest_unverifiable_static_actionless_finish,
    maybe_honest_unverifiable_static_finish,
    record_verifier_failure_to_context,
    static_deliverable_present,
    verifier_unavailable_disposition,
)
from .verify_gate_parts.host_deliverable import (
    _GAME_INTERACTION_CLAIM_ID as _GAME_INTERACTION_CLAIM_ID,
)
from .verify_gate_parts.host_deliverable import (
    _GAME_INTERACTION_EXPECTED as _GAME_INTERACTION_EXPECTED,
)
from .verify_gate_parts.host_deliverable import (
    artifact_manifest_records,
    host_artifact_file_exists,
    host_unavailable_verdict,
    host_unverifiable_verdict,
    host_verify_artifact_path,
    host_verify_deliverable,
    host_verify_manifest_deliverable,
    manifest_present_for_html,
    read_workspace_bytes,
    related_game_texts_for_html,
    verdict_failures,
    verdict_first_failure,
    verdict_label,
    verifier_context_seed,
    verifier_contract_payload,
    verifier_deliverable_paths,
    verifier_medium_hint,
    with_host_verification_profile,
)
from .verify_gate_parts.probe_events import latest_event_for_action

# Compatibility re-exports. These names used to be defined directly in this
# module; the logic now lives in verify_gate_parts/ and the mixins below
# delegate to it. Several are pulled through their OWNING part module (the
# `_pt_*` aliases above) rather than imported by bare name, because their
# name is identical to a public method this module defines below (e.g. both
# a `gate_host_verify` function and a `gate_host_verify` method exist) —
# routing through the module keeps the call site unambiguous. The bindings
# below restore the ORIGINAL pre-split names because external callers
# (mainly tests) import a number of them directly from
# `disco.core.loop.finish.verify_gates` by that exact name.
_appkit_typed_gate_disposition = _pt_appkit_typed.appkit_typed_gate_disposition
_prepare_appkit_typed_authority = _pt_appkit_typed.prepare_appkit_typed_authority
_record_appkit_typed_verdict = _pt_appkit_typed.record_appkit_typed_verdict
_recorded_appkit_typed_result = _pt_appkit_typed.recorded_appkit_typed_result
_recorded_appkit_typed_status = _pt_appkit_typed.recorded_appkit_typed_status
_browser_verify_delegated_to_host_fn = _pt_browser_gate.browser_verify_delegated_to_host
_gate_browser_verify_fn = _pt_browser_gate.gate_browser_verify
_gate_verify_web_app_fn = _pt_browser_gate.gate_verify_web_app
_actionless_static_finish_fn = maybe_honest_unverifiable_static_actionless_finish
_gate_export_render_fn = _pt_export_gate.gate_export_render
_manifest_export_artifact_path_fn = _pt_export_gate.manifest_export_artifact_path
_MODEL_VERIFIER_META_KEYS = _pt_host_authority._MODEL_VERIFIER_META_KEYS
_bind_host_verification_authority = _pt_host_authority.bind_host_verification_authority
_emit_browser_unavailable_release = _pt_host_authority.emit_browser_unavailable_release
_emit_host_verdict_audit = _pt_host_authority.emit_host_verdict_audit
_emit_verifier_started_event = _pt_host_authority.emit_verifier_started_event
_prepare_typed_host_verdict = _pt_host_authority.prepare_typed_host_verdict
_typed_host_mismatch = _pt_host_authority.typed_host_mismatch
_typed_host_result = _pt_host_authority.typed_host_result
_finish_verify_passed_fn = _pt_finish_probe.finish_verify_passed
_gate_host_verify_fn = _pt_host_check.gate_host_verify
_governed_check_deliverables_fn = _pt_host_check.governed_check_deliverables
_run_host_verifier_check_fn = _pt_host_check.run_host_verifier_check
_HOST_VERDICT_SCREENSHOT_PATH_MAX_CHARS = _pt_host_claims._HOST_VERDICT_SCREENSHOT_PATH_MAX_CHARS
_SAFE_MODEL_VERIFIER_CAUSE_RE = _pt_host_claims._SAFE_MODEL_VERIFIER_CAUSE_RE
_STARTUP_DIAGNOSTIC_SECRET_RE = _pt_host_claims._STARTUP_DIAGNOSTIC_SECRET_RE
_appkit_runtime_identity = _pt_host_claims.appkit_runtime_identity
_bounded_host_verdict_screenshot_path = _pt_host_claims.bounded_host_verdict_screenshot_path
_bounded_model_verifier_cause = _pt_host_claims.bounded_model_verifier_cause
_governed_structured_browser_target = _pt_host_claims.governed_structured_browser_target
_governed_verification_contract = _pt_host_claims.governed_verification_contract
_governed_verification_required = _pt_host_claims.governed_verification_required
_handoff_clauses = _pt_host_claims.handoff_clauses
_handoff_matches_verification_contract = _pt_host_claims.handoff_matches_verification_contract
_handoff_refusal_detail = _pt_host_claims.handoff_refusal_detail
_host_verification_claims = _pt_host_claims.host_verification_claims
_latest_appkit_verification_outcome = _pt_host_claims.latest_appkit_verification_outcome
_latest_matching_appkit_start = _pt_host_claims.latest_matching_appkit_start
_preview_selection_at = _pt_host_claims.preview_selection_at
_strict_appkit_compatibility_error = _pt_host_claims.strict_appkit_compatibility_error
_strict_appkit_contract = _pt_host_claims.strict_appkit_contract
_user_verification_material = _pt_host_claims.user_verification_material
_governed_contract_refusal_fn = _pt_host_disposition.governed_contract_refusal
_governed_non_pass_disposition_fn = _pt_host_disposition.governed_non_pass_disposition
_host_verify_failure_disposition_fn = _pt_host_disposition.host_verify_failure_disposition
_run_finish_verify_gates_fn = _pt_render_sequence.run_finish_verify_gates
_finish_verification_medium = _pt_semantic_claims.finish_verification_medium
_judge_semantic_claims = _pt_semantic_claims.judge_semantic_claims
_verification_failure_details = _pt_semantic_claims.verification_failure_details
_gate_workflow_output_contract_fn = _pt_workflow_gate.gate_workflow_output_contract
_workflow_output_path_exists_fn = _pt_workflow_gate.workflow_output_path_exists
_WorkflowPathParams = _pt_workflow_paths._WorkflowPathParams
_render_workflow_output_path = _pt_workflow_paths.render_workflow_output_path


class _FinishVerifyService(_FinishGateComponent):
    async def finish_verify_passed(self, command: str) -> tuple[bool, bool]:
        """Run the agent's stated acceptance check before allowing `finish`
        (verify-on-finish post-condition gate). The agent attaches a shell
        command to finish whose exit 0 means the deliverable is good; we run it,
        VISIBLE in the trace, and on failure REFUSE the finish so the agent fixes
        the real problem instead of declaring a broken build complete.

        The verify command is NOT privileged: it passes the same hard-deny gate
        AND the same confirmation policy as any action. A command that would
        normally require confirmation is refused here (we don't silently run a
        gated command as a 'verification') — the agent is told to run it as an
        ordinary, gated action first. Ordinary test/build/lint checks assess as
        MEDIUM and run unimpeded. Returns (passed, malformed): `passed` is True
        iff the check ran and passed; `malformed` is True iff the verify command
        itself is broken (command-not-found / SyntaxError) rather than the task."""
        return await _finish_verify_passed_fn(self, command)

    async def _drive_finish_browser_probe(self, target_url: str | None = None) -> bool:
        """ACTIVE finish-verify: instead of TRUSTING the agent to have browsed the
        deliverable, the gate DRIVES a `browser navigate <preview>` itself and
        judges the result on ground truth. The preview platform assigns a RANDOM
        port, so the gate navigates to the RESOLVED preview (`target_url`, from
        `_detect_preview_url` — backend-aware, never the agent-server's :8000 on the
        shared-host backend); only when no preview is detectable does it fall back
        to the legacy `http://127.0.0.1:8000/` (the isolated-backend app port). This
        closes the
        verification-overclaim hole: an agent that declares done without ever
        looking can no longer land a JS-broken/blank page as FINISHED — the gate
        looks for it.

        Returns True iff the probe RAN and produced a usable browser observation
        (ObservationEvent.tool_result.success). The caller then re-judges via
        `_browser_verified` (console errors) + `_browser_content_meaningful`
        (blank render). Returns False (no-op) when the browser backend is
        unavailable (the process backend ships no `browser` tool) or the probe is
        hard-denied — so the gate degrades to its prior passive nudge/release
        behavior on browserless backends rather than hanging or false-refusing.

        The probe ActionEvent is tagged `meta={"verify_probe": True}` so signals.py
        keeps it OUT of agent-work accounting (actions_since_last_resume /
        productive_actions_since_approval) — the same marker finish_verify_passed
        uses. Running the gate's own probe is never evidence the AGENT did work."""
        # Browserless backend? (process backend exposes no `browser` tool) → no-op,
        # let the gate fall back to today's passive behavior.
        try:
            tool_names = {getattr(t, "name", None) for t in self._loop.executor.available_tools()}
        except Exception:  # noqa: BLE001 — any introspection failure → degrade safely
            tool_names = set()
        if "browser" not in tool_names:
            return False

        nav_url = target_url or "http://127.0.0.1:8000/"
        call = ToolCall(
            tool_name="browser",
            arguments={"action": "navigate", "url": nav_url},
        )
        action = ActionEvent(
            thought=f"Verifying the app renders: navigating to {nav_url}",
            tool_call=call,
            meta={"verify_probe": True},
        )
        # A hard-denied probe (should never happen for a browser navigate, but the
        # contract surface is shared) is a no-op → degrade to passive.
        if signals.hard_deny_reason(action) is not None:
            return False

        action = cast(ActionEvent, await self._loop._emit(action))
        await self._loop._execute_and_observe(action)
        obs = await latest_event_for_action(self._loop, action.id)
        return isinstance(obs, ObservationEvent) and obs.tool_result.success

    def _available_tool_names(self) -> set[str | None]:
        try:
            return {getattr(t, "name", None) for t in self._loop.executor.available_tools()}
        except Exception:  # noqa: BLE001 — introspection failure → degrade safely
            return set()

    def _verify_tool_available(self) -> bool:
        """W-45 — is a structured app verifier in the execution set?"""
        return self._active_verify_tool() is not None

    def _active_verify_tool(self) -> str | None:
        """Return the structured verifier active for this executor.

        Strict AppKit mode advertises `verify_appkit_app` from the AppKit-only
        allowlist. Prefer it; otherwise fall back to normal Build's `verify_web_app`.
        """
        tool_names = self._available_tool_names()
        if "verify_appkit_app" in tool_names:
            return "verify_appkit_app"
        if "verify_web_app" in tool_names:
            return "verify_web_app"
        return None


class _HostVerifyBaseService(_FinishGateComponent):
    def _active_verify_tool(self) -> str | None:
        return self._coordinator._active_verify_tool()

    def _contract_required_deliverable_paths(self) -> list[str]:
        return self._coordinator._content._contract_required_deliverable_paths()

    async def _record_verifier_failure_to_context(self, **kwargs):
        return await self._coordinator._record_verifier_failure_to_context(**kwargs)

    async def _host_artifact_file_exists(self, path: str) -> bool | None:
        return await host_artifact_file_exists(self, path)

    async def _artifact_manifest_records(
        self, events: list[Event], *, consumer: str
    ) -> tuple[Any, ...] | None:
        """Read REL-2a artifact manifest records for promoted readers.

        Empty manifests are NOT agreement: the REL-1e flip exposed how easy it is
        to bank a shadow claim when no shadow data was actually recorded. This
        helper only returns records when the manifest has at least one path, and
        always logs the event-projection comparison while the reader soaks.
        """
        return await artifact_manifest_records(self, events, consumer=consumer)

    async def _host_verify_artifact_path(self, raw_path: str) -> str | None:
        return await host_verify_artifact_path(self, raw_path)

    async def _host_verify_manifest_deliverable(
        self,
        step: AgentStep,
        events: list[Event],
        *,
        include_unverifiable: bool,
    ) -> HostVerificationDeliverable | None:
        return await host_verify_manifest_deliverable(
            self, step, events, include_unverifiable=include_unverifiable
        )

    def _host_verify_authoritative(self) -> bool:
        return bool(getattr(self._loop, "_host_verify_authoritative", False))

    async def _host_verify_deliverable(
        self,
        step: AgentStep,
        events: list[Event],
        *,
        include_unverifiable: bool = False,
    ) -> HostVerificationDeliverable | None:
        """REL-1c — reconstruct the web-like deliverable for host shadow verify.

        The host verifier is advisory in this PR, so missing/ambiguous delivery
        evidence simply skips the shadow path. A first-class app handoff wins;
        otherwise the existing web-finish convention (index.html/server_status) is
        treated as index.html. App-root handoffs such as "." or "dist" are
        resolved to their primary index.html-style file, never verified as the
        directory path itself.
        """
        return await host_verify_deliverable(
            self, step, events, include_unverifiable=include_unverifiable
        )

    async def _with_host_verification_profile(
        self,
        deliverable: HostVerificationDeliverable,
        events: list[Event],
    ) -> HostVerificationDeliverable:
        """Lower deterministic target-medium evidence into exact host claims."""
        return await with_host_verification_profile(self, deliverable, events)

    @staticmethod
    def _verdict_label(verdict: dict | None) -> str | None:
        return verdict_label(verdict)

    @staticmethod
    def _verdict_failures(verdict: dict) -> list[dict[str, object]]:
        return verdict_failures(verdict)

    @staticmethod
    def _host_unavailable_verdict(
        deliverable: HostVerificationDeliverable, detail: str
    ) -> dict[str, object]:
        return host_unavailable_verdict(deliverable, detail)

    @staticmethod
    def _host_unverifiable_verdict(
        deliverable: HostVerificationDeliverable,
    ) -> dict[str, object]:
        return host_unverifiable_verdict(deliverable)

    @staticmethod
    def _verdict_first_failure(verdict: dict) -> str:
        return verdict_first_failure(verdict)

    def _verifier_contract_payload(self) -> dict[str, Any]:
        return verifier_contract_payload(self)

    async def _verifier_deliverable_paths(
        self,
        deliverable: HostVerificationDeliverable,
        events: list[Event],
    ) -> list[str]:
        return await verifier_deliverable_paths(self, deliverable, events)

    async def _read_workspace_bytes(self, path: str) -> bytes | None:
        return await read_workspace_bytes(self, path)

    async def _manifest_present_for_html(
        self, html_path: str, html_text: str, deliverable_paths: list[str]
    ) -> bool:
        return await manifest_present_for_html(self, html_path, html_text, deliverable_paths)

    async def _related_game_texts_for_html(self, html_path: str, html_text: str) -> list[str]:
        return await related_game_texts_for_html(self, html_path, html_text)

    async def _verifier_medium_hint(
        self,
        deliverable_paths: list[str],
    ) -> VerifierMediumHint | None:
        return await verifier_medium_hint(self, deliverable_paths)

    async def _verifier_context_seed(
        self,
        deliverable: HostVerificationDeliverable,
        events: list[Event],
        check_verdict: dict[str, Any],
        *,
        contract: dict[str, Any],
        claims: tuple[HostVerificationClaim, ...] | None = None,
    ) -> VerifierContextSeed:
        return await verifier_context_seed(
            self, deliverable, events, check_verdict, contract=contract, claims=claims
        )

    async def _model_judged_verdict(
        self,
        deliverable: HostVerificationDeliverable,
        events: list[Event],
        host_verdict: dict[str, Any],
    ) -> dict[str, Any]:
        return await _judge_semantic_claims(self, deliverable, events, host_verdict)


class _HostVerifyGateService(_HostVerifyBaseService):
    async def _host_verify_failure_disposition(
        self, deliverable: HostVerificationDeliverable, verdict: dict
    ) -> Disp:
        return await _host_verify_failure_disposition_fn(self, deliverable, verdict)

    async def _governed_non_pass_disposition(
        self,
        deliverable: HostVerificationDeliverable,
        verdict: dict,
        typed_result: HostVerificationResult | None,
        events: list[Event],
    ) -> Disp:
        """Refuse governed completion without a current, complete typed PASS.

        Recovery is progress-sensitive rather than retry-counted.  The first
        failure returns exact guidance and stamps its stable fingerprint.  The
        same failure repeated with no intervening authority change lands STUCK;
        any productive mutation, Preview lifecycle change, handoff, admission,
        or new user instruction moves the authority floor past that marker and
        permits another honest verification attempt.
        """
        return await _governed_non_pass_disposition_fn(
            self, deliverable, verdict, typed_result, events
        )

    async def _governed_contract_refusal(
        self,
        events: list[Event],
        *,
        failure_key: str,
        guidance: str,
    ) -> Disp:
        """Progress-sensitive fail-closed refusal without a retry release cap."""
        return await _governed_contract_refusal_fn(
            self, events, failure_key=failure_key, guidance=guidance
        )

    async def _governed_check_deliverables(
        self,
        deliverable: HostVerificationDeliverable,
        events: list[Event],
        *,
        skip_check_ids: frozenset[str] = frozenset(),
        preverified_claim_ids: dict[str, frozenset[str]] | None = None,
    ) -> tuple[tuple[HostVerificationDeliverable, ...], str | None]:
        return await _governed_check_deliverables_fn(
            self,
            deliverable,
            events,
            skip_check_ids=skip_check_ids,
            preverified_claim_ids=preverified_claim_ids,
        )

    async def _run_host_verifier_check(
        self,
        deliverable: HostVerificationDeliverable,
        events: list[Event],
        *,
        host_verifier: Any,
        authoritative: bool,
        governed_target: bool,
        forced_unavailable: str | None = None,
    ) -> tuple[dict[str, Any], HostVerificationResult | None, bool, str | None]:
        return await _run_host_verifier_check_fn(
            self,
            deliverable,
            events,
            host_verifier=host_verifier,
            authoritative=authoritative,
            governed_target=governed_target,
            forced_unavailable=forced_unavailable,
        )

    async def gate_host_verify(
        self,
        step: AgentStep,
        events: list[Event],
        *,
        preverified: tuple[tuple[HostVerificationDeliverable, HostVerificationResult], ...] = (),
    ) -> Disp:
        """Run every admitted verifier check; legacy web remains a compatibility path."""
        return await _gate_host_verify_fn(self, step, events, preverified=preverified)


class _BrowserVerifyGateService(_FinishGateComponent):
    async def _detect_preview_url(self) -> str | None:
        """P1-1 — detect the URL the live deliverable currently serves on, using the
        SAME backend-aware resolver the `verify_web_app` tool uses
        (`preview_target.resolve_preview_port`). Duck-typed over the executor's
        sandbox (`exec_shell`): core never imports `tools`, so the gate runs the
        shared in-sandbox ownership probe and feeds the result to the shared
        resolver.

        On a SHARED-host backend (process/local — `sandbox.workspace_path` is set)
        the resolver NEVER returns a reserved control port (8000 = the agent-server):
        it prefers a CONVERSATION-OWNED served port, else any non-reserved owned
        port, else None — so a stale verdict about `:8000` (the agent-server, Bug 7)
        can never bind the finish gate. On an ISOLATED backend (gVisor/Podman —
        `workspace_path` is None) `:8000` IS the app, so the legacy first-reachable
        socket probe is kept.

        Returns `http://127.0.0.1:<port>/` or None (no sandbox / no exec_shell /
        nothing detected), in which case binding is NOT enforced (see
        `_verdict_targets_preview`) and the gate drives a fresh verify against the
        real preview. Never raises (a detection failure must not wedge the gate)."""
        return await detect_preview_url(self)

    async def _drive_verify_web_app(
        self,
        target_url: str | None = None,
        tool_name: str = "verify_web_app",
        *,
        medium: str = "web",
    ) -> bool:
        """W-45 ACTIVE verify: DRIVE one structured verifier call when the agent
        declares done without a fresh verdict. Mirrors `_drive_finish_browser_probe`
        — the probe ActionEvent is tagged `verify_probe` so it never counts as
        agent work. Returns True iff the call produced a usable observation.

        When the gate has already resolved the real preview target (`target_url`,
        backend-aware — never the agent-server's 8000 on the process backend), it is
        passed EXPLICITLY so the tool verifies the build's actual served port instead
        of repeating its own auto-detect (Bug 7). None ⇒ `{}` ⇒ the tool auto-detects
        (which itself uses the same backend-aware resolver)."""
        return await drive_verify_web_app(self, target_url, tool_name, medium=medium)

    async def _gate_verify_web_app(
        self,
        events: list[Event],
        tool_name: str = "verify_web_app",
        *,
        step: AgentStep | None = None,
        appkit_prepared: (tuple[VerifierStartedEvent, HostVerificationDeliverable] | None) = None,
    ) -> Disp:
        """W-45 — verdict-consuming finish gate replacing `_browser_verified()` for web
        builds with "the latest structured verifier verdict since the last productive
        edit". At finish it drives once: pass → finish; fail → surface the verdict's
        evidence and next action, then CONTINUE.

        LOOP BREAKER: the verdict is cached by `_last_productive_seq` — a browser/
        navigate/verify probe is NOT a productive edit, so re-verifying without an
        edit reads the SAME cached verdict (no real re-run). When the SAME
        `failure_fingerprint` recurs without a productive edit that changes the
        served output, the gate marks the run STUCK instead of reloading 25-40×.
        The 3-refusal release no longer
        silently converts repeated failed verification into 'done' — it finishes
        ONLY with an explicit blocked/incomplete summary."""
        return await _gate_verify_web_app_fn(
            self._coordinator,
            events,
            tool_name,
            step=step,
            appkit_prepared=appkit_prepared,
        )

    def _browser_verification_unavailable(self) -> bool:
        """True when this backend cannot run browser-based verification (the process
        backend ships no `browser` tool). Mirrors `_drive_finish_browser_probe`'s
        availability check — the precise "the render/console check cannot run here"
        signal that distinguishes an UNVERIFIABLE delivery from a BROKEN app."""
        return browser_verification_unavailable(self)

    async def _static_deliverable_present(self) -> bool:
        """True when the static web deliverable (index.html) exists on disk in the
        sandbox workspace — the file-truth half of the honest unverifiable finish (a
        zero-action run wrote nothing, so this is False and it cannot finish)."""
        return await static_deliverable_present(self)

    async def _record_verifier_failure_to_context(
        self, *, message: str, rel_path: str | None
    ) -> None:
        """CXT-7 — persist a verify_web_app failure to .disco/context/verifier_failures.json
        (best-effort; never alters the gate flow). Survives truncation/resume so the
        CXT-4 assembler can surface the unresolved failure to the model later."""
        await record_verifier_failure_to_context(self, message=message, rel_path=rel_path)

    async def _maybe_honest_unverifiable_static_finish(self, verdict: dict) -> Disp | None:
        """Bug 6 — when a web build's verify FAILS ONLY because nothing is serving
        (no console/network errors, no blank-render judgement: the browser never ran)
        AND this backend cannot run a headless browser AND index.html exists on disk,
        return FALLTHROUGH with an explicit honest-unverifiable marker so a delivered
        static build FINISHES instead of pausing/STUCKing. Returns None (let the
        normal refuse/loop-break path run) for every other case — a real fail
        (console/network/blank) or a missing deliverable is NEVER converted to a
        success (W-45 preserved)."""
        return await maybe_honest_unverifiable_static_finish(self, verdict)

    async def maybe_honest_unverifiable_static_actionless_finish(self, events: list[Event]) -> bool:
        """Bug 6 — the ACTIONLESS-VALVE twin of `_maybe_honest_unverifiable_static_finish`.

        The finish-gate honest path only runs when the model REACHES the finish gate.
        On a browserless backend with a final browser-verify plan step the model never
        does — it churns on the unsatisfiable step and the actionless valve would PAUSE
        a substantively-complete build. This applies the SAME honest-finish concept at
        the valve: when (and ONLY when) the conservative conditions below all hold, emit
        the honest marker + a clean terminal FINISHED and return True; otherwise return
        False so the valve keeps its existing pause/stuck behavior.

        Conservative conditions (ALL required — any failure ⇒ False ⇒ no honest finish):
          1. a plan exists, is INCOMPLETE, and EVERY not-done step is verify-only;
          2. real productive work happened since approval (APPROVE_PLAN_NO_EXECUTION —
             a zero-action run can never honest-finish here);
          3. the static deliverable (index.html) exists on disk;
          4. a NON-browser validation PASSED after the last write/edit (a failed or
             absent validation blocks);
          5. browser verification is GENUINELY unavailable (no browser tool, OR a
             browser observation/error carried the unavailable signal);
          6. NO real web-failure evidence (console/network errors or a served-but-blank
             render) — W-45: a genuinely BROKEN app is never converted to a success.
        """
        return await _actionless_static_finish_fn(self, events)

    async def _verifier_unavailable_disposition(self, tool_name: str = "verify_web_app") -> Disp:
        """P1-2 — disposition when the structured verifier is advertised but produced NO
        usable verdict (verifier execution error / empty / the driven verify
        failed). On the build/web surface a clean FINISH requires a real PASS
        verdict, so this must NOT fall through to finalization (the W-32 regression
        codex found). Refuse-and-continue with a "verification could not run"
        reminder while under the cap; at the cap, release EXPLICITLY as unverified
        (distinct status marker + visible message) rather than a silent clean
        finish. Bounded by the shared `_browser_verify_refusals` cap so a verifier
        that can never run still terminates."""
        return await verifier_unavailable_disposition(self, tool_name)

    async def _browser_verify_delegated_to_host(self, step: AgentStep, events: list[Event]) -> bool:
        return await _browser_verify_delegated_to_host_fn(self, step, events)

    async def gate_browser_verify(
        self,
        step: AgentStep,
        events: list[Event],
        *,
        appkit_prepared: (tuple[VerifierStartedEvent, HostVerificationDeliverable] | None) = None,
    ) -> Disp:
        # BROWSER-VERIFY GATE — §BP-05. If web deliverable holds, refuse finish
        # until a clean browser observation (zero console errors) exists
        # since the last state-changing edit.
        return await _gate_browser_verify_fn(
            self._coordinator, step, events, appkit_prepared=appkit_prepared
        )


class _ExportRenderGateService(_FinishGateComponent):
    async def _manifest_export_artifact_path(
        self, events: list[Event], *, require_shown: bool
    ) -> str | None:
        return await _manifest_export_artifact_path_fn(
            self._coordinator, events, require_shown=require_shown
        )

    async def gate_export_render(self, step: AgentStep, events: list[Event]) -> Disp:
        """[P10] Refuse FINISHED when a rendered export (deck/document) is BLANK,
        TRUNCATED, or CORRUPT — the "looks done but the file is empty" false
        completeness. The real executor for the inert ``ExportContract.validate``
        stage.

        Reads the render facts the producer stamped from the ACTUAL output bytes
        (``latest_export_render_facts``), NOT the model's declared slide_count. A
        broken export re-enters the loop with a concrete steer; a good one (or none
        produced) falls through. Bounded by ``EXPORT_GATE_MAX_REFUSALS`` so a
        genuinely-broken renderer can't trap the run — it releases with a loud
        UNVERIFIED warning, exactly like the browser-verify valve. Decision/message
        logic is pure (``contract.export_render``); this method only emits."""
        return await _gate_export_render_fn(self._coordinator, step, events)


class _RenderVerifyGateService(_FinishGateComponent):
    async def _workflow_output_path_exists(self, path: str) -> tuple[bool, str]:
        return await _workflow_output_path_exists_fn(self, path)

    async def gate_workflow_output_contract(
        self,
        step: AgentStep,
        events: list[Event],
    ) -> Disp:
        return await _gate_workflow_output_contract_fn(self, step, events)

    async def run_finish_verify_gates(self, step: AgentStep, events: list[Event]) -> Disp:
        """The render-verify gate sequence shared by EVERY finish path: host+browser
        app-verify (order depends on the authoritative flag) THEN the P10 export-render
        gate for file deliverables.

        Extracted so ``handle_finish_path`` (affirmative ``finish()``) and
        ``_completed_via_notify_finish`` (the actionless/notify valve) run the SAME
        gates — they had DRIFTED: the notify path historically skipped both
        ``gate_host_verify`` (so shadow agreement was never measurable on
        notify-completed builds, and an authoritative flip would leave a verify
        bypass) AND ``gate_export_render`` (a blank deck finishing via notify escaped
        the P10 check). Returns CONTINUE (a gate refused — caller must not finish),
        HALT (a gate landed the terminal status / loop-breaker), or FALLTHROUGH (all
        render-verify gates clear)."""
        return await _run_finish_verify_gates_fn(self._coordinator.verification, step, events)
