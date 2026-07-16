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

import ipaddress
import math
import re
from typing import NamedTuple

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
_PUBLIC_BIND_BRACED_REF_RE = re.compile(r"^0\.0\.0\.0:\$\{([A-Z_][A-Z0-9_]*)\}$")
_PUBLIC_BIND_BARE_REF_RE = re.compile(r"^0\.0\.0\.0:\$([A-Z_][A-Z0-9_]*)$")

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


def public_bind_env_ref(token: str) -> str | None:
    """The env NAME in the one sanctioned combined host/port token.

    Gunicorn and Hypercorn require a combined ``host:port`` value, while the release
    contract deliberately carries a provider-neutral port NAME rather than a local adapter
    number. Exactly ``0.0.0.0:${NAME}`` or ``0.0.0.0:$NAME`` is accepted. No other partial
    interpolation is recognized, and callers must still prove NAME is declared.
    """
    match = _PUBLIC_BIND_BRACED_REF_RE.match(token) or _PUBLIC_BIND_BARE_REF_RE.match(token)
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


def check_token_hygiene(token: str, *, field: str, allow_public_bind: bool = False) -> None:
    """Validate ONE argv token as a safe exec-vector element. Raises ``ValueError``
    (value-free) if the token is anything other than a whole env reference or a plain
    shell-inert literal. The order matters: a whole reference is accepted first; then a
    stray ``$`` (a non-whole-reference use — substitution / partial interpolation) is
    rejected; then an inline ``NAME=`` assignment; then URL userinfo; then any
    character outside the closed literal set (separators, quotes, backslash, control,
    NUL, Unicode separators)."""
    if whole_env_ref(token) is not None:
        return
    # The combined Gunicorn/Hypercorn bind is a narrowly context-bound exception, not
    # a generally expandable argv token. Only the structured effective-start parser (or
    # a start-field schema validator that immediately re-parses that context) may opt in.
    if allow_public_bind and public_bind_env_ref(token) is not None:
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
    # A control character (NUL / CR / LF / tab / …) must be rejected explicitly: the
    # `_REL_PATH_RE` anchors with `$`, which matches BEFORE a single trailing `\n`, so a
    # value like `dist\n` would otherwise slip past the regex and split the emitted
    # `COPY /app/<output_dir>/` line into a new Dockerfile instruction (R2 / G06).
    if any(ch < " " or ch == "\x7f" for ch in value):
        raise ValueError(f"{field} must not contain a control character (NUL, CR, LF, tab, …)")
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


class ParsedCommandOption(NamedTuple):
    """One canonical option from a proven runtime start command."""

    name: str
    value: str | None


class EffectiveStartCommand(NamedTuple):
    """The single structured interpretation shared by declaration and detection.

    ``runtime`` is the selected neutral image family. Python server commands carry the
    effective server plus their dotted target; direct Node commands carry ``node_script``;
    npm commands carry the exact package script name. No other typed start shape is
    represented, so callers cannot accidentally infer a runnable candidate from an opaque
    executable or a second ad-hoc scan.
    """

    argv: tuple[str, ...]
    runtime: str
    executable: str
    server: str | None = None
    server_index: int | None = None
    target_module: str | None = None
    target_attribute: str | None = None
    node_script: str | None = None
    npm_script: str | None = None
    options: tuple[ParsedCommandOption, ...] = ()

    def option_value(self, name: str) -> str | None:
        for option in self.options:
            if option.name == name:
                return option.value
        return None


_PYTHON_START_OPTIONS = frozenset({"-b", "-bb", "-B", "-O", "-OO", "-u"})
_SERVER_TARGET_RE = re.compile(
    r"^([A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)*):"
    r"([A-Za-z_][A-Za-z0-9_]*)$"
)

# option spelling -> (canonical spelling, takes a value)
_SERVER_OPTION_SPECS: dict[str, dict[str, tuple[str, bool]]] = {
    "uvicorn": {
        "--host": ("--host", True),
        "--port": ("--port", True),
        "--workers": ("--workers", True),
        "--log-level": ("--log-level", True),
        "--reload": ("--reload", False),
        "--factory": ("--factory", False),
        "--app-dir": ("--app-dir", True),
        "--root-path": ("--root-path", True),
        "--proxy-headers": ("--proxy-headers", False),
        "--forwarded-allow-ips": ("--forwarded-allow-ips", True),
        "--no-access-log": ("--no-access-log", False),
        "--timeout-keep-alive": ("--timeout-keep-alive", True),
        "--limit-concurrency": ("--limit-concurrency", True),
        "--loop": ("--loop", True),
        "--http": ("--http", True),
        "--lifespan": ("--lifespan", True),
        "--ssl-keyfile": ("--ssl-keyfile", True),
        "--ssl-certfile": ("--ssl-certfile", True),
    },
    "gunicorn": {
        "--bind": ("--bind", True),
        "-b": ("--bind", True),
        "--workers": ("--workers", True),
        "-w": ("--workers", True),
        "--worker-class": ("--worker-class", True),
        "-k": ("--worker-class", True),
        "--config": ("--config", True),
        "-c": ("--config", True),
        "--timeout": ("--timeout", True),
        "-t": ("--timeout", True),
        "--threads": ("--threads", True),
        "--access-logfile": ("--access-logfile", True),
        "--error-logfile": ("--error-logfile", True),
        "--log-level": ("--log-level", True),
        "--chdir": ("--chdir", True),
        "--preload": ("--preload", False),
        "--forwarded-allow-ips": ("--forwarded-allow-ips", True),
        "--graceful-timeout": ("--graceful-timeout", True),
        "--keep-alive": ("--keep-alive", True),
        "--max-requests": ("--max-requests", True),
        "--worker-tmp-dir": ("--worker-tmp-dir", True),
        "--name": ("--name", True),
        "-n": ("--name", True),
    },
    "hypercorn": {
        "--bind": ("--bind", True),
        "-b": ("--bind", True),
        "--workers": ("--workers", True),
        "-w": ("--workers", True),
        "--worker-class": ("--worker-class", True),
        "-k": ("--worker-class", True),
        "--config": ("--config", True),
        "-c": ("--config", True),
        "--access-logfile": ("--access-logfile", True),
        "--error-logfile": ("--error-logfile", True),
        "--log-level": ("--log-level", True),
        "--root-path": ("--root-path", True),
        "--keep-alive": ("--keep-alive", True),
        "--graceful-timeout": ("--graceful-timeout", True),
    },
}

