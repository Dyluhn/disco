"""FinishGate facade and finish-path compatibility exports."""

from __future__ import annotations

from typing import TYPE_CHECKING

from .common import (
    _DICTATED_CONTENT_REFUSAL_CAP,
    _DOD_REFUSAL_CAP,
    _EXECUTION_NUDGE,
    _EXECUTION_NUDGE_CAP,
    _FINISH_VERIFY_CAP,
    _PREVIEW_PORTS,
    _app_verify_command,
    _browser_content_meaningful,
    _browser_verified,
    _DoDWorkspaceUnavailable,
    _is_web_deliverable,
    _last_productive_seq,
    _last_verification_authority_seq,
    _latest_app_deliverable_event,
    _latest_browser_error,
    _latest_browser_structured,
    _latest_verify_verdict,
    _plan_file_exists_paths,
    _preview_key,
    _prior_verify_marker_fp,
    _safe_deliverable_file_path,
    _static_verify_command,
    _url_targets_preview,
    host_verify_authoritative_enabled,
)
from .content_gates import _ContentGateService
from .finalize import _FinalizeService
from .verify_gates import (
    _BrowserVerifyGateService,
    _ExportRenderGateService,
    _FinishVerifyService,
    _HostVerifyGateService,
    _RenderVerifyGateService,
)

if TYPE_CHECKING:
    from ..loop_facade_compat import _AgentLoopCompatibility as AgentLoop

__all__ = [
    "FinishGate",
    "_DICTATED_CONTENT_REFUSAL_CAP",
    "_DOD_REFUSAL_CAP",
    "_EXECUTION_NUDGE",
    "_EXECUTION_NUDGE_CAP",
    "_FINISH_VERIFY_CAP",
    "_PREVIEW_PORTS",
    "_app_verify_command",
    "_browser_content_meaningful",
    "_browser_verified",
    "_DoDWorkspaceUnavailable",
    "_is_web_deliverable",
    "_last_productive_seq",
    "_latest_browser_error",
    "_latest_browser_structured",
    "_latest_verify_verdict",
    "_is_web_deliverable",
    "_last_productive_seq",
    "_last_verification_authority_seq",
    "_latest_app_deliverable_event",
    "_plan_file_exists_paths",
    "_safe_deliverable_file_path",
    "_preview_key",
    "_prior_verify_marker_fp",
    "_static_verify_command",
    "_url_targets_preview",
    "host_verify_authoritative_enabled",
]


