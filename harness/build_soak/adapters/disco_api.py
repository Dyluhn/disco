"""disco_api.py — async client that drives the LIVE Disco agent-server (PR S3).

The runner ACTS AS THE USER over the product's real surfaces (REST verbs verified in
routes/conversations.py; the plan gate + steer are WS-ONLY, verified in routes/ws.py
`_handle_frame` — there is no REST approval route):

  create_build_conversation  POST /conversations (surface="build")    conversations.py:49
                             + POST /conversations/{cid}/messages      conversations.py:149
                               (appends the user message AND kicks the loop)
  approve_plan               WS {"type":"approve_plan"}    ws.py:95 -> runtime.approve_plan
  confirm                    WS {"type":"confirm"}         ws.py:89 -> runtime.confirm (clears
                             WAITING_FOR_CONFIRMATION — the confirm analogue of approve_plan;
                             a plain message does NOT clear this gate)
  send_followup (message)    WS {"type":"send_message"}    ws.py:48 -> store.append + kick
  send_followup (steer)      WS {"type":"steer",...}       ws.py:58 -> store.append(steer)+kick
  send_followup (request_plan) WS {"type":"request_plan"}  ws.py:98 -> runtime.request_plan
  poll_until_terminal        GET /conversations/{cid}/state           conversations.py:195
  collect_events             read disco.db DIRECTLY, post-terminal (race-free; trace pattern)
  collect_state              GET /conversations/{cid}/state
  collect_workspace          read the host ProjectStore SNAPSHOT directly (authoritative;
                             Bug 9 fix — NOT the dev-server preview proxy, which 404s when
                             the served app isn't up). No generated-content fallback.
  collect_preview            GET /conversations/{cid}/preview + isolated path capability

approve_plan / request_plan are WS-ONLY (there is no REST approval route — verified
in routes/conversations.py: only create/messages/followup/events/state/kill/resume
exist; the plan gate lives in routes/ws.py `_handle_frame`). So the runner opens a
short-lived WS, sends the one control frame, and closes — the loop state machine
acts on it; truth is read back from the durable event log / state.

INFRA GATE (codex #3): `pre_create_probe` is the ONLY place INFRA_FAILURE can come
from — a RUNNER-SIDE failure BEFORE a conversation_id exists. A post-create
driver-preflight error surfaces as StatusEvent(ERROR) AFTER the user event and is a
PRODUCT outcome (classified by the oracle), never infra.

Transport is abstracted so the deterministic tests can drive the orchestration with
a FAKE transport (no live model spend); the live path uses HttpTransport (httpx +
websockets) behind the opt-in `@pytest.mark.live` marker.
"""

from __future__ import annotations

import asyncio as asyncio
import tempfile as tempfile
import time as time
from pathlib import Path as Path
from typing import Any as Any