_NODE_START_VALUE_OPTIONS = {
    "--max-old-space-size",
    "--experimental-specifier-resolution",
    "--require",
    "-r",
    "--import",
    "--loader",
    "--conditions",
}
_NODE_START_BOOLEAN_OPTIONS = {
    "--enable-source-maps",
    "--experimental-vm-modules",
    "--no-warnings",
    "--trace-warnings",
    "--openssl-legacy-provider",
}


def _declared_reference_name(value: str) -> str | None:
    return whole_env_ref(value) or public_bind_env_ref(value)


def _check_declared_reference(value: str, declared_names: frozenset[str], *, field: str) -> None:
    name = _declared_reference_name(value)
    if name is not None and name not in declared_names:
        raise ValueError(
            f"{field} references an env var that is not declared; only a whole declared "
            "${NAME} reference or the exact public-bind form 0.0.0.0:${NAME} is allowed"
        )


def _positive_integer(value: str, *, option: str, allow_zero: bool = False) -> None:
    if not value.isdigit() or (int(value) < (0 if allow_zero else 1)):
        qualifier = "non-negative" if allow_zero else "positive"
        raise ValueError(f"{option} must be a {qualifier} integer")


# The gunicorn worker classes gunicorn BUNDLES — the only ones provable without a forgotten
# config dependency. gunicorn imports the worker class at startup (``util.load_class`` inside
# ``Arbiter.setup``, BEFORE it binds a socket), and the modules for every other worker
# (``gevent``/``eventlet``/``tornado``/a custom dotted path) do ``import <extra>`` at module
# scope, so a worker class outside this set whose distribution is unprovable raises at boot and
# aborts the master before any worker can bind. This is the SINGLE ratified ceiling shared by
# BOTH surfaces that can select the worker class: the CLI ``--worker-class`` flag proven just
# below, AND the module-scope ``worker_class = "<str>"`` config setting proven in
# ``detect._gunicorn_config_blocker`` (``detect`` reads THIS constant, so the config and the CLI
# cannot drift). Widening it is an owner decision that must loosen both surfaces together.
GUNICORN_BUILTIN_WORKER_CLASSES = frozenset({"sync", "gthread"})


def _validate_server_option_value(server: str, option: str, value: str) -> None:
    """Validate the runtime domain, not merely the spelling, of a server option."""
    reference = whole_env_ref(value)
    if reference is not None:
        if server == "uvicorn" and option == "--port":
            return
        raise ValueError(f"{server} {option} cannot be domain-proven from an env reference")
    if option == "--bind":
        if public_bind_env_ref(value) is not None:
            return
        match = re.fullmatch(r"(?:0\.0\.0\.0|127\.0\.0\.1|localhost):([0-9]+)", value)
        if match is None:
            raise ValueError(f"{server} --bind must be a proven TCP host:port")
        _positive_integer(match.group(1), option=f"{server} --bind port")
        if int(match.group(1)) > 65535:
            raise ValueError(f"{server} --bind port is outside the TCP range")
        return
    if option == "--host":
        if value not in {"0.0.0.0", "127.0.0.1", "localhost"}:
            raise ValueError(f"{server} --host is outside the proven host set")
        return
    if option == "--port":
        _positive_integer(value, option=f"{server} --port")
        if int(value) > 65535:
            raise ValueError(f"{server} --port is outside the TCP range")
        return
    if option in {"--workers", "--threads", "--limit-concurrency"}:
        _positive_integer(value, option=f"{server} {option}")
        return
    if option in {
        "--timeout",
        "--timeout-keep-alive",
        "--graceful-timeout",
        "--keep-alive",
        "--max-requests",
    }:
        _positive_integer(value, option=f"{server} {option}", allow_zero=True)
        return
    if option == "--log-level":
        allowed = {"critical", "error", "warning", "info", "debug"}
        if server == "uvicorn":
            allowed.add("trace")
        if value.lower() not in allowed:
            raise ValueError(f"{server} --log-level is outside the proven enum")
        return
    if option == "--loop":
        if value not in {"auto", "asyncio", "uvloop"}:
            raise ValueError("uvicorn --loop is outside the supported enum")
        return
    if option == "--http":
        if value not in {"auto", "h11", "httptools"}:
            raise ValueError("uvicorn --http is outside the supported enum")
        return
    if option == "--lifespan":
        if value not in {"auto", "on", "off"}:
            raise ValueError("uvicorn --lifespan is outside the supported enum")
        return
    if option == "--worker-class":
        allowed = GUNICORN_BUILTIN_WORKER_CLASSES if server == "gunicorn" else {"asyncio"}
        if value not in allowed:
            raise ValueError(f"{server} --worker-class is outside the built-in proven set")
        return
    if option in {"--access-logfile", "--error-logfile"}:
        if value == "-":
            return
        check_workspace_rel_path(value, field=f"{server} {option}")
        return
    if option in {"--config", "--app-dir", "--chdir", "--worker-tmp-dir"}:
        check_workspace_rel_path(value, field=f"{server} {option}")
        return
    if option == "--root-path":
        check_health_path(value, field=f"{server} --root-path")
        return
    if option == "--forwarded-allow-ips":
        if value != "*" and re.fullmatch(r"[A-Za-z0-9:.,_-]+", value) is None:
            raise ValueError(f"{server} --forwarded-allow-ips is outside the proven syntax")
        return
    if option == "--name":
        if re.fullmatch(r"[A-Za-z0-9_.-]+", value) is None:
            raise ValueError("gunicorn --name is outside the proven syntax")
        return
    if option in {"--ssl-keyfile", "--ssl-certfile"}:
        raise ValueError("TLS material is outside the statically proven release ceiling")
    raise ValueError(f"{server} {option} has no closed value-domain proof")


