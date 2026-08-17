"""Shell substitution parsing — split from ``security/analyzers.py``.

A bounded lexical recognizer for ``$()``, ``$()``, backtick, and ``${}``
substitutions.  It is never an evaluator and never a general shell parser: it
extracts immediate active substitution bodies without executing anything, so a
static analyzer can re-inspect command/process substitutions that execute
before their apparently-benign outer command.

Bounds keep adversarial nesting linear and deterministic; exceeding one fails
closed.
"""

from __future__ import annotations

import re
from enum import Enum, auto
from typing import Final, Literal

_SUBSTITUTION_MAX_DEPTH = 8
_SUBSTITUTION_MAX_COUNT = 32
_SUBSTITUTION_MAX_CHARS = 64 * 1024
_UNSAFE_HEREDOC = -2
_DOUBLE_QUOTE_ESCAPES = frozenset({"$", "`", '"', "\\", "\n"})

_ASSIGNMENT_RE = re.compile(r"[A-Za-z_]\w*=.*")


def backtick_end(command: str, start: int, *, max_chars: int) -> int | None:
    """Return the closing index for an active backtick starting at ``start``."""
    index = start + 1
    while index < len(command):
        if index - (start + 1) > max_chars:
            return -1
        if command[index : index + 2] == "<<":
            return _UNSAFE_HEREDOC
        if command[index] == "\\" and index + 1 < len(command):
            index += 2
            continue
        if command[index] == "`":
            return index
        index += 1
    return None


def decode_backtick_body(body: str) -> str:
    """Decode escapes Bash removes before parsing a legacy backtick body."""
    out: list[str] = []
    index = 0
    while index < len(body):
        if body[index] == "\\" and index + 1 < len(body):
            escaped = body[index + 1]
            if escaped == "\n":
                index += 2
                continue
            if escaped not in {"$", "`", "\\"}:
                out.append(body[index])
                index += 1
                continue
            out.append(escaped)
            index += 2
            continue
        out.append(body[index])
        index += 1
    return "".join(out).rstrip("\\")


def _is_assignment(token: str) -> bool:
    return _ASSIGNMENT_RE.fullmatch(token) is not None


# ---------------------------------------------------------------------------
# Case-statement word tracking (extracted from balanced_substitution_end)
# ---------------------------------------------------------------------------


class _CaseWordTracker:
    """Track reserved-word transitions inside ``$()`` for case/esac patterns.

    Each entry in ``case_stack`` is ``[phase, pattern_paren_depth, pattern_started]``,
    where phase is ``waiting_in``, ``pattern``, or ``body``.  This lets
    case-pattern ``)`` be distinguished from the surrounding substitution close
    in one pass, including escaped/quoted words or alternatives that merely
    spell ``esac``.
    """

    __slots__ = ("case_stack", "command_position", "word", "word_quoted")

    def __init__(self) -> None:
        self.case_stack: list[list[str | int]] = []
        self.command_position = True
        self.word: list[str] = []
        self.word_quoted = False

    def flush(self) -> None:
        if not self.word:
            return
        token = "".join(self.word)
        eligible = not self.word_quoted
        phase = str(self.case_stack[-1][0]) if self.case_stack else ""
        self._dispatch_token(token, eligible, phase)
        self.word.clear()
        self.word_quoted = False

    def _dispatch_token(self, token: str, eligible: bool, phase: str) -> None:
        if phase == "waiting_in":
            self._handle_waiting_in(token, eligible)
        elif phase == "pattern":
            self._handle_pattern(token, eligible)
        else:
            self._handle_command_position(token, eligible)

    def _handle_waiting_in(self, token: str, eligible: bool) -> None:
        if eligible and token == "in":
            self.case_stack[-1][0] = "pattern"
            self.case_stack[-1][1] = 0
            self.case_stack[-1][2] = 0

    def _handle_pattern(self, token: str, eligible: bool) -> None:
        if eligible and token == "esac" and not self.case_stack[-1][2]:
            self.case_stack.pop()
        else:
            self.case_stack[-1][2] = 1

    def _handle_command_position(self, token: str, eligible: bool) -> None:
        if self.command_position and eligible and token == "case":
            self.case_stack.append(["waiting_in", 0, 0])
            self.command_position = False
        elif self.command_position and eligible and token == "esac" and self.case_stack:
            self.case_stack.pop()
            self.command_position = False
        elif eligible and token in {"do", "elif", "else", "then"}:
            self.command_position = True
        elif self.command_position and not _is_assignment(token):
            self.command_position = False


