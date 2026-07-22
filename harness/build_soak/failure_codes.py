"""The Build Soak failure taxonomy (guidelines §12) + run outcomes (§8).

Every `FAIL` must carry a code that lives here, with a known severity. No generic
"failed" bucket is allowed in promotion runs. `UNKNOWN_FAILURE` exists for
development only and is P0 until the classifier learns the case (§13).

Codes are plain string constants (not an enum) so a frozen evidence
`classification.json` round-trips byte-for-byte without enum-name coupling, and so
this module stays import-free / disco-free.
"""

from __future__ import annotations

# ---- run outcomes (§8) ------------------------------------------------------

PASS = "PASS"
FAIL = "FAIL"
INVALID_RUN = "INVALID_RUN"
INFRA_FAILURE = "INFRA_FAILURE"

OUTCOMES = frozenset({PASS, FAIL, INVALID_RUN, INFRA_FAILURE})

# ---- severities (§12) -------------------------------------------------------

P0 = "P0"  # release blocker
P1 = "P1"  # major reliability bug
P2 = "P2"  # quality bug
NONE = "NONE"  # PASS / no failure

SEVERITIES = frozenset({P0, P1, P2, NONE})

# ---- P0 — release blockers (§12) --------------------------------------------

NO_USER_EVENT_AFTER_SUBMIT = "NO_USER_EVENT_AFTER_SUBMIT"
NO_PLAN_AFTER_USER_TURN = "NO_PLAN_AFTER_USER_TURN"
NO_REPLAN_AFTER_REVISION = "NO_REPLAN_AFTER_REVISION"
WRITE_TOOL_ALLOWED_IN_PLANNING = "WRITE_TOOL_ALLOWED_IN_PLANNING"
WRITE_TOOL_ATTEMPTED_IN_PLANNING = "WRITE_TOOL_ATTEMPTED_IN_PLANNING"
WRONG_TOOL_REJECTION_ABORTS_LOOP = "WRONG_TOOL_REJECTION_ABORTS_LOOP"
APPROVE_PLAN_NO_EXECUTION = "APPROVE_PLAN_NO_EXECUTION"
DUPLICATE_EXECUTION_AFTER_APPROVAL = "DUPLICATE_EXECUTION_AFTER_APPROVAL"
EXECUTION_NO_ACTION = "EXECUTION_NO_ACTION"
ACTION_NO_OBSERVATION = "ACTION_NO_OBSERVATION"
FALSE_FINISH_NO_OUTPUT = "FALSE_FINISH_NO_OUTPUT"
FALSE_FINISH_PREVIEW_BROKEN = "FALSE_FINISH_PREVIEW_BROKEN"
DUPLICATE_KICK_ON_RESUME = "DUPLICATE_KICK_ON_RESUME"
EVENT_LOG_STATE_DIVERGENCE = "EVENT_LOG_STATE_DIVERGENCE"
WS_FRAME_DROPPED_NO_ERROR = "WS_FRAME_DROPPED_NO_ERROR"
UI_ENABLED_DEAD_CONTROL = "UI_ENABLED_DEAD_CONTROL"
UNKNOWN_FAILURE = "UNKNOWN_FAILURE"

# ---- P1 — major reliability bugs (§12) --------------------------------------

