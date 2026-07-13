"""WO-C5 — safe-by-construction command / path / health / URL grammar.

The injection boundary of the release plane, factored into PURE functions so the
representation is safe BY CONSTRUCTION rather than by a growing blacklist (the §9
"forbidden shortcut"). Every function here either returns normally (the value is a
provably-safe literal, a whole typed env reference, or a restricted path/URL) or
raises a ``ValueError`` whose message names the FIELD and the RULE but NEVER echoes
the offending value — a rejected token can itself carry a secret, so the error text
is value-free by construction.

Three layers, each with a single responsibility:

* TOKEN HYGIENE (``check_token_hygiene``) — a per-argv-element representation guard
  applied to EVERY command field everywhere (install/build/migrate/start). An argv
  element is one exec-vector token, never a shell fragment: the only expandable form
  is an ENTIRE typed env reference (``${NAME}`` / ``$NAME``); anything else must be a
  plain literal drawn from a closed, shell-inert character set. `$` outside a whole
  reference, command substitution, backticks, separators, redirections, quotes,
  backslashes, control characters, NUL, the Unicode line/paragraph/space separators,
  an inline ``NAME=value`` assignment, and URL userinfo are all rejected — so a token
  can never smuggle a separator, a substitution, a partial interpolation, or an inline
  credential.

* RESTRICTED GRAMMARS (``check_health_path`` / ``check_workspace_rel_path`` /
  ``check_persistent_path`` / ``check_sqlite_local_url``) — the non-argv value fields
  are each pinned to a restricted grammar (an HTTP path, a workspace-relative POSIX
  path, an absolute POSIX resource path, a credential-free ``file:`` URL consistent
  with its persistent path) so they are safe to render as DATA and can never create a
  new Dockerfile line/flag/stage, alter a healthcheck program, or carry credentials.

* RUNTIME COMMAND GRAMMAR (``check_declaration_argv``) — a DECLARATION-boundary policy
  (the ``release_declare`` tool): an accepted start/build command must parse through a
  runtime-specific grammar — a SUPPORTED executable head plus known-safe flags — and
  the only value a flag may carry (beyond a known head flag) is a WHOLE DECLARED
  ``${NAME}`` env reference. There is no catch-all "arbitrary argv is probably fine"
  path: an unsupported executable, an unknown flag, an inline literal secret, or an
  undeclared expansion is rejected regardless of any secret-flag blacklist.

Layering: this is a leaf of ``disco.core`` — it imports ONLY ``re`` + the stdlib, so
both the spec models (``spec.py``) and the emitter (``local_compose.py``) can depend
on it, and the ``tools`` layer can import the runtime grammar downward.
"""

from __future__ import annotations

import re

# ---- whole env reference (the ONE expandable command-token form) --------------
#
# A WHOLE, entire env reference is EXACTLY ``${NAME}`` or ``$NAME`` where NAME is an
# uppercase env-var name and the reference is the ENTIRE token. A partial
# interpolation (``${PORT}suffix`` / ``prefix${PORT}``), an empty/braced-default form
# (``${}`` / ``${PORT:-x}``), or a substitution (``$(...)``) does NOT match — it is a
# `$`-bearing token that is not a whole reference and is therefore rejected by
# ``check_token_hygiene``.
_WHOLE_BRACED_REF_RE = re.compile(r"^\$\{([A-Z_][A-Z0-9_]*)\}$")
_WHOLE_BARE_REF_RE = re.compile(r"^\$([A-Z_][A-Z0-9_]*)$")

# The closed character set a PLAIN (non-reference) argv token may draw from:
# alphanumerics plus the punctuation that appears in real executables, script paths,
# module targets, and flags (``main:app``, ``--max-old-space-size=512``,
# ``--file=./schema.sql``, ``dist/server.js``). Deliberately EXCLUDES every POSIX
# shell metacharacter, quote, backslash, brace, glob char, tilde, whitespace, control
# char, and any non-ASCII byte (so the Unicode line/paragraph/no-break separators are
# rejected by construction). A token that is not a whole env reference must match this.
_ARGV_LITERAL_RE = re.compile(r"^[A-Za-z0-9._/:=@,+%-]+$")