# ---------------------------------------------------------------------------
# Nested substitution helpers (shared by parameter_expansion_end and
# balanced_substitution_end)
# ---------------------------------------------------------------------------


def _skip_nested_param(
    command: str, index: int, body_start: int, max_chars: int, syntax_depth: int
) -> int | None:
    """Skip a ``${...}`` at ``index``; return the index past its close or an error."""
    closing = parameter_expansion_end(
        command,
        index,
        max_chars=max_chars - (index - body_start),
        syntax_depth=syntax_depth + 1,
    )
    return closing


def _skip_nested_paren(
    command: str, index: int, body_start: int, max_chars: int, syntax_depth: int
) -> int | None:
    """Skip a ``$(...)`` at ``index``; return the index past its close or an error."""
    closing = balanced_substitution_end(
        command,
        index,
        max_chars=max_chars - (index - body_start),
        syntax_depth=syntax_depth + 1,
    )
    return closing


def _skip_backtick(
    command: str, index: int, body_start: int, max_chars: int
) -> int | None:
    """Skip a backtick at ``index``; return the index past its close or an error."""
    closing = backtick_end(
        command, index, max_chars=max_chars - (index - body_start)
    )
    return closing


# ---------------------------------------------------------------------------
# parameter_expansion_end — ${...}
# ---------------------------------------------------------------------------


def _param_double_quote_step(
    command: str, index: int, n: int
) -> int:
    """Handle one character inside a double quote in ``${...}``. Returns new index."""
    character = command[index]
    if character == "\\" and index + 1 < n:
        return index + 2
    return index + 1


def parameter_expansion_end(
    command: str,
    start: int,
    *,
    max_chars: int,
    syntax_depth: int,
) -> int | None:
    """Return the closing brace for an active, possibly nested ``${...}``."""
    if syntax_depth > _SUBSTITUTION_MAX_DEPTH:
        return -1
    depth = 1
    quote: str | None = None
    index = start + 2
    body_start = index
    while index < len(command):
        if index - body_start > max_chars:
            return -1
        character = command[index]
        if quote == "'":
            if character == "'":
                quote = None
            index += 1
            continue
        if quote == '"':
            if character == '"':
                quote = None
                index += 1
                continue
            index = _param_double_quote_step(command, index, len(command))
            continue
        if character == "\\" and index + 1 < len(command):
            index += 2
            continue
        if character in {"'", '"'}:
            quote = character
            index += 1
            continue
        closing = _param_nested_step(
            command, index, body_start, max_chars, syntax_depth, character, depth
        )
        if closing is _HEREDOC_SENTINEL:
            return _UNSAFE_HEREDOC
        if closing is _ERROR_SENTINEL:
            return -1
        if isinstance(closing, tuple):
            new_index, new_depth = closing
            if new_depth == 0:
                return new_index
            depth = new_depth
            index = new_index
            continue
        index += 1
    return None


class _SubstitutionSentinel(Enum):
    HEREDOC = auto()
    ERROR = auto()


type _HeredocSentinel = Literal[_SubstitutionSentinel.HEREDOC]
type _ErrorSentinel = Literal[_SubstitutionSentinel.ERROR]
type _SubstitutionStep = int | _HeredocSentinel | _ErrorSentinel

_HEREDOC_SENTINEL: Final[_HeredocSentinel] = _SubstitutionSentinel.HEREDOC
_ERROR_SENTINEL: Final[_ErrorSentinel] = _SubstitutionSentinel.ERROR


