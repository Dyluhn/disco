"""Deterministic model-thrashing oracle over Disco's durable event stream.

The product already contains bounded no-progress valves.  Those valves protect
cost; they do not make a run healthy.  A build that eventually finishes after
repeatedly guessing tool syntax, repeating the same call, or actionless-pausing
is precisely the friction a reliability soak must surface.

Thresholds are scenario-owned under ``assertions.thrash`` so migrations are
explicit and evidence-locked.  Without that block the oracle SKIPs, preserving
classification of older frozen dossiers.
"""

from __future__ import annotations

import hashlib
import json
import posixpath
import re
import shlex
from collections import Counter
from typing import Any

from .. import failure_codes as fc
from ..events import (
    KIND_ACTION,
    KIND_AGENT_ERROR,
    KIND_MESSAGE,
    KIND_OBSERVATION,
    KIND_STATUS,
    SRC_USER,
    action_id_of,
    kind_of,
    seq_of,
    tool_name_of,
)
from .schema import OracleResult, failing, passing, skipping

_ORACLE = "ThrashOracle"
_UUID = re.compile(r"\b(?:[0-9a-f]{8}-?){2,}[0-9a-f]*\b", re.IGNORECASE)
_NUMBER = re.compile(r"\b\d+\b")
_SPACE = re.compile(r"\s+")
_THRASH_STUCK_DETAILS = {
    "repeated_action_observation",
    "repeated_noop",
    "verify_no_progress",
    "verifier_no_progress",
    "identical_plan_streak",
}
_SHELL_TOOLS = frozenset({"shell", "shell_exec"})
_RUNTIME_CLEANUP_TOOLS = frozenset({"shell_kill_process"})
_SHELL_CONTROL_OPERATORS = frozenset({";", "&", "&&", "|", "|&", "||"})
_SHELL_PIPE_OPERATORS = frozenset({"|", "|&"})
_RUNTIME_CLEANUP_COMMANDS = frozenset({"kill", "killall", "pkill"})
_SHELL_PUNCTUATION = ";&|<>"
_STATIC_TEE_SINK_UNSAFE = re.compile(r"[$`*?\[\]{}()~\r\n]")
_STATIC_TEE_SINK_VIRTUAL_ROOTS = ("/dev", "/proc", "/sys")
_QUOTED_PUNCTUATION_MASK = str.maketrans(
    {char: chr(0xE100 + index) for index, char in enumerate(_SHELL_PUNCTUATION)}
)
_QUOTED_PUNCTUATION_UNMASK = str.maketrans(
    {chr(0xE100 + index): char for index, char in enumerate(_SHELL_PUNCTUATION)}
)
_FILE_MUTATION_TOOLS = frozenset(
    {
        "exact_replace",
        "file_append",
        "file_edit",
        "file_insert_lines",
        "file_replace_lines",
        "file_str_replace",
        "file_write",
        "safe_write_file",
        "write_file",
    }
)
_DIRECT_SCRIPT_RUNNERS: tuple[tuple[re.Pattern[str], str, frozenset[str]], ...] = (
    (re.compile(r"python(?:\d+(?:\.\d+)?)?\Z"), "python", frozenset({".py"})),
    (re.compile(r"node(?:js)?\Z"), "node", frozenset({".cjs", ".js", ".mjs"})),
    (re.compile(r"ruby\Z"), "ruby", frozenset({".rb"})),
    (re.compile(r"perl\Z"), "perl", frozenset({".pl", ".pm"})),
    (re.compile(r"(?:ba|da|k|z)?sh\Z"), "shell", frozenset({".sh"})),
)
_SOURCE_SUFFIX_FAMILY = {
    suffix: family for _pattern, family, suffixes in _DIRECT_SCRIPT_RUNNERS for suffix in suffixes
}


def _limits(scenario: dict[str, Any] | None) -> dict[str, int] | None:
    block = ((scenario or {}).get("assertions") or {}).get("thrash")
    if not isinstance(block, dict):
        return None
    defaults = {
        "max_identical_action_repeats": 2,
        "max_same_tool_error_repeats": 2,
        "max_actionless_pauses": 0,
        "max_same_model_repair_repeats": 1,
        "max_total_model_repairs": 3,
    }
    out: dict[str, int] = {}
    for key, default in defaults.items():
        raw = block.get(key, default)
        if not isinstance(raw, int) or raw < 0:
            raise ValueError(f"assertions.thrash.{key} must be a non-negative integer")
        out[key] = raw
    return out


def repair_spans(spans: Any) -> list[dict[str, Any]]:
    """The model-repair point spans in an inspect trace.

    Repairs happen BEFORE an ActionEvent is committed, so they are invisible to
    the durable event log; DISCO_INSPECT records a bounded, redacted
    ``agent.repair`` point span for unknown-tool guesses, degenerate turns,
    tool-history repairs, and provider request rejections.

    Shared so the thrash gate and the efficiency readout agree on what a repair
    IS. A span with no usable ``repair_kind`` is not a repair — it carries no
    attributable behaviour to count.
    """
    if not isinstance(spans, list):
        return []
    return [
        span
        for span in spans
        if isinstance(span, dict)
        and span.get("span") == "agent.repair"
        and span.get("event") == "point"
        and str(span.get("repair_kind") or "")
    ]