# An inline ``NAME=...`` assignment, case-INSENSITIVE (the classic ``VAR=value cmd``
# secret-value smuggle). A legitimate flag (``--file=...``) starts with ``-`` and does
# NOT match; a whole reference starts with ``$`` and is handled before this.
_INLINE_ASSIGN_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")

# URL userinfo — a ``scheme://user[:password]@host`` authority. A credential carried
# in a URL literal (``https://user:pw@host``) is rejected; a plain URL without an
# ``@`` authority (``http://example.com/x``) does not match.
_URL_USERINFO_RE = re.compile(r"://[^/@\s]*@")

# A restricted HTTP path: a leading ``/`` then only unreserved path characters. No
# query, fragment, quote, ``$``, separator, or space can appear, so the path is safe
# to carry verbatim as DATA in a healthcheck URL and can never alter a probe program.
_HEALTH_PATH_RE = re.compile(r"^/[A-Za-z0-9._~/-]*$")

# A workspace-relative POSIX path (a service ``root`` / static ``output_dir`` /
# ``lockfile``): a closed safe charset, no leading ``/`` (absolute), no ``..`` segment,
# no control char — so a build-context source can never become absolute, traverse out
# of the context, or split a ``COPY`` line into a new Dockerfile instruction.
_REL_PATH_RE = re.compile(r"^[A-Za-z0-9._/-]+$")

# An absolute POSIX path (the path inside a ``file:`` resource URL): a leading ``/``
# then the same closed safe charset.
_ABS_PATH_RE = re.compile(r"^/[A-Za-z0-9._/-]*$")

# A resource ``persistent_path`` uses the same closed safe charset as a POSIX path but
# is not required to be absolute (the schema does not mandate it); metacharacters and
# control characters are still rejected.
_PERSISTENT_PATH_RE = re.compile(r"^[A-Za-z0-9._/-]+$")


def whole_env_ref(token: str) -> str | None:
    """The NAME of a token that is EXACTLY a whole env reference (``${NAME}`` or
    ``$NAME``), else ``None``. This is the sole predicate the safe renderer uses to
    decide a token is expandable — never a ``"$" in token`` substring test."""
    match = _WHOLE_BRACED_REF_RE.match(token) or _WHOLE_BARE_REF_RE.match(token)
    return match.group(1) if match else None


def flag_env_ref(token: str) -> tuple[str, str] | None:
    """For a ``--flag=${NAME}`` / ``--flag=$NAME`` single token — a flag prefix and a
    WHOLE env-reference value (the ``=``-joined form of ``--flag ${NAME}``, the
    sanctioned ``--token=${API_TOKEN}`` shape) — the ``(flag_prefix, NAME)`` pair where
    ``flag_prefix`` INCLUDES the trailing ``=``; else ``None``. The emitter uses it to
    EXPAND such a token (``'--flag='"${NAME}"``) instead of rendering it as an inert
    literal, so a `--flag=${NAME}` command never resolves broken (no accept-but-break).
    A non-reference value (``--x=${A}extra`` / ``--x=$(cmd)``) yields ``None`` — those
    are already rejected by ``check_token_hygiene``."""
    if token.startswith("-") and "=" in token:
        flag, _, value = token.partition("=")
        name = whole_env_ref(value)
        if _ARGV_LITERAL_RE.match(flag) and name is not None:
            return (flag + "=", name)
    return None