def _parse_server_options(
    argv: tuple[str, ...],
    *,
    server: str,
    server_index: int,
    declared_names: frozenset[str],
    field: str,
) -> tuple[tuple[ParsedCommandOption, ...], str, str]:
    specs = _SERVER_OPTION_SPECS[server]
    options: list[ParsedCommandOption] = []
    seen: set[str] = set()
    positionals: list[str] = []
    tokens = argv[server_index + 1 :]
    index = 0
    while index < len(tokens):
        token = tokens[index]
        if token.startswith("-"):
            spelling = token
            joined_value: str | None = None
            if "=" in token:
                spelling, _, joined_value = token.partition("=")
            spec = specs.get(spelling)
            if spec is None:
                raise ValueError(
                    f"{field} carries an unknown {server} option; even an env-valued "
                    "unknown option is rejected because its runtime meaning is unproved"
                )
            canonical, takes_value = spec
            if canonical in seen:
                raise ValueError(
                    f"{field} repeats a {server} option (including an alias); duplicate "
                    "last-wins semantics are ambiguous and rejected"
                )
            seen.add(canonical)
            if not takes_value:
                if joined_value is not None:
                    raise ValueError(
                        f"{field} assigns a value to a boolean {server} option; boolean "
                        "options must be present without =VALUE"
                    )
                if server == "uvicorn" and canonical == "--reload":
                    raise ValueError("uvicorn --reload is a development-only release mode")
                options.append(ParsedCommandOption(canonical, None))
                index += 1
                continue
            if joined_value is not None:
                if not spelling.startswith("--") or not joined_value:
                    raise ValueError(
                        f"{field} uses an empty or unsupported joined {server} option value"
                    )
                value = joined_value
                index += 1
            else:
                next_is_stdout = (
                    index + 1 < len(tokens)
                    and tokens[index + 1] == "-"
                    and canonical in {"--access-logfile", "--error-logfile"}
                )
                if index + 1 >= len(tokens) or (
                    tokens[index + 1].startswith("-") and not next_is_stdout
                ):
                    raise ValueError(f"{field} has a value-taking {server} option with no value")
                value = tokens[index + 1]
                index += 2
            if public_bind_env_ref(value) is not None and canonical != "--bind":
                raise ValueError(
                    f"{field} may use the combined public host/env reference only as "
                    "the structured gunicorn/hypercorn --bind value"
                )
            _check_declared_reference(value, declared_names, field=field)
            _validate_server_option_value(server, canonical, value)
            options.append(ParsedCommandOption(canonical, value))
            continue
        positionals.append(token)
        index += 1
    if len(positionals) != 1:
        raise ValueError(
            f"{field} must carry exactly one dotted module:attribute application target"
        )
    target = _SERVER_TARGET_RE.fullmatch(positionals[0])
    if target is None:
        raise ValueError(
            f"{field} application target must be a dotted module:attribute (no path, "
            "hyphen, missing module, or missing attribute)"
        )
    return tuple(options), target.group(1), target.group(2)


def _effective_python_start(
    argv: tuple[str, ...], *, declared_names: frozenset[str], field: str
) -> EffectiveStartCommand:
    executable = argv[0]
    if executable in _SERVER_OPTION_SPECS:
        server = executable
        server_index = 0
    else:
        index = 1
        while index < len(argv) and argv[index] in _PYTHON_START_OPTIONS:
            index += 1
        if index >= len(argv) or argv[index] != "-m" or index + 1 >= len(argv):
            raise ValueError(
                f"{field} python starts must use a proven `python -m "
                "uvicorn|gunicorn|hypercorn` server mode; scripts, -c, --, help/version, "
                "and opaque modules are rejected"
            )
        server = argv[index + 1]
        if server not in _SERVER_OPTION_SPECS:
            raise ValueError(
                f"{field} python -m module is not a proven release server; only "
                "uvicorn, gunicorn, and hypercorn are supported"
            )
        server_index = index + 1
    options, module, attribute = _parse_server_options(
        argv,
        server=server,
        server_index=server_index,
        declared_names=declared_names,
        field=field,
    )
    return EffectiveStartCommand(
        argv=argv,
        runtime="python",
        executable=executable,
        server=server,
        server_index=server_index,
        target_module=module,
        target_attribute=attribute,
        options=options,
    )


def _effective_node_start(argv: tuple[str, ...], *, field: str) -> EffectiveStartCommand:
    executable = argv[0]
    if executable == "npm":
        if argv[1:] == ("start",) or argv[1:] == ("run", "start"):
            return EffectiveStartCommand(
                argv=argv, runtime="node", executable=executable, npm_script="start"
            )
        raise ValueError(
            f"{field} npm start must be exactly `npm start` or `npm run start`; flags and "
            "other scripts are not a proven long-running ingress"
        )
    if executable != "node":
        raise ValueError(
            f"{field} node runtime supports only a direct `node <file>` or exact npm start; "
            "npx and opaque launchers are rejected"
        )
    positionals: list[str] = []
    index = 1
    while index < len(argv):
        token = argv[index]
        if token.startswith("-"):
            raise ValueError(
                f"{field} direct node starts must be exactly `node <workspace-file>`; "
                "runtime flags and preload hooks are outside the proven release shape"
            )
        positionals.append(token)
        index += 1
    if len(positionals) != 1:
        raise ValueError(
            f"{field} direct node start must carry exactly one workspace script and no "
            "opaque positional arguments"
        )
    if whole_env_ref(positionals[0]) is not None:
        raise ValueError(f"{field} direct node script must be a literal workspace path")
    check_workspace_rel_path(positionals[0], field=f"{field} direct node script")
    return EffectiveStartCommand(
        argv=argv,
        runtime="node",
        executable=executable,
        node_script=positionals[0],
    )


def parse_effective_start_argv(
    argv: tuple[str, ...], *, declared_names: frozenset[str], field: str
) -> EffectiveStartCommand:
    """Parse one typed start argv into the closed, provider-neutral runtime matrix.

    This is the sole effective-start parser used by release declaration and detection.
    Token/secret/credential hygiene is applied once here; then the context-specific
    structured parser owns option arity and meaning. In particular, a ``python -m
    uvicorn`` start is parsed using uvicorn's option table rather than incorrectly
    applying the outer Python interpreter's generic build-command flag table.
    """
    if not argv:
        raise ValueError(f"{field} must not be empty")
    for token in argv:
        check_token_hygiene(token, field=field, allow_public_bind=True)
    check_no_inline_secret_cli(argv, declared_names=declared_names, field=field)
    check_no_positional_credential(argv, declared_names=declared_names, field=field)
    executable = argv[0]
    if executable in {"python", "python3", "uvicorn", "gunicorn", "hypercorn"}:
        return _effective_python_start(argv, declared_names=declared_names, field=field)
    return _effective_node_start(argv, field=field)