def _fingerprint(event: dict[str, Any]) -> str:
    call = event.get("tool_call") or {}
    args = call.get("arguments") or {}
    encoded = json.dumps(args, sort_keys=True, separators=(",", ":"), default=str)
    return f"{tool_name_of(event) or '?'}:{encoded}"


def _normalized_error_text(text: str) -> str:
    normalized = _UUID.sub("<id>", text.lower())
    normalized = _NUMBER.sub("<n>", normalized)
    return _SPACE.sub(" ", normalized).strip()


def _error_signature(text: str) -> str:
    return _normalized_error_text(text)[:240]


def _detail_signature(error: str, detail: str) -> str:
    """Return a non-disclosing identity for actionable failure detail.

    Built-in tools commonly keep a stable machine error code while putting the
    actual refusal reason in ``AgentErrorEvent.detail`` (or failed
    ``ToolOutcome.content``). Two different reasons are two different recovery
    steps, not evidence of a repeated failed behavior. Normalize volatile IDs and
    numbers before hashing so retries of the same reason still group, while the
    evidence dossier never gains the potentially sensitive raw detail.
    """

    normalized = _normalized_error_text(detail)
    if not normalized or normalized == _normalized_error_text(error):
        return ""
    return "sha256:" + hashlib.sha256(normalized.encode("utf-8", "surrogatepass")).hexdigest()


def _recovered_blocked_marker(marker: dict[str, Any], events: list[dict[str, Any]]) -> bool:
    """Whether a historical STUCK marker was explicitly superseded and recovered.

    The product retains the legacy STUCK event for audit/counter continuity when
    it lands a bounded failure at a user-question gate.  That marker is not the
    run terminal only when its typed lineage is complete: exact metadata, the
    immediately following status-owned blocked landing, a real user answer, and
    a later clean terminal.  Malformed, autonomous, or unrecovered markers remain
    thrash failures.
    """

    detail = str(marker.get("detail") or "")
    meta = marker.get("meta")
    if not isinstance(meta, dict) or not (
        meta.get("blocked_landing") is True
        and meta.get("superseded_by_landing") is True
        and meta.get("blocked_reason") == detail
        and meta.get("legacy_detail") == detail
        and meta.get("legacy_status") == "STUCK"
    ):
        return False

    marker_seq = seq_of(marker)
    subsequent_statuses = [
        event for event in events if seq_of(event) > marker_seq and kind_of(event) == KIND_STATUS
    ]
    if not subsequent_statuses:
        return False
    landing = subsequent_statuses[0]
    landing_meta = landing.get("meta")
    if not (
        landing.get("status") == "AWAITING_USER_QUESTION"
        and isinstance(landing_meta, dict)
        and landing_meta.get("blocked_landing") is True
        and landing_meta.get("blocked_reason") == detail
        and landing_meta.get("legacy_detail") == detail
        and landing_meta.get("legacy_status") == "STUCK"
    ):
        return False

    landing_seq = seq_of(landing)
    user_seqs = [
        seq_of(event)
        for event in events
        if seq_of(event) > landing_seq
        and kind_of(event) == KIND_MESSAGE
        and event.get("source") == SRC_USER
    ]
    if not user_seqs:
        return False
    user_seq = user_seqs[0]
    return any(
        seq_of(event) > user_seq
        and kind_of(event) == KIND_STATUS
        and event.get("status") in {"FINISHED", "VERIFIED"}
        for event in events
    )


def _failed_action_signature(action: dict[str, Any], error_signature: str) -> str:
    """Bound the intent context needed to distinguish generic shell failures.

    Shell adapters intentionally expose terse errors such as ``command exited
    1``. That error alone does not mean two different diagnostic commands are
    the same failed behavior. Preserve the strict error grouping for structured
    tools. Only the lossy generic shell exit additionally requires the exact
    command bytes to match; the bounded digest never adds raw command text to
    oracle facts.
    """

    if tool_name_of(action) not in _SHELL_TOOLS or error_signature != "command exited <n>":
        return ""
    args = (action.get("tool_call") or {}).get("arguments") or {}
    command = args.get("command", args.get("cmd"))
    if not isinstance(command, str):
        return ""
    return "sha256:" + hashlib.sha256(command.encode("utf-8", "surrogatepass")).hexdigest()


# Verifier V3: mirror the product's receipt-tool gating exactly
# (`successful_mutation_with_receipt`): the exact path+sha256 shape is trusted
# only from the file-mutating tools, `applied` only from run_project_script,
# `state_changed` from any tool. Kept inline over the dict encoding — a new
# receipt-bearing tool added in stuck.py must be added here too.
_RECEIPT_FILE_TOOLS = _FILE_MUTATION_TOOLS
_RECEIPT_APPLIED_TOOLS = frozenset({"run_project_script"})