class FinishGate:
    """Coordinator for the typed finish services.

    The public finish surface is deliberately declared here; policy families
    are collaborators rather than inherited mixins.
    """

    def __init__(self, loop: AgentLoop) -> None:
        self._loop = loop
        self._content = _ContentGateService(loop, self)
        self._finalize = _FinalizeService(loop, self)
        self._finish_verify = _FinishVerifyService(loop, self)
        self._host_verify = _HostVerifyGateService(loop, self)
        self._browser_verify = _BrowserVerifyGateService(loop, self)
        self._export_render = _ExportRenderGateService(loop, self)
        self._render_verify = _RenderVerifyGateService(loop, self)

    def __setattr__(self, name, value):
        object.__setattr__(self, name, value)
        if name.startswith("_") and name not in {"_loop"}:
            for service_name in (
                "_content",
                "_finalize",
                "_finish_verify",
                "_host_verify",
                "_browser_verify",
                "_export_render",
                "_render_verify",
            ):
                service = self.__dict__.get(service_name)
                if service is not None:
                    try:
                        object.__getattribute__(service, name)
                    except AttributeError:
                        continue
                    object.__setattr__(service, name, value)

    async def handle_finish_path(self, step, state, events):
        return await self._finalize.handle_finish_path(step, state, events)

    async def normalize_finish_step(self, step, events):
        return await self._finalize.normalize_finish_step(step, events)

    async def finalize_finish(self, step, state, events):
        return await self._finalize.finalize_finish(step, state, events)

    async def finish_dod_gate_passed(self):
        return await self._content.finish_dod_gate_passed()

    async def synthetic_finish_after_actionless_pauses(self, state, events):
        return await self._finalize.synthetic_finish_after_actionless_pauses(state, events)

    async def maybe_honest_unverifiable_static_actionless_finish(self, events):
        return await self._browser_verify.maybe_honest_unverifiable_static_actionless_finish(events)

    async def finish_verify_passed(self, command):
        return await self._finish_verify.finish_verify_passed(command)

    def _active_verify_tool(self):
        return self._finish_verify._active_verify_tool()

    def _verify_tool_available(self):
        return self._finish_verify._verify_tool_available()

    async def gate_execution_nudge(self, step, events):
        return await self._content.gate_execution_nudge(step, events)

    async def dictated_content_gate_passed(self, events):
        return await self._content.dictated_content_gate_passed(events)

    async def run_finish_verify_gates(self, step, events):
        return await self._render_verify.run_finish_verify_gates(step, events)

    async def seal_gate_allows_finish(self):
        return await self._finalize.seal_gate_allows_finish()

    async def _detect_preview_url(self):
        return await self._browser_verify._detect_preview_url()

    async def _drive_finish_browser_probe(self, target_url=None):
        return await self._finish_verify._drive_finish_browser_probe(target_url)

    async def _gate_verify_web_app(self, events, tool_name="verify_web_app", **kwargs):
        return await self._browser_verify._gate_verify_web_app(events, tool_name, **kwargs)

    async def _maybe_honest_unverifiable_static_finish(self, verdict):
        return await self._browser_verify._maybe_honest_unverifiable_static_finish(verdict)

    async def _record_verifier_failure_to_context(self, **kwargs):
        return await self._browser_verify._record_verifier_failure_to_context(**kwargs)

    async def _host_verify_deliverable(self, step, events, **kwargs):
        return await self._host_verify._host_verify_deliverable(step, events, **kwargs)

    async def _governed_contract_refusal(self, events, **kwargs):
        return await self._host_verify._governed_contract_refusal(events, **kwargs)

    def _verifier_contract_payload(self):
        return self._host_verify._verifier_contract_payload()

    @staticmethod
    def _verdict_label(verdict):
        return _HostVerifyGateService._verdict_label(verdict)

    @staticmethod
    def _verdict_failures(verdict):
        return _HostVerifyGateService._verdict_failures(verdict)

    @staticmethod
    def _verdict_first_failure(verdict):
        return _HostVerifyGateService._verdict_first_failure(verdict)

    def _host_unavailable_verdict(self, deliverable, detail):
        return self._host_verify._host_unavailable_verdict(deliverable, detail)

    def _host_unverifiable_verdict(self, deliverable):
        return self._host_verify._host_unverifiable_verdict(deliverable)

    async def _verifier_deliverable_paths(self, deliverable, events):
        return await self._host_verify._verifier_deliverable_paths(deliverable, events)

    async def _verifier_medium_hint(self, deliverable_paths):
        return await self._host_verify._verifier_medium_hint(deliverable_paths)

    async def _verifier_context_seed(self, deliverable, events, check_verdict, **kwargs):
        return await self._host_verify._verifier_context_seed(
            deliverable, events, check_verdict, **kwargs
        )

    async def _model_judged_verdict(self, deliverable, events, host_verdict):
        return await self._host_verify._model_judged_verdict(
            deliverable, events, host_verdict
        )

    async def _governed_check_deliverables(self, deliverable, events, **kwargs):
        return await self._host_verify._governed_check_deliverables(
            deliverable, events, **kwargs
        )

    async def _run_host_verifier_check(self, deliverable, events, **kwargs):
        return await self._host_verify._run_host_verifier_check(
            deliverable, events, **kwargs
        )

    async def _with_host_verification_profile(self, deliverable, events):
        return await self._host_verify._with_host_verification_profile(deliverable, events)

    async def build_dod_evaluator(self):
        return await self._content.build_dod_evaluator()

    async def gate_host_verify(self, step, events, **kwargs):
        return await self._host_verify.gate_host_verify(step, events, **kwargs)

    def _host_verify_authoritative(self):
        return self._host_verify._host_verify_authoritative()

    async def gate_browser_verify(self, step, events, **kwargs):
        return await self._browser_verify.gate_browser_verify(step, events, **kwargs)

    async def gate_export_render(self, step, events):
        return await self._export_render.gate_export_render(step, events)

    async def _manifest_export_artifact_path(self, events, **kwargs):
        return await self._export_render._manifest_export_artifact_path(events, **kwargs)

    async def _artifact_manifest_records(self, events, **kwargs):
        return await self._host_verify._artifact_manifest_records(events, **kwargs)

    async def gate_workflow_output_contract(self, step, events):
        return await self._render_verify.gate_workflow_output_contract(step, events)
