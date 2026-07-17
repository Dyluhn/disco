"""Analyzer strategies — security-analyzer-contract.md §4.

- `RuleBasedAnalyzer` (§4.1): fast, deterministic, no model call; the always-on
  floor. Parses the shell tool's raw command for known-dangerous shapes and
  factors other tools' base_risk + argument shape.
- `LLMBasedAnalyzer` (§4.2): contextual judgement via an injected sync `scorer`;
  ADVISORY + ADDITIVE only (it can raise caution via the ensemble, never lower).
- `EnsembleAnalyzer` (§4.3): composes analyzers, returns the MOST cautious verdict
  via `max_risk`; runs the LLM only when the rule-based result warrants it.

The interface (§3) is the contract; the rule list / prompt are [INTERIOR] and
tunable. The strategy roles + ensemble behavior are contractual.
"""

from __future__ import annotations

import posixpath
import re
import shlex
from collections.abc import Callable, Mapping

from ..events import ActionEvent, SecurityRisk
from .assessment import RiskAssessment
from .risk import at_or_above, max_risk

# A sync risk scorer (the LLM analyzer's injected dependency). The real one
# bridges to the router; tests inject a fake. Kept sync so the loop's assess()
# boundary stays sync (contract §3 [VERIFY]); async router scoring is deferred.
Scorer = Callable[[ActionEvent], RiskAssessment]

_H = SecurityRisk.HIGH
_M = SecurityRisk.MEDIUM
_L = SecurityRisk.LOW
_U = SecurityRisk.UNKNOWN


# ---- shell-command rules [INTERIOR] -----------------------------------------
# Start conservative (over-flag rather than under-flag); relax with experience.

_SHELL_HIGH: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"\b(sudo|su)\b"), "privilege escalation"),
    (re.compile(r"\bmkfs\b"), "filesystem creation (destructive)"),
    (re.compile(r"\bdd\b[^\n;|&]*\b(if|of)="), "raw disk write/copy (dd)"),
    (re.compile(r">\s*/dev/(sd|nvme|hd|disk)"), "redirect to a raw device"),
    (re.compile(r">\s*/(etc|boot|sys|proc)/"), "write to a system path"),
    (re.compile(r":\(\)\s*\{"), "fork bomb"),
    (re.compile(r"\b(shutdown|reboot|halt|poweroff)\b|\binit\s+0\b"), "host power/state change"),
    (re.compile(r"\b(iptables|nft|ufw)\b"), "firewall/network rule change"),
    (re.compile(r"\bn(et)?cat?\b[^\n;|&]*-e\b|\bnc\b[^\n;|&]*-e\b"), "reverse-shell pattern"),
    (re.compile(r"\bchmod\b[^\n;|&]*\b777\b"), "world-writable permissions"),
    (re.compile(r"\bchown\b[^\n;|&]*\broot\b"), "ownership change to root"),
    # fetch-and-execute (curl/wget/base64 piped into a shell) — exfil/RCE shape
    (
        re.compile(r"\b(curl|wget|fetch|base64)\b[^\n]*\|\s*(sudo\s+)?(ba|z)?sh\b"),
        "fetch-and-execute (pipe to shell)",
    ),
]

_SHELL_MEDIUM: list[tuple[re.Pattern[str], str]] = [
    (
        re.compile(
            r"\b(apt|apt-get|yum|dnf|pip3?|npm|pnpm|yarn|gem|cargo|brew|go|go install)\b"
            r"[^\n;|&]*\b(install|add|get)\b"
        ),
        "package installation",
    ),
    (re.compile(r"\bgit\s+push\b"), "pushing to a remote"),
    (re.compile(r"\b(chmod|chown)\b"), "permission/ownership change"),
    (re.compile(r"\b(curl|wget|scp|rsync|ssh|ftp)\b"), "outbound network access"),
    (re.compile(r"\b(docker|systemctl|service|kubectl|podman)\b"), "service/container control"),
    (re.compile(r"\b(kill|pkill|killall)\b"), "process termination"),
]

# Clearly read-only commands → LOW. Anchored at the start (the leading command).
_SHELL_READ = re.compile(
    r"^\s*(ls|cat|less|more|head|tail|grep|rg|ag|pwd|echo|printf|wc|stat|file|which|type|"
    r"whoami|id|date|env|printenv|du|df|ps|top|htop|tree|find|sort|uniq|cut|awk|sed|jq|"
    r"git\s+(status|log|diff|show|branch|remote|config\s+--get))\b"
)


def _destructive_rm(low: str) -> bool:
    """`rm` invoked with BOTH recursive and force flags (any ordering/clustering)."""
    m = re.search(r"\brm\s+([^\n;|&]*)", low)
    if not m:
        return False
    flagchars = "".join(re.findall(r"(?:^|\s)-(\w+)", m.group(1)))
    return "r" in flagchars and "f" in flagchars


# Cluster 3 — HARD DENY: catastrophic, never-allowed commands. Distinct from
# HIGH (which routes to human confirmation): these are refused outright by the
# loop BEFORE the confirm gate — no approval, no policy, no LLM can run them.
# OS-level enforcement (egress/filesystem isolation) is the deeper layer; this
# is the command-level non-negotiable floor.
#
# NOTE on `rm` (W3 C-3): a regex CANNOT parse shell — it can't resolve the `--`
# end-of-options token, long options (`--recursive`), `--no-preserve-root`, flag
# clustering/reordering, quoting, `$VAR` indirection, or `$(...)`/backtick
# substitution. The old `rm … -[rfRF]* (/|/*)` pattern was defeated by every one
# of those (e.g. `rm -rf -- /` slipped straight through). So recursive-delete of
# a protected root is handled by a dedicated shlex-based analyzer below; the
# patterns here are only the non-`rm` shapes a tokenizer doesn't help with.
_SHELL_DENY: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"\bmkfs\b"), "format a filesystem (irreversible)"),
    (re.compile(r"\bdd\b[^\n;|&]*\bof=/dev/(sd|nvme|hd|disk|vd)"), "raw write to a disk device"),
    (re.compile(r">\s*/dev/(sd|nvme|hd|disk|vd)"), "redirect to a raw disk device"),
    (re.compile(r":\(\)\s*\{\s*:?\s*\|?\s*:?\s*&?\s*\}\s*;?\s*:"), "fork bomb"),
]