def _trusted_mutation_receipt_outcome(
    event: dict[str, Any], *, action_ids: frozenset[str] | None = None
) -> bool:
    """Whether one observation event carries a trusted changed-state receipt.

    Mirrors the product's `successful_mutation_with_receipt` trust boundary
    over the durable dict encoding: a PAIRED successful outcome whose
    structured payload proves a concrete state change through the shape the
    emitting tool class actually produces (exact path+sha256 from a
    file-mutating tool, a non-empty all-string `applied` list from
    run_project_script, or an explicit `state_changed: true`). Unpaired
    observations, foreign tool/shape combinations, and degenerate values are
    inert (verifier finding V3).
    """

    if kind_of(event) != KIND_OBSERVATION:
        return False
    action_id = event.get("action_id")
    if not isinstance(action_id, str) or not action_id:
        return False
    if action_ids is not None and action_id not in action_ids:
        return False
    result = event.get("tool_result") or {}
    if result.get("success") is not True:
        return False
    tool_name = result.get("tool_name")
    structured = result.get("structured")
    if not isinstance(structured, dict):
        return False
    if structured.get("state_changed") is True:
        return True
    if tool_name in _RECEIPT_APPLIED_TOOLS:
        applied = structured.get("applied")
        return (
            isinstance(applied, list)
            and bool(applied)
            and all(isinstance(item, str) and item for item in applied)
        )
    if tool_name in _RECEIPT_FILE_TOOLS:
        path = structured.get("path")
        sha256 = structured.get("sha256")
        return (
            isinstance(path, str)
            and bool(path.strip())
            and isinstance(sha256, str)
            and re.fullmatch(r"[0-9a-f]{64}", sha256) is not None
        )
    return False


def progress_epoch_boundary(
    event: dict[str, Any], *, action_ids: frozenset[str] | None = None
) -> bool:
    """Whether *event* closes the current progress epoch for error accounting.

    k6g finding F2: two identical error signatures separated by trusted
    progress are two independent recoverable errors, not one repeated failed
    behavior. Boundaries are host-authored and typed — a real user turn (an
    answered question lands as one), a server-authored approved plan
    transition, a typed blocking proof obligation, or a trusted changed-state
    receipt. Plain environment prose is NOT a boundary. Pass ``action_ids``
    (the log's real ActionEvent ids) so receipt-based boundaries require exact
    pairing; without it the observation still needs a non-empty action_id.
    """

    if _approved_plan_predicate_scope(event) is not None:
        return True
    if _trusted_mutation_receipt_outcome(event, action_ids=action_ids):
        return True
    if kind_of(event) != "message":
        return False
    if event.get("source") == "user":
        return True
    meta = event.get("meta")
    blocking = meta.get("blocking") if isinstance(meta, dict) else None
    return event.get("source") == "environment" and isinstance(blocking, str) and bool(blocking)


# A durable preview generation id, as minted by PreviewSession.projection_id.
_PREVIEW_GENERATION_RE = re.compile(r"^pv_[0-9a-f]{32}$")


def preview_generation_of(event: dict[str, Any]) -> str | None:
    """The preview generation a successful, typed preview receipt establishes.

    Keyed on the RECEIPT SHAPE, not on a tool name: a host-authored preview
    projection carries both a `generation` (`pv_<32 hex>`) and the matching
    `projection_id`. Prose mentioning a generation cannot forge that pair.
    """
    if kind_of(event) != KIND_OBSERVATION:
        return None
    result = event.get("tool_result") or {}
    if result.get("success") is not True:
        return None
    structured = result.get("structured")
    if not isinstance(structured, dict):
        return None
    generation = structured.get("generation")
    if not isinstance(generation, str) or _PREVIEW_GENERATION_RE.fullmatch(generation) is None:
        return None
    if structured.get("projection_id") != generation:
        return None
    return generation


class ProgressEpochs:
    """Stateful progress-epoch boundaries: typed events AND authority replacement.

    :func:`progress_epoch_boundary` is stateless, so it can recognize a boundary
    that one event carries on its own but not one that only exists as a CHANGE
    between two events. A preview generation replacement is the latter kind.

    Counted-promotion failure 2026-07-27 (`p4_ff_node_pause` seed 400025): two
    browser failures separated by a `preview_stop`, a replacement `preview_start`
    (`pv_ad5f…` → `pv_c1b9…`), a successful `verify_web_app` and a curl proving
    the new service were still grouped into one epoch and adjudicated
    TOOL_ERROR_THRASH. Replacing the serving authority IS progress.

    An idempotent restart that returns the SAME generation is deliberately NOT a
    boundary — otherwise re-calling `preview_start` would launder any repeat.
    """

    def __init__(self) -> None:
        self._preview_generation: str | None = None

    def crosses(self, event: dict[str, Any], *, action_ids: frozenset[str] | None = None) -> bool:
        generation = preview_generation_of(event)
        if generation is not None:
            previous, self._preview_generation = self._preview_generation, generation
            # The FIRST generation establishes authority; it replaces nothing.
            if previous is not None and previous != generation:
                return True
        return progress_epoch_boundary(event, action_ids=action_ids)