def bound_public_port_token(argv: tuple[str, ...]) -> str | None:
    """The port/bind token of a CLEANLY-PARSING Python server start, or ``None``.

    A narrow convenience extractor: it reuses the shared effective-start parser (never a
    second ad-hoc argv scan), treating every ``${NAME}`` / ``0.0.0.0:${NAME}`` /
    ``--flag=${NAME}`` reference PRESENT in ``argv`` as declared so the parse resolves purely
    on grammar + value-domain. It returns the uvicorn ``--port`` value or the
    gunicorn/hypercorn ``--bind`` value ONLY when the whole command parses as a Python server
    start that names one; it returns ``None`` for a start with no such token, a direct
    ``node <file>`` start, OR any command that does not fully parse (a duplicate/unknown
    flag, a non-TCP bind, a value outside the proven domain).

    Because a ``None`` here conflates "no port token" with "did not parse", this helper is
    NOT a fail-closed guard on its own — a schema belt that trusted it would FAIL OPEN on an
    unparseable-but-recognized server start. The authoritative port-contract belt is
    ``public_bind_contract_reason`` (below), which scans robustly and fails CLOSED. This
    function is kept only as an honest, narrowly-scoped port-token accessor for a
    cleanly-parsing start."""
    declared: set[str] = set()
    for token in argv:
        name = whole_env_ref(token) or public_bind_env_ref(token)
        if name is None:
            pair = flag_env_ref(token)
            if pair is not None:
                name = pair[1]
        if name is not None:
            declared.add(name)
    try:
        parsed = parse_effective_start_argv(
            argv, declared_names=frozenset(declared), field="start_cmd"
        )
    except ValueError:
        return None
    if parsed.runtime != "python":
        return None
    if parsed.server == "uvicorn":
        return parsed.option_value("--port")
    return parsed.option_value("--bind")


# ---- the port-contract BELT (schema layer, fail CLOSED by POSITIVE proof) ------
#
# Loopback interfaces: a server bound here is unreachable from the published ingress
# (the host maps the injected port to the container's PUBLIC interface). Loopback is
# classified STRUCTURALLY (``_is_loopback_host``), NOT by a small literal set: a literal
# set of ``127.0.0.1`` misses the rest of the ``127.0.0.0/8`` block (``127.0.0.2`` /
# ``127.0.1.1`` are loopback too), which a hostile start can bind to sit unreachable.
_LOOPBACK_HOSTNAMES = frozenset({"localhost", "ip6-localhost", "ip6-loopback"})


def _is_loopback_host(host: str) -> bool:
    """Whether a bind host names a LOOPBACK interface — unreachable from the published
    ingress (the host maps the injected port to the container's PUBLIC interface).

    Recognizes the WHOLE ``127.0.0.0/8`` IPv4 loopback block and ``::1`` via
    ``ipaddress.ip_address(...).is_loopback`` (so ``127.0.0.2`` / ``127.0.1.1`` are caught,
    not just the single ``127.0.0.1`` literal a naive set would hold), the bracketed IPv6
    spelling (``[::1]``) a combined ``--bind`` value carries (unwrapped first), and the
    loopback HOSTNAMEs (``localhost`` and friends, matched case-insensitively). A
    non-loopback host — the ``0.0.0.0`` public wildcard, a routable IP, or an opaque
    hostname — returns ``False`` (the belt defers / accepts, never a false loopback)."""
    stripped = host[1:-1] if host.startswith("[") and host.endswith("]") else host
    if stripped.lower() in _LOOPBACK_HOSTNAMES:
        return True
    try:
        return ipaddress.ip_address(stripped).is_loopback
    except ValueError:
        return False


# A literal ``host:port`` combined bind. The host is either a bracketed IPv6 literal or a
# hostname/IPv4 run; the port is a literal integer (a ``${NAME}`` port would not match, so
# an env-referenced bind is never mistaken for a literal one). Used only to POSITIVELY
# classify a bind the schema layer can fully analyze.
_HOST_PORT_LITERAL_RE = re.compile(r"^(\[[0-9A-Fa-f:]+\]|[A-Za-z0-9.\-]+):([0-9]+)$")

# An EMPTY/omitted-host combined bind carrying a LITERAL integer port: gunicorn/hypercorn
# ``--bind :9999`` binds ALL interfaces on the hard-coded port 9999. ``_HOST_PORT_LITERAL_RE``
# requires a non-empty host, so this empty-host shape is matched separately and classified
# exactly like ``0.0.0.0:9999`` — a positively-wrong hard-coded port. (The env-referenced
# empty-host form ``:${PORT}`` never reaches the belt: a token bearing a non-whole ``$`` is
# rejected upstream by ``check_token_hygiene`` before any model validation runs.)
_EMPTY_HOST_PORT_LITERAL_RE = re.compile(r"^:([0-9]+)$")

# Value-free belt reasons: each names the RULE, never the offending token (a rejected
# value can itself be a secret / an attacker-chosen string).
_PORT_CONTRACT_LITERAL = (
    "ReleaseService start_cmd binds a LITERAL public port/bind value instead of the "
    "adapter-owned port variable; the host publishes ingress on the port it injects "
    "through port_env, so a hard-coded port leaves the deployment unreachable. Bind "
    "${port_env} (--port ${PORT} / --bind 0.0.0.0:${PORT}) or omit the port so the "
    "release contract adds it."
)
_PORT_CONTRACT_FOREIGN = (
    "ReleaseService start_cmd binds a public port through an env var that is not this "
    "service's port_env; the host publishes ingress on the port_env it injects, so a "
    "foreign port env var leaves the published ingress unreachable. The port/bind "
    "reference must name port_env exactly."
)
_PORT_CONTRACT_LOOPBACK = (
    "ReleaseService start_cmd binds a LOOPBACK interface (127.0.0.1 / localhost / ::1); "
    "the published ingress reaches the container's public interface, so a loopback bind "
    "is unreachable. Bind the public wildcard (0.0.0.0) or omit the host."
)
_PORT_CONTRACT_NON_TCP = (
    "ReleaseService start_cmd binds a unix/uds socket (no TCP listener); the published "
    "ingress maps a TCP port, so a socket bind exposes nothing. Bind a public TCP "
    "${port_env} or omit the bind so the release contract adds it."
)


def _dedicated_server_position(argv: tuple[str, ...]) -> tuple[str, int] | None:
    """The (server, index-of-server-token) for a start whose PUBLIC bind is governed by an
    argv ``--port``/``--bind`` option — a direct ``uvicorn``/``gunicorn``/``hypercorn`` head
    or a ``python[3] -m <server>`` form — else ``None``.

    Only the grammar-supported dedicated servers qualify: for these the belt can POSITIVELY
    analyze the bind token. A start whose port is bound by application SOURCE (``node
    server.js``), injected by the adapter (a plain interpreter or an unrecognized launcher),
    or otherwise not one of these is NOT a dedicated server here — the belt defers on it and
    detection remains the primary guard. Mirrors ``_effective_python_start``'s dispatch so
    the belt and the parser agree on what a server start is."""
    if not argv:
        return None
    head = argv[0]
    if head in _SERVER_OPTION_SPECS:
        return (head, 0)
    if head in {"python", "python3"}:
        index = 1
        while index < len(argv) and argv[index] in _PYTHON_START_OPTIONS:
            index += 1
        if (
            index + 1 < len(argv)
            and argv[index] == "-m"
            and argv[index + 1] in _SERVER_OPTION_SPECS
        ):
            return (argv[index + 1], index + 1)
    return None


