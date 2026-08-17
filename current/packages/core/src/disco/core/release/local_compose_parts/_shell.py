"""Safe-by-construction shell/exec-array rendering + small path/guard helpers.

Extracted from ``local_compose.py`` to reduce module size; the public facade
re-imports these names unchanged (indirectly, via the sibling parts that use
them).

This is a tier-1 (leaf) module within ``local_compose_parts``: besides the
sibling ``command_grammar`` module (a leaf of ``disco.core.release`` itself,
not a parent of this subpackage), it has no dependency on any sibling part or
on the parent ``local_compose`` module.
"""

from __future__ import annotations

import json

from disco.core.release.command_grammar import flag_env_ref, whole_env_ref

# The single fixed container port. The spec carries the port-env NAME (the `$PORT`
# contract), never a number; the adapter owns the concrete in-container port and
# injects it as `<port_env>=8080` so the app binds it, then publishes it on the
# host as `${HOST_PORT:-8080}`. Shared by several sibling parts (and the parent
# facade's `emit_local_compose`), so it lives here — the one dependency-free
# (tier-1) module every other part already depends on.
_CONTAINER_PORT = 8080
_HOST_PORT_VAR = "HOST_PORT"


def _norm_root(root: str) -> str:
    stripped = root.strip()
    if stripped.startswith("./"):
        stripped = stripped[2:]
    stripped = stripped.rstrip("/")
    return stripped


def _is_subroot(root: str) -> bool:
    return _norm_root(root) not in ("", ".")


def _required_guard(name: str) -> str:
    # A compose interpolation guard: unset -> `compose` errors with our message.
    return f"${{{name}:?Set {name} — see .env.example}}"


def _shell_quote_literal(token: str) -> str:
    """POSIX single-quote a literal so EVERY character in it is inert to the shell
    (no metacharacter, `$`, quote, or space is ever interpreted). A single quote
    inside the token is closed, escaped, and reopened (`'\\''`)."""
    return "'" + token.replace("'", "'\\''") + "'"


def _token_expands(token: str) -> bool:
    """Whether a token carries an env reference the shell must resolve — a WHOLE
    `${NAME}` reference OR the `--flag=${NAME}` value form (WO-C5 #2 F4). A token
    without one is a pure literal that needs no shell."""
    return whole_env_ref(token) is not None or flag_env_ref(token) is not None


def _shell_expand(argv: tuple[str, ...]) -> str:
    """Render an argv as a `sh -c` program body that is SAFE BY CONSTRUCTION (WO-C5
    §9.10). A token is expandable ONLY as a validated env reference: a WHOLE `${NAME}`
    token renders as a double-quoted `"${NAME}"`; a `--flag=${NAME}` token renders as
    the shell concatenation `'--flag='"${NAME}"` (the literal flag prefix single-quoted,
    the reference double-quoted) so `${NAME}` resolves at runtime instead of shipping
    broken (F4). EVERY other token is single-quoted as an inert literal. There is no
    `if "$" in token: paste it unquoted` path — a `$` can reach the shell source solely
    as a validated reference, never as a substitution/partial interpolation."""
    parts: list[str] = []
    for token in argv:
        name = whole_env_ref(token)
        if name is not None:
            parts.append(f'"${{{name}}}"')
            continue
        flag = flag_env_ref(token)
        if flag is not None:
            prefix, ref_name = flag
            parts.append(_shell_quote_literal(prefix) + f'"${{{ref_name}}}"')
            continue
        parts.append(_shell_quote_literal(token))
    return " ".join(parts)


def _exec_or_shell(argv: tuple[str, ...]) -> list[str]:
    """A container command as an exec array. When NO token carries an env reference,
    keep the pure exec form (no shell involved). When a token DOES (a whole `${NAME}`
    or a `--flag=${NAME}`), wrap in `sh -c 'exec ...'` whose body is built by
    `_shell_expand` — literals single-quoted, only the validated reference expanded —
    so a reference is never shipped as an inert exec-array literal (F4)."""
    if not any(_token_expands(token) for token in argv):
        return list(argv)
    return ["sh", "-c", "exec " + _shell_expand(argv)]


def _run_line(argv: tuple[str, ...]) -> str:
    return "RUN " + json.dumps(list(argv), ensure_ascii=False)


def _cmd_line(argv: tuple[str, ...]) -> str:
    return "CMD " + json.dumps(_exec_or_shell(argv), ensure_ascii=False)


def _copy_line(root: str, dest: str) -> str:
    if _is_subroot(root):
        return f"COPY {_norm_root(root)}/ {dest}"
    return f"COPY ./ {dest}"