def _action_outcomes(events: list[dict[str, Any]]) -> dict[str, tuple[bool, str, str]]:
    outcomes: dict[str, tuple[bool, str, str]] = {}
    for event in events:
        kind = kind_of(event)
        action_id = str(event.get("action_id") or "")
        if not action_id:
            continue
        if kind == KIND_OBSERVATION:
            result = event.get("tool_result") or {}
            success = result.get("success") is True
            content = str(result.get("content") or "")
            error = str(result.get("error") or ("" if success else content))
            outcomes[action_id] = (
                success,
                _error_signature(error),
                "" if success else _detail_signature(error, content),
            )
        elif kind == KIND_AGENT_ERROR:
            error = str(event.get("error") or "")
            outcomes[action_id] = (
                False,
                _error_signature(error),
                _detail_signature(error, str(event.get("detail") or "")),
            )
    return outcomes


def _approved_plan_predicate_scope(event: dict[str, Any]) -> tuple[str, ...] | None:
    """Return one trusted approved plan-verifier authority scope.

    H368 makes model-authored verification conditions revision-scoped.  Only the
    server-authored approval transition can replace that scope.  Plan prose, a
    malformed transition, or reapproval with the same predicate fingerprints does
    not create a new identity.
    """

    if (
        kind_of(event) != KIND_STATUS
        or event.get("source") != "system"
        or event.get("detail") != "plan_approved"
    ):
        return None
    transition = event.get("plan_verification_transition")
    if not isinstance(transition, dict):
        return None
    revision = transition.get("new_plan_revision")
    plan_event_id = transition.get("new_plan_event_id")
    fingerprints = transition.get("new_predicate_fingerprints")
    if (
        transition.get("new_authority") != "plan"
        or transition.get("reason") not in {"approved_initial_plan", "approved_plan_revision"}
        or not isinstance(revision, int)
        or isinstance(revision, bool)
        or revision < 1
        or not isinstance(plan_event_id, str)
        or not plan_event_id
        or not isinstance(fingerprints, list)
        or any(not isinstance(item, str) or not item for item in fingerprints)
    ):
        return None
    return tuple(sorted(fingerprints))


def _longest_identical_streak(
    events: list[dict[str, Any]], *, action_ids: frozenset[str]
) -> tuple[int, str, list[int]]:
    """Return the longest exact-action streak within one trusted progress epoch.

    A typed blocking obligation or a genuinely changed approved plan begins a
    new causal unit of work. Repeating a read after either boundary is fresh
    recovery/execution, not a third attempt in the earlier no-progress streak.
    Plain prose and same-predicate reapproval remain inert.
    """

    best_count, best_fp, best_seqs = 0, "", []
    current_fp, current_seqs = "", []
    approved_scope: tuple[str, ...] | None = None
    epochs = ProgressEpochs()
    for event in events:
        next_scope = _approved_plan_predicate_scope(event)
        if next_scope is not None:
            if approved_scope != next_scope:
                current_fp, current_seqs = "", []
                approved_scope = next_scope
            continue
        if epochs.crosses(event, action_ids=action_ids):
            current_fp, current_seqs = "", []
            continue
        if kind_of(event) != KIND_ACTION:
            continue
        fp = _fingerprint(event)
        if fp == current_fp:
            current_seqs.append(seq_of(event))
        else:
            current_fp, current_seqs = fp, [seq_of(event)]
        if len(current_seqs) > best_count:
            best_count, best_fp, best_seqs = len(current_seqs), fp, list(current_seqs)
    return best_count, best_fp, best_seqs


def _shell_segments(command: str) -> list[tuple[list[str], str]]:
    """Split a shell command into ``(argv, terminator)`` pairs.

    Preserving the exact terminator is evidence-critical: ``python check.py`` is a
    foreground verification, while ``python server.py &`` starts a background
    lifecycle process. ``2>&1`` is redirection, not a background terminator.
    Invalid/incomplete shell is deliberately opaque: tool-error accounting handles
    it, while semantic-repeat accounting skips it.
    """

    # shlex intentionally removes quotes, so a quoted literal '&' otherwise becomes
    # indistinguishable from the background operator token. Mask punctuation while it
    # is quoted/escaped, let shlex parse real control edges, then restore argv bytes.
    masked: list[str] = []
    quote = ""
    escaped = False
    for index, char in enumerate(command):
        if escaped:
            masked.append(char.translate(_QUOTED_PUNCTUATION_MASK))
            escaped = False
            continue
        if char == "\\" and quote != "'":
            masked.append(char)
            escaped = True
            continue
        if quote:
            masked.append(char.translate(_QUOTED_PUNCTUATION_MASK))
            if char == quote:
                quote = ""
            continue
        if char == "#" and (
            index == 0 or command[index - 1].isspace() or command[index - 1] in ";|&()"
        ):
            break
        masked.append(char)
        if char in ("'", '"'):
            quote = char
    try:
        lexer = shlex.shlex("".join(masked), posix=True, punctuation_chars=_SHELL_PUNCTUATION)
        lexer.whitespace_split = True
        lexer.commenters = ""
        tokens = list(lexer)
    except ValueError:
        return []
    segments: list[tuple[list[str], str]] = []
    current: list[str] = []
    for token in tokens:
        if token in _SHELL_CONTROL_OPERATORS:
            if current:
                segments.append((current, token))
                current = []
            continue
        current.append(token.translate(_QUOTED_PUNCTUATION_UNMASK))
    if current:
        segments.append((current, ""))
    return segments


