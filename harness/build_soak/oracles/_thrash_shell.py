"""Shell parsing and semantic/background repeat grouping for the thrash oracle."""

from __future__ import annotations

import json
import posixpath
import re
import shlex
from collections import Counter
from typing import Any

from ..events import KIND_ACTION, action_id_of, kind_of, seq_of, tool_name_of
from ._thrash_recovery import approved_plan_predicate_scope

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


def _mask_shell_command(command: str) -> str:
    masked: list[str] = []
    quote = ""
    escaped = False
    for index, char in enumerate(command):
        if escaped:
            masked.append(char.translate(_QUOTED_PUNCTUATION_MASK))
            escaped = False
        elif char == "\\" and quote != "'":
            masked.append(char)
            escaped = True
        elif quote:
            masked.append(char.translate(_QUOTED_PUNCTUATION_MASK))
            if char == quote:
                quote = ""
        elif char == "#" and (
            index == 0 or command[index - 1].isspace() or command[index - 1] in ";|&()"
        ):
            break
        else:
            masked.append(char)
            if char in ("'", '"'):
                quote = char
    return "".join(masked)


def _shell_tokens(command: str) -> list[str] | None:
    try:
        lexer = shlex.shlex(
            _mask_shell_command(command),
            posix=True,
            punctuation_chars=_SHELL_PUNCTUATION,
        )
        lexer.whitespace_split = True
        lexer.commenters = ""
        return list(lexer)
    except ValueError:
        return None


def _segments_from_tokens(tokens: list[str]) -> list[tuple[list[str], str]]:
    segments: list[tuple[list[str], str]] = []
    current: list[str] = []
    for token in tokens:
        if token in _SHELL_CONTROL_OPERATORS:
            if current:
                segments.append((current, token))
                current = []
        else:
            current.append(token.translate(_QUOTED_PUNCTUATION_UNMASK))
    if current:
        segments.append((current, ""))
    return segments


def shell_segments(command: str) -> list[tuple[list[str], str]]:
    """Split a shell command into ``(argv, terminator)`` pairs."""
    tokens = _shell_tokens(command)
    return [] if tokens is None else _segments_from_tokens(tokens)


def _validate_tee_options(args: list[str]) -> list[str] | None:
    operands: list[str] = []
    options_done = False
    for token in args:
        if not options_done and token == "--":
            options_done = True
        elif not options_done and token.startswith("--"):
            if token not in {"--append", "--ignore-interrupts"}:
                return None
        elif not options_done and token.startswith("-") and token != "-":
            if not re.fullmatch(r"-[ai]+", token):
                return None
        else:
            operands.append(token)
    return operands


def _safe_tee_sink(operand: str, cwd: str) -> str | None:
    if operand == "-" or _STATIC_TEE_SINK_UNSAFE.search(operand):
        return None
    sink = posixpath.normpath(operand if operand.startswith("/") else posixpath.join(cwd, operand))
    if sink == "/" or any(
        sink == root or sink.startswith(root + "/") for root in _STATIC_TEE_SINK_VIRTUAL_ROOTS
    ):
        return None
    return sink


def static_tee_sinks(segment: list[str], cwd: str) -> tuple[str, ...]:
    """Return proven static file operands for one terminal ``tee`` stage."""
    if not segment or posixpath.basename(segment[0]).lower() != "tee":
        return ()
    args = segment[1:]
    if any(token and set(token) <= set("<>") for token in args):
        return ()
    operands = _validate_tee_options(args)
    if operands is None:
        return ()
    return tuple(
        sorted(sink for operand in operands if (sink := _safe_tee_sink(operand, cwd)) is not None)
    )


def _downstream_static_tee_sinks(
    segments: list[tuple[list[str], str]], script_index: int, cwd: str
) -> tuple[str, ...]:
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
    return static_tee_sinks(final_segment, cwd)


def _script_identity(segment: list[str]) -> tuple[str, str] | None:
    if not segment:
        return None
    executable = posixpath.basename(segment[0]).lower()
    for pattern, family, suffixes in _DIRECT_SCRIPT_RUNNERS:
        if not pattern.fullmatch(executable):
            continue
        if len(segment) < 2 or segment[1].startswith("-"):
            return None
        if posixpath.splitext(segment[1])[1].lower() not in suffixes:
            return None
        return family, segment[1]
    return None


def _script_fingerprint(
    segment: list[str],
    family: str,
    script_arg: str,
    cwd: str,
    sinks: tuple[str, ...],
) -> str:
    script_path = posixpath.normpath(
        script_arg if script_arg.startswith("/") else posixpath.join(cwd, script_arg)
    )
    parts: list[Any] = [family, script_path, segment[2:]]
    if sinks:
        parts.append({"tee_sinks": list(sinks)})
    return json.dumps(parts, separators=(",", ":"), ensure_ascii=True)


