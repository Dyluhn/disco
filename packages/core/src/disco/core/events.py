"""The Event type hierarchy — event-state-contract.md §2.

Events are the atomic unit of all conversation and agent state. The log of
events is append-only and the single source of truth (BoD Principle 3); State
(state.py) and the LLM View (view.py) are *pure functions* of the ordered log.

Field names, types, and method signatures here are **normative** (the contract
marks them [CONTRACT]); other subsystems deserialize and compare these shapes
without further coordination. Method *bodies* are implementation.

This module is the compatibility facade and sole owner of the ordered ``Event``
union, the ``EventAdapter`` serializer, and the JSON helpers.  Concrete event
models live in the sibling ``_event_types``, ``_event_interaction``,
``_event_control``, ``_event_outputs``, and ``_event_audit`` modules.  Render
helpers live in ``_event_render``.  Platform and workspace folds live in
``_event_folds_platform`` and ``_event_folds_workspace``.
"""

# Compatibility re-exports are the facade's contract.
# ruff: noqa: F401

from __future__ import annotations

import json
import re
import uuid
from collections.abc import Iterable
from contextvars import ContextVar
from datetime import UTC, datetime
from enum import Enum
from typing import Annotated, Any, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    TypeAdapter,
    field_validator,
    model_validator,
)

from . import _event_folds_platform as _platform_folds
from . import _event_folds_workspace as _workspace_folds

# ---- audit events -----------------------------------------------------------
from ._event_audit import (
    ClarifyEvent,
    ContextResolvedEvent,
    ContextSummaryEvent,
    QuestionsV2Event,
    ScheduleEvent,
    ScheduleRunEvent,
    VerifierShadowEvent,
    VerifierStartedEvent,
    VerifierVerdictEvent,
)

# ---- control events --------------------------------------------------------
from ._event_control import (
    AppKitEjectionEvent,
    BuildPlatformAdmissionEvent,
    StatusEvent,
    WorkspaceMutationEvent,
    WorkspaceRestoredEvent,
    WorkspaceVersionEvent,
)

# ---- folds ------------------------------------------------------------------
from ._event_folds_platform import (
    active_verification_requirements_event,
    current_appkit_ejection,
    current_build_platform_admission,
)
from ._event_folds_workspace import (
    AgentViewProjection,
    _strict_workspace_actions_are_closed,
    _workspace_run_intent_state,
    agent_view_consistent_events,
    current_workspace_agent_view_id,
    current_workspace_agent_view_seq,
    derive_final_workspace_fence,
    event_matches_current_workspace_intent,
    event_matches_current_workspace_view,
    latest_workspace_run_intent,
    pending_workspace_run_intent,
    workspace_run_intent_admission_required,
    workspace_terminal_matches_current_run,
)

# ---- interaction events -----------------------------------------------------
from ._event_interaction import (
    ActionEvent,
    AgentErrorEvent,
    CondensationEvent,
    ErrorEvent,
    MessageEvent,
    ObservationEvent,
)

# ---- output events ----------------------------------------------------------
from ._event_outputs import (
    AlternativesEvent,
    DatasourceEvent,
    DeliverableEvent,
    KnowledgeEvent,
    PlanEvent,
    ReportEvent,
    RuntimeConstraintEvent,
)

# ---- render helpers ---------------------------------------------------------
from ._event_render import (
    _ARG_SNIP_CHARS,
    _ELISION_ANGLE_PARTIAL_RE,
    _ELISION_COUNT_RE,
    _ELISION_MARKER_RE,
    _ELISION_PARAPHRASE_RE,
    _ELISION_PARTIAL_RE,
    _EXIT_RECEIPT_MAX_ABS,
    _NON_TRUNCATING_BOUNDS,
    _OBS_SNIP_CHARS,
    _OBS_SNIP_HEAD,
    _OBS_SNIP_TAIL,
    WORKSPACE_SNAPSHOT_SENTINEL,
    _arg_snip_marker,
    _arg_snip_marker_below,
    _arg_snip_marker_neutral,
    _execution_receipt_trailer,
    _snip_args,
    find_elided_arg_markers,
    obs_snip_override,
    report_truncation,
    retarget_elided_arg_markers,
    snip_content,
    value_is_only_elision_marker,
)

# ---- base infrastructure, enums, and payload value objects -----------------
# ---- payload value objects re-exported for back-compat ----------------------
from ._event_types import (
    APPKIT_EJECTION_LOST_GUARANTEES,
    APPKIT_EJECTION_SOURCE_TRIGGER,
    APPKIT_EJECTION_TARGET_TRIGGER,
    SCHEMA_VERSION,
    AlternativeOption,
    BaseEvent,
    ClarifyQuestionItem,
    ConversationStatus,
    EventKind,
    EventSource,
    LLMConvertible,
    LLMMessage,
    PlanStep,
    PlanVerificationTransition,
    PlanVerifierFailure,
    PlanVerifierPass,
    QuestionsV2Item,
    ReportSection,
    RuntimeConstraintDeclaration,
    SecurityRisk,
    ToolCall,
    ToolResult,
    _new_id,
    _now,
)
from .dod import DoDPredicate, predicate_to_dict
from .effects import (
    ActionProfile,
    EffectReceipt,
    FinalWorkspaceSeal,
    RecoveryLeaseTransition,
)
from .verification import (
    AdmittedVerificationContract,
    HostVerificationClaim,
    HostVerificationResult,
    PreviewSelectionIdentity,
    VerificationArtifactIdentity,
    VerificationClaimStatus,
    VerificationDeliveryContract,
    VerificationExecutionIdentity,
    VerificationRequirementsDirective,
)

# ---- the discriminated union the store/serde use ----------------------------

Event = Annotated[
    MessageEvent
    | ActionEvent
    | ObservationEvent
    | AgentErrorEvent
    | CondensationEvent
    | StatusEvent
    | WorkspaceVersionEvent
    | WorkspaceRestoredEvent
    | WorkspaceMutationEvent
    | BuildPlatformAdmissionEvent
    | AppKitEjectionEvent
    | PlanEvent
    | ReportEvent
    | AlternativesEvent
    | KnowledgeEvent
    | RuntimeConstraintEvent
    | DatasourceEvent
    | DeliverableEvent
    | VerifierStartedEvent
    | VerifierVerdictEvent
    | VerifierShadowEvent
    | ErrorEvent
    | ScheduleEvent
    | ScheduleRunEvent
    | ClarifyEvent
    | QuestionsV2Event
    | ContextResolvedEvent
    | ContextSummaryEvent,
    Field(discriminator="kind"),
]

# The fold modules intentionally do not import this facade (which would create a
# cycle) or define a second discriminated union. Bind their forward annotation
# namespace to the sole canonical union once it exists.
_platform_folds.Event = Event
_workspace_folds.Event = Event

# Single shared validator/serializer for the union. Consumers parse arbitrary
# event dicts (post-migration) through this; the `kind` field selects the
# concrete type. (§4 serialization contract.)
EventAdapter: TypeAdapter[Event] = TypeAdapter(Event)


def event_to_json_dict(event: Event) -> dict[str, Any]:
    """Serialize one event to a JSON-safe dict (§4 rule 1): ISO datetimes,
    enums by value. The inverse is `event_from_json_dict`."""
    return event.model_dump(mode="json")


def event_from_json_dict(raw: dict[str, Any]) -> Event:
    """Deserialize a (already-migrated) JSON dict back into the concrete event
    type via the discriminated union. Callers should run `migrate_event` first
    (see migration.py) so old persisted events stay readable forever."""
    return EventAdapter.validate_python(raw)