def _param_nested_step(
    command: str,
    index: int,
    body_start: int,
    max_chars: int,
    syntax_depth: int,
    character: str,
    depth: int,
) -> object:
    """Handle one non-quote character in ``${...}``. Returns a sentinel or (index, depth)."""
    if command[index : index + 2] == "${":
        return (index + 2, depth + 1)
    if command[index : index + 2] == "$(":
        closing = _skip_nested_paren(command, index, body_start, max_chars, syntax_depth)
        if closing is None or closing < 0:
            return _ERROR_SENTINEL
        return (closing + 1, depth)
    if character == "`":
        closing = _skip_backtick(command, index, body_start, max_chars)
        if closing is None or closing < 0:
            return _ERROR_SENTINEL
        return (closing + 1, depth)
    if command[index : index + 2] == "<<":
        return _HEREDOC_SENTINEL
    if character == "}":
        new_depth = depth - 1
        if new_depth == 0:
            return (index, 0)
        return (index + 1, new_depth)
    return None


# ---------------------------------------------------------------------------
# balanced_substitution_end — $(...) / <(...) / >(...)
# ---------------------------------------------------------------------------


def _dq_substitution_step(
    command: str,
    index: int,
    body_start: int,
    max_chars: int,
    syntax_depth: int,
    tracker: _CaseWordTracker,
) -> _SubstitutionStep:
    """Handle one character inside a double quote in ``$(...)``.

    Returns the new index, or a sentinel on error/heredoc.
    """
    n = len(command)
    character = command[index]
    if character == "\\" and index + 1 < n:
        if command[index + 1] in _DOUBLE_QUOTE_ESCAPES:
            tracker.word.append(command[index + 1])
            tracker.word_quoted = True
            return index + 2
        return index + 2
    if character == '"':
        return index + 1
    if command[index : index + 2] == "${":
        closing = _skip_nested_param(command, index, body_start, max_chars, syntax_depth)
        if closing is None or closing < 0:
            return _ERROR_SENTINEL
        tracker.word.append("parameter")
        tracker.word_quoted = True
        return closing + 1
    if command[index : index + 2] == "$(":
        closing = _skip_nested_paren(command, index, body_start, max_chars, syntax_depth)
        if closing is None or closing < 0:
            return _ERROR_SENTINEL
        tracker.word.append("substitution")
        tracker.word_quoted = True
        return closing + 1
    if character == "`":
        closing = _skip_backtick(command, index, body_start, max_chars)
        if closing is None or closing < 0:
            return _ERROR_SENTINEL
        tracker.word.append("substitution")
        tracker.word_quoted = True
        return closing + 1
    tracker.word.append(character)
    return index + 1


def _unquoted_substitution_step(
    command: str,
    index: int,
    body_start: int,
    max_chars: int,
    syntax_depth: int,
    tracker: _CaseWordTracker,
) -> _SubstitutionStep:
    """Handle one unquoted character in ``$(...)`` (not whitespace/operator/paren).

    Returns the new index, or a sentinel on error/heredoc.
    """
    character = command[index]
    if character == "\\" and index + 1 < len(command):
        tracker.word.append(command[index + 1])
        tracker.word_quoted = True
        return index + 2
    if character in {"'", '"'}:
        tracker.word_quoted = True
        return index + 1
    if character == "#" and not tracker.word:
        return _skip_comment_line(command, index, body_start, max_chars, tracker)
    if command[index : index + 2] == "<<":
        return _HEREDOC_SENTINEL
    nested_result = _unquoted_nested_step(
        command,
        index,
        body_start,
        max_chars,
        syntax_depth,
        tracker,
    )
    if nested_result is not None:
        return nested_result
    tracker.word.append(character)
    return index + 1


def _skip_comment_line(
    command: str, index: int, body_start: int, max_chars: int, tracker: _CaseWordTracker
) -> int | _ErrorSentinel:
    """Skip a ``#`` comment through newline; return the new index or error sentinel."""
    while index < len(command) and command[index] != "\n":
        if index - body_start > max_chars:
            return _ERROR_SENTINEL
        index += 1
    tracker.command_position = True
    return index


def _unquoted_nested_step(
    command: str,
    index: int,
    body_start: int,
    max_chars: int,
    syntax_depth: int,
    tracker: _CaseWordTracker,
) -> _SubstitutionStep | None:
    """Handle nested substitution/backtick in unquoted context; return None if not one."""
    character = command[index]
    if character == "`":
        closing = _skip_backtick(command, index, body_start, max_chars)
        if closing is None or closing < 0:
            return _ERROR_SENTINEL
        tracker.word.append("substitution")
        tracker.word_quoted = True
        return closing + 1
    if command[index : index + 2] == "${":
        closing = _skip_nested_param(command, index, body_start, max_chars, syntax_depth)
        if closing is None or closing < 0:
            return _ERROR_SENTINEL
        tracker.word.append("parameter")
        tracker.word_quoted = True
        return closing + 1
    return None