from ..efficiency import LiveEfficiencyProgress as LiveEfficiencyProgress
from ..ports import ProductClient as ProductClient
from ._api_browser import (
    _PROOF_BY_EXPECTED_KIND as _PROOF_BY_EXPECTED_KIND,
)
from ._api_browser import (
    _SHELL_META_SUBSTRINGS as _SHELL_META_SUBSTRINGS,
)
from ._api_browser import (
    _SHELL_META_TOKENS as _SHELL_META_TOKENS,
)
from ._api_browser import (
    _choose_alternative as _choose_alternative,
)
from ._api_browser import (
    _file_entry as _file_entry,
)
from ._api_browser import (
    _jailed_browser_evidence_path as _jailed_browser_evidence_path,
)
from ._api_browser import (
    _last_terminal as _last_terminal,
)
from ._api_browser import (
    _manifest_ident as _manifest_ident,
)
from ._api_browser import (
    _manifest_lookup as _manifest_lookup,
)
from ._api_browser import (
    _manifest_present as _manifest_present,
)
from ._api_browser import (
    _manifest_sha as _manifest_sha,
)
from ._api_browser import (
    _needs_resolved_write_proof as _needs_resolved_write_proof,
)
from ._api_browser import (
    _norm_rel as _norm_rel,
)
from ._api_browser import (
    _payload as _payload,
)
from ._api_browser import (
    _proof_level as _proof_level,
)
from ._api_browser import (
    _referenced_screenshot_paths as _referenced_screenshot_paths,
)
from ._api_browser import (
    _successful_observation_refs as _successful_observation_refs,
)
from ._api_browser import (
    validate_browser_evidence_relpath as validate_browser_evidence_relpath,
)
from ._api_inspect import (
    _InspectTraceAggregation as _InspectTraceAggregation,
)
from ._api_inspect_validation import (
    _INSPECT_AGGREGATION_COUNTER_KEYS as _INSPECT_AGGREGATION_COUNTER_KEYS,
)
from ._api_inspect_validation import (
    _INSPECT_AGGREGATION_KEYS as _INSPECT_AGGREGATION_KEYS,
)
from ._api_inspect_validation import (
    _INSPECT_TRACE_TOP_LEVEL_KEYS as _INSPECT_TRACE_TOP_LEVEL_KEYS,
)
from ._api_inspect_validation import (
    _canonical_json_bytes as _canonical_json_bytes,
)
from ._api_inspect_validation import (
    _exact_nonneg_int as _exact_nonneg_int,
)
from ._api_inspect_validation import (
    _inspect_events_and_projections as _inspect_events_and_projections,
)
from ._api_inspect_validation import (
    _live_thrash_finding_is_current as _live_thrash_finding_is_current,
)
from ._api_inspect_validation import (
    inspect_aggregate_violations as inspect_aggregate_violations,
)
from ._api_shell import (
    _contains_relpath as _contains_relpath,
)
from ._api_shell import (
    _curl_segment_is_read_only as _curl_segment_is_read_only,
)
from ._api_shell import (
    _has_shell_meta as _has_shell_meta,
)
from ._api_shell import (
    _http_server_segment_is_read_only as _http_server_segment_is_read_only,
)
from ._api_shell import (
    _mv_removes_declared as _mv_removes_declared,
)
from ._api_shell import (
    _rm_has_recursive_flag as _rm_has_recursive_flag,
)
from ._api_shell import (
    _rm_removes_declared as _rm_removes_declared,
)
from ._api_shell import (
    _shell_is_proven_read_only as _shell_is_proven_read_only,
)
from ._api_shell import (
    _shell_norm_rel as _shell_norm_rel,
)
from ._api_shell import (
    _shell_pipeline_tokens as _shell_pipeline_tokens,
)
from ._api_shell import (
    _shell_removes as _shell_removes,
)
from ._api_shell import (
    _shell_tokens as _shell_tokens,
)
from ._api_shell import (
    _split_flags_and_paths as _split_flags_and_paths,
)
from ._api_transport import HttpTransport as _HttpTransport
from ._api_types import (
    _ACTION_RESULT_PERSISTENCE_GRACE_S as _ACTION_RESULT_PERSISTENCE_GRACE_S,
)
from ._api_types import (
    _BROWSER_EVIDENCE_MAX_FILE_BYTES as _BROWSER_EVIDENCE_MAX_FILE_BYTES,
)
from ._api_types import (
    _BROWSER_EVIDENCE_MAX_FILES as _BROWSER_EVIDENCE_MAX_FILES,
)
from ._api_types import (
    _BROWSER_EVIDENCE_MAX_TOTAL_BYTES as _BROWSER_EVIDENCE_MAX_TOTAL_BYTES,
)
from ._api_types import (
    _BROWSER_EVIDENCE_TOOLS as _BROWSER_EVIDENCE_TOOLS,
)
from ._api_types import (
    _DEFAULT_TOOL_TIMEOUT_S as _DEFAULT_TOOL_TIMEOUT_S,
)
from ._api_types import (
    _ELISION_MARKER_RE as _ELISION_MARKER_RE,
)
from ._api_types import (
    _FILE_MUTATION_TOOLS as _FILE_MUTATION_TOOLS,
)
from ._api_types import (
    _FILE_READ_FULL_HEADER_RE as _FILE_READ_FULL_HEADER_RE,
)
from ._api_types import (
    _FILE_READ_TOOLS as _FILE_READ_TOOLS,
)
from ._api_types import (
    _FILE_WRITE_FULL_TOOLS as _FILE_WRITE_FULL_TOOLS,
)
from ._api_types import (
    _FILE_WRITE_PARTIAL_TOOLS as _FILE_WRITE_PARTIAL_TOOLS,
)
from ._api_types import (
    _FINAL_SEAL_EFFECT_KINDS as _FINAL_SEAL_EFFECT_KINDS,
)
from ._api_types import (
    _FINAL_SEAL_KEYS as _FINAL_SEAL_KEYS,
)
from ._api_types import (
    _FINAL_SEAL_SCOPE_KEYS as _FINAL_SEAL_SCOPE_KEYS,
)
from ._api_types import (
    _FOLLOWUP_PICKUP_TIMEOUT_DEFAULT_S as _FOLLOWUP_PICKUP_TIMEOUT_DEFAULT_S,
)
from ._api_types import (
    _FOLLOWUP_PICKUP_TIMEOUT_ENV as _FOLLOWUP_PICKUP_TIMEOUT_ENV,
)
from ._api_types import (
    _INSPECT_AGGREGATION_REASON_ORDER as _INSPECT_AGGREGATION_REASON_ORDER,
)
from ._api_types import (
    _INSPECT_PROJECTIONS as _INSPECT_PROJECTIONS,
)
from ._api_types import (
    _LOG as _LOG,
)
from ._api_types import (
    _MAX_PREVIEW_HANDOFF_CHARS as _MAX_PREVIEW_HANDOFF_CHARS,
)
from ._api_types import (
    _NO_SYSTEM_FINISHED_TERMINAL as _NO_SYSTEM_FINISHED_TERMINAL,
)
from ._api_types import (
    _NON_MUTATING_TOOLS as _NON_MUTATING_TOOLS,
)
from ._api_types import (
    _PLANNING_DETAIL as _PLANNING_DETAIL,
)
from ._api_types import (
    _PRECREATE_INFRA_ERRORS as _PRECREATE_INFRA_ERRORS,
)
from ._api_types import (
    _PREVIEW_RESET_BODY_RE as _PREVIEW_RESET_BODY_RE,
)
from ._api_types import (
    _RAW_SHA256_RE as _RAW_SHA256_RE,
)
from ._api_types import (
    _SLIDES_GENERATE_TIMEOUT_S as _SLIDES_GENERATE_TIMEOUT_S,
)
from ._api_types import (
    _SNAPSHOT_POLL_S as _SNAPSHOT_POLL_S,
)
from ._api_types import (
    _SNAPSHOT_UNPROVEN_STABLE_POLLS as _SNAPSHOT_UNPROVEN_STABLE_POLLS,
)
from ._api_types import (
    _STRICT_SHA256_RE as _STRICT_SHA256_RE,
)
from ._api_types import (
    _TOOL_TIMEOUT_OVERRIDES_S as _TOOL_TIMEOUT_OVERRIDES_S,
)
from ._api_types import (
    _WORK_TERMINALS as _WORK_TERMINALS,
)
from ._api_types import (
    _WS_MANIFEST_MAX_BYTES as _WS_MANIFEST_MAX_BYTES,
)
from ._api_types import (
    _WS_MANIFEST_MAX_FILES as _WS_MANIFEST_MAX_FILES,
)
from ._api_types import (
    AWAITING_PLAN_APPROVAL as AWAITING_PLAN_APPROVAL,
)
from ._api_types import (
    AWAITING_USER_DECISION as AWAITING_USER_DECISION,
)
from ._api_types import (
    AWAITING_USER_QUESTION as AWAITING_USER_QUESTION,
)
from ._api_types import (
    FOLLOWUP_PICKED_UP as FOLLOWUP_PICKED_UP,
)
from ._api_types import (
    FOLLOWUP_PICKUP_TIMEOUT as FOLLOWUP_PICKUP_TIMEOUT,
)
from ._api_types import (
    FOLLOWUP_REPLANNED as FOLLOWUP_REPLANNED,
)
from ._api_types import (
    GATE_STATES as GATE_STATES,
)
from ._api_types import (
    INACTIVE_TIMEOUT as INACTIVE_TIMEOUT,
)
from ._api_types import (
    LIVE_THRASH_STOP as LIVE_THRASH_STOP,
)
from ._api_types import (
    PAUSED_STATE as PAUSED_STATE,
)
from ._api_types import (
    PROGRESSING_TIMEOUT as PROGRESSING_TIMEOUT,
)
from ._api_types import (
    SEAL_INCOMPLETE_CONTENT_KIND as SEAL_INCOMPLETE_CONTENT_KIND,
)
from ._api_types import (
    TERMINAL_STATES as TERMINAL_STATES,
)
from ._api_types import (
    WAITING_FOR_CONFIRMATION as WAITING_FOR_CONFIRMATION,
)
from ._api_types import (
    BrowserEvidenceCollectionError as BrowserEvidenceCollectionError,
)
from ._api_types import (
    CollectedRun as CollectedRun,
)
from ._api_types import (
    FinishUnsealableContentError as FinishUnsealableContentError,
)
from ._api_types import (
    FollowupPickupError as FollowupPickupError,
)
from ._api_types import (
    InconclusiveRunError as InconclusiveRunError,
)
from ._api_types import (
    InfraProbeError as InfraProbeError,
)
from ._api_types import (
    SnapshotNotReadyError as SnapshotNotReadyError,
)
from ._api_types import (
    Transport as Transport,
)
from ._api_types import (
    _event_status_value as _event_status_value,
)
from ._api_types import (
    _exact_seq as _exact_seq,
)
from ._api_types import (
    _FinalWorkspaceSealEvidence as _FinalWorkspaceSealEvidence,
)
from ._api_types import (
    _followup_pickup_timeout_s as _followup_pickup_timeout_s,
)
from ._api_types import (
    _freeze_horizon_violation as _freeze_horizon_violation,
)
from ._api_types import (
    _latest_agent_view_id as _latest_agent_view_id,
)
from ._api_types import (
    _latest_run_intent_id as _latest_run_intent_id,
)
from ._api_types import (
    _strict_final_workspace_seal as _strict_final_workspace_seal,
)
from ._api_types import (
    _typed_content_seal_refusal as _typed_content_seal_refusal,
)
from ._api_types import (
    _valid_workspace_version as _valid_workspace_version,
)
from ._api_types import (
    _VerifiedWorkspaceVersion as _VerifiedWorkspaceVersion,
)
from ._api_workspace import (
    _SCRIPT_MUTATE_TOOLS as _SCRIPT_MUTATE_TOOLS,
)
from ._api_workspace import (
    _SHELL_TOOLS as _SHELL_TOOLS,
)
from ._api_workspace import (
    _agent_declared_expected as _agent_declared_expected,
)
from ._api_workspace import (
    _evaluate_snapshot_readiness as _evaluate_snapshot_readiness,
)
from ._api_workspace import (
    _full_readback_body as _full_readback_body,
)
from ._api_workspace import (
    _manifest_content as _manifest_content,
)
from ._api_workspace import (
    _render_numbered as _render_numbered,
)
from ._client_collection import _CollectionMixin
from ._client_conversation import _ConversationMixin
from ._client_freeze import _FreezeMixin
from ._client_observability import _ObservabilityMixin
from ._client_polling import _PollingMixin
from ._client_snapshot import _SnapshotMixin


