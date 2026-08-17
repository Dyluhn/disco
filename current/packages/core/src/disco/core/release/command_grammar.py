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

import math
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
    """The supported runtime family for a command head (basename, lowercased), or
    ``None`` if the executable is not one the neutral base images run."""
    if head.startswith("python"):
        return "python"
    if head in _KNOWN_FLAGS:
        return head
    return None


def _declaration_env_ref(token: str, *, declared_names: frozenset[str], field: str) -> str | None:
    """If `token` is a whole ``${NAME}``/``$NAME`` reference, return its NAME —
    raising (value-free) if that NAME is not DECLARED. Returns ``None`` for a
    token that is not a whole reference at all (the caller then tries the next
    grammar rule)."""
    ref = whole_env_ref(token)
    if ref is None:
        return None
    if ref not in declared_names:
        raise ValueError(
            f"{field} references an env var that is not declared in required_env "
            "(the only expandable token is a whole ${NAME} reference to a "
            "declared var, or the $PORT contract)"
        )
    return ref


def _declaration_flag_end_index(
    argv: tuple[str, ...],
    index: int,
    token: str,
    *,
    known: frozenset[str],
    declared_names: frozenset[str],
    field: str,
) -> int:
    """Consume ONE flag token (`token` starts with ``-``) per the runtime grammar:
    a known head flag, a ``--flag=${NAME}`` single token, or a ``--flag ${NAME}``
    two-token pair, where every ``${NAME}`` must be DECLARED. Returns the index of
    the NEXT unconsumed argv element; raises (value-free) on an unknown flag or an
    inline literal flag value."""
    name = token.split("=", 1)[0]
    if name in known:
        return index + 1
    if "=" in token:
        value_ref = whole_env_ref(token.split("=", 1)[1])
        if value_ref is not None and value_ref in declared_names:
            return index + 1
        raise ValueError(
            f"{field} carries an unknown flag or an inline literal value; a flag "
            "value must be a whole ${NAME} reference to a declared env var"
        )
    following = argv[index + 1] if index + 1 < len(argv) else None
    following_ref = whole_env_ref(following) if following is not None else None
    if following_ref is not None and following_ref in declared_names:
        return index + 2
    raise ValueError(
        f"{field} carries an unknown flag or an inline literal value; a flag "
        "value must be a whole ${NAME} reference to a declared env var"
    )


def _check_declaration_positional(token: str, *, field: str) -> None:
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
    # GAP G02 (operand ROLE rail): a `config set <credential-key> <literal>` form must
    # take a whole ${NAME} reference, not a literal secret value.
    check_credential_config_role(argv, declared_names=declared_names, field=field)
    known = _KNOWN_FLAGS[family]
    index = 1
    while index < len(argv):
        token = argv[index]
        if _declaration_env_ref(token, declared_names=declared_names, field=field) is not None:
            index += 1
            continue
        if token.startswith("-"):
            index = _declaration_flag_end_index(
                argv, index, token, known=known, declared_names=declared_names, field=field
            )
            continue
        _check_declaration_positional(token, field=field)
        index += 1


__all__ = [
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
    "whole_env_ref",
]