def balanced_substitution_end(
    command: str,
    start: int,
    *,
    max_chars: int,
    syntax_depth: int = 0,
) -> int | None:
    """Return the closing-paren index for ``$(`/``<(`/``>(`` at ``start``.

    Parentheses inside quotes/backticks do not balance the active substitution.
    Nested unquoted parentheses do.  This is a bounded lexical recognizer, never
    an evaluator and never a general shell parser.
    """
    if syntax_depth > _SUBSTITUTION_MAX_DEPTH:
        return -1
    scanner = _ParenScanner(command, start, max_chars, syntax_depth)
    return scanner.run()


class _ParenScanner:
    """Bounded lexical scanner for ``$(...)`` / ``<(...)`` / ``>(...)``.

    Encapsulates the depth, quote, and case-word tracking state that
    ``balanced_substitution_end`` needs across iterations.
    """

    __slots__ = ("command", "index", "body_start", "max_chars", "syntax_depth",
                 "depth", "quote", "tracker", "_n")

    def __init__(self, command: str, start: int, max_chars: int, syntax_depth: int) -> None:
        self.command = command
        self.index = start + 2
        self.body_start = self.index
        self.max_chars = max_chars
        self.syntax_depth = syntax_depth
        self.depth = 1
        self.quote: str | None = None
        self.tracker = _CaseWordTracker()
        self._n = len(command)

    def run(self) -> int | None:
        while self.index < self._n:
            if self.index - self.body_start > self.max_chars:
                return -1
            character = self.command[self.index]
            if self.quote == "'":
                self._sq_step(character)
                continue
            if self.quote == '"':
                result = self._dq_step()
                if result is not None:
                    return result
                continue
            result = self._unquoted_step(character)
            if result is not None:
                return result
        return None

    def _sq_step(self, character: str) -> None:
        if character == "'":
            self.quote = None
        else:
            self.tracker.word.append(character)
        self.index += 1

    def _dq_step(self) -> int | None:
        result = _dq_substitution_step(
            self.command, self.index, self.body_start, self.max_chars,
            self.syntax_depth, self.tracker,
        )
        if result is _HEREDOC_SENTINEL:
            return _UNSAFE_HEREDOC
        if result is _ERROR_SENTINEL:
            return -1
        if result == self.index + 1 and self.command[self.index] == '"':
            self.quote = None
        self.index = result
        return None

    def _unquoted_step(self, character: str) -> int | None:
        if character in {"'", '"'}:
            self.tracker.word_quoted = True
            self.quote = character
            self.index += 1
            return None
        if character.isspace():
            self.tracker.flush()
            if character == "\n":
                self.tracker.command_position = True
            self.index += 1
            return None
        if character in ";&|":
            self.index = _operator_step(self.command, self.index, self.tracker)
            return None
        if character == "(":
            self.tracker.flush()
            self._open_paren()
            self.index += 1
            return None
        if character == ")":
            self.tracker.flush()
            if self._close_paren():
                return self.index
            self.index += 1
            return None
        return self._unquoted_other(character)

    def _open_paren(self) -> None:
        if self.tracker.case_stack and str(self.tracker.case_stack[-1][0]) == "pattern":
            self.tracker.case_stack[-1][1] = int(self.tracker.case_stack[-1][1]) + 1
            self.tracker.case_stack[-1][2] = 1
        else:
            self.depth += 1

    def _close_paren(self) -> bool:
        """Handle ``)``; return True if this is the substitution close."""
        if self.tracker.case_stack and str(self.tracker.case_stack[-1][0]) == "pattern":
            pattern_depth = int(self.tracker.case_stack[-1][1])
            if pattern_depth > 0:
                self.tracker.case_stack[-1][1] = pattern_depth - 1
            else:
                self.tracker.case_stack[-1][0] = "body"
                self.tracker.command_position = True
            return False
        self.depth -= 1
        return self.depth == 0

    def _unquoted_other(self, character: str) -> int | None:
        result = _unquoted_substitution_step(
            self.command, self.index, self.body_start, self.max_chars,
            self.syntax_depth, self.tracker,
        )
        if result is _HEREDOC_SENTINEL:
            return _UNSAFE_HEREDOC
        if result is _ERROR_SENTINEL:
            return -1
        self.index = result
        return None