def _scan_flag_values(tokens: tuple[str, ...], names: frozenset[str]) -> list[str]:
    """Every value bound to one of ``names`` in ``--flag value`` or ``--flag=value`` form.

    A TOLERANT scan (not the strict parser): it never aborts on an unknown or duplicate flag,
    so a wrong port token is still surfaced even amid a duplicate ``--port`` or a trailing
    unknown flag (the exact fail-open the strict parse produced). Deterministic left-to-right
    order; every occurrence is collected so a duplicate wrong value is caught."""
    values: list[str] = []
    index = 0
    while index < len(tokens):
        token = tokens[index]
        consumed_value = False
        if token in names:
            if index + 1 < len(tokens):
                values.append(tokens[index + 1])
                consumed_value = True
        elif token.startswith("-") and "=" in token:
            flag, _, value = token.partition("=")
            if flag in names:
                values.append(value)
        index += 2 if consumed_value else 1
    return values


def _port_reference_reason(value: str, port_env: str) -> str | None:
    """A belt reason for a scanned ``--port`` value, or ``None`` when it provably binds
    ``${port_env}``. A whole ``${port_env}`` reference is the one accepted form; any literal
    is a hard-coded port and any other whole ``${NAME}`` is a foreign env — both POSITIVELY
    wrong (the published ingress would map a different port)."""
    name = whole_env_ref(value)
    if name is None:
        return _PORT_CONTRACT_LITERAL
    if name != port_env:
        return _PORT_CONTRACT_FOREIGN
    return None


def _bind_value_reason(value: str, port_env: str) -> str | None:
    """A belt reason for a scanned combined ``--bind`` value, or ``None``.

    POSITIVELY rejects a unix/uds socket (no TCP), a loopback ``host:port`` literal (the whole
    ``127.0.0.0/8`` block + ``::1``), an EMPTY-host literal port (``:9999`` binds all
    interfaces on a hard-coded port), and any literal ``host:port`` (a hard-coded port) or
    ``0.0.0.0:${NAME}`` whose NAME is foreign. Accepts exactly ``0.0.0.0:${port_env}``. DEFERS
    (returns ``None``) on a bind the schema layer cannot fully analyze — an opaque whole
    ``${VAR}`` bind or an IPv6 wildcard env form ``[::]:${PORT}`` — where detection stays the
    primary guard."""
    if value.startswith("unix:") or value.startswith("fd://"):
        return _PORT_CONTRACT_NON_TCP
    name = public_bind_env_ref(value)
    if name is not None:
        return _PORT_CONTRACT_FOREIGN if name != port_env else None
    if _EMPTY_HOST_PORT_LITERAL_RE.match(value):
        return _PORT_CONTRACT_LITERAL
    literal = _HOST_PORT_LITERAL_RE.match(value)
    if literal is not None:
        if _is_loopback_host(literal.group(1)):
            return _PORT_CONTRACT_LOOPBACK
        return _PORT_CONTRACT_LITERAL
    return None


def public_bind_contract_reason(argv: tuple[str, ...], *, port_env: str) -> str | None:
    """The value-free belt reason a RECOGNIZED dedicated-server start POSITIVELY violates the
    port contract, or ``None`` (accept / defer).

    Fails CLOSED by positive proof, never by inability to parse. A ``reason`` is returned ONLY
    when the start is a grammar-supported dedicated server (uvicorn / gunicorn / hypercorn,
    incl. ``python -m <server>``) AND an ANALYZABLE bind token is provably wrong: a ``--port``
    that is a literal or a foreign ``${VAR}`` (not ``port_env``), a loopback ``--host`` /
    combined bind, or a unix/uds socket. It returns ``None`` — deferring to detection, the
    primary guard — for an unrecognized launcher, a source/adapter-bound start (``node
    server.js``, a plain interpreter), a start with no analyzable bind token (the adapter
    injects ``port_env``), or a ``--config``-only bind whose value the schema layer cannot
    read. Robust to duplicate/unknown flags: it SCANS bind tokens rather than requiring the
    whole argv to parse, so a wrong port amid a duplicate or unknown-flag start is still
    caught (closing the ``bound_public_port_token`` fail-open)."""
    found = _dedicated_server_position(argv)
    if found is None:
        return None
    server, server_index = found
    tokens = argv[server_index + 1 :]
    if server == "uvicorn":
        for host in _scan_flag_values(tokens, frozenset({"--host"})):
            if _is_loopback_host(host):
                return _PORT_CONTRACT_LOOPBACK
        for port in _scan_flag_values(tokens, frozenset({"--port"})):
            reason = _port_reference_reason(port, port_env)
            if reason is not None:
                return reason
        return None
    for bind in _scan_flag_values(tokens, frozenset({"--bind", "-b"})):
        reason = _bind_value_reason(bind, port_env)
        if reason is not None:
            return reason
    return None


# Known credential-bearing CLI flag NAMES (lowercased). A VALUE carried on one of
# these must be a WHOLE DECLARED `${NAME}` env reference, never an inline literal —
# so a secret is never baked into an emitted command. Deliberately a focused set of
# unambiguously-secret flags (not `--key`/`--pass`, which have benign non-secret
# uses) so head-agnostic hygiene on a `migrate_cmd` never false-rejects a legitimate
# non-secret argument.
_SECRET_CLI_FLAGS = frozenset(
    {
        "--token",
        "--api-token",
        "--auth-token",
        "--access-token",
        "--refresh-token",
        "--password",
        "--passwd",
        "--secret",
        "--client-secret",
        "--api-key",
        "--apikey",
        "--credential",
        "--credentials",
    }
)


