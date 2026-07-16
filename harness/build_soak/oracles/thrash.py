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
    KIND_OBSERVATION,
    KIND_STATUS,
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


def _longest_identical_streak(actions: list[dict[str, Any]]) -> tuple[int, str, list[int]]:
    best_count, best_fp, best_seqs = 0, "", []
    current_fp, current_seqs = "", []
    for action in actions:
        fp = _fingerprint(action)
        if fp == current_fp:
            current_seqs.append(seq_of(action))
        else:
            current_fp, current_seqs = fp, [seq_of(action)]
        if len(current_seqs) > best_count:
            best_count, best_fp, best_seqs = len(current_seqs), fp, list(current_seqs)
    return best_count, best_fp, best_seqs


def _shell_segments(command: str) -> list[list[str]]:
    """Split a shell command conservatively at control operators.

    ``shlex`` with punctuation support handles both spaced and unspaced forms of
    ``&&``, ``;`` and pipelines.  Invalid/incomplete shell is deliberately opaque:
    tool-error accounting handles it, while semantic-repeat accounting skips it.
    """

    try:
        lexer = shlex.shlex(command, posix=True, punctuation_chars=";&|")
        lexer.whitespace_split = True
        lexer.commenters = ""
        tokens = list(lexer)
    except ValueError:
        return []
    segments: list[list[str]] = []
    current: list[str] = []
    for token in tokens:
        if token and set(token) <= {";", "&", "|"}:
            if current:
                segments.append(current)
                current = []
            continue
        current.append(token)
    if current:
        segments.append(current)
    return segments


def _direct_script_invocations(command: str) -> list[tuple[str, str]]:
    """Return conservative ``(fingerprint, runner_family)`` shell invocations.

    This intentionally recognizes only an interpreter directly executing a
    script with an explicit extension.  Module runners (``python -m pytest``),
    inline programs, package managers, and arbitrary binaries remain opaque;
    equating those would need dependency/intent knowledge and risks false FAILs.
    Wrapper commands such as ``cd``, ``ls``, ``echo``, or a trailing pipeline do
    not hide the direct invocation.
    """

    cwd = "/workspace"
    invocations: list[tuple[str, str]] = []
    for segment in _shell_segments(command):
        if len(segment) == 2 and segment[0] == "cd":
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
            fingerprint = json.dumps(
                [family, script_path, segment[2:]], separators=(",", ":"), ensure_ascii=True
            )
            invocations.append((fingerprint, family))
            break
    return invocations


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
    actions: list[dict[str, Any]], outcomes: dict[str, tuple[bool, str, str]]
) -> tuple[int, str, list[int]]:
    """Find repeated successful direct-script executions between source edits.

    Counts are non-contiguous because adding ``echo $?`` or surrounding a command
    with ``ls`` must not erase the repeated core execution.  Product-generated
    completion probes carry ``meta.verify_probe`` and are excluded: they are
    independent verification, not a model retry.  A successful structured source
    edit for the runner family starts a new generation; this covers dependencies,
    not merely the directly executed script.
    """

    generations: Counter[str] = Counter()
    groups: dict[tuple[str, int], list[int]] = {}
    for action in actions:
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
        for fingerprint, family in sorted(set(_direct_script_invocations(command))):
            groups.setdefault((fingerprint, generations[family]), []).append(seq_of(action))
    if not groups:
        return 0, "", []
    (fingerprint, _generation), seqs = max(groups.items(), key=lambda item: (len(item[1]), item[1]))
    return len(seqs), fingerprint, seqs


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
        terminal_markers = [
            {"seq": seq_of(event), "detail": str(event.get("detail") or "")}
            for event in events
            if kind_of(event) == KIND_STATUS
            and str(event.get("status")) == "STUCK"
            and str(event.get("detail") or "") in _THRASH_STUCK_DETAILS
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

        # Repairs happen before an ActionEvent is committed and were historically
        # invisible to the durable event log. DISCO_INSPECT now records a bounded,
        # redacted `agent.repair` point span for unknown-tool guesses, degenerate
        # turns, tool-history repairs, and provider request rejections.
        spans = (inspect_trace or {}).get("spans")
        repairs = (
            [
                span
                for span in spans or []
                if isinstance(span, dict)
                and span.get("span") == "agent.repair"
                and span.get("event") == "point"
                and str(span.get("repair_kind") or "")
            ]
            if isinstance(spans, list)
            else []
        )
        repair_counts = Counter(str(span["repair_kind"]) for span in repairs)
        if repair_counts:
            repair_kind, repair_count = repair_counts.most_common(1)[0]
            if repair_count > limits["max_same_model_repair_repeats"]:
                return [
                    failing(
                        _ORACLE,
                        fc.MODEL_REPAIR_THRASH,
                        first_broken_link="model_request -> repeated_hidden_repair",
                        facts={
                            "repair_kind": repair_kind,
                            "count": repair_count,
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

        failed_signatures: Counter[tuple[str, str, str, str]] = Counter()
        failure_seqs: dict[tuple[str, str, str, str], list[int]] = {}
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
            )
            failed_signatures[key] += 1
            failure_seqs.setdefault(key, []).append(seq_of(action))
        if failed_signatures:
            (tool, signature, detail_signature, action_signature), count = (
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
                            "action_seqs": failure_seqs[
                                (tool, signature, detail_signature, action_signature)
                            ],
                        },
                    )
                ]

        streak, fingerprint, streak_seqs = _longest_identical_streak(actions)
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
            actions, outcomes
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

        return [
            passing(
                _ORACLE,
                facts={
                    "action_count": len(actions),
                    "actionless_pauses": len(actionless),
                    "longest_identical_action_streak": streak,
                    "largest_semantic_shell_repeat_group": semantic_count,
                    "largest_same_tool_error_group": max(failed_signatures.values(), default=0),
                    "model_repair_count": len(repairs),
                    "model_repair_counts": dict(repair_counts),
                    "limits": limits,
                },
            )
        ]