# Whole-system paths whose RECURSIVE deletion is catastrophic and never a
# legitimate build step. Deliberately narrow: subpaths (`/etc/myapp`, `/tmp/x`,
# `./build`, `node_modules`, `dist/`) are NOT here — this floor cannot be
# overridden, so a false positive would block ordinary builds.
_PROTECTED_ROOTS: frozenset[str] = frozenset(
    {
        "/",
        "/*",
        "/.",
        "/..",
        "~",
        "$HOME",
        "${HOME}",
        "/bin",
        "/boot",
        "/dev",
        "/etc",
        "/home",
        "/lib",
        "/lib32",
        "/lib64",
        "/libx32",
        "/mnt",
        "/opt",
        "/proc",
        "/root",
        "/run",
        "/sbin",
        "/srv",
        "/sys",
        "/usr",
        "/var",
    }
)

# Shell interpreters whose `-c <string>` argument is itself a command we must
# re-inspect (`bash -c "rm -rf /"` would otherwise slip past a token scan).
_SHELL_WRAPPERS: frozenset[str] = frozenset({"sh", "bash", "zsh", "dash", "ash", "ksh"})

# H343 — bounded recursive inspection for command/process substitutions.  These
# bodies execute before their apparently-benign outer command, so command-head
# analysis alone is not a hard-deny floor.  Bounds keep adversarial nesting
# linear and deterministic; exceeding one fails closed.
_SUBSTITUTION_MAX_DEPTH = 8
_SUBSTITUTION_MAX_COUNT = 32
_SUBSTITUTION_MAX_CHARS = 64 * 1024
_UNSAFE_HEREDOC = -2
_DOUBLE_QUOTE_ESCAPES = frozenset({'$', '`', '"', "\\", "\n"})


def _without_shell_line_continuations(command: str) -> str:
    """Remove Bash backslash-newline continuations outside single quotes."""
    out: list[str] = []
    quote: str | None = None
    index = 0
    while index < len(command):
        character = command[index]
        if quote == "'":
            out.append(character)
            if character == "'":
                quote = None
            index += 1
            continue
        if character == "\\" and command[index + 1 : index + 2] == "\n":
            index += 2
            continue
        out.append(character)
        if character == '"':
            quote = None if quote == '"' else '"'
        elif character == "'" and quote is None:
            quote = "'"
        if character == "\\" and index + 1 < len(command):
            out.append(command[index + 1])
            index += 2
            continue
        index += 1
    return "".join(out)


def _backtick_end(command: str, start: int, *, max_chars: int) -> int | None:
    """Return the closing index for an active backtick starting at ``start``."""
    index = start + 1
    while index < len(command):
        if index - (start + 1) > max_chars:
            return -1
        # A heredoc can contain an arbitrary backtick delimiter as data. The
        # legacy syntax is not safely inspectable with this bounded recognizer;
        # fail closed instead of guessing where the executable body resumes.
        if command[index : index + 2] == "<<":
            return _UNSAFE_HEREDOC
        if command[index] == "\\" and index + 1 < len(command):
            index += 2
            continue
        if command[index] == "`":
            return index
        index += 1
    return None


def _decode_backtick_body(body: str) -> str:
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
    # Backslashes that quote the legacy closing delimiter are syntax, not part
    # of the command operand. Multiple nested levels can leave an odd residual
    # after one decode pass; remove only the body-final delimiter quoting run.
    return "".join(out).rstrip("\\")