PLAN_REVISION_NOT_INCREMENTED = "PLAN_REVISION_NOT_INCREMENTED"
STALE_PLAN_USED_AFTER_FOLLOWUP = "STALE_PLAN_USED_AFTER_FOLLOWUP"
PLAN_APPROVED_STATUS_MISSING = "PLAN_APPROVED_STATUS_MISSING"
TOOL_SCOPE_MISMATCH = "TOOL_SCOPE_MISMATCH"
TOOL_REJECTION_NOT_VISIBLE_TO_MODEL = "TOOL_REJECTION_NOT_VISIBLE_TO_MODEL"
MODEL_TOLD_WRONG_MODE = "MODEL_TOLD_WRONG_MODE"
STUCK_RUNNING_NO_EVENTS = "STUCK_RUNNING_NO_EVENTS"
PREVIEW_TRUTH_MISMATCH = "PREVIEW_TRUTH_MISMATCH"
ARTIFACT_TRUTH_MISMATCH = "ARTIFACT_TRUTH_MISMATCH"
CONTEXT_PRESSURE_NOT_OBSERVED = "CONTEXT_PRESSURE_NOT_OBSERVED"
# CXT-5: a final deliverable contains DESTRUCTIVE elision (an "(elided)"/"[trimmed]"/
# "content omitted"/"truncated for brevity" marker with NO recover cue) — unrecoverable
# placeholder text shipped into a deliverable. Rule set v1 (disco.core.observations).
DESTRUCTIVE_ELISION = "DESTRUCTIVE_ELISION"
# P1/HARN-1a: provider-ledger violations of the soak constraint. A provider call went
# to a FORBIDDEN host (e.g. openrouter) or a host that isn't the required one; used the
# WRONG model (not the pinned soak model); or fired AFTER the conversation went terminal
# (runaway burn). These enforce the MiniMax-only / no-OpenRouter / zero-calls-after-terminal
# rule from the provider-call-ledger evidence. All P0.
PROVIDER_FORBIDDEN = "PROVIDER_FORBIDDEN"
PROVIDER_WRONG_MODEL = "PROVIDER_WRONG_MODEL"
PROVIDER_CALL_AFTER_TERMINAL = "PROVIDER_CALL_AFTER_TERMINAL"
# P1/HARN-2: browser product-harness oracle violations (judged over the HARN-1b browser
# evidence streams). These prove the REAL UI path — not just that the engine built files.
BROWSER_WS_NOT_CONNECTED = "BROWSER_WS_NOT_CONNECTED"  # UI never opened the build WS
LIFECYCLE_SEQUENCE_INVALID = (
    "LIFECYCLE_SEQUENCE_INVALID"  # bad status progression / no clean terminal
)
SIDECAR_NOT_STOPPED = "SIDECAR_NOT_STOPPED"  # sidecar/provider alive after terminal
PREVIEW_OWNERSHIP_VIOLATION = (
    "PREVIEW_OWNERSHIP_VIOLATION"  # model owned a port/url, not the platform
)
ARTIFACT_NOT_SHOWN_TO_USER = "ARTIFACT_NOT_SHOWN_TO_USER"  # file exists but UI never showed it
VERIFICATION_GATE_BYPASSED = (
    "VERIFICATION_GATE_BYPASSED"  # finished without ready_for_*_verification
)
EXPORT_DOWNLOAD_MISSING = "EXPORT_DOWNLOAD_MISSING"  # export claimed but no real download
WORKSPACE_NOT_CLEANED = "WORKSPACE_NOT_CLEANED"  # orphan container/sandbox/preview after terminal
NO_CLEAR_FAILURE_TO_USER = "NO_CLEAR_FAILURE_TO_USER"
OBSERVATION_WITHOUT_ACTION = "OBSERVATION_WITHOUT_ACTION"
WRITE_BEFORE_REVISION_APPROVAL = "WRITE_BEFORE_REVISION_APPROVAL"
# The run was required to FINISH and deliver output (scenario asserts
# workspace/preview output and/or terminal_status_in) but ended at a non-finished,
# non-required terminal — e.g. a no-progress / actionless PAUSE, or never reached a
# terminal at all. NOT a "false finish" (nothing claimed finished); a "never
# finished". Fail-closed: a build that does not complete is not a PASS even if some
# files happen to exist (migration 2026_06_24_paused_incomplete_not_pass).
BUILD_DID_NOT_FINISH = "BUILD_DID_NOT_FINISH"
# The model repeatedly issued the same call, hit the same tool/schema error, or
# paused without taking an action. These are product-quality failures even when a
# later lucky guess lets the run reach FINISHED: the soak is meant to remove the
# friction, not merely wait it out.
TOOL_CALL_THRASH = "TOOL_CALL_THRASH"
TOOL_ERROR_THRASH = "TOOL_ERROR_THRASH"
ACTIONLESS_THRASH = "ACTIONLESS_THRASH"
MODEL_REPAIR_THRASH = "MODEL_REPAIR_THRASH"

# P8D — targeted-edit / manual-edit discipline (an edit touched only what it should,
# preserved direct work + anchors, and did not rewrite the world).
TARGETED_EDIT_TOUCHED_UNEXPECTED_FILES = "TARGETED_EDIT_TOUCHED_UNEXPECTED_FILES"
SMALL_EDIT_FULL_REWRITE = "SMALL_EDIT_FULL_REWRITE"  # a small edit churned past the bound
MANUAL_EDIT_CLOBBERED = "MANUAL_EDIT_CLOBBERED"  # a direct/manual override was overwritten
COMMENT_ANCHOR_LOST = "COMMENT_ANCHOR_LOST"  # an anchor disappeared (text edit / reorder)
# present-but-malformed edit evidence — a harness-validity failure (→ INVALID_RUN), never a
# silent pass.
EDIT_ORACLE_EVIDENCE_MALFORMED = "EDIT_ORACLE_EVIDENCE_MALFORMED"

# ---- P2 — quality bugs (§12) ------------------------------------------------