def _static_tee_sinks(segment: list[str], cwd: str) -> tuple[str, ...]:
    """Return proven static file operands for one terminal ``tee`` stage.

    The shell action reports only the complete command's final exit status.  Callers
    must therefore invoke this helper only for a ``tee`` that is both the final
    pipeline stage and the final shell segment, so later control flow cannot hide
    its failure.
    Unknown options and shell/redirection syntax deliberately return no identity:
    an unproven side effect must never become a thrash bypass.
    """

    if not segment or posixpath.basename(segment[0]).lower() != "tee":
        return ()
    args = segment[1:]
    if any(token and set(token) <= set("<>") for token in args):
        return ()
    operands: list[str] = []
    options_done = False
    for token in args:
        if not options_done and token == "--":
            options_done = True
            continue
        if not options_done and token.startswith("--"):
            if token not in {"--append", "--ignore-interrupts"}:
                return ()
            continue
        if not options_done and token.startswith("-") and token != "-":
            if not re.fullmatch(r"-[ai]+", token):
                return ()
            continue
        operands.append(token)

    sinks: set[str] = set()
    for operand in operands:
        if operand == "-" or _STATIC_TEE_SINK_UNSAFE.search(operand):
            continue
        sink = posixpath.normpath(
            operand if operand.startswith("/") else posixpath.join(cwd, operand)
        )
        if sink == "/" or any(
            sink == root or sink.startswith(root + "/") for root in _STATIC_TEE_SINK_VIRTUAL_ROOTS
        ):
            continue
        sinks.add(sink)
    return tuple(sorted(sinks))


def _downstream_static_tee_sinks(
    segments: list[tuple[list[str], str]], script_index: int, cwd: str
) -> tuple[str, ...]:
    """Bind a direct script run to a trustworthy downstream output artifact."""

    index = script_index
    pipeline: list[tuple[list[str], str]] = []
    while index < len(segments) - 1 and segments[index][1] in _SHELL_PIPE_OPERATORS:
        index += 1
        pipeline.append(segments[index])
    if not pipeline:
        return ()
    final_segment, final_terminator = pipeline[-1]
    if index != len(segments) - 1 or final_terminator != "":
        return ()
    return _static_tee_sinks(final_segment, cwd)


def _direct_script_invocations(command: str) -> list[tuple[str, str, bool]]:
    """Return conservative direct-script invocations with lifecycle role.

    This intentionally recognizes only an interpreter directly executing a
    script with an explicit extension.  Module runners (``python -m pytest``),
    inline programs, package managers, and arbitrary binaries remain opaque;
    equating those would need dependency/intent knowledge and risks false FAILs.
    Wrapper commands such as ``cd``, ``ls``, ``echo``, or a trailing pipeline do
    not hide the direct invocation.
    """

    segments = _shell_segments(command)
    cwd = "/workspace"
    invocations: list[tuple[str, str, bool]] = []
    for index, (segment, terminator) in enumerate(segments):
        if len(segment) == 2 and segment[0] == "cd" and terminator != "&":
            destination = segment[1]
            cwd = posixpath.normpath(
                destination if destination.startswith("/") else posixpath.join(cwd, destination)
            )
            continue
        executable = posixpath.basename(segment[0]).lower() if segment else ""
        for pattern, family, suffixes in _DIRECT_SCRIPT_RUNNERS:
            if not pattern.fullmatch(executable):
                continue
            # Flags may alter interpreter semantics or consume their own values;
            # do not guess where the script operand begins.
            if len(segment) < 2 or segment[1].startswith("-"):
                break
            script_arg = segment[1]
            suffix = posixpath.splitext(script_arg)[1].lower()
            if suffix not in suffixes:
                break
            script_path = posixpath.normpath(
                script_arg if script_arg.startswith("/") else posixpath.join(cwd, script_arg)
            )
            fingerprint_parts: list[Any] = [family, script_path, segment[2:]]
            # ``shlex`` treats a literal newline as whitespace, while the shell
            # treats an unquoted newline as a command separator.  Without retaining
            # quote provenance we cannot prove which meaning applied, so multiline
            # commands never receive side-effect identity.
            sinks = (
                ()
                if "\n" in command or "\r" in command
                else _downstream_static_tee_sinks(segments, index, cwd)
            )
            if sinks:
                fingerprint_parts.append({"tee_sinks": list(sinks)})
            fingerprint = json.dumps(fingerprint_parts, separators=(",", ":"), ensure_ascii=True)
            invocations.append((fingerprint, family, terminator == "&"))
            break
    return invocations


def _is_runtime_cleanup_action(
    action: dict[str, Any], outcomes: dict[str, tuple[bool, str, str]]
) -> bool:
    """Whether a successful model action deliberately changed process/listener state."""

    success, _, _ = outcomes.get(str(action_id_of(action)), (False, "missing outcome", ""))
    if not success:
        return False
    tool = tool_name_of(action)
    if tool in _RUNTIME_CLEANUP_TOOLS:
        return True
    if tool not in _SHELL_TOOLS:
        return False
    args = (action.get("tool_call") or {}).get("arguments") or {}
    command = args.get("command", args.get("cmd"))
    if not isinstance(command, str):
        return False
    for segment, _terminator in _shell_segments(command):
        executable = posixpath.basename(segment[0]).lower() if segment else ""
        if executable in _RUNTIME_CLEANUP_COMMANDS:
            return True
        if executable == "fuser" and "-k" in segment:
            return True
    return False