def check_no_inline_secret_cli(
    argv: tuple[str, ...], *, declared_names: frozenset[str], field: str
) -> None:
    """Reject an INLINE literal secret carried on a known credential-bearing CLI flag
    (`--token VALUE` / `--token=VALUE`, `--password`, `--api-key`, `--secret`,
    `--credential`, `--access-token`, … in both the space and `=` forms) UNLESS the
    value is a WHOLE DECLARED `${NAME}` env reference (WO-C5 #3 / F5).

    HEAD-AGNOSTIC by design: it does NOT constrain the executable and does NOT reject
    unknown non-secret flags or positionals, so a legitimate migration command (an
    `alembic -c alembic.ini upgrade head`, a `wrangler d1 migrations apply`, positional
    operands) stays valid. This is inline-secret DATA HYGIENE on an argv that is already
    rendered as a SAFE exec-array (never a shell string) — NOT the §9 runtime grammar,
    which would wrongly reject a non-runtime migration tool. Value-free message: a
    rejected value can itself be the secret. A migration credential must be a declared
    `${NAME}` reference (bound to the resource / in required_env), never inline."""
    index = 0
    while index < len(argv):
        token = argv[index]
        if token.startswith("-") and "=" in token:
            name, _, value = token.partition("=")
            if name.lower() in _SECRET_CLI_FLAGS:
                ref = whole_env_ref(value)
                if ref is None or ref not in declared_names:
                    raise _inline_secret_error(field)
        elif token.lower() in _SECRET_CLI_FLAGS:
            following = argv[index + 1] if index + 1 < len(argv) else None
            ref = whole_env_ref(following) if following is not None else None
            if ref is None or ref not in declared_names:
                raise _inline_secret_error(field)
            index += 1  # the reference value is consumed by this secret flag
        index += 1


def _inline_secret_error(field: str) -> ValueError:
    return ValueError(
        f"{field} carries an inline secret on a credential-bearing flag "
        "(--token / --password / --api-key / --secret / --credential / --access-token / "
        "…); the value must be a whole ${NAME} reference to a declared env var, never an "
        "inline literal that would be baked into the exported command"
    )


# ---- positional credential literals (the G02 hole) ----------------------------
#
# A bare POSITIONAL command operand is NOT automatically harmless. Token hygiene
# proves a token is a shell-inert literal, and the runtime grammar (below) proves
# every FLAG is known and every flag VALUE is a whole ${NAME} reference — but a
# credential can also ride a POSITIONAL slot with no flag, no `=`, and no shell
# metacharacter (`node MYSECRET server.js`, `npm config set //host/:_authToken TOKEN`).
# Such a token passes every existing guard yet bakes a shipped secret into the
# exported start command (GAP G02). `looks_like_credential_literal` closes that hole
# PRINCIPLED: by the SHAPE of the value and, for config-set, by the ROLE of the
# operand — NEVER a deny-list of known secret VALUES, registry hosts, or fixture
# commands (that would be the §9 forbidden shortcut). A credential-capable value must
# be a whole ${NAME} reference to a declared env var; it may never be literal data.

# Rail 1 — KNOWN secret token FORMAT FAMILIES: structural regexes anchored at the
# token start (an argv token is one exec element, so the credential is the whole token
# or its recognizable vendor-prefixed body). This is a FORMAT TAXONOMY, not a value
# list: each pattern matches the *shape* an entire family of secrets shares, so a
# brand-new token of that family is caught without naming any specific value.
_CREDENTIAL_FORMAT_RES: tuple[re.Pattern[str], ...] = (
    re.compile(r"^npm_[A-Za-z0-9]{30,}$"),  # npm access token
    re.compile(r"^gh[pousr]_[A-Za-z0-9]{30,}$"),  # GitHub PAT / OAuth / refresh / server
    re.compile(r"^github_pat_[A-Za-z0-9_]{30,}$"),  # GitHub fine-grained PAT
    re.compile(r"^sk-(?:ant-)?[A-Za-z0-9_-]{20,}$"),  # OpenAI / Anthropic secret key
    re.compile(r"^xox[baprs]-[A-Za-z0-9-]{10,}$"),  # Slack token
    re.compile(r"^AKIA[0-9A-Z]{16}$"),  # AWS access key id
    re.compile(r"^AIza[0-9A-Za-z_-]{35}$"),  # Google API key
    re.compile(r"^glpat-[A-Za-z0-9_-]{20,}$"),  # GitLab personal access token
    re.compile(r"^[sr]k_(?:live|test)_[A-Za-z0-9]{20,}$"),  # Stripe secret / restricted key
    re.compile(r"^dop_v1_[A-Za-z0-9]{40,}$"),  # DigitalOcean PAT
    re.compile(r"^SG\.[A-Za-z0-9_-]{16,}\.[A-Za-z0-9_-]{16,}$"),  # SendGrid API key
    re.compile(r"^eyJ[A-Za-z0-9_-]{6,}\.eyJ[A-Za-z0-9_-]{6,}\.[A-Za-z0-9_-]{6,}$"),  # JWT
    re.compile(r"^hf_[A-Za-z0-9]{30,}$"),  # HuggingFace access token
    re.compile(r"^shp(?:at|ss|ca|pa)_[A-Fa-f0-9]{32}$"),  # Shopify access/shared/app secret
    re.compile(r"^xapp-[0-9]-[A-Za-z0-9-]{15,}$"),  # Slack app-level token
    re.compile(r"^AGE-SECRET-KEY-1[0-9A-Z]{40,}$"),  # age identity (secret key)
    re.compile(r"^age1[0-9a-z]{40,}$"),  # age recipient (public key)
)

