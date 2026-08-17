"""Database evidence classification (sqlite vs. unknown engine), the sqlite
resource declarations for both the generic and AppKit shapes, and secret-name
classification.
"""

from __future__ import annotations

from collections.abc import Mapping

from disco.core.release.spec import (
    LocalResourceProfile,
    ResourceDecl,
    ResourceKind,
    ResourceProfiles,
    SecretClass,
)

from ._constants import (
    _APPKIT_DEFAULT_DB,
    _APPKIT_DEFAULT_SECRET,
    _APPKIT_STATE_PATH,
    _APPKIT_STATE_URL,
    _APPKIT_STATE_VOLUME,
    _DB_FILE_URL_RE,
    _DB_ID,
    _DEV_VARS_EXAMPLE,
    _DEV_VARS_NAME_RE,
    _INGRESS_ID,
    _LIBSQL_RE,
    _SECRET_NAME_MARKERS,
    _SQLITE_PATH,
    _SQLITE_URL,
    _SQLITE_VOLUME,
    _SQLITE_WORD_RE,
    _UNKNOWN_DB_SCHEMES,
    _WRANGLER_DB_NAME_RE,
)
from ._models import _DbFinding, _DbKind
from ._text import _as_text, _norm, _scan_text


def _detect_database(files: Mapping[str, str | bytes]) -> _DbFinding:
    """Classify database evidence: `unknown` (any non-sqlite url scheme — fail
    closed) beats `sqlite` (a file: url / libSQL-drizzle config / `*.db` file)."""
    unknown_engine = ""
    unknown_evidence: list[str] = []
    sqlite_evidence: list[str] = []
    has_url = False
    for path in sorted(files):
        norm = _norm(path)
        text = _scan_text(files[path])
        low = text.lower()
        for scheme in _UNKNOWN_DB_SCHEMES:
            if scheme in low:
                if not unknown_engine:
                    unknown_engine = scheme.split("://", 1)[0]
                unknown_evidence.append(f"unrecognized database url '{scheme}' in {norm}")
        if _DB_FILE_URL_RE.search(text):
            has_url = True
            sqlite_evidence.append(f"DATABASE_URL file: reference in {norm}")
        if _LIBSQL_RE.search(text) and (_SQLITE_WORD_RE.search(text) or "file:" in low):
            sqlite_evidence.append(f"libSQL/drizzle sqlite config in {norm}")
        if norm.endswith(".db"):
            sqlite_evidence.append(f"sqlite database file {norm}")
    if unknown_engine:
        return _DbFinding(
            _DbKind.unknown, unknown_engine, tuple(dict.fromkeys(unknown_evidence)), False
        )
    if sqlite_evidence:
        return _DbFinding(_DbKind.sqlite, "sqlite", tuple(dict.fromkeys(sqlite_evidence)), has_url)
    return _DbFinding(_DbKind.none, "", (), False)


def _sqlite_resource(consumer_id: str) -> ResourceDecl:
    return ResourceDecl(
        id=_DB_ID,
        kind=ResourceKind.sqlite,
        persistent_path=_SQLITE_PATH,
        profiles=ResourceProfiles(
            local=LocalResourceProfile(url=_SQLITE_URL, volume=_SQLITE_VOLUME)
        ),
        consumers=(consumer_id,),
    )


def _appkit_db_name(files: Mapping[str, str | bytes]) -> str:
    """The D1 `database_name` declared in `wrangler.toml` (the name `wrangler d1
    execute` targets), or a safe fallback if none is declared. Read from the
    IMMUTABLE contents so the derived migrate command targets the app's real DB."""
    for path in files:
        if _norm(path) == "wrangler.toml":
            match = _WRANGLER_DB_NAME_RE.search(_as_text(files[path]))
            if match is not None and match.group(1).strip():
                return match.group(1).strip()
    return _APPKIT_DEFAULT_DB


def _appkit_secret_names(files: Mapping[str, str | bytes]) -> tuple[str, ...]:
    """The secret env-var NAMES an AppKit app reads locally, taken from its
    `.dev.vars.example` template (NAMES only — the template's placeholder VALUES
    never enter the spec). Falls back to AppKit's `ADMIN_TOKEN` admin-gate secret
    when the template is absent (e.g. a minimal contract-only tree)."""
    for path in files:
        if _norm(path) == _DEV_VARS_EXAMPLE:
            names = tuple(dict.fromkeys(_DEV_VARS_NAME_RE.findall(_as_text(files[path]))))
            if names:
                return names
    return (_APPKIT_DEFAULT_SECRET,)


def _appkit_sqlite_resource(db_name: str) -> ResourceDecl:
    """The AppKit persistent-state resource: a sqlite-class resource whose data
    lives under `/data/state` (where `wrangler dev --persist-to` writes its local
    D1). Its `migrate_cmd` is the `wrangler d1 execute` argv that applies
    `schema.sql` to that local DB; the local-run adapter appends the persist dir."""
    return ResourceDecl(
        id=_DB_ID,
        kind=ResourceKind.sqlite,
        persistent_path=_APPKIT_STATE_PATH,
        profiles=ResourceProfiles(
            local=LocalResourceProfile(url=_APPKIT_STATE_URL, volume=_APPKIT_STATE_VOLUME)
        ),
        consumers=(_INGRESS_ID,),
        migrate_cmd=("npx", "wrangler", "d1", "execute", db_name, "--local", "--file=./schema.sql"),
    )


def _classify_secret(name: str) -> SecretClass:
    """Classify a declared env-var NAME as `secret` or `public` by its shape.

    A name carrying a secret-shaped marker (TOKEN / SECRET / KEY / PASSWORD /
    CREDENTIAL / …) is treated as `secret` so the export guards it and never labels
    it public. Fails CLOSED toward `secret`: over-classifying a public var is
    harmless (it is still just guarded), while under-classifying a real secret is
    the actual hazard."""
    upper = name.upper()
    if any(marker in upper for marker in _SECRET_NAME_MARKERS):
        return SecretClass.secret
    return SecretClass.public