def check_token_hygiene(token: str, *, field: str) -> None:
    """Validate ONE argv token as a safe exec-vector element. Raises ``ValueError``
    (value-free) if the token is anything other than a whole env reference or a plain
    shell-inert literal. The order matters: a whole reference is accepted first; then a
    stray ``$`` (a non-whole-reference use — substitution / partial interpolation) is
    rejected; then an inline ``NAME=`` assignment; then URL userinfo; then any
    character outside the closed literal set (separators, quotes, backslash, control,
    NUL, Unicode separators)."""
    if whole_env_ref(token) is not None:
        return
    # A `--flag=${NAME}` single token: a flag prefix and a WHOLE-reference value (the
    # `=`-joined form of `--flag ${NAME}`). The value after the first `=` must itself be
    # an entire env reference, and the flag prefix a clean literal — so this admits the
    # sanctioned `--token=${API_TOKEN}` form without admitting a partial interpolation
    # (`--x=${A}extra` / `--x=$(cmd)` leave a non-reference value and are rejected).
    if token.startswith("-") and "=" in token:
        flag, _, value = token.partition("=")
        if _ARGV_LITERAL_RE.match(flag) and whole_env_ref(value) is not None:
            return
    if "$" in token:
        raise ValueError(
            f"{field} uses '$' outside a whole ${{NAME}} env reference (command "
            "substitution, a bare/partial variable, or a braced default is rejected); "
            "the only expandable token is an entire ${NAME} reference to a declared var"
        )
    if _INLINE_ASSIGN_RE.match(token):
        raise ValueError(
            f"{field} looks like an inline env assignment (NAME=...), which could "
            "smuggle a secret VALUE through a command token; declare env vars by NAME "
            "only, never as an argv element"
        )
    if _URL_USERINFO_RE.search(token):
        raise ValueError(
            f"{field} embeds credentials in a URL (userinfo before '@'); pass secrets "
            "as a whole ${NAME} reference to a declared env var, never inline"
        )
    if not _ARGV_LITERAL_RE.match(token):
        raise ValueError(
            f"{field} contains a disallowed character; an argv token is a single exec "
            "element (letters, digits and '._/:=@,+%-' only), never a shell fragment — "
            "separators, redirections, quotes, backslashes, control characters and "
            "non-ASCII separators are rejected"
        )


def check_health_path(value: str, *, field: str) -> None:
    """A health path is DATA in a restricted HTTP-path grammar: a leading ``/`` then
    only unreserved path characters, and no ``..`` segment. Rejects (value-free) any
    quote, ``$``, separator, CR/LF, or space that could alter a healthcheck program or
    split an HTTP request."""
    if not value.startswith("/"):
        raise ValueError(f"{field} must start with '/'")
    if not _HEALTH_PATH_RE.match(value):
        raise ValueError(
            f"{field} must be a plain HTTP path (a leading '/' then letters, digits "
            "and '._~/-' only); metacharacters, quotes, '$', CR/LF and spaces are "
            "rejected so the path is safe to carry as data"
        )
    if ".." in value.split("/"):
        raise ValueError(f"{field} must not contain a '..' path segment")


def check_workspace_rel_path(value: str, *, field: str) -> None:
    """A workspace-relative POSIX path (service ``root`` / static ``output_dir`` /
    ``lockfile``). Rejects (value-free) an absolute path, a ``..`` traversal segment, a
    NUL, and any character outside the closed safe set — so the path can never escape
    the build context or split a ``COPY``/``RUN`` line into a new Dockerfile
    instruction."""
    if not value.strip():
        raise ValueError(f"{field} must be a non-empty path")
    if value.startswith("/"):
        raise ValueError(f"{field} must be workspace-relative (no leading '/')")
    if "\x00" in value:
        raise ValueError(f"{field} must not contain a NUL byte")
    if not _REL_PATH_RE.match(value):
        raise ValueError(
            f"{field} contains a disallowed character; a workspace path may use letters, "
            "digits and '._/-' only (no whitespace, control characters, or shell "
            "metacharacters that could add a Dockerfile line)"
        )
    for segment in value.split("/"):
        if segment == "..":
            raise ValueError(f"{field} must not contain a '..' path segment")
        # A segment beginning with '-' would emit a `COPY -flag/ ...` line whose leading
        # token the Dockerfile parser reads as a COPY FLAG (a malformed instruction that
        # fails the build); reject it so a `root`/`output_dir` cannot spell a COPY flag.
        if segment.startswith("-"):
            raise ValueError(
                f"{field} must not contain a path segment beginning with '-' "
                "(it would be parsed as a Dockerfile COPY flag)"
            )


def check_persistent_path(value: str, *, field: str) -> None:
    """A resource ``persistent_path`` — the mount/volume path whose data must survive.
    Rejects (value-free) a NUL, a ``..`` segment, and any character outside the closed
    safe set so it carries no shell metacharacter or control character."""
    if not value.strip():
        raise ValueError(f"{field} must be a non-empty path")
    if "\x00" in value:
        raise ValueError(f"{field} must not contain a NUL byte")
    if not _PERSISTENT_PATH_RE.match(value):
        raise ValueError(
            f"{field} contains a disallowed character; a persistent path may use letters, "
            "digits and '._/-' only (no whitespace, control characters, or shell "
            "metacharacters)"
        )
    if ".." in value.split("/"):
        raise ValueError(f"{field} must not contain a '..' path segment")


