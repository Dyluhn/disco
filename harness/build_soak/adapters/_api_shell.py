"""Bounded read-only shell analysis extracted behind the disco_api compatibility facade."""

from __future__ import annotations

import posixpath
import shlex

from ._api_browser import (
    _SHELL_META_SUBSTRINGS,
    _SHELL_META_TOKENS,
    _norm_rel,
)


def _shell_tokens(command: str) -> list[str]:
    try:
        return shlex.split(command)
    except ValueError:
        return command.split()


def _shell_pipeline_tokens(command: str) -> list[str] | None:
    """Tokenize a small shell pipeline while retaining control operators.

    This is used only to *prove* a command read-only.  Any syntax we do not understand returns
    None and therefore keeps the existing fail-safe opaque-mutation downgrade.
    """
    if not command or any(marker in command for marker in ("\n", "\r", "`", "$(", "${")):
        return None
    try:
        lexer = shlex.shlex(command, posix=True, punctuation_chars="|&;<>")
        lexer.whitespace_split = True
        lexer.commenters = ""
        return list(lexer)
    except ValueError:
        return None


def _curl_segment_is_read_only(tokens: list[str]) -> bool:
    """True for the bounded curl GET/probe surface used by build verification.

    curl has several local-file output switches, so this is an allowlist rather than a blacklist.
    ``-o /dev/null`` is the only permitted output target; response bodies otherwise go to stdout.
    """
    no_value = {
        "-s",
        "--silent",
        "-S",
        "--show-error",
        "-f",
        "--fail",
        "-L",
        "--location",
        "-I",
        "--head",
    }
    with_value = {"-w", "--write-out"}
    saw_url = False
    i = 1
    while i < len(tokens):
        token = tokens[i]
        if token in no_value:
            i += 1
            continue
        if token in with_value:
            # curl 8.3+ supports `%output{filename}` inside --write-out; accepting an
            # arbitrary format would let a command classified "read-only" overwrite a
            # declared file.  The live readiness probe needs only this stdout-only token.
            if i + 1 >= len(tokens) or tokens[i + 1] != "%{http_code}":
                return False
            i += 2
            continue
        if token in {"-o", "--output"}:
            if i + 1 >= len(tokens) or tokens[i + 1] != "/dev/null":
                return False
            i += 2
            continue
        if token.startswith("--output="):
            if token != "--output=/dev/null":
                return False
            i += 1
            continue
        if token == "--":
            return i + 1 < len(tokens) and all(
                part.startswith(("http://", "https://")) for part in tokens[i + 1 :]
            )
        if token.startswith(("http://", "https://")):
            saw_url = True
            i += 1
            continue
        return False
    return saw_url


def _http_server_segment_is_read_only(tokens: list[str]) -> bool:
    """Recognize only Python's standard static HTTP server invocation."""
    if len(tokens) not in {4, 6} or posixpath.basename(tokens[0]) not in {"python", "python3"}:
        return False
    if tokens[1:3] != ["-m", "http.server"] or not tokens[3].isdigit():
        return False
    if len(tokens) == 4:
        return True
    return tokens[4] in {"-b", "--bind", "-d", "--directory"} and bool(tokens[5])


def _matches_static_verify_command(command: str, declared: set[str]) -> bool:
    try:
        from disco.core.loop.finish.common import _static_verify_command

        return any(command == _static_verify_command(path) for path in declared)
    except (ImportError, TypeError, ValueError):
        return False


def _read_only_pipeline_segments(command: str) -> list[list[str]] | None:
    tokens = _shell_pipeline_tokens(command)
    if not tokens:
        return None
    operators = {"&&", "||", "|", "|&", ";"}
    segments: list[list[str]] = [[]]
    for token in tokens:
        if token in {"<", ">", ">>", "<<", "&"}:
            return None
        if token in operators:
            if not segments[-1]:
                return None
            segments.append([])
        else:
            segments[-1].append(token)
    if not segments[-1]:
        return None
    return segments