def _operator_step(command: str, index: int, tracker: _CaseWordTracker) -> int:
    """Handle a ``;&|`` operator run; return the new index."""
    tracker.flush()
    run_end = index + 1
    while run_end < len(command) and command[run_end] in ";&|":
        run_end += 1
    operator = command[index:run_end]
    if tracker.case_stack and str(tracker.case_stack[-1][0]) == "body" and ";" in operator:
        tracker.case_stack[-1][0] = "pattern"
        tracker.case_stack[-1][1] = 0
        tracker.case_stack[-1][2] = 0
    tracker.command_position = True
    return run_end


# ---------------------------------------------------------------------------
# active_shell_substitutions — top-level extraction
# ---------------------------------------------------------------------------


def active_shell_substitutions(
    command: str,
    *,
    budget: list[int],
) -> tuple[list[tuple[str, str]], str | None]:
    """Extract immediate active substitution bodies without executing anything.

    Single-quoted and escaped spellings are literals. ``$()`` and backticks stay
    active inside double quotes; Bash process substitution does not. Arithmetic
    ``$((...))`` is tagged separately so only substitutions nested *inside* its
    expression are treated as commands.
    """
    scanner = _ActiveSubstitutionScanner(command, budget)
    return scanner.run()


class _ActiveSubstitutionScanner:
    """Top-level scanner for active shell substitutions."""

    __slots__ = ("command", "budget", "found", "quote", "token_start", "index", "_n")

    def __init__(self, command: str, budget: list[int]) -> None:
        self.command = command
        self.budget = budget
        self.found: list[tuple[str, str]] = []
        self.quote: str | None = None
        self.token_start = True
        self.index = 0
        self._n = len(command)

    def run(self) -> tuple[list[tuple[str, str]], str | None]:
        while self.index < self._n:
            character = self.command[self.index]
            if self.quote == "'":
                if character == "'":
                    self.quote = None
                    self.token_start = False
                self.index += 1
                continue
            if self.quote == '"':
                err = self._dq_step()
                if err is not None:
                    return self.found, err
                continue
            err = self._unquoted_step(character)
            if err is not None:
                return self.found, err
        return self.found, None

    def _dq_step(self) -> str | None:
        """Handle one character inside a double quote; return error string or None."""
        character = self.command[self.index]
        if character == "\\" and self.index + 1 < self._n:
            if self.command[self.index + 1] in _DOUBLE_QUOTE_ESCAPES:
                self.index += 2
                return None
        if character == '"':
            self.quote = None
            self.token_start = False
            self.index += 1
            return None
        if self.command[self.index : self.index + 2] == "$(":
            kind, body, new_index, err = _consume_dollar_paren(
                self.command, self.index, self.budget
            )
            if err is not None:
                return err
            self.found.append((kind, body))
            self.index = new_index
            return None
        if character == "`":
            body, new_index, err = _consume_backtick(
                self.command, self.index, self.budget
            )
            if err is not None:
                return err
            self.found.append(("backtick", body))
            self.index = new_index
            return None
        self.index += 1
        return None

    def _unquoted_step(self, character: str) -> str | None:
        """Handle one unquoted character; return error string or None."""
        if character == "\\" and self.index + 1 < self._n:
            self.token_start = False
            self.index += 2
            return None
        if character in {"'", '"'}:
            self.quote = character
            self.token_start = False
            self.index += 1
            return None
        if character == "#" and self.token_start:
            newline = self.command.find("\n", self.index + 1)
            if newline < 0:
                return "done"
            self.index = newline + 1
            self.token_start = True
            return None
        marker = self.command[self.index : self.index + 2]
        if marker in {"$(", "<(", ">("}:
            return self._consume_marker(marker)
        if character == "`":
            return self._consume_unquoted_backtick()
        if character.isspace() or character in ";&|()":
            self.token_start = True
        else:
            self.token_start = False
        self.index += 1
        return None

    def _consume_marker(self, marker: str) -> str | None:
        kind, body, new_index, err = _consume_substitution_marker(
            self.command, self.index, marker, self.budget
        )
        if err is not None:
            return err
        self.found.append((kind, body))
        self.index = new_index
        self.token_start = False
        return None

    def _consume_unquoted_backtick(self) -> str | None:
        body, new_index, err = _consume_backtick(
            self.command, self.index, self.budget
        )
        if err is not None:
            return err
        self.found.append(("backtick", body))
        self.index = new_index
        self.token_start = False
        return None