def check_sqlite_local_url(url: str, persistent_path: str, *, field: str) -> None:
    """A sqlite resource's LOCAL url must be a credential-free ``file:`` URL with an
    absolute POSIX path CONSISTENT with ``persistent_path``. Rejects (value-free) a
    non-``file`` scheme, any authority/userinfo, a query, a fragment, a relative or
    metacharacter-bearing path, or a path that neither equals nor contains/​is-contained
    by ``persistent_path``."""
    if not url.startswith("file:"):
        raise ValueError(
            f"{field} local url must be a credential-free 'file:' URL "
            "(a non-file scheme or a database URL with userinfo is rejected)"
        )
    rest = url[len("file:") :]
    if rest.startswith("//"):
        raise ValueError(f"{field} local url must not carry an authority or userinfo")
    if "?" in rest or "#" in rest:
        raise ValueError(f"{field} local url must not carry a query or fragment")
    if not rest.startswith("/"):
        raise ValueError(f"{field} local url must have an absolute path")
    if "\x00" in rest or not _ABS_PATH_RE.match(rest):
        raise ValueError(f"{field} local url path contains a disallowed character")
    if ".." in rest.split("/"):
        raise ValueError(f"{field} local url path must not contain a '..' segment")
    # Consistency: the url's file path must EQUAL, contain, or be contained by the
    # persistent path (a file under its mount dir, or the dir of a file). Leading/
    # trailing slashes are normalized so an absolute url path (`/data/app.db`) is
    # consistent with a relative persistent path spelling of the same location
    # (`data/app.db`), while an unrelated path (`/other/x.db`) is still rejected.
    url_path = rest.strip("/")
    persist = persistent_path.strip("/")
    consistent = (
        url_path == persist
        or url_path.startswith(persist + "/")
        or persist.startswith(url_path + "/")
    )
    if not consistent:
        raise ValueError(
            f"{field} local url path is not consistent with the resource persistent_path"
        )


# ---- runtime command grammar (the DECLARATION-boundary policy) ----------------
#
# An ALLOWLIST of runtime executables the neutral base images actually run, and, per
# family, the known-safe flags. A start/build command that heads with anything else —
# or carries a flag that is neither a known head flag nor immediately followed by a
# whole DECLARED ${NAME} env reference — is an opaque/unsupported shape and is rejected
# (there is no "arbitrary argv is probably fine" path). ``python`` matches by prefix so
# a versioned interpreter (``python3.12``) resolves to the python family.
_NODE_FLAGS = frozenset(
    {
        "--max-old-space-size",
        "--enable-source-maps",
        "--experimental-vm-modules",
        "--experimental-specifier-resolution",
        "--no-warnings",
        "--trace-warnings",
        "--require",
        "-r",
        "--import",
        "--loader",
        "--conditions",
        "--openssl-legacy-provider",
    }
)
_NPM_FLAGS = frozenset(
    {
        "--frozen-lockfile",
        "--production",
        "--omit",
        "--if-present",
        "--silent",
        "--prefix",
        "--workspace",
        "--workspaces",
        "--offline",
        "--prefer-offline",
        "--no-audit",
        "--no-fund",
        "--no-save",
        "--loglevel",
        "--",
    }
)
_NPX_FLAGS = frozenset({"--yes", "-y", "--no-install", "--package", "-p", "--"})
_UVICORN_FLAGS = frozenset(
    {
        "--host",
        "--port",
        "--workers",
        "--log-level",
        "--reload",
        "--factory",
        "--app-dir",
        "--root-path",
        "--proxy-headers",
        "--forwarded-allow-ips",
        "--no-access-log",
        "--timeout-keep-alive",
        "--limit-concurrency",
        "--loop",
        "--http",
        "--lifespan",
        "--ssl-keyfile",
        "--ssl-certfile",
    }
)
_GUNICORN_FLAGS = frozenset(
    {
        "--bind",
        "-b",
        "--workers",
        "-w",
        "--worker-class",
        "-k",
        "--config",
        "-c",
        "--timeout",
        "-t",
        "--threads",
        "--access-logfile",
        "--error-logfile",
        "--log-level",
        "--chdir",
        "--preload",
        "--forwarded-allow-ips",
        "--graceful-timeout",
        "--keep-alive",
        "--max-requests",
        "--worker-tmp-dir",
        "--name",
        "-n",
    }
)
_HYPERCORN_FLAGS = frozenset(
    {
        "--bind",
        "-b",
        "--workers",
        "-w",
        "--worker-class",
        "-k",
        "--config",
        "-c",
        "--access-logfile",
        "--error-logfile",
        "--log-level",
        "--root-path",
        "--keep-alive",
        "--graceful-timeout",
    }
)
_PYTHON_FLAGS = frozenset(
    {"-m", "-u", "-O", "-OO", "-B", "-W", "-I", "-E", "-s", "-S", "-X", "-b", "-q"}
)