def _segment_is_proven_read_only(segment: list[str]) -> bool:
    program = posixpath.basename(segment[0])
    if program in {"test", "echo", "head", "grep"}:
        return True
    if program == "[":
        return segment[-1] == "]"
    if program == "cd":
        return len(segment) == 2
    if program == "curl":
        return _curl_segment_is_read_only(segment)
    return _http_server_segment_is_read_only(segment)


def _shell_is_proven_read_only(command: str, declared: set[str]) -> bool:
    """Conservatively prove that a shell action cannot alter declared workspace files.

    Unknown programs, redirects, substitutions, backgrounding, and malformed syntax remain
    opaque.  The one Python ``-c`` exception is compared to the product's own exact generated
    static-verifier command, avoiding a second permissive parser for arbitrary Python.
    """
    if any(marker in command for marker in ("\n", "\r", "`", "$(", "${")):
        return False
    if _matches_static_verify_command(command, declared):
        return True
    segments = _read_only_pipeline_segments(command)
    return bool(segments) and all(_segment_is_proven_read_only(segment) for segment in segments)


def _has_shell_meta(tokens: list[str]) -> bool:
    return any(
        tok in _SHELL_META_TOKENS or any(marker in tok for marker in _SHELL_META_SUBSTRINGS)
        for tok in tokens
    )


def _shell_norm_rel(path: str) -> str:
    norm = posixpath.normpath(_norm_rel(path))
    return "." if norm == "" else norm


def _split_flags_and_paths(tokens: list[str]) -> tuple[list[str], list[str]]:
    flags: list[str] = []
    paths: list[str] = []
    after_separator = False
    for tok in tokens:
        if not after_separator and tok == "--":
            after_separator = True
            continue
        if not after_separator and tok.startswith("-") and tok != "-":
            flags.append(tok)
            continue
        paths.append(tok)
    return flags, paths


def _rm_has_recursive_flag(flags: list[str]) -> bool:
    for flag in flags:
        if flag in {"-r", "-R", "--recursive"}:
            return True
        if flag.startswith("--"):
            continue
        if flag.startswith("-") and any(ch in flag[1:] for ch in ("r", "R")):
            return True
    return False


def _contains_relpath(parent: str, child: str) -> bool:
    if parent == child:
        return True
    if parent == ".":
        return child not in {"", "."}
    return child.startswith(f"{parent}/")


def _rm_removes_declared(tokens: list[str], norm_path: str) -> bool:
    flags, raw_paths = _split_flags_and_paths(tokens)
    declared = _shell_norm_rel(norm_path)
    targets = [_shell_norm_rel(p) for p in raw_paths]
    if declared in targets:
        return True
    if not _rm_has_recursive_flag(flags):
        return False
    return any(_contains_relpath(target, declared) for target in targets)


def _mv_removes_declared(tokens: list[str], norm_path: str) -> bool:
    _flags, raw_paths = _split_flags_and_paths(tokens)
    if len(raw_paths) != 2:
        return False
    declared = _shell_norm_rel(norm_path)
    source = _shell_norm_rel(raw_paths[0])
    dest = _shell_norm_rel(raw_paths[1])
    return source == declared and dest != declared


def _shell_removes(command: str, norm_path: str) -> bool:
    """True iff `command` is a PURE delete/rename that makes `norm_path` absent.

    Marking a declared file ABSENT is fail-FAST (unsatisfied → hard INVALID), so this must
    be strict, not fuzzy: the old basename fallback marked the ROOT deliverable absent when
    an export flow rm'd a COPY (export/index.html) — REL-6 EXPORT class false-INVALID
    (conv_bc52c276). Now: tokens are parsed shell-style, every token must belong to a pure
    rm/mv invocation (no zip/cp/&&/; compound ops — those go to the fail-safe OPAQUE
    downgrade instead), and the declared path must match by normalized relpath. Recursive rm
    of a parent directory marks declared children absent; a pure two-path mv marks the source
    absent. Anything less certain degrades to present_unproven, which only ever relaxes."""
    toks = _shell_tokens(command)
    if not toks or _has_shell_meta(toks):
        return False
    if toks[0] == "rm":
        return _rm_removes_declared(toks[1:], norm_path)
    if toks[0] == "mv":
        return _mv_removes_declared(toks[1:], norm_path)
    return False
