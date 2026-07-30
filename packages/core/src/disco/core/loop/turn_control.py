"""Compatibility facade for Loop turn-taking control."""

from __future__ import annotations

import ipaddress as ipaddress
import logging as logging
from typing import TYPE_CHECKING as TYPE_CHECKING
from typing import cast as cast
from urllib.parse import urlsplit as urlsplit

from ..effects import ActionProfile as ActionProfile
from ..effects import EffectCapability as EffectCapability
from ..events import ActionEvent as ActionEvent
from ..events import AgentErrorEvent as AgentErrorEvent
from ..events import AlternativeOption as AlternativeOption
from ..events import AlternativesEvent as AlternativesEvent
from ..events import ConversationStatus as ConversationStatus
from ..events import DeliverableEvent as DeliverableEvent
from ..events import Event as Event
from ..events import EventSource as EventSource
from ..events import KnowledgeEvent as KnowledgeEvent
from ..events import LLMMessage as LLMMessage
from ..events import MessageEvent as MessageEvent
from ..events import ObservationEvent as ObservationEvent
from ..events import PlanEvent as PlanEvent
from ..events import PlanStep as PlanStep
from ..events import StatusEvent as StatusEvent
from ..events import ToolCall as ToolCall
from ..events import ToolResult as ToolResult
from ..events import current_build_platform_admission as current_build_platform_admission
from ..events import (
    event_matches_current_workspace_intent as event_matches_current_workspace_intent,
)
from ..llm import OperatingMode as OperatingMode
from ..verification import (
    requires_structured_browser_runtime as requires_structured_browser_runtime,
)
from . import signals as signals
from . import view_render as view_render
from .actionless_control import ActionlessValveMixin as _ActionlessValveMixin
from .bootstrap import _detect_project_bootstrap as _detect_project_bootstrap
from .boundaries import AgentStep as AgentStep
from .control import Disp as Disp
from .messages import _stuck_escape_reminder as _stuck_escape_reminder
from .meta_tool_handlers import MetaToolHandlerMixin as _MetaToolHandlerMixin
from .observe import _DELEGATE_ACTION_PROFILE as _DELEGATE_ACTION_PROFILE
from .observe import _FANOUT_INPUT_MAX_CHARS as _FANOUT_INPUT_MAX_CHARS
from .phase_gates import PhaseGateMixin as _PhaseGateMixin
from .phase_gates import ProgressGateMixin as _ProgressGateMixin
from .plan_revisions import normalized_execution_contract as normalized_execution_contract
from .plan_revisions import preflight_plan_revision as preflight_plan_revision
from .plan_revisions import (
    reject_invalid_revision_conditions as reject_invalid_revision_conditions,
)
from .planning_harvest import (
    harvest_revision_plan_after_refusal as harvest_revision_plan_after_refusal,
)
from .stuck import F6_FILE_MUTATING_TOOLS as F6_FILE_MUTATING_TOOLS
from .stuck import VerifierEvidenceInvalid as VerifierEvidenceInvalid
from .stuck import VerifierFailureNoProgress as VerifierFailureNoProgress
from .stuck import no_progress_detected as no_progress_detected
from .stuck import (
    repeated_failed_verifier_no_progress as repeated_failed_verifier_no_progress,
)
from .tool_specs import _ask_user_tool_singleton as _ask_user_tool_singleton
from .turn_control_support import (
    _ACTIONLESS_AUTO_RESUME_MARKER as _ACTIONLESS_AUTO_RESUME_MARKER,
)
from .turn_control_support import (
    _ACTIONLESS_AUTO_RESUME_SEGMENT_CAP as _ACTIONLESS_AUTO_RESUME_SEGMENT_CAP,
)
from .turn_control_support import (
    _BLOCKED_DETAIL_PREFIX as _BLOCKED_DETAIL_PREFIX,
)
from .turn_control_support import (
    _BLOCKED_LANDING_META_KEY as _BLOCKED_LANDING_META_KEY,
)
from .turn_control_support import (
    _BOOKKEEPING_PLAN_SLACK as _BOOKKEEPING_PLAN_SLACK,
)
from .turn_control_support import (
    _BOOKKEEPING_STREAK_HALT_AT as _BOOKKEEPING_STREAK_HALT_AT,
)
from .turn_control_support import (
    _BOOKKEEPING_STREAK_NUDGE_AT as _BOOKKEEPING_STREAK_NUDGE_AT,
)
from .turn_control_support import (
    _CONTINUE_OPTION_ID as _CONTINUE_OPTION_ID,
)
from .turn_control_support import (
    _IDENTICAL_PLAN_NUDGE_DIAGNOSTIC as _IDENTICAL_PLAN_NUDGE_DIAGNOSTIC,
)
from .turn_control_support import (
    _IDENTICAL_PLAN_NUDGE_TEXT as _IDENTICAL_PLAN_NUDGE_TEXT,
)
from .turn_control_support import (
    _NO_PROGRESS_REMINDER as _NO_PROGRESS_REMINDER,
)
from .turn_control_support import (
    _PROPOSE_PLAN_UPDATE_REPEAT_CAP as _PROPOSE_PLAN_UPDATE_REPEAT_CAP,
)
from .turn_control_support import (
    _QUESTIONS_V2_REQUIRED_OPTIONS as _QUESTIONS_V2_REQUIRED_OPTIONS,
)
from .turn_control_support import (
    _SERVE_DUPLICATE_DIAGNOSTIC as _SERVE_DUPLICATE_DIAGNOSTIC,
)
from .turn_control_support import (
    _SERVE_DUPLICATE_GUIDANCE as _SERVE_DUPLICATE_GUIDANCE,
)
from .turn_control_support import (
    _SERVE_HANDOFF_DIAGNOSTIC as _SERVE_HANDOFF_DIAGNOSTIC,
)
from .turn_control_support import (
    _SERVE_HANDOFF_GUIDANCE as _SERVE_HANDOFF_GUIDANCE,
)
from .turn_control_support import (
    _SERVE_TARGET_SHAPE_DIAGNOSTIC as _SERVE_TARGET_SHAPE_DIAGNOSTIC,
)
from .turn_control_support import (
    _SERVE_TARGET_SHAPE_GUIDANCE as _SERVE_TARGET_SHAPE_GUIDANCE,
)
from .turn_control_support import (
    _STUCK_ESCAPE_BLOCKED_TOOLS_BY_REASON as _STUCK_ESCAPE_BLOCKED_TOOLS_BY_REASON,
)
from .turn_control_support import (
    _VERIFIER_EVIDENCE_INVALID_DETAIL as _VERIFIER_EVIDENCE_INVALID_DETAIL,
)
from .turn_control_support import (
    _VERIFIER_NO_PROGRESS_DETAIL as _VERIFIER_NO_PROGRESS_DETAIL,
)
from .turn_control_support import (
    _canonical_deployment_url as _canonical_deployment_url,
)
from .turn_control_support import (
    _coerce_choice_label as _coerce_choice_label,
)
from .turn_control_support import (
    _coerce_serve_entry_path as _coerce_serve_entry_path,
)
from .turn_control_support import (
    _ensure_question as _ensure_question,
)
from .turn_control_support import (
    _handle_serve as _handle_serve,
)
from .turn_control_support import (
    _last_verify_web_app_passed as _last_verify_web_app_passed,
)
from .turn_control_support import (
    _no_progress_finish_hinted as _no_progress_finish_hinted,
)
from .turn_control_support import (
    _no_progress_marker_seq as _no_progress_marker_seq,
)
from .turn_control_support import (
    _normalize_clarify_options as _normalize_clarify_options,
)
from .turn_control_support import (
    _normalize_questions_v2_options as _normalize_questions_v2_options,
)
from .turn_control_support import (
    _normalize_serve_path as _normalize_serve_path,
)
from .turn_control_support import (
    _plan_done_and_verified as _plan_done_and_verified,
)
from .turn_control_support import (
    _prior_plan_and_productive_action_between as _prior_plan_and_productive_action_between,
)
from .turn_control_support import (
    _questions_v2_used_since_last_plan as _questions_v2_used_since_last_plan,
)
from .turn_control_support import (
    _rewrite_directive_marker_active as _rewrite_directive_marker_active,
)
from .turn_control_support import (
    _serve_duplicate_guidance as _serve_duplicate_guidance,
)
from .turn_control_support import (
    _serve_path_is_workspace_root as _serve_path_is_workspace_root,
)
from .turn_control_support import (
    _serve_path_missing as _serve_path_missing,
)
from .turn_control_support import (
    _serve_path_verified_present as _serve_path_verified_present,
)
from .turn_control_support import (
    _serve_root_refusal as _serve_root_refusal,
)
from .turn_control_support import (
    _verifier_no_progress_marker_seq as _verifier_no_progress_marker_seq,
)
from .valve_landing import ValveLandingMixin as _ValveLandingMixin

if TYPE_CHECKING:
    from .loop_facade_compat import _AgentLoopCompatibility as AgentLoop

_LOG = logging.getLogger("disco.loop")


class Valve(
    _ValveLandingMixin,
    _ActionlessValveMixin,
    _PhaseGateMixin,
    _ProgressGateMixin,
):
    """Compatibility owner for the ordered Loop valve surface."""

    def __init__(self, loop: AgentLoop) -> None:
        self._loop = loop
        self._post_noop_active = False

    async def gate_f4_bootstrap(self, events: list[Event]) -> list[Event]:
        """Compatibility wrapper for the non-disposition event refresh."""
        return await self.refresh_f4_bootstrap(events)


class MetaToolHandlers(_MetaToolHandlerMixin):
    """Compatibility owner for the Loop virtual-tool handler surface."""

    def __init__(self, loop: AgentLoop) -> None:
        self._loop = loop
