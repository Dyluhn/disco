"""Shell hard-deny parsing — split from ``security/analyzers.py``.

Cluster 3 — HARD DENY: catastrophic, never-allowed commands.  Distinct from
HIGH (which routes to human confirmation): these are refused outright by the
loop BEFORE the confirm gate — no approval, no policy, no LLM can run them.

Also includes the shlex-based recursive-delete-of-protected-root analyzer that
a regex cannot parse reliably (``--`` end-of-options, long options, flag
clustering/reordering, quoting, ``$VAR`` indirection, ``$(...)``/backtick
substitution, path traversal).
"""

from __future__ import annotations

import posixpath
import re
import shlex

from .shell_substitution import (
    _SUBSTITUTION_MAX_DEPTH,
    active_shell_substitutions,
)


def without_shell_line_continuations(command: str) -> str:
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
SHELL_DENY: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"\bmkfs\b"), "format a filesystem (irreversible)"),
    (re.compile(r"\bdd\b[^\n;|&]*\bof=/dev/(sd|nvme|hd|disk|vd)"), "raw write to a disk device"),
    (re.compile(r">\s*/dev/(sd|nvme|hd|disk|vd)"), "redirect to a raw disk device"),
    (re.compile(r":\(\)\s*\{\s*:?\s*\|?\s*:?\s*&?\s*\}\s*;?\s*:"), "fork bomb"),
]