def direct_script_invocations(command: str) -> list[tuple[str, str, bool]]:
    """Return conservative direct-script invocations with lifecycle role."""
    segments = shell_segments(command)
    cwd = "/workspace"
    invocations: list[tuple[str, str, bool]] = []
    for index, (segment, terminator) in enumerate(segments):
        if len(segment) == 2 and segment[0] == "cd" and terminator != "&":
            destination = segment[1]
            cwd = posixpath.normpath(
                destination if destination.startswith("/") else posixpath.join(cwd, destination)
            )
            continue
        identity = _script_identity(segment)
        if identity is None:
            continue
        family, script_arg = identity
        sinks = (
            ()
            if "\n" in command or "\r" in command
            else _downstream_static_tee_sinks(segments, index, cwd)
        )
        invocations.append(
            (
                _script_fingerprint(segment, family, script_arg, cwd, sinks),
                family,
                terminator == "&",
            )
        )
    return invocations


def _is_runtime_cleanup_action(
    action: dict[str, Any], outcomes: dict[str, tuple[bool, str, str]]
) -> bool:
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
    return any(
        (executable := posixpath.basename(segment[0]).lower() if segment else "")
        in _RUNTIME_CLEANUP_COMMANDS
        or (executable == "fuser" and "-k" in segment)
        for segment, _terminator in shell_segments(command)
    )


def _explicit_mutation_path(action: dict[str, Any]) -> str | None:
    if tool_name_of(action) not in _FILE_MUTATION_TOOLS:
        return None
    args = (action.get("tool_call") or {}).get("arguments") or {}
    return next(
        (
            posixpath.normpath(raw if raw.startswith("/") else posixpath.join("/workspace", raw))
            for key in ("path", "file_path")
            if isinstance((raw := args.get(key)), str) and raw.strip()
        ),
        None,
    )


def _update_generations(
    action: dict[str, Any],
    outcomes: dict[str, tuple[bool, str, str]],
    generations: Counter[str],
) -> None:
    mutation_path = _explicit_mutation_path(action)
    mutation_succeeded, _, _ = outcomes.get(
        str(action_id_of(action)), (False, "missing outcome", "")
    )
    if mutation_path is None or not mutation_succeeded:
        return
    family = _SOURCE_SUFFIX_FAMILY.get(posixpath.splitext(mutation_path)[1].lower())
    if family is not None:
        generations[family] += 1


def _script_command(action: dict[str, Any]) -> str | None:
    if tool_name_of(action) not in _SHELL_TOOLS:
        return None
    if (action.get("meta") or {}).get("verify_probe"):
        return None
    args = (action.get("tool_call") or {}).get("arguments") or {}
    command = args.get("command", args.get("cmd"))
    return command if isinstance(command, str) else None


def largest_semantic_shell_repeat_group(
    events: list[dict[str, Any]],
    outcomes: dict[str, tuple[bool, str, str]],
) -> tuple[int, str, list[int]]:
    """Find repeated successful direct-script executions in one verifier scope."""
    generations: Counter[str] = Counter()
    plan_scope = ("<no-approved-plan>",)
    groups: dict[tuple[str, int, tuple[str, ...]], list[int]] = {}
    for action in events:
        if (approved_scope := approved_plan_predicate_scope(action)) is not None:
            plan_scope = approved_scope
            continue
        if kind_of(action) != KIND_ACTION:
            continue
        _update_generations(action, outcomes, generations)
        command = _script_command(action)
        success, _, _ = outcomes.get(str(action_id_of(action)), (False, "missing outcome", ""))
        if command is None or not success:
            continue
        for fingerprint, family, background in sorted(set(direct_script_invocations(command))):
            if not background:
                groups.setdefault((fingerprint, generations[family], plan_scope), []).append(
                    seq_of(action)
                )
    if not groups:
        return 0, "", []
    (fingerprint, _generation, _plan_scope), seqs = max(
        groups.items(), key=lambda item: (len(item[1]), item[1])
    )
    return len(seqs), fingerprint, seqs


def largest_background_script_restart_group(
    actions: list[dict[str, Any]],
    outcomes: dict[str, tuple[bool, str, str]],
) -> tuple[int, str, list[int], list[int], int]:
    """Find repeated background script starts with one bounded recovery credit."""
    generations: Counter[str] = Counter()
    groups: dict[tuple[str, int], list[int]] = {}
    cleanup_seqs: list[int] = []
    for action in actions:
        _update_generations(action, outcomes, generations)
        if _is_runtime_cleanup_action(action, outcomes):
            cleanup_seqs.append(seq_of(action))
        command = _script_command(action)
        if command is None:
            continue
        for fingerprint, family, background in sorted(set(direct_script_invocations(command))):
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