class DiscoApiClient(
    _ObservabilityMixin,
    _ConversationMixin,
    _PollingMixin,
    _CollectionMixin,
    _SnapshotMixin,
    _FreezeMixin,
    ProductClient,
):
    @staticmethod
    def _default_tool_timeout_s() -> float:
        return _DEFAULT_TOOL_TIMEOUT_S

    @staticmethod
    def _tool_timeout_overrides_s() -> dict[str, float]:
        return _TOOL_TIMEOUT_OVERRIDES_S

    @staticmethod
    def _action_result_persistence_grace_s() -> float:
        return _ACTION_RESULT_PERSISTENCE_GRACE_S

    @staticmethod
    def _browser_evidence_max_files() -> int:
        return _BROWSER_EVIDENCE_MAX_FILES

    @staticmethod
    def _browser_evidence_max_file_bytes() -> int:
        return _BROWSER_EVIDENCE_MAX_FILE_BYTES

    @staticmethod
    def _browser_evidence_max_total_bytes() -> int:
        return _BROWSER_EVIDENCE_MAX_TOTAL_BYTES

    def __init__(
        self,
        transport: Transport,
        *,
        db_path: str,
        poll_interval_s: float = 1.0,
        projects_root: str | None = None,
        snapshot_wait_s: float = 0.0,
        require_workspace_commit: bool = False,
    ) -> None:
        super().__init__(
            transport,
            db_path=db_path,
            poll_interval_s=poll_interval_s,
            projects_root=projects_root,
            snapshot_wait_s=snapshot_wait_s,
            require_workspace_commit=require_workspace_commit,
        )


class HttpTransport(_HttpTransport):
    """Compatibility definition for the bounded live HTTP adapter."""


if __name__ == "__main__":
    raise SystemExit("harness.build_soak.adapters.disco_api is not an entrypoint")