# Whole-system paths whose RECURSIVE deletion is catastrophic and never a
# legitimate build step. Deliberately narrow: subpaths (`/etc/myapp`, `/tmp/x`,
# `./build`, `node_modules`, `dist/`) are NOT here — this floor cannot be
# overridden, so a false positive would block ordinary builds.
PROTECTED_ROOTS: frozenset[str] = frozenset(
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
SHELL_WRAPPERS: frozenset[str] = frozenset({"sh", "bash", "zsh", "dash", "ash", "ksh"})

# Transparent prefix commands: they run the command that FOLLOWS, so the real
# command word is past them (`sudo rm …`, `env VAR=x rm …`). Skipping these — and
# leading `NAME=val` assignments — is what lets us analyze the COMMAND position
# only, so `echo rm -rf /` (rm is an ARGUMENT, harmless) is never a false positive.
CMD_PREFIXES: frozenset[str] = frozenset(
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
SEG_OPERATORS: frozenset[str] = frozenset({";", "&", "&&", "|", "||", "\n"})

# Reserved words / grouping tokens after which the next token is in command
# position.  Stripping these before command-head analysis catches the body of
# ``if/then``, ``for/do``, subshell, and brace-group constructs without treating
# an ordinary argument named ``rm`` as executable.
COMMAND_POSITION_PREFIXES: frozenset[str] = frozenset(
    {"!", "(", "{", "case", "do", "elif", "else", "for", "if", "select", "then", "until", "while"}
)

# find's leading GLOBAL options (precede the paths). `-D`/`-O` additionally take
# an argument.
FIND_GLOBAL_OPTS: frozenset[str] = frozenset({"-H", "-L", "-P"})


def var_assignments(command: str) -> dict[str, str]:
    """Best-effort scan of `NAME=value` assignments so a later `rm -rf "$X"` can
    be resolved when an earlier segment set `X=/`. One level, simple unquoted
    values only — intentionally conservative (used only to CATCH, never to
    exonerate)."""
    out: dict[str, str] = {}
    for m in re.finditer(r"(?:^|[;&|\n]|&&|\|\|)\s*([A-Za-z_]\w*)=([^\s;&|]+)", command):
        out[m.group(1)] = m.group(2).strip("\"'")
    return out


def resolve_target(token: str, assignments: dict[str, str]) -> str:
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


def c_flag_arg_index(tokens: list[str]) -> int | None:
    """Index of the command STRING a shell interpreter's `-c` selects, handling a
    bare `-c` AND clustered forms (`-lc`, `-ec`, `-xc`). None if absent."""
    for i, t in enumerate(tokens):
        if t.startswith("-") and not t.startswith("--") and "c" in t[1:]:
            return i + 1 if i + 1 < len(tokens) else None
    return None


def rm_recursive_operands(rest: list[str]) -> tuple[bool, list[str]]:
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


def find_deny(rest: list[str], assignments: dict[str, str]) -> str | None:
    """`find <protected-root> … (-delete | -exec rm …)` wipes a root without an
    explicit rm operand. Conservative: requires a protected-root PATH arg (find's
    paths precede the first `-expression`), so `find . -delete` stays allowed."""
    # Skip find's leading GLOBAL options (-H/-L/-P, and -D/-O <arg>) before paths.
    i = 0
    while i < len(rest):
        if rest[i] in FIND_GLOBAL_OPTS:
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
    if not any(resolve_target(p, assignments) in PROTECTED_ROOTS for p in paths):
        return None
    # `-exec /bin/rm …` is still rm — match by basename, not the literal token.
    has_rm = any(t.rsplit("/", 1)[-1] == "rm" for t in rest)
    destructive = "-delete" in rest or (("-exec" in rest or "-execdir" in rest) and has_rm)
    return "recursive find-delete of a protected system path" if destructive else None


def analyze_command(
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
    if base in SHELL_WRAPPERS:
        j = c_flag_arg_index(rest)
        return (
            rm_protected_root_deny(rest[j], _depth=depth + 1, _budget=budget)
            if j is not None
            else None
        )
    if base == "find":
        return find_deny(rest, assignments)
    if base != "rm":
        return None
    recursive, operands = rm_recursive_operands(rest)
    if not recursive:
        return None
    for op in operands:
        if "$(" in op or "`" in op:  # substitution we refuse to evaluate
            return "recursive rm with command substitution (unevaluable target)"
        if resolve_target(op, assignments) in PROTECTED_ROOTS:
            return f"recursive delete of a protected system path ({op!r})"
    return None


def _strip_leading_assignments(tokens: list[str]) -> list[str]:
    """Skip leading ``NAME=val`` env-assignments (``VAR=val cmd``)."""
    i = 0
    while i < len(tokens) and re.fullmatch(r"[A-Za-z_]\w*=.*", tokens[i]):
        i += 1
    return tokens[i:]


def _normalize_wrappers(toks: list[str]) -> list[str]:
    """Normalize command-position wrappers to a fixed point.

    A valid Bash command may compose control words, assignments, coproc, and
    function syntax in any of these command-position layers
    (``then coproc X=1 rm ...``).
    """
    while toks:
        before = len(toks)
        while toks and re.fullmatch(r"[A-Za-z_]\w*=.*", toks[0]):
            toks = toks[1:]
        while toks and toks[0] in COMMAND_POSITION_PREFIXES:
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
    return toks


def _scan_prefix_for_dangerous(
    toks: list[str],
    assignments: dict[str, str],
    *,
    depth: int,
    budget: list[int],
) -> str | None:
    """Scan past a transparent prefix for the first rm/find/wrapper command word."""
    for k in range(1, len(toks)):
        b = toks[k].rsplit("/", 1)[-1]
        if b in ("rm", "find") or b in SHELL_WRAPPERS:
            reason = analyze_command(
                b,
                toks[k + 1 :],
                assignments,
                depth=depth,
                budget=budget,
            )
            if reason is not None:
                return reason
    return None


def rm_tokens_deny(
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
    toks = _strip_leading_assignments(tokens)
    toks = _normalize_wrappers(toks)
    if not toks:
        return None
    base0 = toks[0].rsplit("/", 1)[-1]
    if base0 in CMD_PREFIXES:
        return _scan_prefix_for_dangerous(toks, assignments, depth=depth, budget=budget)
    return analyze_command(base0, toks[1:], assignments, depth=depth, budget=budget)


def without_shell_comments(command: str) -> str:
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


def split_command_segments(command: str) -> list[list[str]] | None:
    """Quote-AWARE split of a command line into per-command token lists, cut on the
    shell control operators (`; & && | ||`). Because shlex respects quotes, a `;`
    or `|` INSIDE quotes stays part of the word — so `printf 'rm -rf /;'` is one
    benign `printf` command, not a spurious `rm` segment (the false positive a
    naive regex split produced). Returns None if the whole command has unbalanced
    quotes (malformed)."""
    lex = shlex.shlex(without_shell_comments(command), posix=True, punctuation_chars=";&|()\n")
    lex.whitespace_split = True
    lex.whitespace = " \t\r"
    lex.commenters = ""
    try:
        raw = list(lex)
    except ValueError:
        return None
    segs: list[list[str]] = [[]]
    for t in raw:
        if t in SEG_OPERATORS or (t and all(character in ";&|()" for character in t)):
            segs.append([])
        else:
            segs[-1].append(t)
    return [s for s in segs if s]


def nested_substitution_deny(command: str, *, depth: int, budget: list[int]) -> str | None:
    substitutions, malformed = active_shell_substitutions(command, budget=budget)
    if malformed is not None:
        return malformed
    if substitutions and depth >= _SUBSTITUTION_MAX_DEPTH:
        return "nested shell substitution exceeds analysis bounds"
    for kind, body in substitutions:
        if kind == "arithmetic":
            reason = nested_substitution_deny(body, depth=depth + 1, budget=budget)
        else:
            reason = rm_protected_root_deny(body, _depth=depth + 1, _budget=budget)
        if reason is not None:
            if reason in {
                "nested shell substitution exceeds analysis bounds",
                "nested shell heredoc is not safely analyzable",
                "unparseable nested shell substitution",
            }:
                return reason
            return f"destructive command inside {kind} substitution: {reason}"
    return None


def rm_protected_root_deny(
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
    command = without_shell_line_continuations(command)
    budget = _budget if _budget is not None else [0, 0]
    nested_reason = nested_substitution_deny(command, depth=_depth, budget=budget)
    if nested_reason is not None:
        return nested_reason
    assignments = var_assignments(command)
    segs = split_command_segments(command)
    if segs is None:
        # Genuinely unbalanced quotes (malformed, wouldn't run). Only refuse if it
        # clearly reads as a recursive rm of a rooted path; else don't guess.
        if re.search(r"\brm\b[^\n]*-{1,2}[A-Za-z]*r", command, re.I) and re.search(
            r"\s/(\s|\*|$)", command
        ):
            return "recursive rm of a protected path (unparseable command)"
        return None
    for tokens in segs:
        reason = rm_tokens_deny(tokens, assignments, depth=_depth, budget=budget)
        if reason:
            return reason
    return None


def hard_deny_reason(command: str) -> str | None:
    """Return a reason string if a shell command is HARD-DENIED (never runnable),
    else None. Pure + deterministic. The loop refuses denied actions outright."""
    normalized = without_shell_line_continuations(command)
    low = normalized.lower()
    for pat, why in SHELL_DENY:
        if pat.search(low):
            return why
    return rm_protected_root_deny(normalized)
