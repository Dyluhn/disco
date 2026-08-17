"""The single owner of script-run identity ("did the model run THIS script before?").

This is the sibling of `tool_fingerprint.py`, created for the same binding reason
(A6.3 §3.1) and for a class that rule was never applied to.

`tool_fingerprint` answers "is this the same *call*?" — tool name plus canonical
argument equality. `ThrashOracle` counts repetitions on that class, and the loop's
W-39 freshness memo computes the same class, so the two agree.

But `ThrashOracle` counts answered-question repetitions on a **second** class as
well: `repeated_semantic_shell_verification` groups shell calls that re-run the
same *script*, however the command around it is spelled. That class lived only in
the harness, so the loop could not compute it, and the memo could not fire on it.
`tool_fingerprint.py`'s docstring names that exact hazard —

    "a memo that fires on a different equivalence class than the oracle that
    grades the run is worse than no memo at all — the drift would be invisible
    until a canary fired on a class the memo had never seen"

— and 2026-08-06v is the canary. `diag_script_run` seed 97903 issued
`cd /workspace && python3 primes.py`, then
`cd /workspace && python3 primes.py | wc -l && python3 primes.py | tail -1`, then
`python3 primes.py | tee /tmp/primes.out | wc -l && tail -n 1 /tmp/primes.out`.
The oracle grouped all three under `["python","/workspace/primes.py",[]]` and
faulted the run; `tool_call_fingerprint` grouped none of them, so the product never
told the agent it already had the answer. F47.

So the normalization moves HERE, into the lowest layer, and both sides import it:

* the **loop**, deciding whether to hand back an answer the agent already has
  before a repeat executes (`loop/dedup.py`, W-39);
* the **harness** `ThrashOracle`, deciding after the fact whether a run thrashed
  (`development/harness/build_soak/oracles/_thrash_shell.py`).

It is never copied. The harness module keeps its public names as re-exports so
frozen artifacts stay greppable and its own call sites are unchanged.

Import-free by design (stdlib only), for the same reason `tool_fingerprint` is:
the harness depends on it without pulling product machinery into evidence
adjudication.

**Conservative by construction.** Every helper here returns "no identity" rather
than guessing: an unparseable command, an unrecognized runner, a flag where a
script path should be. A missed identity costs one un-handed-back answer; a wrong
identity would hand back the answer to a question the agent did not ask.
"""

from __future__ import annotations

import json
import posixpath
import re
import shlex
from typing import Any

# The shell-exec tools whose repeated run is a redundant verify. Both carry the
# `command` arg (ShellTool / ShellExecTool). `code_exec` is deliberately excluded:
# re-running code is more often a deliberate re-execution, and it uses a different
# arg key.
SHELL_TOOLS = frozenset({"shell", "shell_exec"})

_SHELL_CONTROL_OPERATORS = frozenset({";", "&", "&&", "|", "|&", "||"})
_SHELL_PIPE_OPERATORS = frozenset({"|", "|&"})
_SHELL_PUNCTUATION = ";&|<>"
_STATIC_TEE_SINK_UNSAFE = re.compile(r"[$`*?\[\]{}()~\r\n]")
_STATIC_TEE_SINK_VIRTUAL_ROOTS = ("/dev", "/proc", "/sys")
_QUOTED_PUNCTUATION_MASK = str.maketrans(
    {char: chr(0xE100 + index) for index, char in enumerate(_SHELL_PUNCTUATION)}
)
_QUOTED_PUNCTUATION_UNMASK = str.maketrans(
    {chr(0xE100 + index): char for index, char in enumerate(_SHELL_PUNCTUATION)}
)
_DIRECT_SCRIPT_RUNNERS: tuple[tuple[re.Pattern[str], str, frozenset[str]], ...] = (
    (re.compile(r"python(?:\d+(?:\.\d+)?)?\Z"), "python", frozenset({".py"})),
    (re.compile(r"node(?:js)?\Z"), "node", frozenset({".cjs", ".js", ".mjs"})),
    (re.compile(r"ruby\Z"), "ruby", frozenset({".rb"})),
    (re.compile(r"perl\Z"), "perl", frozenset({".pl", ".pm"})),
    (re.compile(r"(?:ba|da|k|z)?sh\Z"), "shell", frozenset({".sh"})),
)
SOURCE_SUFFIX_FAMILY = {
    suffix: family for _pattern, family, suffixes in _DIRECT_SCRIPT_RUNNERS for suffix in suffixes
}

# The workspace root every relative script path is resolved against, so
# `cd /workspace && python3 primes.py` and `python3 /workspace/primes.py` are one
# identity.
_DEFAULT_CWD = "/workspace"


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
    """Conservative direct-script invocations in one command, with lifecycle role.

    Each entry is ``(fingerprint, family, is_background)``. The fingerprint is the
    equivalence class both the oracle and the memo group on: runner family, the
    script path resolved against the command's own ``cd`` chain, the arguments
    after the script, and any proven static ``tee`` sink.
    """
    segments = shell_segments(command)
    cwd = _DEFAULT_CWD
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


def describe_script_fingerprint(fingerprint: str) -> str:
    """Render a script identity as the agent-facing phrase for it.

    ``["python","/workspace/primes.py",[]]`` becomes ``python /workspace/primes.py``.
    The rendering is what the freshness memo names when the repeat is spelled
    differently from the run it repeats, so the agent is told WHICH question it
    already asked rather than being shown a command it did not issue. It is also
    the memo's anti-spam key, so it must be stable for a given identity.
    """
    try:
        parts = json.loads(fingerprint)
    except (TypeError, ValueError):
        return fingerprint
    if not isinstance(parts, list) or len(parts) < 2:
        return fingerprint
    family, script_path = parts[0], parts[1]
    if not isinstance(family, str) or not isinstance(script_path, str):
        return fingerprint
    extra = parts[2] if len(parts) > 2 and isinstance(parts[2], list) else []
    tail = "".join(f" {token}" for token in extra if isinstance(token, str))
    return f"{family} {script_path}{tail}"


def foreground_script_fingerprints(command: str) -> frozenset[str]:
    """The FOREGROUND script identities in one command.

    Foreground only, because that is the class `ThrashOracle` counts as
    answered-question repetition (`largest_semantic_shell_repeat_group` skips
    background starts; those are a separate shape with its own cleanup credit).
    Returned as a set: one command can invoke the same script twice
    (`python3 p.py | wc -l && python3 p.py | tail -1`), and that is ONE identity,
    not two occurrences — the oracle dedupes the same way.
    """
    return frozenset(
        fingerprint
        for fingerprint, _family, background in direct_script_invocations(command)
        if not background
    )