_KNOWN_FLAGS: dict[str, frozenset[str]] = {
    "node": _NODE_FLAGS,
    "npm": _NPM_FLAGS,
    "yarn": _NPM_FLAGS,
    "pnpm": _NPM_FLAGS,
    "npx": _NPX_FLAGS,
    "python": _PYTHON_FLAGS,
    "uvicorn": _UVICORN_FLAGS,
    "gunicorn": _GUNICORN_FLAGS,
    "hypercorn": _HYPERCORN_FLAGS,
}


def _head_family(head: str) -> str | None:
    """The supported runtime family for a command head (basename, lowercased), or
    ``None`` if the executable is not one the neutral base images run."""
    if head.startswith("python"):
        return "python"
    if head in _KNOWN_FLAGS:
        return head
    return None


def check_declaration_argv(
    argv: tuple[str, ...], *, declared_names: frozenset[str], field: str
) -> None:
    """Parse a DECLARED start/build command through the runtime-specific grammar.

    Raises ``ValueError`` (value-free) unless: the head is a supported runtime
    executable; every whole ``${NAME}`` reference names a DECLARED env var; and every
    flag is either a known head flag or is immediately followed by a whole DECLARED
    ``${NAME}`` value (or is a ``--flag=${NAME}`` single token whose NAME is declared).
    A boolean/unknown flag, an inline literal value after an unknown flag (every secret
    CLI form: ``--token VALUE`` / ``--password VALUE`` / …), and a URL-userinfo literal
    are rejected — independent of any secret-flag blacklist. An empty command is a
    no-op (the emptiness contract lives in the schema)."""
    if not argv:
        return
    head = argv[0].rsplit("/", 1)[-1].lower()
    family = _head_family(head)
    if family is None:
        raise ValueError(
            f"{field} must start with a supported runtime executable "
            "(node/npm/npx/yarn/pnpm, python, or uvicorn/gunicorn/hypercorn); an "
            "arbitrary program, shell, or path is rejected"
        )
    known = _KNOWN_FLAGS[family]
    index = 1
    while index < len(argv):
        token = argv[index]
        ref = whole_env_ref(token)
        if ref is not None:
            if ref not in declared_names:
                raise ValueError(
                    f"{field} references an env var that is not declared in required_env "
                    "(the only expandable token is a whole ${NAME} reference to a "
                    "declared var, or the $PORT contract)"
                )
            index += 1
            continue
        if token.startswith("-"):
            name = token.split("=", 1)[0]
            if name in known:
                index += 1
                continue
            if "=" in token:
                value_ref = whole_env_ref(token.split("=", 1)[1])
                if value_ref is not None and value_ref in declared_names:
                    index += 1
                    continue
                raise ValueError(
                    f"{field} carries an unknown flag or an inline literal value; a flag "
                    "value must be a whole ${NAME} reference to a declared env var"
                )
            following = argv[index + 1] if index + 1 < len(argv) else None
            following_ref = whole_env_ref(following) if following is not None else None
            if following_ref is not None and following_ref in declared_names:
                index += 2
                continue
            raise ValueError(
                f"{field} carries an unknown flag or an inline literal value; a flag "
                "value must be a whole ${NAME} reference to a declared env var"
            )
        # A non-flag operand (script / module / subcommand): already hygiene-checked as
        # a shell-inert literal, so it has a defined role as a command argument.
        index += 1


__all__ = [
    "check_declaration_argv",
    "check_health_path",
    "check_persistent_path",
    "check_sqlite_local_url",
    "check_token_hygiene",
    "check_workspace_rel_path",
    "flag_env_ref",
    "whole_env_ref",
]