def _budget_exceeded(budget: list[int]) -> str | None:
    if budget[0] >= _SUBSTITUTION_MAX_COUNT:
        return "nested shell substitution exceeds analysis bounds"
    return None


def _consume_dollar_paren(
    command: str, index: int, budget: list[int]
) -> tuple[str, str, int, str | None]:
    """Consume a ``$(...)`` inside double quotes; return (kind, body, new_index, err)."""
    err = _budget_exceeded(budget)
    if err is not None:
        return "", "", index, err
    closing = balanced_substitution_end(
        command, index, max_chars=_SUBSTITUTION_MAX_CHARS - budget[1]
    )
    if closing is None:
        return "", "", index, "unparseable nested shell substitution"
    if closing == _UNSAFE_HEREDOC:
        return "", "", index, "nested shell heredoc is not safely analyzable"
    if closing < 0:
        return "", "", index, "nested shell substitution exceeds analysis bounds"
    kind = "arithmetic" if command[index : index + 3] == "$((" else "command"
    body_length = closing - (index + 2)
    budget[0] += 1
    budget[1] += body_length
    if budget[0] > _SUBSTITUTION_MAX_COUNT or budget[1] > _SUBSTITUTION_MAX_CHARS:
        return "", "", index, "nested shell substitution exceeds analysis bounds"
    return kind, command[index + 2 : closing], closing + 1, None


def _consume_substitution_marker(
    command: str, index: int, marker: str, budget: list[int]
) -> tuple[str, str, int, str | None]:
    """Consume ``$(``, ``<(``, or ``>(`` outside quotes; return (kind, body, new_index, err)."""
    err = _budget_exceeded(budget)
    if err is not None:
        return "", "", index, err
    closing = balanced_substitution_end(
        command, index, max_chars=_SUBSTITUTION_MAX_CHARS - budget[1]
    )
    if closing is None:
        return "", "", index, "unparseable nested shell substitution"
    if closing == _UNSAFE_HEREDOC:
        return "", "", index, "nested shell heredoc is not safely analyzable"
    if closing < 0:
        return "", "", index, "nested shell substitution exceeds analysis bounds"
    if marker == "$(":
        kind = "arithmetic" if command[index : index + 3] == "$((" else "command"
    else:
        kind = "process"
    body_length = closing - (index + 2)
    budget[0] += 1
    budget[1] += body_length
    if budget[0] > _SUBSTITUTION_MAX_COUNT or budget[1] > _SUBSTITUTION_MAX_CHARS:
        return "", "", index, "nested shell substitution exceeds analysis bounds"
    return kind, command[index + 2 : closing], closing + 1, None


def _consume_backtick(
    command: str, index: int, budget: list[int]
) -> tuple[str, int, str | None]:
    """Consume a backtick; return (body, new_index, err)."""
    err = _budget_exceeded(budget)
    if err is not None:
        return "", index, err
    closing = backtick_end(
        command, index, max_chars=_SUBSTITUTION_MAX_CHARS - budget[1]
    )
    if closing is None:
        return "", index, "unparseable nested shell substitution"
    if closing == _UNSAFE_HEREDOC:
        return "", index, "nested shell heredoc is not safely analyzable"
    if closing < 0:
        return "", index, "nested shell substitution exceeds analysis bounds"
    body_length = closing - (index + 1)
    budget[0] += 1
    budget[1] += body_length
    if budget[0] > _SUBSTITUTION_MAX_COUNT or budget[1] > _SUBSTITUTION_MAX_CHARS:
        return "", index, "nested shell substitution exceeds analysis bounds"
    return decode_backtick_body(command[index + 1 : closing]), closing + 1, None