def _explicit_mutation_path(action: dict[str, Any]) -> str | None:
    if tool_name_of(action) not in _FILE_MUTATION_TOOLS:
        return None
    args = (action.get("tool_call") or {}).get("arguments") or {}
    for key in ("path", "file_path"):
        raw = args.get(key)
        if isinstance(raw, str) and raw.strip():
            return posixpath.normpath(
                raw if raw.startswith("/") else posixpath.join("/workspace", raw)
            )
    return None


def _largest_semantic_shell_repeat_group(
    events: list[dict[str, Any]],
    outcomes: dict[str, tuple[bool, str, str]],
) -> tuple[int, str, list[int]]:
    """Find repeated successful direct-script executions in one verifier scope.

    Counts are non-contiguous because adding ``echo $?`` or surrounding a command
    with ``ls`` must not erase the repeated core execution.  Product-generated
    completion probes carry ``meta.verify_probe`` and are excluded: they are
    independent verification, not a model retry.  A successful structured source
    edit for the runner family starts a new generation.  An approved replacement
    plan predicate set also starts a new authority scope because the prior H368
    plan-owned conditions are no longer enforced.  Same-predicate reapproval does
    not reset the bound.
    """

    generations: Counter[str] = Counter()
    plan_scope = ("<no-approved-plan>",)
    groups: dict[tuple[str, int, tuple[str, ...]], list[int]] = {}
    for action in events:
        approved_scope = _approved_plan_predicate_scope(action)
        if approved_scope is not None:
            plan_scope = approved_scope
            continue
        if kind_of(action) != KIND_ACTION:
            continue
        mutation_path = _explicit_mutation_path(action)
        mutation_succeeded, _, _ = outcomes.get(
            str(action_id_of(action)), (False, "missing outcome", "")
        )
        if mutation_path is not None and mutation_succeeded:
            family = _SOURCE_SUFFIX_FAMILY.get(posixpath.splitext(mutation_path)[1].lower())
            if family is not None:
                generations[family] += 1
        if tool_name_of(action) not in _SHELL_TOOLS or (action.get("meta") or {}).get(
            "verify_probe"
        ):
            continue
        success, _, _ = outcomes.get(str(action_id_of(action)), (False, "missing outcome", ""))
        if not success:
            continue
        args = (action.get("tool_call") or {}).get("arguments") or {}
        command = args.get("command", args.get("cmd"))
        if not isinstance(command, str):
            continue
        # One action may mention the same invocation more than once; count model
        # decisions/actions, not textual duplication inside a single shell string.
        for fingerprint, family, background in sorted(set(_direct_script_invocations(command))):
            if background:
                continue
            groups.setdefault((fingerprint, generations[family], plan_scope), []).append(
                seq_of(action)
            )
    if not groups:
        return 0, "", []
    (fingerprint, _generation, _plan_scope), seqs = max(
        groups.items(), key=lambda item: (len(item[1]), item[1])
    )
    return len(seqs), fingerprint, seqs


def _largest_background_script_restart_group(
    actions: list[dict[str, Any]], outcomes: dict[str, tuple[bool, str, str]]
) -> tuple[int, str, list[int], list[int], int]:
    """Find repeated background script starts with one bounded recovery credit.

    Background lifecycle starts are not foreground verification. They still need a
    strict no-thrash bound: the model gets at most one extra attempt when durable
    evidence shows a successful process/listener cleanup between the first and last
    start. Repeating kill/start cycles cannot reset the allowance indefinitely.
    """

    generations: Counter[str] = Counter()
    groups: dict[tuple[str, int], list[int]] = {}
    cleanup_seqs: list[int] = []
    for action in actions:
        mutation_path = _explicit_mutation_path(action)
        mutation_succeeded, _, _ = outcomes.get(
            str(action_id_of(action)), (False, "missing outcome", "")
        )
        if mutation_path is not None and mutation_succeeded:
            family = _SOURCE_SUFFIX_FAMILY.get(posixpath.splitext(mutation_path)[1].lower())
            if family is not None:
                generations[family] += 1
        if _is_runtime_cleanup_action(action, outcomes):
            cleanup_seqs.append(seq_of(action))
        if tool_name_of(action) not in _SHELL_TOOLS or (action.get("meta") or {}).get(
            "verify_probe"
        ):
            continue
        args = (action.get("tool_call") or {}).get("arguments") or {}
        command = args.get("command", args.get("cmd"))
        if not isinstance(command, str):
            continue
        for fingerprint, family, background in sorted(set(_direct_script_invocations(command))):
            if background:
                groups.setdefault((fingerprint, generations[family]), []).append(seq_of(action))
    if not groups:
        return 0, "", [], [], 0
    (fingerprint, _generation), start_seqs = max(
        groups.items(), key=lambda item: (len(item[1]), item[1])
    )
    qualifying_cleanup = [seq for seq in cleanup_seqs if start_seqs[0] < seq <= start_seqs[-1]]
    cleanup_credit = 1 if qualifying_cleanup else 0
    return len(start_seqs), fingerprint, start_seqs, qualifying_cleanup, cleanup_credit