# Rail 2 — a GENERIC high-entropy opaque single-blob token, in TWO tiers so an opaque
# secret is caught whether or not it carries the base64url word-separators `-`/`_`, WHILE
# a human-readable kebab/snake identifier (a D1 `database_name`, a resource id, a service
# slug) is spared. Both tiers exclude '/' (a path/registry key), '.', and ':' — so a
# dotted host, a `main:app` module target, a versioned interpreter (`python3.12`), and any
# file with an extension are excluded BY CONSTRUCTION — and require length >= 32.
#
# * TIER A — a CONTIGUOUS base16/32/62 run (letters, digits, base64 fillers `=+`; NO
#   `-`/`_`). A blob with no word-separators is opaque by construction, so a modest
#   entropy floor (`_MIN_OPAQUE_SECRET_ENTROPY`) suffices (a hex API key sits near
#   H≈3.99 and must stay caught). This catches base62/hex keys and a separator-free
#   base64url token.
# * TIER B — a SEPARATOR-BEARING run that also admits `-`/`_` (base64url, `token_urlsafe`,
#   generic opaque `-`/`_` secrets). Here `-`/`_` alone no longer prove "secret" (they
#   also spell a kebab/snake NAME), so this tier demands BOTH a HIGHER entropy floor
#   (`_MIN_SEPARATOR_SECRET_ENTROPY`) AND that the token is NOT a wordlike identifier
#   (`_is_wordlike_identifier`). Empirically the classes separate: benign kebab/snake
#   identifiers top out near H≈3.95, while base64url/opaque `-`/`_` secrets start near
#   H≈4.24 — the 4.1 floor sits in that gap, and the wordlike guard adds a structural
#   margin so a word-rich identifier above the floor (e.g. `alpha-bravo-charlie-…`) is
#   still spared. Vendor tokens that carry `-`/`_` with a known prefix (JWT, `github_pat_…`,
#   Slack, age, Shopify) are caught structurally by Rail 1 regardless of entropy.
_OPAQUE_BLOB_RE = re.compile(r"^[A-Za-z0-9=+]+$")
_SEPARATOR_BLOB_RE = re.compile(r"^[A-Za-z0-9=+_-]+$")
_LOWER_HEX_SEGMENT_RE = re.compile(r"[0-9a-f]+")
_MIN_OPAQUE_SECRET_LEN = 32
_MIN_OPAQUE_SECRET_ENTROPY = 3.5
_MIN_SEPARATOR_SECRET_ENTROPY = 4.1
# A `[-_]`-split segment no longer than this MAY be a benign short id fragment (a hex
# shard suffix like `1ef23532`); a longer non-word, non-number segment is opaque.
_MAX_WORDLIKE_SEGMENT = 12

# Substrings (matched case-INSENSITIVELY) that mark a package-manager CONFIG KEY as
# credential-config — a key whose VALUE is a credential (`_authToken`,
# `//host/:_auth`, `password`, `token`, `apikey`, `credential`, …). Detected by the
# SHAPE of the key, NEVER by a specific registry host: a `config set` whose key is
# credential-shaped must take a whole ${NAME} reference, never a literal secret value.
_CREDENTIAL_CONFIG_KEY_MARKERS = (
    "authtoken",
    "_auth",
    "token",
    "password",
    "secret",
    "apikey",
    "api_key",
    "credential",
)

# The package-manager heads whose `set <key> <value>` (no `config`) shorthand also
# writes config (`npm set //host/:_authToken VALUE`).
_CONFIG_SET_HEADS = frozenset({"npm", "pnpm", "yarn", "pip", "pip3"})


def _is_wordlike_identifier(token: str) -> bool:
    """Whether a `-`/`_`-separated token reads as a HUMAN identifier (a kebab/snake
    name) rather than an opaque secret blob. True iff it splits into >= 2 non-empty
    segments where EVERY segment is a readable fragment: a pure-alphabetic word, a
    pure-numeric run, a very short (<= 4 char) version/shard token (`q3`, `v2`, `01`),
    or a short lowercase-hex id (`1ef23532`). A single long mixed-case alphanumeric
    segment — the signature of a base64url / opaque secret chunk — makes it False."""
    segments = [segment for segment in re.split(r"[-_]", token) if segment]
    if len(segments) < 2:
        return False
    for segment in segments:
        if segment.isalpha() or segment.isdigit() or len(segment) <= 4:
            continue
        if len(segment) <= _MAX_WORDLIKE_SEGMENT and _LOWER_HEX_SEGMENT_RE.fullmatch(segment):
            continue
        return False
    return True


def _shannon_entropy_bits_per_char(token: str) -> float:
    """The Shannon entropy of `token` in bits per character (0.0 for an empty or
    single-repeated-character string). A high value means an opaque, near-random blob;
    a low value means a repetitive / dictionary-like word."""
    if not token:
        return 0.0
    counts: dict[str, int] = {}
    for char in token:
        counts[char] = counts.get(char, 0) + 1
    length = len(token)
    entropy = 0.0
    for count in counts.values():
        probability = count / length
        entropy -= probability * math.log2(probability)
    return entropy


def looks_like_credential_literal(token: str) -> bool:
    """Whether a bare argv operand is UNMISTAKABLY credential material by STRUCTURE.

    Two principled rails, neither a value list: (1) the token matches a KNOWN secret
    token FORMAT FAMILY (npm / GitHub / OpenAI-Anthropic / Slack incl. app-level / AWS /
    Google / GitLab / Stripe / DigitalOcean / SendGrid / JWT / HuggingFace / Shopify /
    age — structural regexes over the *shape* a whole family shares), or (2) it is a
    GENERIC opaque high-entropy blob (length >= 32, NO '/'), in two tiers: a CONTIGUOUS
    base62/hex run (no `-`/`_`) at entropy >= 3.5, OR a SEPARATOR-BEARING base64url run
    (admits `-`/`_`) at entropy >= 4.1 that is NOT a wordlike kebab/snake identifier. A
    whole ${NAME}/$NAME reference is the sanctioned way to carry a secret and is NEVER
    flagged. Returns False for the benign operands real commands use — `server.js`,
    `worker.js`, `node`, `npm`, `config`, `set`, `//registry.example/:_authToken` (a
    path/key with '/'), `main:app`, `dist/server.js`, a kebab/snake identifier like a D1
    `database_name` (`acme-records-team_members-1ef2`), versioned interpreters, flags,
    and short subcommands — so it is a shape detector, not a single-sentinel matcher."""
    if whole_env_ref(token) is not None:
        return False
    for pattern in _CREDENTIAL_FORMAT_RES:
        if pattern.match(token):
            return True
    # Generic high-entropy fallback (two tiers). A '/' marks a path/registry key; a
    # too-short token cannot be a high-entropy secret.
    if "/" in token or len(token) < _MIN_OPAQUE_SECRET_LEN:
        return False
    entropy = _shannon_entropy_bits_per_char(token)
    if _OPAQUE_BLOB_RE.match(token):
        # Tier A — a contiguous base62/hex blob with NO word-separators.
        return entropy >= _MIN_OPAQUE_SECRET_ENTROPY
    if _SEPARATOR_BLOB_RE.match(token):
        # Tier B — admits base64url `-`/`_`: demand a higher entropy floor AND a
        # non-wordlike structure, so a kebab/snake identifier is never mistaken for one.
        return entropy >= _MIN_SEPARATOR_SECRET_ENTROPY and not _is_wordlike_identifier(token)
    return False


def _is_credential_config_key(key: str) -> bool:
    """Whether a package-manager config KEY is credential-config by SHAPE (its
    lowercased form contains an auth-token / password / secret / api-key / credential
    marker) — never a specific registry host."""
    lowered = key.lower()
    return any(marker in lowered for marker in _CREDENTIAL_CONFIG_KEY_MARKERS)