def _parameter_expansion_end(
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
            if character == "\\" and index + 1 < len(command):
                index += 2
                continue
            if character == '"':
                quote = None
                index += 1
                continue
        elif character == "\\" and index + 1 < len(command):
            index += 2
            continue
        elif character in {"'", '"'}:
            quote = character
            index += 1
            continue
        if command[index : index + 2] == "${":
            depth += 1
            index += 2
            continue
        if command[index : index + 2] == "$(":
            closing = _balanced_substitution_end(
                command,
                index,
                max_chars=max_chars - (index - body_start),
                syntax_depth=syntax_depth + 1,
            )
            if closing is None or closing < 0:
                return closing
            index = closing + 1
            continue
        if character == "`":
            closing = _backtick_end(
                command,
                index,
                max_chars=max_chars - (index - body_start),
            )
            if closing is None or closing < 0:
                return closing
            index = closing + 1
            continue
        # Heredoc bodies may contain a bare ``)`` line that is data, not the
        # command-substitution close. Conservatively refuse this uncommon shell
        # shape (the product prompt already directs agents away from heredocs).
        if command[index : index + 2] == "<<":
            return _UNSAFE_HEREDOC
        if character == "}":
            depth -= 1
            if depth == 0:
                return index
        index += 1
    return None


def _balanced_substitution_end(
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
    depth = 1
    quote: str | None = None
    index = start + 2
    body_start = index
    # Each entry is [phase, pattern_paren_depth, pattern_started], where phase
    # is waiting_in, pattern, or body. This lets case-pattern ``)`` be
    # distinguished from the surrounding substitution close in one pass,
    # including escaped/quoted words or alternatives that merely spell ``esac``.
    case_stack: list[list[str | int]] = []
    command_position = True
    word: list[str] = []
    word_quoted = False

    def _flush_word() -> None:
        nonlocal command_position, word_quoted
        if not word:
            return
        token = "".join(word)
        eligible_reserved = not word_quoted
        phase = str(case_stack[-1][0]) if case_stack else ""
        if phase == "waiting_in":
            if eligible_reserved and token == "in":
                case_stack[-1][0] = "pattern"
                case_stack[-1][1] = 0
                case_stack[-1][2] = 0
        elif phase == "pattern":
            if eligible_reserved and token == "esac" and not case_stack[-1][2]:
                case_stack.pop()
            else:
                case_stack[-1][2] = 1
        elif command_position and eligible_reserved and token == "case":
            case_stack.append(["waiting_in", 0, 0])
            command_position = False
        elif command_position and eligible_reserved and token == "esac" and case_stack:
            case_stack.pop()
            command_position = False
        elif eligible_reserved and token in {"do", "elif", "else", "then"}:
            command_position = True
        elif command_position and not re.fullmatch(r"[A-Za-z_]\w*=.*", token):
            command_position = False
        word.clear()
        word_quoted = False

    while index < len(command):
        if index - body_start > max_chars:
            return -1
        character = command[index]
        if quote == "'":
            if character == "'":
                quote = None
            else:
                word.append(character)
            index += 1
            continue
        if quote == '"':
            if character == "\\" and index + 1 < len(command):
                if command[index + 1] in _DOUBLE_QUOTE_ESCAPES:
                    word.append(command[index + 1])
                    word_quoted = True
                    index += 2
                    continue
            if character == '"':
                quote = None
                index += 1
                continue
            if command[index : index + 2] == "${":
                closing = _parameter_expansion_end(
                    command,
                    index,
                    max_chars=max_chars - (index - body_start),
                    syntax_depth=syntax_depth + 1,
                )
                if closing is None or closing < 0:
                    return closing
                word.append("parameter")
                word_quoted = True
                index = closing + 1
                continue
            if command[index : index + 2] == "$(":
                closing = _balanced_substitution_end(
                    command,
                    index,
                    max_chars=max_chars - (index - body_start),
                    syntax_depth=syntax_depth + 1,
                )
                if closing is None or closing < 0:
                    return closing
                word.append("substitution")
                word_quoted = True
                index = closing + 1
                continue
            if character == "`":
                closing = _backtick_end(
                    command,
                    index,
                    max_chars=max_chars - (index - body_start),
                )
                if closing is None or closing < 0:
                    return closing
                word.append("substitution")
                word_quoted = True
                index = closing + 1
                continue
            word.append(character)
            index += 1
            continue
        if character == "\\" and index + 1 < len(command):
            word.append(command[index + 1])
            word_quoted = True
            index += 2
            continue
        if character in {"'", '"'}:
            quote = character
            word_quoted = True
            index += 1
            continue
        if character == "#" and not word:
            # At a token boundary ``#`` comments through newline; delimiters in
            # that text are data. Keep the scan bounded and resume at the next
            # command line.
            while index < len(command) and command[index] != "\n":
                if index - body_start > max_chars:
                    return -1
                index += 1
            command_position = True
            continue
        if command[index : index + 2] == "<<":
            return _UNSAFE_HEREDOC
        if character == "`":
            closing = _backtick_end(
                command,
                index,
                max_chars=max_chars - (index - body_start),
            )
            if closing is None or closing < 0:
                return closing
            word.append("substitution")
            word_quoted = True
            index = closing + 1
            continue
        if command[index : index + 2] == "${":
            closing = _parameter_expansion_end(
                command,
                index,
                max_chars=max_chars - (index - body_start),
                syntax_depth=syntax_depth + 1,
            )
            if closing is None or closing < 0:
                return closing
            word.append("parameter")
            word_quoted = True
            index = closing + 1
            continue
        if character.isspace():
            _flush_word()
            if character == "\n":
                command_position = True
            index += 1
            continue
        if character in ";&|":
            _flush_word()
            run_end = index + 1
            while run_end < len(command) and command[run_end] in ";&|":
                run_end += 1
            operator = command[index:run_end]
            if case_stack and case_stack[-1][0] == "body" and ";" in operator:
                case_stack[-1][0] = "pattern"
                case_stack[-1][1] = 0
                case_stack[-1][2] = 0
            command_position = True
            index = run_end
            continue
        if character == "(":
            _flush_word()
            if case_stack and case_stack[-1][0] == "pattern":
                case_stack[-1][1] = int(case_stack[-1][1]) + 1
                case_stack[-1][2] = 1
            else:
                depth += 1
        elif character == ")":
            _flush_word()
            if case_stack and case_stack[-1][0] == "pattern":
                pattern_depth = int(case_stack[-1][1])
                if pattern_depth > 0:
                    case_stack[-1][1] = pattern_depth - 1
                else:
                    case_stack[-1][0] = "body"
                    command_position = True
            else:
                depth -= 1
                if depth == 0:
                    return index
        else:
            word.append(character)
        index += 1
    return None


def _active_shell_substitutions(
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
    found: list[tuple[str, str]] = []
    quote: str | None = None
    token_start = True
    index = 0
    while index < len(command):
        character = command[index]
        if quote == "'":
            if character == "'":
                quote = None
                token_start = False
            index += 1
            continue
        if quote == '"':
            if character == "\\" and index + 1 < len(command):
                if command[index + 1] in _DOUBLE_QUOTE_ESCAPES:
                    index += 2
                    continue
            if character == '"':
                quote = None
                token_start = False
                index += 1
                continue
            if command[index : index + 2] == "$(":
                if budget[0] >= _SUBSTITUTION_MAX_COUNT:
                    return found, "nested shell substitution exceeds analysis bounds"
                closing = _balanced_substitution_end(
                    command,
                    index,
                    max_chars=_SUBSTITUTION_MAX_CHARS - budget[1],
                )
                if closing is None:
                    return found, "unparseable nested shell substitution"
                if closing == _UNSAFE_HEREDOC:
                    return found, "nested shell heredoc is not safely analyzable"
                if closing < 0:
                    return found, "nested shell substitution exceeds analysis bounds"
                kind = "arithmetic" if command[index : index + 3] == "$((" else "command"
                body_length = closing - (index + 2)
                budget[0] += 1
                budget[1] += body_length
                if (
                    budget[0] > _SUBSTITUTION_MAX_COUNT
                    or budget[1] > _SUBSTITUTION_MAX_CHARS
                ):
                    return found, "nested shell substitution exceeds analysis bounds"
                found.append((kind, command[index + 2 : closing]))
                index = closing + 1
                continue
            if character == "`":
                if budget[0] >= _SUBSTITUTION_MAX_COUNT:
                    return found, "nested shell substitution exceeds analysis bounds"
                closing = _backtick_end(
                    command,
                    index,
                    max_chars=_SUBSTITUTION_MAX_CHARS - budget[1],
                )
                if closing is None:
                    return found, "unparseable nested shell substitution"
                if closing == _UNSAFE_HEREDOC:
                    return found, "nested shell heredoc is not safely analyzable"
                if closing < 0:
                    return found, "nested shell substitution exceeds analysis bounds"
                body_length = closing - (index + 1)
                budget[0] += 1
                budget[1] += body_length
                if (
                    budget[0] > _SUBSTITUTION_MAX_COUNT
                    or budget[1] > _SUBSTITUTION_MAX_CHARS
                ):
                    return found, "nested shell substitution exceeds analysis bounds"
                found.append(
                    ("backtick", _decode_backtick_body(command[index + 1 : closing]))
                )
                index = closing + 1
                continue
            index += 1
            continue
        if character == "\\" and index + 1 < len(command):
            token_start = False
            index += 2
            continue
        if character in {"'", '"'}:
            quote = character
            token_start = False
            index += 1
            continue
        if character == "#" and token_start:
            newline = command.find("\n", index + 1)
            if newline < 0:
                return found, None
            index = newline + 1
            token_start = True
            continue
        marker = command[index : index + 2]
        if marker in {"$(", "<(", ">("}:
            if budget[0] >= _SUBSTITUTION_MAX_COUNT:
                return found, "nested shell substitution exceeds analysis bounds"
            closing = _balanced_substitution_end(
                command,
                index,
                max_chars=_SUBSTITUTION_MAX_CHARS - budget[1],
            )
            if closing is None:
                return found, "unparseable nested shell substitution"
            if closing == _UNSAFE_HEREDOC:
                return found, "nested shell heredoc is not safely analyzable"
            if closing < 0:
                return found, "nested shell substitution exceeds analysis bounds"
            if marker == "$(":
                kind = "arithmetic" if command[index : index + 3] == "$((" else "command"
            else:
                kind = "process"
            body_length = closing - (index + 2)
            budget[0] += 1
            budget[1] += body_length
            if budget[0] > _SUBSTITUTION_MAX_COUNT or budget[1] > _SUBSTITUTION_MAX_CHARS:
                return found, "nested shell substitution exceeds analysis bounds"
            found.append((kind, command[index + 2 : closing]))
            index = closing + 1
            token_start = False
            continue
        if character == "`":
            if budget[0] >= _SUBSTITUTION_MAX_COUNT:
                return found, "nested shell substitution exceeds analysis bounds"
            closing = _backtick_end(
                command,
                index,
                max_chars=_SUBSTITUTION_MAX_CHARS - budget[1],
            )
            if closing is None:
                return found, "unparseable nested shell substitution"
            if closing == _UNSAFE_HEREDOC:
                return found, "nested shell heredoc is not safely analyzable"
            if closing < 0:
                return found, "nested shell substitution exceeds analysis bounds"
            body_length = closing - (index + 1)
            budget[0] += 1
            budget[1] += body_length
            if budget[0] > _SUBSTITUTION_MAX_COUNT or budget[1] > _SUBSTITUTION_MAX_CHARS:
                return found, "nested shell substitution exceeds analysis bounds"
            found.append(("backtick", _decode_backtick_body(command[index + 1 : closing])))
            index = closing + 1
            token_start = False
            continue
        if character.isspace() or character in ";&|()":
            token_start = True
        else:
            token_start = False
        index += 1
    return found, None


def _var_assignments(command: str) -> dict[str, str]:
    """Best-effort scan of `NAME=value` assignments so a later `rm -rf "$X"` can
    be resolved when an earlier segment set `X=/`. One level, simple unquoted
    values only — intentionally conservative (used only to CATCH, never to
    exonerate)."""
    out: dict[str, str] = {}
    for m in re.finditer(r"(?:^|[;&|\n]|&&|\|\|)\s*([A-Za-z_]\w*)=([^\s;&|]+)", command):
        out[m.group(1)] = m.group(2).strip("\"'")
    return out


def _resolve_target(token: str, assignments: dict[str, str]) -> str:
    """Normalize an `rm` operand for comparison against the protected set: strip
    quotes, resolve one level of `$VAR`/`${VAR}`, drop a trailing `/*` glob
    (`/etc/*`→`/etc`), and `posixpath.normpath` any absolute path so `..`/`//`/
    trailing-slash tricks collapse (`/home/../`→`/`, `///`→`/`, `/etc/../etc`→
    `/etc`). The `~`/`$HOME` sigils are kept verbatim (they're protected tokens,
    not yet real paths). posixpath (not os.path) so the semantics are the sandbox's
    POSIX ones regardless of the host OS."""
    t = token.strip().strip("\"'")
    mvar = re.fullmatch(r"\$\{?([A-Za-z_]\w*)\}?", t)
    if mvar and mvar.group(1) in assignments:
        t = assignments[mvar.group(1)].strip().strip("\"'")
    if t in ("~", "$HOME", "${HOME}"):
        return t
    if t.endswith("/*"):
        t = t[:-2] or "/"
    if t.startswith("/"):
        # POSIX normpath deliberately KEEPS a leading `//` (implementation-defined),
        # but Linux resolves `//etc`→`/etc`, so collapse runs of leading slashes
        # first, then normpath the rest (`.., trailing /).
        t = posixpath.normpath(re.sub(r"^/+", "/", t))
    return t


# Transparent prefix commands: they run the command that FOLLOWS, so the real
# command word is past them (`sudo rm …`, `env VAR=x rm …`). Skipping these — and
# leading `NAME=val` assignments — is what lets us analyze the COMMAND position
# only, so `echo rm -rf /` (rm is an ARGUMENT, harmless) is never a false positive.
_CMD_PREFIXES: frozenset[str] = frozenset(
    {
        "sudo",
        "doas",
        "command",
        "builtin",
        "exec",
        "env",
        "nice",
        "nohup",
        "time",
        "ionice",
        "stdbuf",
        "setsid",
    }
)


# Shell control operators that separate one command from the next.
_SEG_OPERATORS: frozenset[str] = frozenset({";", "&", "&&", "|", "||", "\n"})

# Reserved words / grouping tokens after which the next token is in command
# position.  Stripping these before command-head analysis catches the body of
# ``if/then``, ``for/do``, subshell, and brace-group constructs without treating
# an ordinary argument named ``rm`` as executable.
_COMMAND_POSITION_PREFIXES: frozenset[str] = frozenset(
    {"!", "(", "{", "case", "do", "elif", "else", "for", "if", "select", "then", "until", "while"}
)

# find's leading GLOBAL options (precede the paths). `-D`/`-O` additionally take
# an argument.
_FIND_GLOBAL_OPTS: frozenset[str] = frozenset({"-H", "-L", "-P"})


def _c_flag_arg_index(tokens: list[str]) -> int | None:
    """Index of the command STRING a shell interpreter's `-c` selects, handling a
    bare `-c` AND clustered forms (`-lc`, `-ec`, `-xc`). None if absent."""
    for i, t in enumerate(tokens):
        if t.startswith("-") and not t.startswith("--") and "c" in t[1:]:
            return i + 1 if i + 1 < len(tokens) else None
    return None


def _rm_recursive_operands(rest: list[str]) -> tuple[bool, list[str]]:
    """Split `rm`'s args into (is-recursive, operands), honoring `--`, long
    (`--recursive`), and clustered/reordered short flags (`-rf`, `-fr`, `-Rf`)."""
    recursive = False
    end_of_flags = False
    operands: list[str] = []
    for t in rest:
        if not end_of_flags and t == "--":
            end_of_flags = True
            continue
        if not end_of_flags and t.startswith("-") and len(t) > 1:
            if t.startswith("--"):
                # GNU rm accepts any UNAMBIGUOUS prefix of a long option, so
                # `--r`, `--re`, `--rec`, … all mean `--recursive`.
                if len(t) > 2 and "--recursive".startswith(t):
                    recursive = True
            elif any(c in ("r", "R") for c in t[1:]):  # short cluster -rf/-fr/-Rf
                recursive = True
            continue  # any other flag (--force, --no-preserve-root, …)
        operands.append(t)
    return recursive, operands


def _find_deny(rest: list[str], assignments: dict[str, str]) -> str | None:
    """`find <protected-root> … (-delete | -exec rm …)` wipes a root without an
    explicit rm operand. Conservative: requires a protected-root PATH arg (find's
    paths precede the first `-expression`), so `find . -delete` stays allowed."""
    # Skip find's leading GLOBAL options (-H/-L/-P, and -D/-O <arg>) before paths.
    i = 0
    while i < len(rest):
        if rest[i] in _FIND_GLOBAL_OPTS:
            i += 1
        elif rest[i] in ("-D", "-O") and i + 1 < len(rest):
            i += 2
        else:
            break
    paths: list[str] = []
    for t in rest[i:]:
        if t.startswith("-") or t in ("(", "!"):
            break
        paths.append(t)
    if not any(_resolve_target(p, assignments) in _PROTECTED_ROOTS for p in paths):
        return None
    # `-exec /bin/rm …` is still rm — match by basename, not the literal token.
    has_rm = any(t.rsplit("/", 1)[-1] == "rm" for t in rest)
    destructive = "-delete" in rest or (("-exec" in rest or "-execdir" in rest) and has_rm)
    return "recursive find-delete of a protected system path" if destructive else None


def _analyze_command(
    base: str,
    rest: list[str],
    assignments: dict[str, str],
    *,
    depth: int,
    budget: list[int],
) -> str | None:
    """Given a resolved command word `base` and its arg tokens, deny iff it is a
    recursive delete of a protected root (rm), a `find <root> -delete/-exec rm`,
    or a shell wrapper whose `-c` string is one of those."""
    if base in _SHELL_WRAPPERS:
        j = _c_flag_arg_index(rest)
        return (
            _rm_protected_root_deny(rest[j], _depth=depth + 1, _budget=budget)
            if j is not None
            else None
        )
    if base == "find":
        return _find_deny(rest, assignments)
    if base != "rm":
        return None
    recursive, operands = _rm_recursive_operands(rest)
    if not recursive:
        return None
    for op in operands:
        if "$(" in op or "`" in op:  # substitution we refuse to evaluate
            return "recursive rm with command substitution (unevaluable target)"
        if _resolve_target(op, assignments) in _PROTECTED_ROOTS:
            return f"recursive delete of a protected system path ({op!r})"
    return None


def _rm_tokens_deny(
    tokens: list[str],
    assignments: dict[str, str],
    *,
    depth: int,
    budget: list[int],
) -> str | None:
    """Deny iff the segment's COMMAND is a recursive delete of a protected root.
    Analyzes the COMMAND position only (so `echo rm -rf /` — rm as an argument — is
    never a false positive), after skipping leading `NAME=val` assignments and
    transparent prefix commands (`sudo`/`env`/`nice`/…)."""
    i = 0
    while i < len(tokens) and re.fullmatch(r"[A-Za-z_]\w*=.*", tokens[i]):
        i += 1  # leading env-assignments (VAR=val cmd)
    toks = tokens[i:]
    # Normalize wrappers to a fixed point: a valid Bash command may compose
    # control words, assignments, coproc, and function syntax in any of these
    # command-position layers (``then coproc X=1 rm ...``).
    while toks:
        before = len(toks)
        while toks and re.fullmatch(r"[A-Za-z_]\w*=.*", toks[0]):
            toks = toks[1:]
        while toks and toks[0] in _COMMAND_POSITION_PREFIXES:
            toks = toks[1:]
        if toks[:1] == ["coproc"]:
            toks = toks[1:]
            # Bash named coprocess syntax is ``coproc NAME { command; }``.
            if len(toks) >= 2 and toks[1] == "{":
                toks = toks[2:]
        elif toks[:1] == ["function"]:
            # ``function NAME { command; }`` puts the executable body later in
            # this segment. Parenthesized NAME() bodies start a fresh segment.
            toks = toks[2:] if len(toks) >= 2 else []
        if len(toks) == before:
            break
    if not toks:
        return None
    base0 = toks[0].rsplit("/", 1)[-1]
    if base0 in _CMD_PREFIXES:
        # A transparent prefix's option grammar is unbounded (`sudo -u root`,
        # `nice -n 19`, `env -u PATH`), so rather than guess which tokens are
        # flag-arguments, scan for the FIRST real rm/find/wrapper command word. The
        # recursive+protected-root check is the real gate, so a stray `rm` argument
        # (no -r, no root operand) still won't false-positive.
        for k in range(1, len(toks)):
            b = toks[k].rsplit("/", 1)[-1]
            if b in ("rm", "find") or b in _SHELL_WRAPPERS:
                reason = _analyze_command(
                    b,
                    toks[k + 1 :],
                    assignments,
                    depth=depth,
                    budget=budget,
                )
                if reason is not None:
                    return reason
        return None
    return _analyze_command(base0, toks[1:], assignments, depth=depth, budget=budget)


def _without_shell_comments(command: str) -> str:
    """Remove active shell comments while preserving their newline separator."""
    out: list[str] = []
    quote: str | None = None
    token_start = True
    index = 0
    while index < len(command):
        character = command[index]
        if quote is not None:
            out.append(character)
            if character == "\\" and quote == '"' and index + 1 < len(command):
                out.append(command[index + 1])
                index += 2
                continue
            if character == quote:
                quote = None
                token_start = False
            index += 1
            continue
        if character == "\\" and index + 1 < len(command):
            out.extend((character, command[index + 1]))
            token_start = False
            index += 2
            continue
        if character in {"'", '"'}:
            quote = character
            token_start = False
            out.append(character)
            index += 1
            continue
        if character == "#" and token_start:
            newline = command.find("\n", index + 1)
            if newline < 0:
                break
            out.append("\n")
            index = newline + 1
            token_start = True
            continue
        out.append(character)
        token_start = character.isspace() or character in ";&|()"
        index += 1
    return "".join(out)


def _split_command_segments(command: str) -> list[list[str]] | None:
    """Quote-AWARE split of a command line into per-command token lists, cut on the
    shell control operators (`; & && | ||`). Because shlex respects quotes, a `;`
    or `|` INSIDE quotes stays part of the word — so `printf 'rm -rf /;'` is one
    benign `printf` command, not a spurious `rm` segment (the false positive a
    naive regex split produced). Returns None if the whole command has unbalanced
    quotes (malformed)."""
    lex = shlex.shlex(_without_shell_comments(command), posix=True, punctuation_chars=";&|()\n")
    lex.whitespace_split = True
    lex.whitespace = " \t\r"
    lex.commenters = ""
    try:
        raw = list(lex)
    except ValueError:
        return None
    segs: list[list[str]] = [[]]
    for t in raw:
        if t in _SEG_OPERATORS or (t and all(character in ";&|()" for character in t)):
            segs.append([])
        else:
            segs[-1].append(t)
    return [s for s in segs if s]


def _nested_substitution_deny(command: str, *, depth: int, budget: list[int]) -> str | None:
    substitutions, malformed = _active_shell_substitutions(command, budget=budget)
    if malformed is not None:
        return malformed
    if substitutions and depth >= _SUBSTITUTION_MAX_DEPTH:
        return "nested shell substitution exceeds analysis bounds"
    for kind, body in substitutions:
        if kind == "arithmetic":
            reason = _nested_substitution_deny(body, depth=depth + 1, budget=budget)
        else:
            reason = _rm_protected_root_deny(body, _depth=depth + 1, _budget=budget)
        if reason is not None:
            if reason in {
                "nested shell substitution exceeds analysis bounds",
                "nested shell heredoc is not safely analyzable",
                "unparseable nested shell substitution",
            }:
                return reason
            return f"destructive command inside {kind} substitution: {reason}"
    return None


def _rm_protected_root_deny(
    command: str,
    *,
    _depth: int = 0,
    _budget: list[int] | None = None,
) -> str | None:
    """Deny a recursive delete of a protected system root, robust to the bypasses
    a regex misses (`--`, long options + GNU abbreviations, `--no-preserve-root`,
    flag reordering, quoting, `\\rm`/`'r'm`, transparent prefixes, `$VAR`
    indirection, `$(…)`/backtick, path traversal + `//` collapse, `bash -lc "…"`
    nesting, and `find <root> -delete`). Precise elsewhere: `rm -rf ./build`,
    `echo rm -rf /`, `printf 'rm -rf /;'` are NOT denied.

    Static analysis cannot model runtime shell EXPANSION (`$'r'm`, `/e??` globs,
    `env -S`), so those remain the domain of the sandbox isolation boundary — this
    floor is defense-in-depth, not a complete shell interpreter."""
    if _depth > _SUBSTITUTION_MAX_DEPTH:
        return "nested shell substitution exceeds analysis bounds"
    command = _without_shell_line_continuations(command)
    budget = _budget if _budget is not None else [0, 0]
    nested_reason = _nested_substitution_deny(command, depth=_depth, budget=budget)
    if nested_reason is not None:
        return nested_reason
    assignments = _var_assignments(command)
    segs = _split_command_segments(command)
    if segs is None:
        # Genuinely unbalanced quotes (malformed, wouldn't run). Only refuse if it
        # clearly reads as a recursive rm of a rooted path; else don't guess.
        if re.search(r"\brm\b[^\n]*-{1,2}[A-Za-z]*r", command, re.I) and re.search(
            r"\s/(\s|\*|$)", command
        ):
            return "recursive rm of a protected path (unparseable command)"
        return None
    for tokens in segs:
        reason = _rm_tokens_deny(tokens, assignments, depth=_depth, budget=budget)
        if reason:
            return reason
    return None


def hard_deny_reason(command: str) -> str | None:
    """Return a reason string if a shell command is HARD-DENIED (never runnable),
    else None. Pure + deterministic. The loop refuses denied actions outright."""
    normalized = _without_shell_line_continuations(command)
    low = normalized.lower()
    for pat, why in _SHELL_DENY:
        if pat.search(low):
            return why
    return _rm_protected_root_deny(normalized)


def _score_shell(command: str) -> tuple[SecurityRisk, str]:
    low = command.lower()
    if _destructive_rm(low):
        return _H, "recursive force delete (rm -rf)"
    for pat, why in _SHELL_HIGH:
        if pat.search(low):
            return _H, why
    for pat, why in _SHELL_MEDIUM:
        if pat.search(low):
            return _M, why
    if _SHELL_READ.match(low):
        return _L, "read-only command"
    # Shell is inherently non-trivial: an unrecognized command is MEDIUM, not LOW.
    return _M, "unrecognized shell command (cautious default for shell)"


# ---- the rule-based analyzer ------------------------------------------------


class RuleBasedAnalyzer:
    """[CONTRACT role; INTERIOR rules] Fast, deterministic, no model call — the
    always-on floor under every action. For the `shell` tool it parses the raw
    command; for other tools it factors the injected `base_risk` (the tool/sandbox
    layer's static hint, passed in because `core` cannot import `tools`) and the
    argument shape. Sync + total: any internal error returns UNKNOWN (cautious),
    never raises."""

    name = "rule_based"

    def __init__(self, base_risk_by_tool: Mapping[str, SecurityRisk] | None = None) -> None:
        self._base = dict(base_risk_by_tool or {})

    def assess(self, action: ActionEvent) -> SecurityRisk:
        return self.assess_detailed(action).risk

    def assess_detailed(self, action: ActionEvent) -> RiskAssessment:
        try:
            risk, rationale = self._score(action)
        except Exception as exc:  # noqa: BLE001 — fail toward caution, never crash the gate
            return RiskAssessment(
                risk=_U,
                rationale=f"rule analyzer error: {type(exc).__name__}",
                analyzer=self.name,
                self_assessed=action.self_assessed_risk,
            )
        return RiskAssessment(
            risk=risk,
            rationale=rationale,
            analyzer=self.name,
            self_assessed=action.self_assessed_risk,
        )

    def _score(self, action: ActionEvent) -> tuple[SecurityRisk, str]:
        tc = action.tool_call
        if tc.tool_name in {"shell", "shell_exec"}:
            # The tool contract guarantees the shell tools surface their raw command.
            return _score_shell(str(tc.arguments.get("command", "")))
        return self._score_other(tc.tool_name, tc.arguments)

    # Plan/control meta tools: they update plan-tracker state but have NO workspace side
    # effects. `submit_plan` is intercepted by the loop before the gate; `plan_step` /
    # `update_plan_progress` only report capstone progress. Pin them LOW so a progress
    # marker never interrupts an approved build with a confirmation gate.
    _META_TOOLS = frozenset({"submit_plan", "plan_step", "update_plan_progress"})

    def _score_other(self, tool_name: str, args: Mapping[str, object]) -> tuple[SecurityRisk, str]:
        name = tool_name.lower()
        if name in self._META_TOOLS:
            return _L, f"plan-mode control signal '{tool_name}' (no side effects)"
        inferred = _L
        why = f"tool '{tool_name}'"

        if any(
            k in name for k in ("read", "search", "fetch", "list", "view", "get", "status", "wait")
        ):
            inferred, why = _L, f"read-only tool '{tool_name}'"
        if any(k in name for k in ("write", "create", "edit", "save", "append", "kill")):
            inferred, why = _M, f"state-changing tool '{tool_name}'"
            path = str(args.get("path") or args.get("file") or args.get("filename") or "")
            if path.startswith("/") or ".." in path:
                inferred, why = _H, f"'{tool_name}' writing outside the workspace ({path})"
        if any(k in name for k in ("deploy", "publish", "release")):
            inferred, why = _H, f"deployment tool '{tool_name}'"
        if any(k in name for k in ("delete", "remove", "destroy", "drop")):
            inferred, why = _H, f"destructive tool '{tool_name}'"
        if "browse" in name or "browser" in name:
            act = str(args.get("action") or args.get("op") or "").lower()
            if act in ("submit", "post") or args.get("submit"):
                # Sending form data outward (exfiltration / state-changing) — HIGH, so it
                # hits the gate even when a page tries to induce it.
                inferred, why = max_risk(inferred, _H), f"'{tool_name}' submitting form data"
            elif act in ("click", "type", "fill"):
                inferred, why = max_risk(inferred, _M), f"'{tool_name}' interacting with the page"

        base = self._base.get(tool_name)
        if base is not None:
            return max_risk(base, inferred), f"{why}; base_risk={base.value}"
        return inferred, why


# ---- the LLM-based analyzer (advisory, additive) ----------------------------


class LLMBasedAnalyzer:
    """[CONTRACT role; INTERIOR prompt] Catches novel/contextual risk a rule list
    misses, via an injected sync `scorer`. ADVISORY + ADDITIVE: it only ever
    contributes to the ensemble's `max_risk`, so it can raise caution but never
    lower what the rule-based analyzer flagged (injection-resistance, §4.2). Sync
    + total: scorer errors contribute UNKNOWN, never raise."""

    name = "llm_based"

    def __init__(self, scorer: Scorer) -> None:
        self._scorer = scorer

    def assess(self, action: ActionEvent) -> SecurityRisk:
        return self.assess_detailed(action).risk

    def assess_detailed(self, action: ActionEvent) -> RiskAssessment:
        try:
            result = self._scorer(action)
        except Exception as exc:  # noqa: BLE001 — fail toward caution
            return RiskAssessment(
                risk=_U,
                rationale=f"llm analyzer error: {type(exc).__name__}",
                analyzer=self.name,
                self_assessed=action.self_assessed_risk,
            )
        # Normalize: ensure the contributor is attributed to this analyzer.
        return result.model_copy(update={"analyzer": result.analyzer or self.name})


# ---- the ensemble -----------------------------------------------------------


class EnsembleAnalyzer:
    """[CONTRACT] Composes analyzers and returns the MOST cautious verdict via
    `max_risk` (§4.3). The rule-based analyzer runs on EVERY action (the floor);
    the LLM analyzer runs only when the rule-based result warrants it (at/above a
    configurable trigger, or UNKNOWN) — so trivial reads aren't taxed with a model
    call. No analyzer can relax another's caution."""

    name = "ensemble"

    def __init__(
        self,
        rule_based: RuleBasedAnalyzer,
        llm: LLMBasedAnalyzer | None = None,
        *,
        llm_trigger: SecurityRisk = SecurityRisk.MEDIUM,
    ) -> None:
        self._rule_based = rule_based
        self._llm = llm
        self._llm_trigger = llm_trigger

    def assess(self, action: ActionEvent) -> SecurityRisk:
        return self.assess_detailed(action).risk

    def _warrants_llm(self, rule_risk: SecurityRisk) -> bool:
        # Run the (expensive) LLM only for non-trivial actions: at/above the
        # trigger, or when the rule-based analyzer was itself unsure.
        return rule_risk == _U or at_or_above(rule_risk, self._llm_trigger)

    def assess_detailed(self, action: ActionEvent) -> RiskAssessment:
        contributors: list[RiskAssessment] = []
        rb = self._rule_based.assess_detailed(action)  # always
        contributors.append(rb)
        combined = rb.risk

        if self._llm is not None and self._warrants_llm(rb.risk):
            llm = self._llm.assess_detailed(action)
            contributors.append(llm)
            combined = max_risk(combined, llm.risk)  # additive caution only

        ran = [c.analyzer for c in contributors]
        return RiskAssessment(
            risk=combined,
            rationale=f"ensemble of {', '.join(ran)} → {combined.value} (most cautious)",
            analyzer=self.name,
            contributors=contributors,
            self_assessed=action.self_assessed_risk,
        )


def build_default_analyzer(
    base_risk_by_tool: Mapping[str, SecurityRisk] | None = None,
    llm_scorer: Scorer | None = None,
    *,
    llm_trigger: SecurityRisk = SecurityRisk.MEDIUM,
) -> EnsembleAnalyzer:
    """The default ensemble: rule-based (always) + optional LLM (when warranted).
    `base_risk_by_tool` is injected by the wiring layer (which can see `tools`)."""
    rule = RuleBasedAnalyzer(base_risk_by_tool)
    llm = LLMBasedAnalyzer(llm_scorer) if llm_scorer is not None else None
    return EnsembleAnalyzer(rule, llm, llm_trigger=llm_trigger)