SCREEN_LABEL_UNSTABLE = "SCREEN_LABEL_UNSTABLE"  # an unedited section's screen label changed
SLOW_FIRST_PLAN = "SLOW_FIRST_PLAN"
EXCESSIVE_READS_BEFORE_PLAN = "EXCESSIVE_READS_BEFORE_PLAN"
MISSING_DONE_CONDITIONS = "MISSING_DONE_CONDITIONS"
LOW_QUALITY_PLAN = "LOW_QUALITY_PLAN"
MISSING_PROGRESS_EVENTS = "MISSING_PROGRESS_EVENTS"
WEAK_FINAL_SUMMARY = "WEAK_FINAL_SUMMARY"

# ---- harness-validity codes (§8 INVALID_RUN) --------------------------------
# These do NOT describe a product failure; they say the harness did not collect
# enough durable evidence to adjudicate. The classifier maps them to INVALID_RUN.

NO_EVENTS = "NO_EVENTS"
UNPARSEABLE_EVENTS = "UNPARSEABLE_EVENTS"
EVIDENCE_HASH_MISMATCH = "EVIDENCE_HASH_MISMATCH"
MISSING_REQUIRED_EVIDENCE = "MISSING_REQUIRED_EVIDENCE"
SCENARIO_CONTRACT_UNSATISFIABLE = "SCENARIO_CONTRACT_UNSATISFIABLE"
# A POST-create runner-side outcome that is NOT adjudicable as a product result: the
# runner could not obtain a terminal verdict. RUN_INTERRUPTED — a mid-run transport loss
# (server crashed / network dropped). RUN_TIMEOUT_WHILE_PROGRESSING (Bug 15) — the hard
# cap was hit while the build was STILL actively progressing (a slow-but-advancing build
# cut off mid-flight), so it must NOT be recorded as a product BUILD_DID_NOT_FINISH; §17
# re-runs an INVALID_RUN instead of freezing a false product failure.
RUN_INTERRUPTED = "RUN_INTERRUPTED"
RUN_TIMEOUT_WHILE_PROGRESSING = "RUN_TIMEOUT_WHILE_PROGRESSING"
# The cancel_at after_first_file_write trigger observed a successful file mutation, but the
# build had already reached a terminal status before the harness could issue the kill. That is
# not a valid disconnect/cancel test; re-run instead of counting a post-finish kill as coverage.
CANCEL_MISSED_WINDOW = "CANCEL_MISSED_WINDOW"
# The host ProjectStore workspace snapshot never reached the build's AGENT-FINAL state
# within the snapshot-wait budget — i.e. the snapshot's on-disk bytes for a declared file
# never matched what the agent last wrote to it (a stale/slow flush, or a multi-revision
# build whose later-revision bytes had not yet flushed). This is NOT a product
# ARTIFACT_TRUTH_MISMATCH (the harness read the snapshot before it settled): the collect
# FAILS FAST here rather than laundering a stale capture into a false product failure or a
# silent best-effort pass, so §17 re-runs it. Bounded — never hangs.
WORKSPACE_SNAPSHOT_NOT_READY = "WORKSPACE_SNAPSHOT_NOT_READY"
# A content (must_contain / must_not_contain / exact) assertion mismatched, but EVERY
# mismatching file was captured while its bytes were still churning at the capture deadline:
# proof is non-authoritative (`unproven_extended_stability` / `unknown`) AND the adapter did
# not observe the file stable for the readiness threshold. The captured snapshot may be a
# stale pre-flush copy while the ProjectStore is actually correct, so the mismatch is
# UNRELIABLE and must NOT be reported as a definitive product ARTIFACT_TRUTH_MISMATCH. It is
# recorded as harness-validity evidence → INVALID_RUN so §17 re-runs it. A mismatch on a
# PROVEN basis (raw_sha / rendered_readback / absent) OR on content-stable bytes in the SAME
# run always WINS this fold and stays a hard ARTIFACT_TRUTH_MISMATCH.
WORKSPACE_SNAPSHOT_UNVERIFIED = "WORKSPACE_SNAPSHOT_UNVERIFIED"

HARNESS_VALIDITY_CODES = frozenset(
    {
        NO_EVENTS,
        UNPARSEABLE_EVENTS,
        EVIDENCE_HASH_MISMATCH,
        MISSING_REQUIRED_EVIDENCE,
        SCENARIO_CONTRACT_UNSATISFIABLE,
        RUN_INTERRUPTED,
        RUN_TIMEOUT_WHILE_PROGRESSING,
        CANCEL_MISSED_WINDOW,
        WORKSPACE_SNAPSHOT_NOT_READY,
        WORKSPACE_SNAPSHOT_UNVERIFIED,
        EDIT_ORACLE_EVIDENCE_MALFORMED,  # P8D: present-but-malformed edit evidence → INVALID_RUN
    }
)