def check_credential_config_role(
    argv: tuple[str, ...], *, declared_names: frozenset[str], field: str
) -> None:
    """Reject a package-manager credential-config form that sets a credential-shaped
    key to a LITERAL value (WO / GAP G02, operand ROLE rail).

    Detects the ROLE `... config set <key> <value>` (npm / pnpm / yarn / pip) and the
    `<pm> set <key> <value>` shorthand, then — when `<key>` is credential-config by
    SHAPE (`_is_credential_config_key`) — requires `<value>` to be a whole DECLARED
    ${NAME} reference; a literal value is rejected value-free. Detection is by operand
    role + key shape, NEVER by the specific `//registry.example/...` host or the token
    value, so a brand-new registry / key is caught the same way. Value-free message: a
    rejected value can itself be the secret."""
    length = len(argv)
    for index in range(length):
        token = argv[index].lower()
        key_index: int | None = None
        if token == "config" and index + 1 < length and argv[index + 1].lower() == "set":
            key_index = index + 2
        elif (
            token == "set"
            and index == 1
            and argv[0].rsplit("/", 1)[-1].lower() in _CONFIG_SET_HEADS
        ):
            key_index = index + 1
        if key_index is None or key_index + 1 >= length:
            continue
        key, value = argv[key_index], argv[key_index + 1]
        if not _is_credential_config_key(key):
            continue
        ref = whole_env_ref(value)
        if ref is None or ref not in declared_names:
            raise ValueError(
                f"{field} sets a credential-shaped config key to a literal value "
                "(a 'config set <key> <value>' whose key names an auth token / password "
                "/ secret / api key / credential); the value must be a whole ${NAME} "
                "reference to a declared env var, never an inline literal that would be "
                "baked into the exported command"
            )


def check_no_positional_credential(
    argv: tuple[str, ...], *, declared_names: frozenset[str], field: str
) -> None:
    """HEAD-AGNOSTIC positional-credential hygiene for an argv that is NOT a runtime
    command — a resource / service ``migrate_cmd`` heads with a migration TOOL
    (alembic / wrangler / ``manage.py`` / an npm script), OUTSIDE the §9 runtime grammar,
    so ``check_declaration_argv`` (which would reject the non-runtime head) must NOT run
    on it. Instead this applies the SAME two G02 rails ``check_declaration_argv`` applies
    to a start/build command, minus the head grammar:

    * the operand-ROLE rail (``check_credential_config_role`` — a ``config set
      <credential-key> <literal>`` must reference a declared ``${NAME}``), and
    * the positional-SHAPE rail — any POSITIONAL operand (a token that is neither a flag
      nor a whole ``${NAME}`` reference) that is credential material by SHAPE
      (``looks_like_credential_literal``) is rejected value-free.

    Flags are governed by the sibling ``check_no_inline_secret_cli`` (the ``--token
    VALUE`` inline-secret rail), so a leading-``-`` token is skipped here; a whole
    ``${NAME}`` reference is always allowed. Benign migration commands keep exact argv
    semantics — ``alembic -c alembic.ini upgrade head``, ``wrangler d1 migrations
    apply``, ``python manage.py migrate``, ``npm run migrate`` all pass (their operands
    are short subcommands / dotted module paths, never a credential blob). Value-free: a
    rejected value can itself be the secret."""
    check_credential_config_role(argv, declared_names=declared_names, field=field)
    for token in argv:
        if token.startswith("-"):
            continue
        if whole_env_ref(token) is not None:
            continue
        if looks_like_credential_literal(token):
            raise ValueError(
                f"{field} carries a bare positional literal that is credential material "
                "by its structure (a recognized secret token format, or an opaque "
                "high-entropy blob); pass a credential as a whole ${NAME} reference to a "
                "declared env var, never as an inline literal operand baked into the "
                "exported command"
            )


def _head_family(head: str) -> str | None:
    """The supported runtime family for an EXACT command head.

    The neutral images provision executable *names*, not arbitrary paths, wrappers, case
    variants, or whatever versioned interpreter happens to exist on the detector host.
    Keeping this exact is important: accepting ``/tmp/node`` or ``python3.99`` because its
    basename/prefix looked familiar creates a candidate the emitted image cannot run.
    """
    if head in {"python", "python3"}:
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
    for token in argv:
        check_token_hygiene(token, field=field)
    head = argv[0]
    family = _head_family(head)
    if family is None:
        raise ValueError(
            f"{field} must start with a supported runtime executable "
            "(node/npm/npx/yarn/pnpm, python, or uvicorn/gunicorn/hypercorn); an "
            "arbitrary program, shell, or path is rejected"
        )
    # GAP G02 (operand ROLE rail): a `config set <credential-key> <literal>` form must
    # take a whole ${NAME} reference, not a literal secret value.
    check_credential_config_role(argv, declared_names=declared_names, field=field)
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
        # A non-flag operand (script / module / subcommand). It is hygiene-checked as a
        # shell-inert literal — but "shell-inert" is NOT "harmless": a bare positional
        # literal credential (`node MYSECRET server.js`, `npm config set … npm_XXXX`)
        # would otherwise ride this slot and bake a shipped secret into the exported
        # command (GAP G02). A credential-capable value must be a whole ${NAME} reference
        # (accepted above), never a literal, so a positional operand that is credential
        # material BY SHAPE is rejected here — value-free (the value can be the secret).
        if looks_like_credential_literal(token):
            raise ValueError(
                f"{field} carries a bare positional literal that is credential material "
                "by its structure (a recognized secret token format, or an opaque "
                "high-entropy blob); pass a credential as a whole ${NAME} reference to a "
                "declared env var, never as an inline literal operand baked into the "
                "exported command"
            )
        index += 1


__all__ = [
    "GUNICORN_BUILTIN_WORKER_CLASSES",
    "EffectiveStartCommand",
    "ParsedCommandOption",
    "bound_public_port_token",
    "check_credential_config_role",
    "check_declaration_argv",
    "check_health_path",
    "check_no_inline_secret_cli",
    "check_no_positional_credential",
    "check_persistent_path",
    "check_sqlite_local_url",
    "check_token_hygiene",
    "check_workspace_rel_path",
    "flag_env_ref",
    "looks_like_credential_literal",
    "parse_effective_start_argv",
    "public_bind_contract_reason",
    "public_bind_env_ref",
    "whole_env_ref",
]
