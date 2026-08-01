"""Pure marker/output parsing for `ShellSessionManager` — no tmux, no state.

`strip_output` peels a raw tmux pane delta down to the command's real output;
`completion_dispatch` wraps a command with an unguessable, line-anchored
completion record so the manager can tell "the private record is present"
apart from "the public PS1 string merely echoed in output"; `is_backgrounded`
detects a trailing, un-escaped, un-redirected shell `&`.
"""

from __future__ import annotations

import re
import shlex

_PS1_MARKER = "__DISCO_PS1__"
_PS1 = f"{_PS1_MARKER}$?__$ "
# Derive the parse regex from the marker constant so the WRITE (PS1) and READ
# (parse) sides can never drift — the exact bug a literal `__PMX_PS1__` here
# re-introduced after the marker was renamed.
_MARKER_RE = re.compile(re.escape(_PS1_MARKER) + r"(\d+)__\$\s*$")
_FRESH_MARKER_RE = re.compile(re.escape(_PS1_MARKER) + r"(\d+)__\$[ \t]*")
_DONE_MARKER = "__DISCO_DONE_"


def strip_output(
    delta: str,
    *,
    completion_re: re.Pattern[str] | None = None,
    echoed_dispatch: str | None = None,
) -> tuple[str, int | None]:
    if not delta:
        return "", None

    text = delta.lstrip("\n")
    if echoed_dispatch is not None and text.startswith(echoed_dispatch):
        text = text[len(echoed_dispatch) :].lstrip("\n")
    else:
        # Compatibility for captured panes produced before the private
        # wrapper (and for narrow fakes): discard one echoed command line.
        _echo, separator, remainder = text.partition("\n")
        text = remainder if separator else ""
    text = text.rstrip("\n")

    if not text:
        return "", None
    matches = list((completion_re or _FRESH_MARKER_RE).finditer(text))
    if not matches:
        return text, None
    marker = matches[-1]
    # Strip only the fresh prompt token. Preserve every byte written after it:
    # background-child stderr is the actionable evidence H322 previously hid
    # behind a false "still running" verdict.
    cleaned = text[: marker.start()] + text[marker.end() :]
    if completion_re is not None:
        # Bash prints its normal PS1 immediately after the private completion
        # record.  Strip that display token too, while preserving late stderr
        # emitted by a background child after the prompt.
        cleaned = _FRESH_MARKER_RE.sub("", cleaned, count=1)
    return cleaned.strip("\n"), int(marker.group(1))


def completion_dispatch(command: str, token: str) -> tuple[str, re.Pattern[str]]:
    """Wrap a command with an unguessable, line-anchored completion record.

    A public PS1 string is display, not proof: the command echo or arbitrary
    process output can contain it.  ``eval`` preserves the interactive shell's
    state-changing semantics while the private token is kept out of the marker
    literal in the echoed wrapper (it is supplied separately to ``printf``).
    """

    rc_name = f"__disco_rc_{token}"
    dispatched = (
        f"eval {shlex.quote(command)}; {rc_name}=$?; "
        f"printf '\\n{_DONE_MARKER}%s__%s__\\n' {shlex.quote(token)} \"${rc_name}\""
    )
    completion_re = re.compile(rf"(?m)^{re.escape(_DONE_MARKER + token)}__(\d+)__[ \t]*$")
    return dispatched, completion_re


def _is_comment_start(command: str, index: int, char: str) -> bool:
    if char != "#":
        return False
    return index == 0 or command[index - 1].isspace() or command[index - 1] in ";|&()"


def _is_and_or_redirect_context(command: str, index: int) -> bool:
    previous = command[index - 1] if index else ""
    following = command[index + 1] if index + 1 < len(command) else ""
    return previous in {"&", "<", ">", "|"} or following in {"&", ">", "|"}


def is_backgrounded(command: str) -> bool:
    """Whether clean shell syntax contains a single-ampersand background edge."""

    quote = ""
    escaped = False
    for index, char in enumerate(command):
        if escaped:
            escaped = False
            continue
        if char == "\\" and quote != "'":
            escaped = True
            continue
        if quote:
            if char == quote:
                quote = ""
            continue
        if char in ("'", '"'):
            quote = char
            continue
        if _is_comment_start(command, index, char):
            break
        if char != "&":
            continue
        if _is_and_or_redirect_context(command, index):
            continue
        return True
    return False