class ThrashOracle:
    def check(
        self,
        events: list[dict[str, Any]],
        *,
        scenario: dict[str, Any] | None = None,
        inspect_trace: dict[str, Any] | None = None,
    ) -> list[OracleResult]:
        try:
            limits = _limits(scenario)
        except ValueError as exc:
            return [
                failing(
                    _ORACLE,
                    fc.SCENARIO_CONTRACT_UNSATISFIABLE,
                    first_broken_link="scenario_contract -> thrash_thresholds",
                    facts={"reason": str(exc)},
                )
            ]
        if limits is None:
            return [skipping(_ORACLE, reason="scenario has no assertions.thrash policy")]

        actions = [event for event in events if kind_of(event) == KIND_ACTION]
        outcomes = _action_outcomes(events)
        thrash_markers = [
            event
            for event in events
            if kind_of(event) == KIND_STATUS
            and str(event.get("status")) == "STUCK"
            and str(event.get("detail") or "") in _THRASH_STUCK_DETAILS
        ]
        recovered_marker_seqs = [
            seq_of(event) for event in thrash_markers if _recovered_blocked_marker(event, events)
        ]
        terminal_markers = [
            {"seq": seq_of(event), "detail": str(event.get("detail") or "")}
            for event in thrash_markers
            if seq_of(event) not in recovered_marker_seqs
        ]
        if terminal_markers:
            return [
                failing(
                    _ORACLE,
                    fc.TOOL_CALL_THRASH,
                    first_broken_link="model_turns -> bounded_stuck_valve",
                    facts={"terminal_markers": terminal_markers, "action_count": len(actions)},
                )
            ]

        actionless = [
            seq_of(event)
            for event in events
            if kind_of(event) == KIND_STATUS
            and str(event.get("status")) == "PAUSED"
            and str(event.get("detail") or "") == "actionless"
        ]
        if len(actionless) > limits["max_actionless_pauses"]:
            return [
                failing(
                    _ORACLE,
                    fc.ACTIONLESS_THRASH,
                    first_broken_link="model_turns -> actionless_pause",
                    facts={
                        "count": len(actionless),
                        "allowed": limits["max_actionless_pauses"],
                        "seqs": actionless,
                    },
                )
            ]

        # See `repair_spans` for what counts as a repair and why it lives outside
        # this oracle (the efficiency readout must not restate the rule).
        repairs = repair_spans((inspect_trace or {}).get("spans"))
        repair_counts = Counter(str(span["repair_kind"]) for span in repairs)
        # Every `_record_model_repair` call site passes a 1-based `attempt` taken from a
        # counter initialised inside `drive_step` — so `attempt` is escalation depth
        # WITHIN one drive's repair budget, and a fresh drive legitimately starts over at
        # 1. Counting lifetime occurrences instead made the oracle contradict the product
        # it grades: a scenario with a followup has two drives, each entitled to its one
        # repair, and two honest first attempts were being reported as one repeated
        # behaviour. That is the same correction k6g F2 made for tool errors, which
        # already treats a user turn as a progress boundary.
        #
        # Fail closed on spans that carry no usable attempt: they cannot be attributed to
        # a budget, so they are counted the old way rather than waved through.
        deepest: dict[str, int] = {}
        unattributed: Counter[str] = Counter()
        for span in repairs:
            kind = str(span["repair_kind"])
            raw = span.get("attempt")
            depth = raw if isinstance(raw, int) and not isinstance(raw, bool) and raw >= 1 else 0
            if depth:
                deepest[kind] = max(deepest.get(kind, 0), depth)
            else:
                unattributed[kind] += 1
        escalation = {
            kind: max(deepest.get(kind, 0), unattributed[kind])
            for kind in set(deepest) | set(unattributed)
        }
        if escalation:
            repair_kind, repair_count = max(escalation.items(), key=lambda item: item[1])
            if repair_count > limits["max_same_model_repair_repeats"]:
                return [
                    failing(
                        _ORACLE,
                        fc.MODEL_REPAIR_THRASH,
                        first_broken_link="model_request -> repeated_hidden_repair",
                        facts={
                            "repair_kind": repair_kind,
                            "count": repair_count,
                            "occurrences": repair_counts[repair_kind],
                            "allowed": limits["max_same_model_repair_repeats"],
                            "repairs": repairs,
                        },
                    )
                ]
        if len(repairs) > limits["max_total_model_repairs"]:
            return [
                failing(
                    _ORACLE,
                    fc.MODEL_REPAIR_THRASH,
                    first_broken_link="model_request -> excessive_hidden_repairs",
                    facts={
                        "count": len(repairs),
                        "allowed": limits["max_total_model_repairs"],
                        "repair_counts": dict(repair_counts),
                        "repairs": repairs,
                    },
                )
            ]

        # k6g F2: identical error signatures accumulate only WITHIN one progress
        # epoch. Trusted progress between two occurrences (a receipt-backed
        # mutation, an approved plan transition, a user turn, a typed blocking
        # obligation) makes them independent recoverable errors — the seq-428/
        # seq-514 pair must never again be grouped as one repeated behavior.
        # A true adjacent no-progress repeat still crosses the threshold.
        epoch = 0
        epoch_by_index: dict[int, int] = {}
        known_action_ids = frozenset(
            str(action_id_of(event))
            for event in events
            if kind_of(event) == KIND_ACTION and action_id_of(event)
        )
        epochs = ProgressEpochs()
        for index, event in enumerate(events):
            if epochs.crosses(event, action_ids=known_action_ids):
                epoch += 1
            epoch_by_index[index] = epoch
        action_epochs = {
            str(action_id_of(event)): epoch_by_index[index]
            for index, event in enumerate(events)
            if kind_of(event) == KIND_ACTION
        }
        failed_signatures: Counter[tuple[str, str, str, str, int]] = Counter()
        failure_seqs: dict[tuple[str, str, str, str, int], list[int]] = {}
        for action in actions:
            action_id = action_id_of(action)
            success, signature, detail_signature = outcomes.get(
                str(action_id), (False, "missing outcome", "")
            )
            if success:
                continue
            key = (
                tool_name_of(action) or "?",
                signature,
                detail_signature,
                _failed_action_signature(action, signature),
                action_epochs.get(str(action_id), 0),
            )
            failed_signatures[key] += 1
            failure_seqs.setdefault(key, []).append(seq_of(action))
        if failed_signatures:
            (tool, signature, detail_signature, action_signature, failure_epoch), count = (
                failed_signatures.most_common(1)[0]
            )
            if count > limits["max_same_tool_error_repeats"]:
                return [
                    failing(
                        _ORACLE,
                        fc.TOOL_ERROR_THRASH,
                        first_broken_link="tool_error -> repeated_same_tool_error",
                        facts={
                            "tool": tool,
                            "error_signature": signature,
                            "detail_signature": detail_signature or None,
                            "action_signature": action_signature or None,
                            "count": count,
                            "allowed": limits["max_same_tool_error_repeats"],
                            "progress_epoch": failure_epoch,
                            "action_seqs": failure_seqs[
                                (
                                    tool,
                                    signature,
                                    detail_signature,
                                    action_signature,
                                    failure_epoch,
                                )
                            ],
                        },
                    )
                ]

        streak, fingerprint, streak_seqs = _longest_identical_streak(
            events, action_ids=known_action_ids
        )
        if streak > limits["max_identical_action_repeats"]:
            return [
                failing(
                    _ORACLE,
                    fc.TOOL_CALL_THRASH,
                    first_broken_link="tool_call -> identical_tool_call_streak",
                    facts={
                        "fingerprint": fingerprint,
                        "count": streak,
                        "allowed": limits["max_identical_action_repeats"],
                        "action_seqs": streak_seqs,
                    },
                )
            ]

        semantic_count, semantic_fingerprint, semantic_seqs = _largest_semantic_shell_repeat_group(
            events, outcomes
        )
        if semantic_count > limits["max_identical_action_repeats"]:
            return [
                failing(
                    _ORACLE,
                    fc.TOOL_CALL_THRASH,
                    first_broken_link="tool_call -> repeated_semantic_shell_verification",
                    facts={
                        "fingerprint": semantic_fingerprint,
                        "count": semantic_count,
                        "allowed": limits["max_identical_action_repeats"],
                        "action_seqs": semantic_seqs,
                    },
                )
            ]

        (
            background_count,
            background_fingerprint,
            background_seqs,
            cleanup_seqs,
            cleanup_credit,
        ) = _largest_background_script_restart_group(actions, outcomes)
        background_allowed = limits["max_identical_action_repeats"] + cleanup_credit
        if background_count > background_allowed:
            return [
                failing(
                    _ORACLE,
                    fc.TOOL_CALL_THRASH,
                    first_broken_link="tool_call -> repeated_background_script_restart",
                    facts={
                        "fingerprint": background_fingerprint,
                        "count": background_count,
                        "allowed": background_allowed,
                        "base_allowed": limits["max_identical_action_repeats"],
                        "cleanup_credit": cleanup_credit,
                        "action_seqs": background_seqs,
                        "cleanup_seqs": cleanup_seqs,
                    },
                )
            ]

        return [
            passing(
                _ORACLE,
                facts={
                    "action_count": len(actions),
                    "actionless_pauses": len(actionless),
                    "longest_identical_action_streak": streak,
                    "largest_semantic_shell_repeat_group": semantic_count,
                    "largest_background_script_restart_group": background_count,
                    "background_script_cleanup_credit": cleanup_credit,
                    "largest_same_tool_error_group": max(failed_signatures.values(), default=0),
                    "model_repair_count": len(repairs),
                    "model_repair_counts": dict(repair_counts),
                    "recovered_blocked_marker_seqs": recovered_marker_seqs,
                    "limits": limits,
                },
            )
        ]
