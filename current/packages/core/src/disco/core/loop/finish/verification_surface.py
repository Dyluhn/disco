"""Typed verification surface exposed by the finish coordinator."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from . import FinishGate


class FinishVerificationSurface:
    """The finish operations used by verification-facing loop consumers.

    This is a deliberately typed capability object, not a second finish facade:
    policy remains owned by the services held by ``FinishGate`` while callers
    receive only the verification surface they need.
    """

    def __init__(self, gate: FinishGate) -> None:
        self._gate = gate
        self._loop = gate._loop

    async def finish_dod_gate_passed(self):
        return await self._gate._content.finish_dod_gate_passed()

    async def maybe_honest_unverifiable_static_actionless_finish(self, events):
        return await self._gate._browser_verify.maybe_honest_unverifiable_static_actionless_finish(
            events
        )

    async def finish_verify_passed(self, command):
        return await self._gate._finish_verify.finish_verify_passed(command)

    async def run_finish_verify_gates(self, step, events):
        return await self._gate._render_verify.run_finish_verify_gates(step, events)

    async def seal_gate_allows_finish(self):
        return await self._gate._finalize.seal_gate_allows_finish()

    async def build_dod_evaluator(self):
        return await self._gate._content.build_dod_evaluator()

    async def gate_host_verify(self, step, events, **kwargs):
        return await self._gate._host_verify.gate_host_verify(step, events, **kwargs)

    async def gate_browser_verify(self, step, events, **kwargs):
        return await self._gate._browser_verify.gate_browser_verify(step, events, **kwargs)

    async def gate_export_render(self, step, events):
        return await self._gate._export_render.gate_export_render(step, events)

    async def gate_workflow_output_contract(self, step, events):
        return await self._gate._render_verify.gate_workflow_output_contract(step, events)

    # The render sequence runs against this surface so its internal gate calls
    # cannot reach through the owning FinishGate facade.
    def _active_verify_tool(self):
        return self._gate._finish_verify._active_verify_tool()

    def _verify_tool_available(self):
        return self._gate._finish_verify._verify_tool_available()

    async def _detect_preview_url(self):
        return await self._gate._browser_verify._detect_preview_url()

    async def _drive_finish_browser_probe(self, target_url=None):
        return await self._gate._finish_verify._drive_finish_browser_probe(target_url)

    async def _gate_verify_web_app(self, events, tool_name="verify_web_app", **kwargs):
        return await self._gate._browser_verify._gate_verify_web_app(events, tool_name, **kwargs)

    async def _maybe_honest_unverifiable_static_finish(self, verdict):
        return await self._gate._browser_verify._maybe_honest_unverifiable_static_finish(verdict)

    async def _record_verifier_failure_to_context(self, **kwargs):
        return await self._gate._browser_verify._record_verifier_failure_to_context(**kwargs)

    async def _host_verify_deliverable(self, step, events, **kwargs):
        return await self._gate._host_verify._host_verify_deliverable(step, events, **kwargs)

    async def _governed_contract_refusal(self, events, **kwargs):
        return await self._gate._host_verify._governed_contract_refusal(events, **kwargs)

    def _verifier_contract_payload(self):
        return self._gate._host_verify._verifier_contract_payload()

    @staticmethod
    def _verdict_label(verdict):
        from .verify_gates import _HostVerifyGateService

        return _HostVerifyGateService._verdict_label(verdict)

    @staticmethod
    def _verdict_failures(verdict):
        from .verify_gates import _HostVerifyGateService

        return _HostVerifyGateService._verdict_failures(verdict)

    @staticmethod
    def _verdict_first_failure(verdict):
        from .verify_gates import _HostVerifyGateService

        return _HostVerifyGateService._verdict_first_failure(verdict)

    def _host_unavailable_verdict(self, deliverable, detail):
        return self._gate._host_verify._host_unavailable_verdict(deliverable, detail)

    def _host_unverifiable_verdict(self, deliverable):
        return self._gate._host_verify._host_unverifiable_verdict(deliverable)

    async def _verifier_deliverable_paths(self, deliverable, events):
        return await self._gate._host_verify._verifier_deliverable_paths(deliverable, events)

    async def _verifier_medium_hint(self, deliverable_paths):
        return await self._gate._host_verify._verifier_medium_hint(deliverable_paths)

    async def _verifier_context_seed(self, deliverable, events, check_verdict, **kwargs):
        return await self._gate._host_verify._verifier_context_seed(
            deliverable, events, check_verdict, **kwargs
        )

    async def _model_judged_verdict(self, deliverable, events, host_verdict):
        return await self._gate._host_verify._model_judged_verdict(
            deliverable, events, host_verdict
        )

    async def _governed_check_deliverables(self, deliverable, events, **kwargs):
        return await self._gate._host_verify._governed_check_deliverables(
            deliverable, events, **kwargs
        )

    async def _run_host_verifier_check(self, deliverable, events, **kwargs):
        return await self._gate._host_verify._run_host_verifier_check(
            deliverable, events, **kwargs
        )

    async def _with_host_verification_profile(self, deliverable, events):
        return await self._gate._host_verify._with_host_verification_profile(deliverable, events)

    async def _artifact_manifest_records(self, events, **kwargs):
        return await self._gate._host_verify._artifact_manifest_records(events, **kwargs)

    def _host_verify_authoritative(self):
        return self._gate._host_verify._host_verify_authoritative()