# The severity of every product failure code. (Harness-validity codes are not in
# here — they resolve to INVALID_RUN, which has its own outcome, not a severity.)
SEVERITY_BY_CODE: dict[str, str] = {
    # P0
    NO_USER_EVENT_AFTER_SUBMIT: P0,
    NO_PLAN_AFTER_USER_TURN: P0,
    NO_REPLAN_AFTER_REVISION: P0,
    WRITE_TOOL_ALLOWED_IN_PLANNING: P0,
    WRITE_TOOL_ATTEMPTED_IN_PLANNING: P0,
    WRONG_TOOL_REJECTION_ABORTS_LOOP: P0,
    APPROVE_PLAN_NO_EXECUTION: P0,
    DUPLICATE_EXECUTION_AFTER_APPROVAL: P0,
    EXECUTION_NO_ACTION: P0,
    ACTION_NO_OBSERVATION: P0,
    FALSE_FINISH_NO_OUTPUT: P0,
    FALSE_FINISH_PREVIEW_BROKEN: P0,
    DUPLICATE_KICK_ON_RESUME: P0,
    EVENT_LOG_STATE_DIVERGENCE: P0,
    WS_FRAME_DROPPED_NO_ERROR: P0,
    UI_ENABLED_DEAD_CONTROL: P0,
    PROVIDER_FORBIDDEN: P0,
    PROVIDER_WRONG_MODEL: P0,
    PROVIDER_CALL_AFTER_TERMINAL: P0,
    BROWSER_WS_NOT_CONNECTED: P0,
    LIFECYCLE_SEQUENCE_INVALID: P0,
    SIDECAR_NOT_STOPPED: P0,
    PREVIEW_OWNERSHIP_VIOLATION: P0,
    ARTIFACT_NOT_SHOWN_TO_USER: P0,
    VERIFICATION_GATE_BYPASSED: P0,
    EXPORT_DOWNLOAD_MISSING: P0,
    WORKSPACE_NOT_CLEANED: P0,
    # P8D targeted/manual-edit discipline
    TARGETED_EDIT_TOUCHED_UNEXPECTED_FILES: P1,
    SMALL_EDIT_FULL_REWRITE: P1,
    MANUAL_EDIT_CLOBBERED: P1,
    COMMENT_ANCHOR_LOST: P1,
    SCREEN_LABEL_UNSTABLE: P2,
    UNKNOWN_FAILURE: P0,
    # P1
    PLAN_REVISION_NOT_INCREMENTED: P1,
    STALE_PLAN_USED_AFTER_FOLLOWUP: P1,
    PLAN_APPROVED_STATUS_MISSING: P1,
    TOOL_SCOPE_MISMATCH: P1,
    TOOL_REJECTION_NOT_VISIBLE_TO_MODEL: P1,
    MODEL_TOLD_WRONG_MODE: P1,
    STUCK_RUNNING_NO_EVENTS: P1,
    PREVIEW_TRUTH_MISMATCH: P1,
    ARTIFACT_TRUTH_MISMATCH: P1,
    CONTEXT_PRESSURE_NOT_OBSERVED: P1,
    DESTRUCTIVE_ELISION: P1,
    NO_CLEAR_FAILURE_TO_USER: P1,
    OBSERVATION_WITHOUT_ACTION: P1,
    WRITE_BEFORE_REVISION_APPROVAL: P1,
    BUILD_DID_NOT_FINISH: P1,
    TOOL_CALL_THRASH: P1,
    TOOL_ERROR_THRASH: P1,
    ACTIONLESS_THRASH: P1,
    MODEL_REPAIR_THRASH: P1,
    # P2
    SLOW_FIRST_PLAN: P2,
    EXCESSIVE_READS_BEFORE_PLAN: P2,
    MISSING_DONE_CONDITIONS: P2,
    LOW_QUALITY_PLAN: P2,
    MISSING_PROGRESS_EVENTS: P2,
    WEAK_FINAL_SUMMARY: P2,
}

# Every product failure code, for validation.
PRODUCT_FAILURE_CODES = frozenset(SEVERITY_BY_CODE)

# The "intermittent" prefix (§17): a run that failed once then passed on exact
# replay is recorded as FAIL with code INTERMITTENT_<original_code>, same severity.
INTERMITTENT_PREFIX = "INTERMITTENT_"


def severity_for(code: str) -> str:
    """Severity for a (possibly INTERMITTENT_-prefixed) failure code. Unknown
    codes are treated as P0 (§13: an unclassified failure is a release blocker)."""
    base = code[len(INTERMITTENT_PREFIX) :] if code.startswith(INTERMITTENT_PREFIX) else code
    return SEVERITY_BY_CODE.get(base, P0)


def is_known_code(code: str) -> bool:
    base = code[len(INTERMITTENT_PREFIX) :] if code.startswith(INTERMITTENT_PREFIX) else code
    return base in PRODUCT_FAILURE_CODES or base in HARNESS_VALIDITY_CODES
