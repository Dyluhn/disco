"""AppKit EPIC O — the Cloudflare deploy planner + HARD-GATED executor.

This is the ONE place AppKit makes a REAL outward change (deploys a generated app
to a live Cloudflare account). The capability is built here, but a real mutation
can fire ONLY through ``execute_deploy`` with ALL of:

  1. ``cloudflare_export_ready`` (Epic I) PASS — re-checked FRESH at execute time;
  2. a CONNECTED account — an API token decryptable from the encrypted SecretStore;
  3. EXPLICIT owner confirmation — the exact phrase bound to the freshly-computed
     plan_hash (so a stale plan, or any digest drift, refuses);
  4. NOT an autonomous run — like ``request_custom_build``, refused outright when
     no human is present.

DRY-RUN IS THE DEFAULT. ``execute_deploy(dry_run=True)`` builds the plan and
returns it with ZERO side effects — the command runner is never called. The real
deploy is the owner's explicit, documented action; this module never runs it in
CI (tests drive a FAKE runner; the production path is owner-executed, Epic
M-style live run).

SECRET HANDLING: the API token + ADMIN_TOKEN live only in the encrypted
SecretStore. They are injected into the wrangler subprocess via ENV / stdin —
never argv, never a plan field, never a log. Every captured stdout/stderr is run
through ``redact_text`` before it enters a transcript or the persisted
deployment record. This module is leaf-pure of ``core``: deploy execution lives
in agent-server, ``disco.core`` stays a leaf (it only lends the pure
``cloudflare_export_ready`` model + the SecretStore).
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import hashlib
import hmac
import json
import logging
import os
import re
import secrets as _secrets
import shutil
import stat
import tempfile
import tomllib
from collections.abc import Awaitable, Callable, Iterator
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import Literal, NoReturn, overload

from disco.core.appkit.local_verify import (
    CF_EXPORT_FILES,
    cloudflare_export_ready,
    main_points_at_worker_entry,
)
from disco.core.appkit.spec import (
    AppSpec,
    DesignSpec,
    load_app_spec_from_bytes,
    load_design_spec_from_bytes,
)
from disco.core.appkit.stripe_primitive import stripe_verify
from disco.core.appkit.webhook_primitive import webhook_verify
from disco.core.llm.secrets import SecretStore, WeakSecretError

from ..redaction import redact_text
from ..workspace_process_fence import (
    WorkspaceProcessBusy,
    try_workspace_process_fence,
    workspace_process_lock_dir,
    workspace_process_lock_path,
)
from .models import (
    ConnectionStatus,
    DeployExecutionResult,
    DeployPlan,
    DeployRefused,
    DeployStep,
    RefusalReason,
)
from .stripe_deploy import (
    StripeDeployContext,
    StripeDeployError,
    StripeDeploymentLifecycle,
    stripe_lifecycle_for,
)
from .webhook_deploy import (
    WebhookDeployContext,
    WebhookDeployError,
    WebhookDeploymentLifecycle,
    webhook_lifecycle_for,
)
from .wrangler import BuildBackend, BuildResult, CommandResult, CommandRunner

try:  # fcntl is POSIX-only; a non-POSIX host degrades to the in-process lock (logged).
    import fcntl as _fcntl
except ImportError:  # pragma: no cover - exercised only on non-POSIX hosts
    _fcntl = None  # type: ignore[assignment]

_log = logging.getLogger(__name__)

DeployMutationHook = Callable[[], Awaitable[None]]

_HOST_TOKEN_LIFECYCLE_MUTATIONS = frozenset(
    {
        "host_token_candidate_mint_attempted",
        "host_token_candidate_minted",
        "host_token_rotation_finish_attempted",
        "host_token_rotation_finished",
        "host_token_candidate_revoke_attempted",
        "host_token_candidate_revoked",
    }
)

# ---- credential slots --------------------------------------------------------

#: SecretStore name for the encrypted Cloudflare API token (O1). NEVER plaintext
#: on disk; NEVER logged. Least-privilege scope documented in the OWNER guide:
#: Account "Workers Scripts:Edit" + "D1:Edit" only — no Zone/DNS/Routes.
CF_TOKEN_SECRET = "DISCO_CLOUDFLARE_API_TOKEN"
#: SecretStore name for the account id. Not itself a secret, but stored in the
#: same keyed store so a connection is one decryptable unit (and so we never
#: touch the shared ConfigStore from here).
CF_ACCOUNT_SECRET = "DISCO_CLOUDFLARE_ACCOUNT_ID"

#: Env var names wrangler reads. The token is injected here at execute time only.
_WRANGLER_TOKEN_ENV = "CLOUDFLARE_API_TOKEN"
_WRANGLER_ACCOUNT_ENV = "CLOUDFLARE_ACCOUNT_ID"

#: The ONLY base env vars passed through to the TOKEN-BEARING wrangler subprocess
#: (SEC-32 — deploy env minimization). The server's full env (with its secrets) is
#: NEVER handed to a deploy subprocess; only this minimal, non-sensitive allowlist
#: plus the CF token + a sanitised PATH + a throwaway HOME (overlaid via
#: ``_deploy_env``) reaches the wrangler steps. The UNTRUSTED ``npm run build`` no
#: longer runs as a host subprocess at all — it runs in an isolating sandbox (see
#: ``sandbox_build.SandboxBuildBackend``) with no host env / host FS, so a
#: malicious build script has nothing to exfiltrate.
#:
#: DELIBERATELY EXCLUDED from the token-bearing env (SEC-32): ``HOME`` (would let a
#: planted ``~/.npmrc`` / ``~/.wrangler`` redirect to host creds — overridden to a
#: throwaway dir instead), ``NODE_OPTIONS`` (``--require`` would PRELOAD arbitrary
#: JS into the token-bearing process), and ``npm_config_*`` (a poisoned registry /
#: cache could exfiltrate the token or inject code). ``PATH`` IS forwarded but is
#: SANITISED of any entry inside the workspace/staging tree so an attacker-planted
#: ``node_modules/.bin`` can never shadow a real tool.
_WRANGLER_ENV_ALLOWLIST: tuple[str, ...] = (
    "LANG",
    "LC_ALL",
    "LC_CTYPE",
    "TMPDIR",
    "TEMP",
    "TMP",
    "TERM",
    "SHELL",
    "USER",
    "LOGNAME",
    "NODE_ENV",
)

#: An operator-pinned, ABSOLUTE path to a trusted ``wrangler`` binary (SEC-3). When
#: set, it overrides PATH resolution entirely — the most robust way to guarantee the
#: deploy never runs a workspace-resolved ``wrangler``.
_WRANGLER_BIN_ENV = "DISCO_WRANGLER_BIN"


def _sanitized_path(*unsafe_roots: Path) -> str | None:
    """The host ``PATH`` with EVERY entry that lives inside an untrusted tree
    (the workspace, the staged copy) removed — so a planted ``node_modules/.bin``
    on ``PATH`` can never be used to resolve a tool for the token-bearing wrangler
    process. Returns ``None`` when no ``PATH`` is set."""
    raw = os.environ.get("PATH")
    if not raw:
        return None
    resolved_roots = []
    for r in unsafe_roots:
        try:
            resolved_roots.append(r.resolve())
        except OSError:
            continue
    kept: list[str] = []
    for entry in raw.split(os.pathsep):
        if not entry:
            continue
        # SEC-3/SEC-32: DROP every RELATIVE PATH entry (``.``, ``./x``, ``bin``) — a
        # relative entry is resolved against the (untrusted, workspace) cwd of the
        # wrangler subprocess, so a build-planted ``./node_modules/.bin`` could shadow a
        # real tool. Only ABSOLUTE, outside-the-untrusted-tree entries survive.
        if not Path(entry).is_absolute():
            continue
        try:
            ep = Path(entry).resolve()
        except OSError:
            continue
        if any(ep == root or ep.is_relative_to(root) for root in resolved_roots):
            continue
        kept.append(entry)
    return os.pathsep.join(kept) if kept else None


def _resolve_trusted_wrangler(*unsafe_roots: Path) -> str:
    """Resolve the wrangler invocation from a TRUSTED location (SEC-3/CORR-3), NEVER
    the untrusted workspace. We do NOT use ``npx wrangler`` (npx resolves
    ``./node_modules/.bin/wrangler`` FIRST — an attacker-planted binary would steal
    the CF token + ADMIN_TOKEN). Resolution order:

      1. ``DISCO_WRANGLER_BIN`` — an operator-pinned ABSOLUTE path (must exist and
         lie OUTSIDE every untrusted root);
      2. ``shutil.which("wrangler")`` against a SANITISED ``PATH`` (relative entries +
         any entry inside an untrusted tree stripped), accepted only if it resolves to
         an ABSOLUTE path OUTSIDE every untrusted root.

    WAVE 2 (SEC-3/CORR-3/SEC-32): there is NO bare-``wrangler`` fallback. A bare name is
    resolved by the OS from the subprocess ``PATH`` at exec time, and a relative/unsafe
    ``PATH`` entry (or a build-planted ``dist/wrangler``) could be selected — so if no
    TRUSTED absolute binary outside the workspace/staging tree is found, this FAILS
    CLOSED (:class:`DeployRefused` ``WRANGLER_NOT_TRUSTED``) rather than risk running a
    workspace-controlled wrangler with the CF token + ADMIN_TOKEN. The deploy NEVER
    prefixes it with ``npx`` and NEVER runs it through the workspace ``node_modules``."""
    resolved_roots = []
    for r in unsafe_roots:
        try:
            resolved_roots.append(r.resolve())
        except OSError:
            continue

    def _outside(p: Path) -> bool:
        if not p.is_absolute():
            return False
        try:
            rp = p.resolve()
        except OSError:
            return False
        return not any(rp == root or rp.is_relative_to(root) for root in resolved_roots)

    pinned = os.environ.get(_WRANGLER_BIN_ENV, "").strip()
    if pinned:
        pp = Path(pinned)
        if pp.is_absolute() and pp.exists() and _outside(pp):
            return str(pp)
    # Resolve only against the SANITISED PATH (relative + in-workspace entries already
    # stripped) so ``which`` can never return a workspace-resolved binary.
    sanitized = _sanitized_path(*unsafe_roots)
    found = shutil.which("wrangler", path=sanitized) if sanitized else None
    if found and _outside(Path(found)):
        return found
    raise DeployRefused(
        RefusalReason.WRANGLER_NOT_TRUSTED,
        "No TRUSTED wrangler binary could be resolved from outside the workspace/"
        "staging tree. Pin one via DISCO_WRANGLER_BIN (an absolute path) — refusing to "
        "deploy with a possibly workspace-controlled wrangler (fail closed).",
    )


def _deploy_env(
    token: str, account_id: str | None, *, home: str, unsafe_roots: tuple[Path, ...] = ()
) -> dict[str, str]:
    """The env for the TRUSTED wrangler steps (SEC-32 minimised): a tiny non-sensitive
    allowlist, a SANITISED ``PATH`` (workspace/staging entries stripped), a throwaway
    ``HOME`` (so a planted ``~/.npmrc``/``~/.wrangler`` can't redirect to host creds),
    PLUS only the Cloudflare credentials wrangler reads. ``NODE_OPTIONS`` and
    ``npm_config_*`` are deliberately NOT forwarded (preload / registry-redirect
    vectors). The token rides ONLY here — never in the build env, never argv, never
    logged."""
    src = os.environ
    env = {k: src[k] for k in _WRANGLER_ENV_ALLOWLIST if k in src}
    sanitized_path = _sanitized_path(*unsafe_roots)
    if sanitized_path is not None:
        env["PATH"] = sanitized_path
    env["HOME"] = home
    env[_WRANGLER_TOKEN_ENV] = token
    if account_id:
        env[_WRANGLER_ACCOUNT_ENV] = account_id
    return env


# ---- per-workspace deploy mutex (SEC-25/CORR-11) -----------------------------

#: One asyncio mutex per resolved workspace path. A real deploy mutates a single,
#: shared blast radius — the staged dist, the remote D1, wrangler.toml, the
#: deployment record — so two concurrent deploys of the SAME workspace must NEVER
#: interleave (a half-substituted wrangler.toml, a doubled D1 create, a torn
#: record). Keyed on the resolved live-workspace path; created lazily (no ``await``
#: between the get and the set, so this is race-free on the single-threaded loop).
_DEPLOY_LOCKS: dict[str, asyncio.Lock] = {}


def _deploy_lock_for(workspace: Path) -> asyncio.Lock:
    try:
        key = str(workspace.resolve())
    except OSError:
        key = str(workspace)
    lock = _DEPLOY_LOCKS.get(key)
    if lock is None:
        lock = asyncio.Lock()
        _DEPLOY_LOCKS[key] = lock
    return lock


# ---- cross-process advisory deploy lock (SEC-25) -----------------------------

#: The in-process ``_DEPLOY_LOCKS`` mutex + the routes per-conversation in-flight set
#: only serialise deploys WITHIN one server process. A multi-worker server (gunicorn
#: ``-w N``, multiple uvicorn workers, a restarted process racing the old one) runs N
#: independent processes that DON'T share that state, so two of them could interleave a
#: deploy of the SAME workspace — a torn wrangler.toml substitution, a doubled D1
#: create, a half-written deployment record. This file ``flock`` is the cross-process
#: backstop: an OS-level advisory lock, keyed by the resolved workspace path, that
#: serialises across processes (and auto-releases on fd close / process death, so a
#: crashed holder never deadlocks the next deploy).
#:
#: Operator/test override for the directory holding the lockfiles. MUST be a
#: server-controlled path OUTSIDE the untrusted workspace.
_DEPLOY_LOCK_DIR_ENV = "DISCO_DEPLOY_LOCK_DIR"


def _deploy_lock_dir() -> Path:
    """The server-controlled directory the cross-process deploy lockfiles live in —
    NEVER the untrusted workspace, so the sandboxed build agent can't read, delete, or
    pre-plant a lockfile to defeat the guard. Resolution: an explicit
    ``DISCO_DEPLOY_LOCK_DIR`` override, else ``$XDG_STATE_HOME/disco/deploy-locks``
    (or ``$XDG_CONFIG_HOME/disco/deploy-locks``), else a fixed subdir of the system temp
    dir. Created (parents, 0o700) on first use."""
    return workspace_process_lock_dir()


def _deploy_lock_path(workspace: Path) -> Path:
    """The lockfile path for *workspace*: ``<lock dir>/<sha256(resolved path)>.lock``.
    Keyed by a hash of the RESOLVED workspace path so two processes targeting the same
    workspace pick the same lockfile (and distinct workspaces never collide), and the
    filename never leaks a host path."""
    return workspace_process_lock_path(workspace)


@contextlib.contextmanager
def _cross_process_deploy_lock(workspace: Path) -> Iterator[None]:
    """Hold a CROSS-PROCESS advisory lock for *workspace* for the body's duration
    (SEC-25). Acquires ``flock(LOCK_EX | LOCK_NB)`` on a server-controlled lockfile; if
    ANOTHER process already holds it, FAIL FAST with :class:`DeployRefused`
    (``DEPLOY_IN_PROGRESS``) rather than block or interleave. The lock is released — and
    the fd closed — in a ``finally`` (and ``flock`` auto-releases on process death, so a
    crashed holder leaves no deadlock; the stale lockfile on disk is harmless, the next
    process re-acquires it).

    PLATFORM-SPECIFIC POSTURE (the cross-process guarantee is conditional on ``fcntl``):
      * POSIX hosts (``fcntl`` present): a real OS-level ``flock`` — the lock is held
        CROSS-PROCESS, so a multi-worker server is fully serialised for this workspace.
      * Non-POSIX hosts (no ``fcntl`` — e.g. native Windows): this DEGRADES to a no-op
        with a logged warning. Locking is then IN-PROCESS ONLY (the ``_DEPLOY_LOCKS``
        asyncio mutex + the routes in-flight set still serialise WITHIN the one process);
        the cross-process backstop is simply unavailable. The deploy is NOT crashed — a
        single-process deployment (the common case) is unaffected, and a multi-worker
        non-POSIX deployment must rely on process-local serialisation. The degradation is
        logged (below) so operators/readers understand the reduced posture."""
    if _fcntl is None:  # pragma: no cover - exercised only on non-POSIX hosts
        # Non-POSIX (no fcntl): no cross-process lock is available — degrade to a logged
        # no-op (in-process serialisation only). See the PLATFORM-SPECIFIC POSTURE note above.
        _log.warning(
            "fcntl is unavailable on this platform; the AppKit deploy lock is "
            "process-local only. With multiple server processes a deploy of the same "
            "workspace could interleave (SEC-25 cross-process backstop disabled)."
        )
        yield
        return
    try:
        with try_workspace_process_fence(workspace):
            yield
    except WorkspaceProcessBusy as exc:
        raise DeployRefused(
            RefusalReason.DEPLOY_IN_PROGRESS,
            "Another workspace operation is already in progress. Wait for it "
            "to finish, then retry the deploy.",
        ) from exc


# ---- server-controlled staging root (out-of-tree wrangler-config bypass) -----

#: Operator/test override for the PARENT directory the immutable per-deploy staging trees
#: are created under. The staging root determines the ANCESTOR chain ``wrangler deploy``
#: walks UP from its cwd to discover a ``.wrangler/deploy/config.json`` redirect, so it MUST
#: be a server-controlled path whose ancestors are verified clean (:func:`_assert_no_ancestor_
#: wrangler_config`). MUST live OUTSIDE the untrusted workspace. Defaults to the system temp
#: dir; an operator can point it at a path with guaranteed-clean ancestors.
_DEPLOY_STAGE_DIR_ENV = "DISCO_DEPLOY_STAGE_DIR"


def _deploy_stage_root() -> Path:
    """The server-controlled PARENT directory the immutable per-deploy staging trees are
    ``mkdtemp``'d under. Resolution: an explicit ``DISCO_DEPLOY_STAGE_DIR`` override, else the
    system temp dir. Created (parents, 0o700) on first use. NEVER the untrusted workspace —
    the staged tree's ancestors are part of wrangler's config-redirect discovery path (it
    walks UP from cwd), so they must be a location WE control + can verify clean."""
    override = os.environ.get(_DEPLOY_STAGE_DIR_ENV, "").strip()
    base = Path(override) if override else Path(tempfile.gettempdir())
    base.mkdir(parents=True, exist_ok=True, mode=0o700)
    return base


#: Workspace-relative AppSpec — the digest anchor (an app must exist to deploy).
_APPSPEC_RELPATH = ".disco/appspec.json"
#: Where the non-secret deployment records live (idempotency + teardown audit).
_DEPLOY_RECORD_DIR = ".disco/cloudflare/deployments"

#: The placeholder the generated ``wrangler.toml`` ships with — substituted with
#: the REAL D1 database_id (from ``wrangler d1 create``/adopt) before deploy (P1-3).
_D1_ID_PLACEHOLDER = "REPLACE_WITH_D1_DATABASE_ID"
#: A Cloudflare D1 database id is a UUID. Used to pull the real id out of the
#: ``d1 create`` / ``d1 list`` output so the placeholder can be substituted.
_UUID_RE = re.compile(
    r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"
)


# ---- O1: account connection (token via the encrypted SecretStore) ------------


def connection_status(store: SecretStore) -> ConnectionStatus:
    """The O1 connection state — derived from the SecretStore, NEVER returning the
    token. ``connected`` is True only when a token is present AND decryptable
    (the app secret is right), mirroring the plan's ``undecryptable_names`` use."""
    token_present = store.has_secret(CF_TOKEN_SECRET)
    if not token_present:
        return ConnectionStatus(
            connected=False,
            token_present=False,
            decryptable=False,
            account_id=None,
            detail="No Cloudflare account connected. Store an API token to connect.",
        )
    undecryptable = set(store.undecryptable_names())
    decryptable = CF_TOKEN_SECRET not in undecryptable
    account_id = store.get_secret(CF_ACCOUNT_SECRET) if decryptable else None
    if not decryptable:
        return ConnectionStatus(
            connected=False,
            token_present=True,
            decryptable=False,
            account_id=None,
            detail=(
                "A Cloudflare token is stored but cannot be decrypted — the app "
                "secret (DISCO_SECRET_KEY) is missing or wrong. Restore it or "
                "reconnect the account."
            ),
        )
    # SEC-29: a real deploy needs the Cloudflare ACCOUNT id (wrangler targets an
    # account). A token-only connection (decryptable token but no/empty account_id) is
    # NOT 'connected' — fail closed so the deploy can't fire against an unknown account.
    if not (account_id and account_id.strip()):
        return ConnectionStatus(
            connected=False,
            token_present=True,
            decryptable=True,
            account_id=None,
            detail=(
                "A Cloudflare token is stored but no account id is connected. "
                "Reconnect the account with both an API token and the account id."
            ),
        )
    return ConnectionStatus(
        connected=True,
        token_present=True,
        decryptable=True,
        account_id=account_id,
        detail="Cloudflare account connected (API token stored encrypted).",
    )


def connect_account(store: SecretStore, *, token: str, account_id: str) -> ConnectionStatus:
    """Store the Cloudflare API token (encrypted) + the account id. Raises if the
    app secret is unset (the store can't encrypt). The plaintext token is written
    only as ciphertext and is never returned or logged."""
    if not token.strip():
        raise ValueError("token must be non-empty")
    if not account_id.strip():
        raise ValueError("account_id must be non-empty")
    # SEC-30: Cloudflare deploy credentials are high-value — refuse to store them
    # under a weak/absent app secret (whose KDF would be brute-forceable), rather
    # than silently encrypting them under a footgun key. Low-value secrets keep
    # their warn-only behaviour; only this deploy-credential path is hard-gated.
    store.set_secret(CF_TOKEN_SECRET, token.strip(), strong_required=True)
    store.set_secret(CF_ACCOUNT_SECRET, account_id.strip(), strong_required=True)
    return connection_status(store)


def disconnect_account(store: SecretStore) -> ConnectionStatus:
    """Remove the stored token + account id."""
    store.clear_secret(CF_TOKEN_SECRET)
    store.clear_secret(CF_ACCOUNT_SECRET)
    return connection_status(store)


# ---- workspace reading + digests (pure, read-only) ---------------------------


def _read_export_files(workspace: Path) -> dict[str, str | None]:
    """Read the CF export deliverables + the optional real ``.dev.vars`` from the
    workspace, exactly as ``verify_appkit_app`` feeds ``cloudflare_export_ready``.

    P0 (round 6): EVERY read here goes through the one containment-guarded reader
    (:func:`read_workspace_file`). The ``.dev.vars`` (the secret-safety input) and
    each ``CF_EXPORT_FILES`` deliverable could be a SYMLINK escaping the workspace
    (→ a host secret); the guard refuses it BEFORE any dereference, so this
    export-readiness path — called before ``_tree_digest`` — can never read a host
    secret through an escaping link."""
    workspace_root = workspace.resolve()
    files: dict[str, str | None] = {}
    for rel in CF_EXPORT_FILES:
        files[rel] = read_workspace_file(workspace / rel, workspace_root)
    files[".dev.vars"] = read_workspace_file(workspace / ".dev.vars", workspace_root)
    return files


def _digest_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


#: Top-level subtrees + files the deploy-tree digest IGNORES (and that staging never
#: copies — :func:`_stage_deploy_tree` prunes the same set). ``node_modules`` and
#: ``.git`` are regenerated / non-authored; ``.disco`` is internal disco state
#: (deployment records + the appspec, which is digested separately so it stays
#: covered); ``.wrangler`` is wrangler's LOCAL state dir — it is NEVER an AppKit-authored
#: input, and it can carry a ``deploy/config.json`` REDIRECT that points wrangler at an
#: arbitrary alt config (the config-source bypass), so it is excluded from staging so a
#: redirect can't even be present (the alt-config guard ALSO refuses if one is found —
#: exclude + assert-absent, defence in depth); ``.dev.vars`` is the real LOCAL secret —
#: never deployed, and it must not bind the plan to secret content. EVERYTHING else the
#: build+deploy consumes is hashed.
_TREE_SKIP_DIRS = frozenset({"node_modules", ".git", ".disco", ".wrangler"})
_TREE_SKIP_FILES = frozenset({".dev.vars"})


def _iter_tree_files(workspace: Path) -> list[Path]:
    """Every deploy-relevant file under *workspace* (recursive), minus the skipped
    subtrees/files. Sorted so the digest is order-independent + stable.

    ``os.walk`` does NOT follow symlinked directories (``followlinks=False``), so a
    symlinked subtree is never descended into; a symlink-to-FILE, however, appears
    in ``names`` and IS returned here — the containment guard in ``_tree_digest``
    (the shared :func:`workspace_contained` check) rejects it before any read."""
    found: list[Path] = []
    for root, dirs, names in os.walk(workspace):
        dirs[:] = [d for d in dirs if d not in _TREE_SKIP_DIRS]
        for name in names:
            if name in _TREE_SKIP_FILES:
                continue
            found.append(Path(root) / name)
    return sorted(found)


def workspace_contained(path: Path, workspace_root: Path) -> bool:
    """The ONE workspace-containment guard shared by BOTH the planner/tree-digest
    reads (:func:`_tree_digest`) and the host→sandbox push (``sandbox_build``), so
    the two can never drift.

    True ONLY when *path* stays STRICTLY inside the already-resolved
    *workspace_root* AFTER resolving symlinks. ``Path.resolve`` follows every
    symlink in the path, so a workspace entry that IS (or traverses) a symlink
    pointing at a host file resolves OUTSIDE the root and is rejected — the caller
    must NEVER ``read_bytes`` through it. A normal regular file inside the
    workspace resolves to a path under the root and is allowed. Fail CLOSED on any
    resolution error (a dangling/cyclic link is treated as not contained)."""
    try:
        resolved = path.resolve()
    except OSError:
        return False
    return resolved == workspace_root or resolved.is_relative_to(workspace_root)


@overload
def read_workspace_file(
    path: Path, workspace_root: Path, *, text: Literal[True] = ...
) -> str | None: ...
@overload
def read_workspace_file(
    path: Path, workspace_root: Path, *, text: Literal[False]
) -> bytes | None: ...
def read_workspace_file(
    path: Path, workspace_root: Path, *, text: bool = True
) -> str | bytes | None:
    """The ONE choke-point reader for EVERY workspace/deployable file. It applies
    the shared containment guard (:func:`workspace_contained`) FIRST — resolving the
    path (following symlinks) and requiring it to stay STRICTLY inside the already
    *resolved* ``workspace_root`` — and only then reads. An escaping path (a
    workspace entry that IS, or traverses, a SYMLINK pointing at a host file — a
    SecretStore file, ``~/.config/disco``, a provider key) is REFUSED with
    :class:`DeployRefused` (``WORKSPACE_SYMLINK_ESCAPE``) BEFORE any open/read, so
    the symlink target's bytes are NEVER dereferenced.

    Every workspace-file read in this package (export-readiness, the secret-safety
    ``.dev.vars`` read, the spec digest, the tree digest, the db_id substitution
    read, and the host→sandbox push) routes through here, so NO workspace file is
    read anywhere without the containment guard and the per-site guards can never
    drift back in. A normal regular file inside the workspace reads fine; a MISSING
    file (or any read error) returns ``None`` so callers can treat absence as 'no
    file' (matching the previous ``_read_text`` behaviour)."""
    if not workspace_contained(path, workspace_root):
        raise DeployRefused(
            RefusalReason.WORKSPACE_SYMLINK_ESCAPE,
            "A workspace path resolves outside the workspace (symlink escape): "
            f"{path.name}. Refusing to read it — the deploy fails closed.",
        )
    try:
        if text:
            return path.read_text(encoding="utf-8", errors="replace")
        return path.read_bytes()
    except (FileNotFoundError, IsADirectoryError, OSError):
        return None


def _guarded_write_target(rel: Path | str, workspace_root: Path) -> Path:
    """Validate (and return) the absolute host path for WRITING
    ``workspace_root/rel``. The WRITE-class mirror of :func:`workspace_contained` /
    :func:`read_workspace_file` — fail CLOSED with :class:`DeployRefused`
    (``WORKSPACE_SYMLINK_ESCAPE``) when:

      * *rel* is absolute or contains a ``..`` component, OR
      * ANY path component, from ``workspace_root`` down to the target, is a SYMLINK.

    Crucially it LSTATs each component (:meth:`Path.is_symlink`) rather than calling
    :meth:`Path.resolve` — ``resolve`` would FOLLOW an in-workspace symlink and MASK
    it. That masking is exactly the round-8 escape codex flagged for the
    digest-SKIPPED ``.disco`` record dir: a planted symlink at ``.disco`` /
    ``.disco/cloudflare`` / ``deployments`` (any component) could redirect the record
    WRITE outside the workspace, and the same for a symlinked ``dist`` root or a
    ``wrangler.toml`` parent. Because no component is a symlink once this passes, the
    lexical target equals its resolved form and is GUARANTEED strictly inside the
    resolved ``workspace_root``. Returns the absolute (lexical, unresolved) target;
    the caller writes ONLY after this passes."""
    rel = Path(rel)
    if rel.is_absolute() or any(part == ".." for part in rel.parts):
        raise DeployRefused(
            RefusalReason.WORKSPACE_SYMLINK_ESCAPE,
            f"A workspace WRITE path is absolute or escapes via '..': {rel}. "
            "Refusing to write it — the deploy fails closed.",
        )
    cur = workspace_root
    if cur.is_symlink():  # the workspace root itself must be a real directory
        raise DeployRefused(
            RefusalReason.WORKSPACE_SYMLINK_ESCAPE,
            "The workspace root is a symlink; refusing to write through it.",
        )
    for part in rel.parts:
        cur = cur / part
        if cur.is_symlink():
            raise DeployRefused(
                RefusalReason.WORKSPACE_SYMLINK_ESCAPE,
                f"A workspace WRITE path component is a symlink: {part} (in {rel}). "
                "Refusing to write through it — the deploy fails closed.",
            )
    return cur


def ensure_workspace_dir(rel: Path | str, workspace_root: Path) -> Path:
    """The ONE guarded ``mkdir -p`` for the package: create ``workspace_root/rel``,
    materialising each component as a REAL directory and REFUSING
    (:class:`DeployRefused` ``WORKSPACE_SYMLINK_ESCAPE``) if any existing component is
    a SYMLINK or already exists as a non-directory. No raw ``mkdir`` of a
    workspace/state path lives outside this helper, so a planted symlinked component
    can never be created (or written) through. Returns the absolute target dir."""
    target = _guarded_write_target(rel, workspace_root)
    cur = workspace_root
    for part in Path(rel).parts:
        cur = cur / part
        if cur.is_symlink():
            raise DeployRefused(
                RefusalReason.WORKSPACE_SYMLINK_ESCAPE,
                f"A workspace dir component is a symlink: {part}. Refusing to create through it.",
            )
        if cur.exists():
            if not cur.is_dir():
                raise DeployRefused(
                    RefusalReason.WORKSPACE_SYMLINK_ESCAPE,
                    f"A workspace path component exists but is not a directory: {part}.",
                )
        else:
            cur.mkdir()
    return target


@contextlib.contextmanager
def _open_workspace_write_parent(
    rel: Path | str,
    workspace_root: Path,
) -> Iterator[tuple[Path, int, str, Callable[[], None]]]:
    """Open a write parent by descriptor, never by a re-resolved host path.

    Every ancestor stays open through publication and can be revalidated against
    its parent entry. A concurrent rename/symlink swap therefore either fails the
    identity check or leaves the write bound to the already-open safe directory;
    it can never redirect publication outside the lexical workspace root.
    """

    relative = Path(rel)
    if relative.is_absolute() or not relative.parts or any(part == ".." for part in relative.parts):
        raise DeployRefused(
            RefusalReason.WORKSPACE_SYMLINK_ESCAPE,
            f"A workspace WRITE path is absolute or escapes via '..': {relative}. "
            "Refusing to write it — the deploy fails closed.",
        )
    if not hasattr(os, "O_NOFOLLOW") or not hasattr(os, "O_DIRECTORY"):
        raise DeployRefused(
            RefusalReason.WORKSPACE_SYMLINK_ESCAPE,
            "This host cannot provide no-follow descriptor traversal for workspace writes.",
        )

    root = Path(os.path.abspath(os.fspath(workspace_root)))
    directory_flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    directory_flags |= getattr(os, "O_CLOEXEC", 0)

    def _identity(info: os.stat_result) -> tuple[int, int, int]:
        return (info.st_dev, info.st_ino, stat.S_IFMT(info.st_mode))

    def _refuse(message: str, exc: BaseException | None = None) -> NoReturn:
        error = DeployRefused(RefusalReason.WORKSPACE_SYMLINK_ESCAPE, message)
        if exc is None:
            raise error
        raise error from exc

    try:
        root_listed = os.lstat(root)
        root_fd = os.open(root, directory_flags)
    except OSError as exc:
        _refuse("The workspace root is missing, replaced, or symlinked; refusing to write.", exc)
    root_opened = os.fstat(root_fd)
    if not stat.S_ISDIR(root_listed.st_mode) or _identity(root_listed) != _identity(root_opened):
        os.close(root_fd)
        _refuse("The workspace root changed before it could be opened safely.")

    opened: list[tuple[int, str, int]] = []
    current_fd = root_fd
    try:
        for part in relative.parts[:-1]:
            try:
                listed = os.stat(part, dir_fd=current_fd, follow_symlinks=False)
            except FileNotFoundError:
                try:
                    os.mkdir(part, 0o700, dir_fd=current_fd)
                    os.fsync(current_fd)
                except FileExistsError:
                    pass
                except OSError as exc:
                    _refuse(f"Workspace directory {part!r} could not be created safely.", exc)
                try:
                    listed = os.stat(part, dir_fd=current_fd, follow_symlinks=False)
                except OSError as exc:
                    _refuse(f"Workspace directory {part!r} changed while being created.", exc)
            except OSError as exc:
                _refuse(f"Workspace directory {part!r} could not be inspected safely.", exc)
            try:
                child_fd = os.open(part, directory_flags, dir_fd=current_fd)
            except OSError as exc:
                _refuse(f"Workspace directory {part!r} is not a stable real directory.", exc)
            opened_info = os.fstat(child_fd)
            if not stat.S_ISDIR(listed.st_mode) or _identity(listed) != _identity(opened_info):
                os.close(child_fd)
                _refuse(f"Workspace directory {part!r} changed during traversal.")
            opened.append((current_fd, part, child_fd))
            current_fd = child_fd

        def revalidate() -> None:
            try:
                current_root = os.lstat(root)
            except OSError as exc:
                _refuse("The workspace root changed during record publication.", exc)
            if _identity(current_root) != _identity(root_opened):
                _refuse("The workspace root changed during record publication.")
            for parent_fd, name, child_fd in opened:
                try:
                    current = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
                except OSError as exc:
                    _refuse(f"Workspace directory {name!r} changed during publication.", exc)
                if _identity(current) != _identity(os.fstat(child_fd)):
                    _refuse(f"Workspace directory {name!r} changed during publication.")

        yield root.joinpath(*relative.parts), current_fd, relative.parts[-1], revalidate
    finally:
        for _parent_fd, _name, child_fd in reversed(opened):
            os.close(child_fd)
        os.close(root_fd)


def write_workspace_file(
    rel: Path | str, data: str | bytes, workspace_root: Path, *, exclusive: bool = False
) -> Path:
    """The ONE choke-point WRITER for EVERY workspace/state file (the WRITE-class
    twin of :func:`read_workspace_file`). It opens the lexical workspace root and
    every parent with descriptor-relative ``O_NOFOLLOW`` traversal, retaining and
    revalidating the entire descriptor chain through atomic publication. A planted
    or concurrently swapped symlink therefore fails closed and can never redirect
    the write outside the workspace. Every workspace/state write in this package
    routes through here. Returns the lexical absolute target after publication.

    SEC-16 (hardlink defence): the per-component symlink check catches a symlinked LEAF,
    but not a HARDLINK aliasing a host file. With ``exclusive`` the leaf is created via
    ``O_CREAT|O_EXCL`` (FAIL if it already exists — defeats a pre-planted file/hardlink at
    the target), and a non-exclusive overwrite first LSTATs the existing leaf and REFUSES a
    multi-link (``st_nlink > 1``) regular file, so a truncating write can never clobber a
    host file aliased into the record path."""
    raw = data.encode("utf-8") if isinstance(data, str) else data
    with _open_workspace_write_parent(rel, workspace_root) as opened:
        target, directory_fd, target_name, revalidate = opened
        if not exclusive:
            try:
                existing = os.stat(target_name, dir_fd=directory_fd, follow_symlinks=False)
            except FileNotFoundError:
                existing = None
            if existing is not None and (
                not stat.S_ISREG(existing.st_mode) or existing.st_nlink > 1
            ):
                raise DeployRefused(
                    RefusalReason.WORKSPACE_SYMLINK_ESCAPE,
                    f"A workspace WRITE target is not a private regular file: {target_name}. "
                    "It could alias a host path — refusing to write (fail closed).",
                )
        temporary_name = f".{target_name}.{_secrets.token_hex(16)}.tmp"
        temporary_fd: int | None = None
        temporary_exists = False
        try:
            temporary_fd = os.open(
                temporary_name,
                os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW,
                0o600,
                dir_fd=directory_fd,
            )
            temporary_exists = True
            view = memoryview(raw)
            while view:
                written = os.write(temporary_fd, view)
                if written <= 0:
                    raise OSError("workspace record write made no progress")
                view = view[written:]
            os.fsync(temporary_fd)
            os.close(temporary_fd)
            temporary_fd = None
            revalidate()
            if exclusive:
                try:
                    os.link(
                        temporary_name,
                        target_name,
                        src_dir_fd=directory_fd,
                        dst_dir_fd=directory_fd,
                        follow_symlinks=False,
                    )
                except FileExistsError as exc:
                    raise DeployRefused(
                        RefusalReason.WORKSPACE_SYMLINK_ESCAPE,
                        f"The deploy record path already exists ({target_name}); refusing to "
                        "overwrite a pre-existing file (fail closed).",
                    ) from exc
                os.unlink(temporary_name, dir_fd=directory_fd)
                temporary_exists = False
            else:
                os.replace(
                    temporary_name,
                    target_name,
                    src_dir_fd=directory_fd,
                    dst_dir_fd=directory_fd,
                )
                temporary_exists = False
            os.fsync(directory_fd)
            revalidate()
            return target
        finally:
            if temporary_fd is not None:
                os.close(temporary_fd)
            if temporary_exists:
                with contextlib.suppress(FileNotFoundError):
                    os.unlink(temporary_name, dir_fd=directory_fd)


def _tree_digest(workspace: Path) -> str:
    """A stable digest over the ENTIRE deployable app tree — EVERY file the
    build+deploy actually consumes: ``package.json``, the lockfile, ALL
    client/worker source, ``tsconfig``/``vite`` config, the schema, and any
    pre-built output — NOT merely the ``CF_EXPORT_FILES`` deliverables. The
    confirmation phrase is bound (via ``plan_hash``) to this digest, so a drift to
    ANY build-affecting file — including the workspace-controlled build script
    (``package.json`` ``"build"``) and the client source ``npm run build``
    compiles — changes the hash and INVALIDATES a stale confirmation. Excludes
    regenerated/non-authored trees + the local secret (see ``_TREE_SKIP_DIRS`` /
    ``_TREE_SKIP_FILES``).

    P0 (round 5/6): EVERY file read here goes through the SINGLE guarded reader
    (:func:`read_workspace_file`), which applies the SAME containment guard the
    push uses. An escaping symlink (→ a host secret) is REFUSED — the reader raises
    :class:`DeployRefused` and the symlink target's bytes are NEVER dereferenced
    into the digest/plan_hash. So no host-secret read can happen on the planner
    path, before the push guard ever runs."""
    workspace_root = workspace.resolve()
    h = hashlib.sha256()
    for path in _iter_tree_files(workspace):
        rel = path.relative_to(workspace).as_posix()
        # Raises DeployRefused(WORKSPACE_SYMLINK_ESCAPE) on an escaping symlink,
        # BEFORE any read — fail closed rather than hash a host secret into plan_hash.
        data = read_workspace_file(path, workspace_root, text=False)
        h.update(rel.encode("utf-8"))
        h.update(b"\0")
        h.update(data if data is not None else b"<unreadable>")
        h.update(b"\0")
    return h.hexdigest()


def _spec_digest(workspace: Path) -> str:
    # Guarded read: a ``.disco/appspec.json`` that is an escaping symlink refuses
    # (WORKSPACE_SYMLINK_ESCAPE) before any dereference.
    text = read_workspace_file(workspace / _APPSPEC_RELPATH, workspace.resolve())
    return _digest_text(text or "")


# ---- immutable staged copy of the deploy tree (SEC-6/SEC-7/CORR-2 + SEC-2/SEC-8) --


def _stage_deploy_tree(workspace: Path) -> Path:
    """Build an IMMUTABLE, symlink-free, hardlink-free STAGED COPY of the
    deploy-relevant workspace, and run the build + wrangler against THAT — never the
    live mutable workspace (SEC-6/SEC-7/CORR-2). This is the single choke point that
    defeats the check-then-use (TOCTOU) races the per-site symlink guards each closed
    round-by-round: once the bytes are copied into a fresh tree we own, an attacker
    can no longer swap a checked path to a symlink between our check and wrangler's
    open.

    The copy is a WHOLE-TREE validation (SEC-2/SEC-8/SEC-17/SEC-18): it walks every
    deploy-followed tree (worker source + config, not just assets) and FAILS CLOSED on

      * a symlinked workspace ROOT (``Path.is_symlink`` on the root),
      * ANY symlink — file OR directory, at any depth (lstat per entry; never the
        silent ``os.walk(followlinks=False)`` skip),
      * ANY hardlink / multi-link regular file (``st_nlink > 1``) — a hardlink to a
        host file outside the workspace would otherwise be copied/published.

    Only REGULAR files (minus the regenerated/internal ``_TREE_SKIP_DIRS`` /
    ``_TREE_SKIP_FILES``) are copied, byte for byte, into a private temp dir. Returns
    the staged root; the caller MUST ``shutil.rmtree`` it when done."""
    workspace_root = workspace.resolve()
    if workspace.is_symlink():
        raise DeployRefused(
            RefusalReason.WORKSPACE_SYMLINK_ESCAPE,
            "The workspace root is a symlink; refusing to stage/deploy through it (fail closed).",
        )
    staged = Path(tempfile.mkdtemp(prefix="disco-deploy-stage-", dir=str(_deploy_stage_root())))
    try:
        for root, dirs, names in os.walk(workspace):
            # Prune the regenerated/internal subtrees BEFORE the symlink check, so a
            # symlinked node_modules/.git/.disco (never deployed) is simply ignored.
            dirs[:] = [d for d in dirs if d not in _TREE_SKIP_DIRS]
            root_path = Path(root)
            for d in dirs:
                if (root_path / d).is_symlink():
                    raise DeployRefused(
                        RefusalReason.WORKSPACE_SYMLINK_ESCAPE,
                        f"A symlinked DIRECTORY is present in the deploy tree: "
                        f"{(root_path / d).name}. Refusing to stage/deploy — wrangler "
                        "would follow it (fail closed).",
                    )
            for name in names:
                if name in _TREE_SKIP_FILES:
                    continue
                src = root_path / name
                st = src.lstat()
                if stat.S_ISLNK(st.st_mode):
                    raise DeployRefused(
                        RefusalReason.WORKSPACE_SYMLINK_ESCAPE,
                        f"A symlinked FILE is present in the deploy tree: {name}. "
                        "Refusing to stage/deploy — wrangler follows symlinks and could "
                        "publish a host-secret target (fail closed).",
                    )
                if stat.S_ISREG(st.st_mode) and st.st_nlink > 1:
                    raise DeployRefused(
                        RefusalReason.WORKSPACE_SYMLINK_ESCAPE,
                        f"A multi-link (hardlinked) file is present in the deploy tree: "
                        f"{name} (st_nlink={st.st_nlink}). A hardlink can alias a host "
                        "file outside the workspace — refusing to stage/deploy (fail "
                        "closed).",
                    )
                rel = src.relative_to(workspace_root)
                dest = staged / rel
                dest.parent.mkdir(parents=True, exist_ok=True)
                # Copy the BYTES only (never the link / metadata that could re-introduce
                # a special file). The READ routes through the guarded reader (the one
                # workspace-read choke point); the WRITE targets the fresh temp staging
                # tree we own (not a workspace path), so it needs no workspace guard.
                data = read_workspace_file(src, workspace_root, text=False)
                dest.write_bytes(data if data is not None else b"")
        # Trusted reconstruction inputs live under the otherwise-pruned `.disco`
        # tree. Freeze them explicitly alongside the deploy bytes so pre/post-build
        # verification cannot race a live AppSpec, DesignSpec, or primitive provenance
        # rewrite. Missing inputs remain absent and the trusted verifier fails closed.
        trusted_inputs = [
            _APPSPEC_RELPATH,
            ".disco/designspec.json",
            ".disco/primitives/stripe.json",
        ]
        raw_app = read_workspace_file(workspace / _APPSPEC_RELPATH, workspace_root)
        if raw_app:
            with contextlib.suppress(Exception):
                if load_app_spec_from_bytes(raw_app).webhooks is not None:
                    trusted_inputs.append(".disco/primitives/webhook.json")
        for relpath in trusted_inputs:
            src = workspace / relpath
            if not src.exists():
                continue
            st = src.lstat()
            if not stat.S_ISREG(st.st_mode) or st.st_nlink > 1:
                raise DeployRefused(
                    RefusalReason.WORKSPACE_SYMLINK_ESCAPE,
                    f"A trusted deployment input is not a single-link regular file: {relpath}.",
                )
            data = read_workspace_file(src, workspace_root, text=False)
            if data is not None:
                dest = staged / relpath
                dest.parent.mkdir(parents=True, exist_ok=True)
                dest.write_bytes(data)
    except BaseException:
        shutil.rmtree(staged, ignore_errors=True)
        raise
    return staged


# ---- SEC (config-source bypass): wrangler.toml is the SOLE config source ------

#: wrangler's local state DIR — never an AppKit-authored input. It can carry a
#: ``deploy/config.json`` REDIRECT (``{"configPath": "../../evil/wrangler.jsonc"}``) that
#: points ``wrangler deploy`` at an arbitrary config, so its mere presence is an injection.
_WRANGLER_STATE_DIR = ".wrangler"
#: ALTERNATE wrangler config FILE names (case-insensitive). ``wrangler deploy`` resolves
#: ``wrangler.json`` → ``wrangler.jsonc`` → ``wrangler.toml`` IN THAT ORDER, so a planted
#: ``wrangler.json``/``.jsonc`` is read BEFORE (and instead of) the gated ``wrangler.toml``.
_ALT_WRANGLER_CONFIG_NAMES = frozenset({"wrangler.json", "wrangler.jsonc"})
#: Regenerated/non-authored subtrees pruned from the alt-config scan so a DEPENDENCY's own
#: ``wrangler.json`` can't false-positive. ``.wrangler`` is DELIBERATELY NOT pruned — the
#: scan must flag it (it is excluded from STAGING separately, via ``_TREE_SKIP_DIRS``).
_ALT_CONFIG_SCAN_SKIP_DIRS = frozenset({"node_modules", ".git", ".disco"})


def _find_alt_wrangler_configs(tree: Path) -> list[str]:
    """Return the tree-relative paths of EVERY alternate wrangler config source under
    *tree*: a ``.wrangler`` entry (dir/file/symlink — the redirect vector) or a
    ``wrangler.json``/``wrangler.jsonc`` file, at ANY depth. Walks with
    ``followlinks=False`` and matches on the NAME only (lstat-free — never dereferences a
    symlink target), pruning the regenerated/internal subtrees so a dependency's own config
    can't false-positive. ``wrangler.toml`` is NOT matched (it is the sole permitted
    source)."""
    try:
        tree_root = tree.resolve()
    except OSError:
        tree_root = tree
    offenders: list[str] = []
    for root, dirs, names in os.walk(tree):
        dirs[:] = [d for d in dirs if d not in _ALT_CONFIG_SCAN_SKIP_DIRS]
        root_path = Path(root)
        for entry in (*dirs, *names):
            if entry == _WRANGLER_STATE_DIR or entry.lower() in _ALT_WRANGLER_CONFIG_NAMES:
                p = root_path / entry
                try:
                    offenders.append(p.relative_to(tree_root).as_posix())
                except ValueError:
                    offenders.append(entry)
    return offenders


def _assert_sole_wrangler_config(*trees: Path) -> None:
    """REFUSE the deploy if ANY alternate wrangler config source exists in *trees* — a
    ``wrangler.json``/``wrangler.jsonc`` or a ``.wrangler`` (redirect) dir anywhere. The
    deploy gates (SEC-5 allowlist, canonical-worker match, SEC-10 ownership) read/validate
    ONLY ``wrangler.toml``, but ``wrangler deploy`` resolves ``wrangler.json`` → ``.jsonc``
    → ``.toml`` by precedence AND honors a ``.wrangler/deploy/config.json`` redirect to an
    ARBITRARY config — so an alt source would deploy an UNCHECKED main/name/routes/account
    that bypasses every gate. AppKit emits ONLY ``wrangler.toml``, so any alt config is an
    injection → :class:`DeployRefused` (``ALT_WRANGLER_CONFIG``, fail closed). Scanned on
    the IMMUTABLE STAGED tree (the bytes wrangler actually runs against — TOCTOU-proof) AND
    on the LIVE workspace (which catches a planted ``.wrangler`` redirect that STAGING
    excludes — exclude + assert-absent, defence in depth)."""
    offenders = sorted({rel for tree in trees for rel in _find_alt_wrangler_configs(tree)})
    if offenders:
        raise DeployRefused(
            RefusalReason.ALT_WRANGLER_CONFIG,
            "An alternate wrangler config source is present in the deploy tree "
            f"({offenders}). AppKit generates ONLY wrangler.toml, but `wrangler deploy` "
            "resolves wrangler.json/.jsonc BEFORE wrangler.toml and honors a "
            ".wrangler/deploy/config.json redirect — an alt config would deploy an "
            "UNCHECKED worker/name/routes/account that bypasses the deploy gates. "
            "wrangler.toml is the sole config source — refusing to deploy (fail closed).",
        )


def _assert_no_ancestor_wrangler_config(cwd: Path) -> None:
    """Close the OUT-OF-TREE wrangler config-redirect bypass. ``_assert_sole_wrangler_config``
    scans the staged copy + the live workspace, but ``wrangler deploy`` discovers a
    ``.wrangler/deploy/config.json`` redirect by walking UP from its CWD — and the deploy runs
    wrangler with ``cwd=<staged>`` where ``<staged>`` is a ``mkdtemp`` dir under the staging
    root (e.g. ``/tmp/disco-deploy-stage-*``). So a PRE-EXISTING ``.wrangler`` at an ANCESTOR of
    that cwd — e.g. a planted ``/tmp/.wrangler/deploy/config.json`` pointing at
    ``/tmp/evil/wrangler.jsonc`` — sits OUTSIDE both scanned trees yet is honored by wrangler's
    upward walk, re-pointing it at an arbitrary config/main/account (the ``--config`` pin may
    NOT override a ``.wrangler`` redirect).

    Defence: walk EVERY directory from *cwd* (the exact cwd wrangler runs in) UP TO the
    filesystem root and REFUSE (:class:`DeployRefused` ``ANCESTOR_WRANGLER_CONFIG``, fail
    closed) if any of them contains a ``.wrangler`` entry (dir/file/symlink — the redirect
    lives at ``.wrangler/deploy/config.json``). Each candidate is ``os.lstat``'d — NEVER
    dereferenced: a ``.wrangler`` symlink is itself an offender and must not be followed to a
    host path. The clean case (no ancestor ``.wrangler`` anywhere above the staged cwd) passes
    and deploys. Paired with the controlled :func:`_deploy_stage_root` (so an operator can put
    staging under a path with verified-clean ancestors), this fails closed even when the
    staging root's ancestors are shared (a multi-tenant ``/tmp``)."""
    try:
        start = cwd.resolve()
    except OSError:
        start = cwd
    offenders: list[str] = []
    for ancestor in (start, *start.parents):
        candidate = ancestor / _WRANGLER_STATE_DIR
        try:
            os.lstat(candidate)  # lstat: presence only, NEVER dereference the .wrangler link
        except OSError:
            continue  # not present here (FileNotFoundError) or unreadable — keep walking up
        offenders.append(str(candidate))
    if offenders:
        raise DeployRefused(
            RefusalReason.ANCESTOR_WRANGLER_CONFIG,
            "A `.wrangler` config dir exists ABOVE the staged deploy cwd "
            f"({offenders}). `wrangler deploy` discovers a `.wrangler/deploy/config.json` "
            "redirect by walking UP from its cwd, so an ancestor `.wrangler` — outside both "
            "the staged copy and the live workspace — could re-point the deploy at an "
            "arbitrary config/main/account that bypasses every gate (and that `--config` may "
            "not override). Refusing to deploy with an ancestor wrangler config reachable "
            "from the deploy cwd (fail closed).",
        )


#: Heuristic: a ``[vars]`` key or ``.env`` assignment NAME that denotes a secret.
#: A secret value belongs in ``wrangler secret put`` (encrypted at the edge), NEVER
#: as a plaintext ``[vars]`` entry (public, build-time inlined) or a deployed
#: ``.env`` file.
_SECRET_NAME_RE = re.compile(
    r"(TOKEN|SECRET|PASSWORD|PASSWD|API[_-]?KEY|PRIVATE[_-]?KEY|CREDENTIAL|"
    r"ACCESS[_-]?KEY|CLIENT[_-]?SECRET|AUTH)",
    re.IGNORECASE,
)


def _assert_no_plaintext_secrets(workspace: Path) -> None:
    """Refuse a deploy whose tree carries a PLAINTEXT secret (SEC-17/SEC-18). Extends
    the ``.dev.vars`` secret-safety check (Epic I) to the rest of the deploy tree:

      * a ``.env`` file present in the deploy tree with a secret-NAMED assignment
        (``API_TOKEN=...``) — ``.env`` is not a Cloudflare secret mechanism and would
        ship plaintext;
      * a ``[vars]`` table in ``wrangler.toml`` with a secret-NAMED key bound to a
        non-empty string — ``[vars]`` are PUBLIC, build-time-inlined plaintext, so a
        secret there is published.

    Fail CLOSED (the dedicated ``PLAINTEXT_SECRET_REFUSED`` reason) so a real secret
    never reaches the public edge."""
    env_text = read_workspace_file(workspace / ".env", workspace.resolve())
    if env_text:
        for line in env_text.splitlines():
            raw = line.strip()
            if not raw or raw.startswith("#") or "=" not in raw:
                continue
            key, _, value = raw.partition("=")
            if value.strip().strip("\"'") and _SECRET_NAME_RE.search(key):
                raise DeployRefused(
                    RefusalReason.PLAINTEXT_SECRET_REFUSED,
                    "A plaintext secret was found in a deployed .env file "
                    f"({key.strip()}). Move it to `wrangler secret put` — refusing to "
                    "deploy a plaintext secret (fail closed).",
                )
    toml_text = read_workspace_file(workspace / "wrangler.toml", workspace.resolve())
    if toml_text:
        try:
            cfg = tomllib.loads(toml_text)
        except tomllib.TOMLDecodeError:
            cfg = {}
        varz = cfg.get("vars")
        if isinstance(varz, dict):
            for key, value in varz.items():
                if isinstance(value, str) and value.strip() and _SECRET_NAME_RE.search(str(key)):
                    raise DeployRefused(
                        RefusalReason.PLAINTEXT_SECRET_REFUSED,
                        f"A plaintext secret was found in wrangler.toml [vars] ({key}). "
                        "[vars] are PUBLIC build-time values; use `wrangler secret put` "
                        "instead — refusing to deploy a plaintext secret (fail closed).",
                    )


#: SEC-17: credential-bearing FILE shapes that must NEVER ship in a deploy tree — a
#: deployable-file DENYLIST (the conservative complement of an allowlist: it refuses the
#: high-confidence dangerous types without false-positiving on the open-ended set of
#: legitimate built web-asset types). Matched on the file NAME (basename), case-insensitive.
_DENY_DEPLOY_FILE_RE = re.compile(
    r"(?:^|[._-])(?:"
    # ``.env`` + ANY suffix variant — a STEM match, not a fixed suffix set: ``.env``,
    # ``.env.local``, ``.env.production.local``, ``.env.prod-1`` (digits/hyphens), … The
    # app reads secrets from CF bindings, so NO dotenv file ever belongs in the deploy
    # tree; a missed variant (``.env.production.local``) would otherwise ship as a PUBLIC
    # static asset (SEC-17 .env.production.local public-asset leak).
    r"env(?:\..*)?"
    # ``.dev.vars`` + ANY suffix (``.dev.vars.production``, ``.dev.vars.staging``, …) —
    # the real local secret, never deployed.
    r"|dev\.vars(?:\..*)?"
    r"|npmrc|netrc"  # registry / machine creds
    r"|id_rsa|id_dsa|id_ecdsa|id_ed25519"  # SSH private keys
    r")$|\.(?:pem|key|p12|pfx|ppk|jks|keystore|asc|gpg|pgp)$",
    re.IGNORECASE,
)

#: Conventional NON-secret placeholder/template suffixes: a file whose basename ENDS in
#: one of these (``.dev.vars.example``, ``.env.sample``, ``.env.dist``, …) is a committed
#: TEMPLATE with no real values — the canonical generated app ships a ROOT
#: ``.dev.vars.example`` — and is EXEMPT from the credential-file denylist. Real-value
#: variants (``.env.production.local``, ``.dev.vars.production``) carry no such marker and
#: stay denied. (Only relaxes the ``.env``/``.dev.vars`` stem branch: ``.pem``/``id_rsa``/…
#: match only when they are the FINAL segment, so a ``*.example`` name never reaches them.)
#:
#: SEC-17 (P1): this exemption is SCOPED — :func:`_assert_no_secret_shaped_files` honours it
#: ONLY OUTSIDE the served ``[assets].directory`` AND only after content-scanning the file
#: for a real secret value. A credential-named template INSIDE the served (PUBLIC) tree, or
#: ANY template carrying a real ``sk-…``/``cfut_…`` token, is still REFUSED — the suffix
#: alone never makes a credential-named public asset (or a token-bearing file) safe.
_DEPLOY_TEMPLATE_SUFFIX_RE = re.compile(
    r"\.(?:example|sample|template|tmpl|tpl|dist)$",
    re.IGNORECASE,
)

#: SEC-18: high-confidence secret-SHAPED VALUE patterns scanned across staged TEXT files
#: (beyond the ``.env`` / ``[vars]`` NAME-based check). These are provider-prefixed /
#: structurally-unmistakable tokens with ~zero false-positive rate, so a generated app's
#: ordinary content never trips them; a real key embedded in any deployed source/config
#: does. (Entropy-only heuristics are deliberately NOT used here — they would false-positive
#: on minified JS / hashes in built assets.)
_SECRET_VALUE_RES: tuple[re.Pattern[str], ...] = (
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),  # PEM private key block
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),  # AWS access key id
    re.compile(r"\bASIA[0-9A-Z]{16}\b"),  # AWS temporary access key id
    re.compile(r"\bgh[pousr]_[A-Za-z0-9]{30,}\b"),  # GitHub PAT / token
    re.compile(r"\bglpat-[A-Za-z0-9_-]{20,}\b"),  # GitLab PAT
    re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{10,}\b"),  # Slack token
    re.compile(r"\bsk-ant-[A-Za-z0-9_-]{20,}\b"),  # Anthropic key
    re.compile(r"\bsk-[A-Za-z0-9]{32,}\b"),  # OpenAI-style key
    re.compile(r"\bsk_live_[A-Za-z0-9]{20,}\b"),  # Stripe live secret key
    re.compile(r"\bcfut_[A-Za-z0-9]{20,}\b"),  # Cloudflare API token
    re.compile(r"\bAIza[0-9A-Za-z_-]{30,}\b"),  # Google API key
    re.compile(r"\beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\b"),  # JWT
)

#: File extensions whose CONTENT is scanned for secret-shaped values. Text source/config
#: types only — binary assets (images/fonts) are skipped, and a basename match in
#: :data:`_DENY_DEPLOY_FILE_RE` is refused outright before content scanning.
_SCANNED_TEXT_SUFFIXES = frozenset(
    {
        ".ts",
        ".tsx",
        ".js",
        ".jsx",
        ".mjs",
        ".cjs",
        ".json",
        ".toml",
        ".yaml",
        ".yml",
        ".env",
        ".vars",
        ".txt",
        ".md",
        ".html",
        ".css",
        ".sql",
        ".sh",
        ".cfg",
        ".ini",
    }
)
#: Cap per-file scan size so a giant minified bundle / source map cannot turn the scan
#: into a DoS (a real key sits in the first chunk of any committed config/source anyway).
_SECRET_SCAN_MAX_BYTES = 2_000_000


def _assert_no_secret_shaped_files(workspace: Path) -> None:
    """SEC-17/18: a WHOLE-TREE plaintext-secret scan of the (immutable, staged) deploy
    tree, beyond the ``.env`` / ``[vars]`` NAME check in :func:`_assert_no_plaintext_secrets`:

      * SEC-17 (deployable-file DENYLIST): REFUSE any credential-bearing file by name
        (``.pem``/``.key``/``id_rsa``/``.npmrc``/``.env*``/``.dev.vars``/…) anywhere in the
        tree — these never belong in a published app; and
      * SEC-18 (secret-SHAPED VALUE scan): REFUSE when any scanned TEXT file's CONTENT
        carries a high-confidence secret-shaped token (PEM key, AWS/GitHub/Slack/Stripe/
        Cloudflare/Anthropic/OpenAI/Google key, JWT) — a real secret pasted into source
        or config would otherwise ship plaintext to the public edge.

    SEC-17 (served-tree scope, P1): the template/placeholder exemption
    (:data:`_DEPLOY_TEMPLATE_SUFFIX_RE`) is honoured ONLY OUTSIDE the served-assets
    directory (the resolved ``[assets].directory`` — the bytes Cloudflare publishes as
    PUBLIC static assets). A credential-named file INSIDE that served tree is denied
    OUTRIGHT — EVEN with a ``.example``/``.sample``/``.template`` suffix — because a
    credential-named PUBLIC asset has no legitimate purpose; this closes the
    ``dist/secrets.env.example`` public-leak (an exempted-by-name, never-content-scanned
    template carrying a real ``sk-…``/``cfut_…`` token uploaded as a public asset). The
    legitimate exemption is the canonical app's ROOT ``.dev.vars.example`` placeholder,
    which lives OUTSIDE the served dir and CF never serves.

    SEC-18 (exempted-file content scan, belt-and-suspenders): a credential-stem template
    that survives the name denial (a ``.example``/``.sample`` OUTSIDE the served tree) is
    STILL content-scanned for a real secret VALUE — even though its ``.example`` suffix is
    not in :data:`_SCANNED_TEXT_SUFFIXES`. A genuine placeholder (``changeme``,
    ``replace-me``, ``your-token-here``) passes; a real token (``sk-…``/``cfut_…``) embedded
    in a root ``.dev.vars.example`` is caught.

    Fail CLOSED (the dedicated ``PLAINTEXT_SECRET_REFUSED`` reason, matching
    :func:`_assert_no_plaintext_secrets`). Reads route through the guarded reader.
    Runs on the STAGED copy, so the tree is already symlink/hardlink-free."""
    workspace_root = workspace.resolve()
    # The served-assets subtree (resolved the SAME way the deploy does — see
    # :func:`_deploy_asset_dir_rel`), i.e. the exact dir wrangler uploads PUBLICLY. Computed
    # lexically under *workspace* so it lines up with the (already symlink-free) staged
    # paths from :func:`_iter_tree_files`. May raise the deploy's own ASSET_DIR refusals on
    # an absolute/``..``/root assets dir — that is a deploy refusal too (fail closed).
    served_root = workspace / _deploy_asset_dir_rel(workspace)
    # Files to scan: the deploy tree (minus the globally-skipped roots) PLUS the resolved
    # served-assets tree walked DIRECTLY. The served tree is scanned UNCONDITIONALLY — even
    # if it were ever resolved UNDER a normally-skipped root (.disco/node_modules/…) that
    # :func:`_iter_tree_files` prunes — so the bytes Cloudflare PUBLISHES are ALWAYS scanned.
    # Defense in depth behind the :func:`_deploy_asset_dir_rel` skip-root refusal: even if
    # some other path resolved a served dir into a skipped area, the published bytes are
    # still scanned here. ``os.walk(followlinks=False)`` on the already-symlink-free staged
    # tree; a set dedupes the overlap (the common case where served_root is a normal subdir).
    seen: set[Path] = set()
    scan_paths: list[Path] = []
    for path in _iter_tree_files(workspace):
        if path not in seen:
            seen.add(path)
            scan_paths.append(path)
    if served_root.is_dir():
        for sroot, _sdirs, snames in os.walk(served_root, followlinks=False):
            for sname in snames:
                p = Path(sroot) / sname
                if p not in seen:
                    seen.add(p)
                    scan_paths.append(p)
    for path in sorted(scan_paths):
        name = path.name
        is_credential_name = _DENY_DEPLOY_FILE_RE.search(name) is not None
        if is_credential_name:
            in_served_tree = path == served_root or path.is_relative_to(served_root)
            # Inside the served (PUBLIC) tree a credential name is denied OUTRIGHT — a
            # template suffix does NOT exempt it. Outside it, only a NON-template
            # (real-value) credential file is denied; a template placeholder falls through
            # to the content scan below.
            if in_served_tree or not _DEPLOY_TEMPLATE_SUFFIX_RE.search(name):
                where = (
                    "the served assets directory (it would publish as a PUBLIC asset)"
                    if in_served_tree
                    else "the deploy tree"
                )
                raise DeployRefused(
                    RefusalReason.PLAINTEXT_SECRET_REFUSED,
                    f"A credential-bearing file is present in {where}: {name}. "
                    "Such files (private keys, .env, .npmrc, …) must never be published — "
                    "a .example/.sample/.template suffix does NOT exempt a credential-named "
                    "file inside the served assets dir. Refusing to deploy (fail closed).",
                )
        # SEC-18 content scan: recognised text suffixes PLUS any credential-stem file that
        # survived the name denial (a template-exempted ``.example``/``.sample`` whose suffix
        # is not in the text-suffix set) — so even an exempted template is refused if it
        # carries a REAL secret value (a genuine ``changeme`` placeholder still passes).
        if path.suffix.lower() not in _SCANNED_TEXT_SUFFIXES and not is_credential_name:
            continue
        try:
            if path.stat().st_size > _SECRET_SCAN_MAX_BYTES:
                continue
        except OSError:
            continue
        text = read_workspace_file(path, workspace_root)
        if not text:
            continue
        for pat in _SECRET_VALUE_RES:
            if pat.search(text):
                rel = path.relative_to(workspace_root).as_posix()
                raise DeployRefused(
                    RefusalReason.PLAINTEXT_SECRET_REFUSED,
                    f"A high-confidence plaintext secret was found in a deployed file "
                    f"({rel}). Move it to `wrangler secret put` — refusing to publish a "
                    "plaintext secret to the public edge (fail closed).",
                )


#: SEC-5: the ONLY top-level wrangler.toml keys an AppKit deploy permits. Anything
#: else broadens the blast radius and is REFUSED before deploy: ``routes``/``route``
#: (custom domains / zone routes), ``build`` (a build hook running arbitrary code on
#: the host at deploy), ``triggers`` (cron), ``vars`` (public plaintext — secrets must
#: use ``wrangler secret put``), and every extra binding (``kv_namespaces``,
#: ``r2_buckets``, ``queues``, ``services``, ``durable_objects``, ``hyperdrive``, …).
#:
#: ``account_id`` is DELIBERATELY EXCLUDED (config-source bypass, P1): wrangler resolves
#: the CONFIG ``account_id`` when present, so a planted ``account_id =
#: "other-account-the-token-can-access"`` would target a DIFFERENT account than the owner
#: confirmed. The account comes ONLY from the confirmed SecretStore (injected as the
#: ``CLOUDFLARE_ACCOUNT_ID`` env at execute time) — never a config override. AppKit's
#: generated wrangler.toml carries no ``account_id``, so any is an injection → refuse.
_ALLOWED_WRANGLER_KEYS = frozenset(
    {
        "name",
        "main",
        "compatibility_date",
        "compatibility_flags",
        "assets",
        "run_worker_first",
        "d1_databases",
        "workers_dev",
        "observability",
        "minify",
        "no_bundle",
    }
)


def _assert_wrangler_allowlisted(workspace: Path) -> None:
    """SEC-5: parse wrangler.toml and ENFORCE the top-level-key allowlist before deploy.
    A key outside :data:`_ALLOWED_WRANGLER_KEYS` (``routes``/custom domains/``build``
    hooks/cron ``triggers``/``vars``/extra bindings/``account_id``) would let a generated
    app broaden its own blast radius past the lead-capture contract — or, for
    ``account_id``, redirect the deploy to a DIFFERENT account than the owner confirmed —
    so REFUSE (fail closed). A malformed TOML is left to the export-readiness gate (which
    fails closed on it)."""
    text = read_workspace_file(workspace / "wrangler.toml", workspace.resolve())
    if not text:
        return
    try:
        cfg = tomllib.loads(text)
    except tomllib.TOMLDecodeError:
        return
    extra = sorted(k for k in cfg if k not in _ALLOWED_WRANGLER_KEYS)
    if extra:
        raise DeployRefused(
            RefusalReason.WRANGLER_CONFIG_REJECTED,
            "wrangler.toml has unexpected top-level keys that broaden the deploy blast "
            f"radius: {extra}. Custom routes/domains, build hooks, cron triggers, "
            "[vars], and extra bindings are not allowed — refusing to deploy "
            "(fail closed).",
        )
    # SEC-1-class: ``main`` is allowlisted as a KEY above, but its VALUE decides which
    # file ``wrangler deploy`` runs as the Worker. The canonical-worker match gate
    # (_assert_worker_is_canonical) validates ``worker/index.ts`` — so ``main`` MUST name
    # EXACTLY that file, else a look-alike (``....worker/index.ts``), an absolute path, or
    # a ``..`` escape would deploy a DIFFERENT, unchecked Worker (and dodge the SEC-10
    # ownership preflight). Exact-match (no lstrip) — fail closed.
    _assert_main_is_canonical(cfg)


def _assert_main_is_canonical(cfg: dict[str, object]) -> None:
    """SEC-1-class: REQUIRE wrangler.toml ``main`` to EXACTLY name the canonical worker
    entry (``worker/index.ts``) — else :class:`DeployRefused` (``MAIN_NOT_CANONICAL``,
    fail closed). ``wrangler deploy`` runs whatever ``main`` points at, while the
    canonical-worker match gate validates ``worker/index.ts``; this ties the two so the
    file we canonical-check IS the file wrangler runs. Reuses the SINGLE pure matcher
    (:func:`main_points_at_worker_entry`) shared with the export-readiness gate (no
    ``lstrip`` — ``"....worker/index.ts"`` / absolute / ``..`` all refuse)."""
    main = cfg.get("main")
    if not main_points_at_worker_entry(main):
        raise DeployRefused(
            RefusalReason.MAIN_NOT_CANONICAL,
            "wrangler.toml `main` does not EXACTLY name the canonical worker entry "
            f'(main = "worker/index.ts"); got {main!r}. `wrangler deploy` runs whatever '
            "`main` points at, but the canonical-worker gate validates worker/index.ts — a "
            "non-canonical main (absolute path, '..' escape, or a look-alike dir like "
            "'....worker/index.ts') would deploy a DIFFERENT, unchecked Worker. Refusing "
            "(fail closed).",
        )


#: SEC-11/CORR-9: a statement at the start of a schema.sql statement that would MUTATE
#: or destroy data on an ADOPTED (pre-existing, owner-owned) D1 database. Only idempotent
#: ``CREATE`` DDL is allowed against an adopted DB.
_DESTRUCTIVE_SQL_RE = re.compile(
    r"^(DROP|DELETE|TRUNCATE|ALTER|UPDATE|INSERT|REPLACE|PRAGMA|ATTACH|DETACH|VACUUM)\b",
    re.IGNORECASE,
)


def _assert_schema_safe_for_adopt(workspace: Path) -> None:
    """SEC-11/CORR-9: before applying ``schema.sql`` to an ADOPTED D1 database, validate
    that EVERY statement is idempotent ``CREATE`` DDL (``CREATE TABLE IF NOT EXISTS`` /
    ``CREATE INDEX`` / ``CREATE VIEW`` / ``CREATE TRIGGER``). A destructive or
    data-mutating statement (DROP/DELETE/ALTER/UPDATE/INSERT/…) against an owner's
    existing database is REFUSED (fail closed) — a fresh ``d1 create`` is unaffected
    (empty DB), but adoption must never run owner-data-destroying SQL."""
    text = read_workspace_file(workspace / "schema.sql", workspace.resolve()) or ""
    cleaned = "\n".join(ln for ln in text.splitlines() if not ln.strip().startswith("--"))
    for stmt in cleaned.split(";"):
        s = stmt.strip()
        if not s:
            continue
        if _DESTRUCTIVE_SQL_RE.match(s):
            raise DeployRefused(
                RefusalReason.SCHEMA_UNSAFE,
                "schema.sql contains a destructive/data-mutating statement that would "
                f"run against the ADOPTED D1 database: {s.splitlines()[0][:80]!r}. "
                "Refusing — an adopted DB accepts only idempotent CREATE DDL "
                "(fail closed).",
            )
        if not re.match(r"^CREATE\b", s, re.IGNORECASE):
            raise DeployRefused(
                RefusalReason.SCHEMA_UNSAFE,
                "schema.sql contains a non-CREATE statement that would run against the "
                f"ADOPTED D1 database: {s.splitlines()[0][:80]!r}. Refusing (fail "
                "closed).",
            )


# ---- SEC-10-B: server-controlled, SIGNED ownership records -------------------

#: Reserved SecretStore slot for the server-side HMAC key that SIGNS deploy/ownership
#: records (SEC-10-B). The key lives in the ENCRYPTED SecretStore — which is stored
#: OUTSIDE the untrusted workspace (``$XDG_CONFIG_HOME/disco`` / ``DISCO_SECRETS``) and is
#: unreadable by the sandboxed build agent — so a record's ownership claim cannot be
#: FORGED by planting a JSON file in the digest-SKIPPED ``.disco`` dir. A planted /
#: unsigned / foreign-keyed record fails signature verification and authorizes NOTHING;
#: only a record THIS server actually wrote (after a real successful mutation) carries a
#: valid signature.
_OWNERSHIP_HMAC_KEY_SECRET = "disco.appkit.ownership_hmac_key"  # noqa: S105 - a key NAME, not a secret


def _ownership_signing_key(store: SecretStore, *, create: bool) -> bytes | None:
    """The server-controlled HMAC key that signs/verifies deploy-ownership records
    (SEC-10-B). Persisted (encrypted) in the SecretStore, which lives OUTSIDE the
    workspace, so the untrusted build agent can neither READ it (to forge a signature)
    nor write it. Returns the raw key bytes, minting + persisting a fresh random key on
    first use when ``create`` is True (the WRITE path, where a real record is being
    signed). On the verify path (``create=False``) a missing key means no record can be
    trusted → returns None and every ownership check fails closed."""
    try:
        existing = store.get_secret(_OWNERSHIP_HMAC_KEY_SECRET)
    except WeakSecretError:
        return None
    if existing:
        try:
            return base64.urlsafe_b64decode(existing.encode("ascii"))
        except (ValueError, UnicodeEncodeError):
            return None
    if not (create and store.can_store):
        return None
    key = _secrets.token_bytes(32)
    store.set_secret(_OWNERSHIP_HMAC_KEY_SECRET, base64.urlsafe_b64encode(key).decode("ascii"))
    return key


def _ownership_payload(
    *, account_id: str | None, worker_name: str, db_name: str, mutations: list[str]
) -> bytes:
    """The EXACT bytes the ownership signature covers: the account, BOTH target resource
    names, and the mutating legs that completed — so a valid signature proves a SPECIFIC
    (account, worker, db) tuple reached a real prior successful mutation and cannot be
    replayed against a different account/resource. ``mutations`` is sorted so the
    signature is order-independent."""
    return json.dumps(
        {
            "v": 1,
            "account_id": account_id or "",
            "worker_name": worker_name,
            "db_name": db_name,
            "mutations": sorted(mutations),
        },
        sort_keys=True,
    ).encode("utf-8")


def _ownership_signature(
    store: SecretStore,
    *,
    account_id: str | None,
    worker_name: str,
    db_name: str,
    mutations: list[str],
    create: bool,
) -> str | None:
    """HMAC-SHA256 over :func:`_ownership_payload`, keyed by the server-side signing key.
    Returns None when no signing key is available (the record is then written UNSIGNED and
    will authorize nothing on a later deploy — fail closed)."""
    key = _ownership_signing_key(store, create=create)
    if key is None:
        return None
    return hmac.new(
        key,
        _ownership_payload(
            account_id=account_id,
            worker_name=worker_name,
            db_name=db_name,
            mutations=mutations,
        ),
        hashlib.sha256,
    ).hexdigest()


def _has_ownership_record(
    workspace: Path, plan: DeployPlan, store: SecretStore, *, kind: Literal["worker", "d1"]
) -> bool:
    """SEC-10 / SEC-10-B: True iff a TRUSTWORTHY prior Disco deploy record under this
    workspace proves we own the target *kind* (``worker`` or ``d1``) — i.e. a previous
    deploy of THIS app actually created/published the SAME resource on the SAME account,
    and the record carries a VALID server-side HMAC signature.

    The record dir (``.disco``) is workspace-local and digest-SKIPPED, so the untrusted
    build agent can PLANT a JSON file there. A planted / unsigned / forged record is
    REJECTED (SEC-10-B): the signature is recomputed (with the server-controlled key the
    agent cannot read) over the record's OWN (account_id, worker_name, db_name, mutations)
    and must match via a constant-time compare. Then the record must additionally (a) bind
    the SAME account as this deploy, (b) name the SAME resource, and (c) reflect a real
    prior successful MUTATION of that resource — ``worker_deploy`` for a Worker, a
    ``d1_create``/``d1_create:<name>`` leg for a D1. Without ALL of that, a name collision
    is treated as an
    UNRELATED resource and adoption/overwrite is refused (never clobber someone else's).
    Reads route through the guarded reader so a planted escaping symlink fails closed."""
    key = _ownership_signing_key(store, create=False)
    if key is None:
        # No server signing key has ever been established → no record can be trusted.
        return False
    rec_dir = workspace / _DEPLOY_RECORD_DIR
    if not rec_dir.is_dir():
        return False
    workspace_root = workspace.resolve()
    try:
        children = sorted(rec_dir.iterdir())
    except OSError:
        return False
    want_name = plan.worker_name if kind == "worker" else plan.db_name

    def _proves_mutation(muts: list[str]) -> bool:
        # The real deploy records a Worker publish as ``worker_deploy`` and a D1 creation
        # as ``d1_create:<db_name>`` (name-suffixed). A D1 we CREATED is a D1 we own.
        if kind == "worker":
            return "worker_deploy" in muts
        return any(m == "d1_create" or m.startswith("d1_create:") for m in muts)

    for child in children:
        if child.suffix != ".json":
            continue
        text = read_workspace_file(child, workspace_root)
        if not text:
            continue
        try:
            data = json.loads(text)
        except ValueError:
            continue
        if not isinstance(data, dict):
            continue
        sig = data.get("signature")
        mutations = data.get("mutations")
        rec_account = data.get("account_id")
        rec_worker = data.get("worker_name")
        rec_db = data.get("db_name")
        if not (isinstance(sig, str) and sig and isinstance(mutations, list)):
            continue
        if not (isinstance(rec_worker, str) and isinstance(rec_db, str)):
            continue
        str_mutations = [m for m in mutations if isinstance(m, str)]
        expected = hmac.new(
            key,
            _ownership_payload(
                account_id=rec_account if isinstance(rec_account, str) else None,
                worker_name=rec_worker,
                db_name=rec_db,
                mutations=str_mutations,
            ),
            hashlib.sha256,
        ).hexdigest()
        # SEC-10-B: a planted/forged/unsigned-by-us record fails here — only a record this
        # server actually signed survives the constant-time compare.
        if not hmac.compare_digest(sig, expected):
            continue
        # The signature is authentic; now the binding must match THIS deploy: same account,
        # same resource name, and the record must prove a REAL prior successful mutation.
        if rec_account != plan.account_id:
            continue
        rec_kind_name = rec_worker if kind == "worker" else rec_db
        if rec_kind_name != want_name:
            continue
        if not _proves_mutation(str_mutations):
            continue
        return True
    return False


def _admin_token_strong(token: str) -> bool:
    """SEC-4: a deployed admin endpoint is only as safe as its ADMIN_TOKEN. Require a
    HIGH-ENTROPY value: at least 16 characters AND at least 8 distinct characters (so a
    short or low-variety owner-supplied token is rejected). The Worker is expected to
    additionally RATE-LIMIT admin auth attempts (documented in the OWNER guide); a strong
    token plus rate-limiting defeats online guessing."""
    t = token.strip()
    return len(t) >= 16 and len(set(t)) >= 8


# ---- SEC-4: worker AUTH-semantics verification (pre-deploy, fail closed) ------

#: The generated Worker entry point — the file ``wrangler deploy`` bundles + publishes.
_WORKER_RELPATH = "worker/index.ts"

#: SEC-4 token-exfiltration scan: ``env.ADMIN_TOKEN`` must NEVER flow into a response
#: body / serialization SINK. Matches a sink opener (``new Response(`` / ``json(`` /
#: ``JSON.stringify(`` / a template-literal ``${…}`` interpolation) followed — within the
#: same expression (not crossing a ``;``) — by a direct ``env.ADMIN_TOKEN`` read. The
#: canonical Worker references ``env.ADMIN_TOKEN`` only as ``const expected =
#: env.ADMIN_TOKEN`` (no sink), so this catches a Worker that echoes the admin token to
#: the public edge while leaving the legitimate auth read untouched.
_ADMIN_TOKEN_LEAK_RE = re.compile(
    r"(?:new\s+Response\s*\(|(?<![\w.])json\s*\(|JSON\.stringify\s*\(|\$\{)"
    r"[^;]{0,400}?\benv\.ADMIN_TOKEN\b",
    re.DOTALL,
)


def _strip_ts_comments_lite(src: str) -> str:
    """Drop ``/* … */`` block + ``// …`` line comments so the token-leak scan never
    fires on (or is fooled by) commented-out code. Intentionally simple — it may
    over-strip a ``//`` that lives inside a string literal, which only makes the leak
    scan MORE conservative (it can never hide a real sink that way)."""
    src = re.sub(r"/\*.*?\*/", " ", src, flags=re.DOTALL)
    return re.sub(r"//[^\n]*", " ", src)


def _assert_worker_auth_verified(workspace: Path) -> None:
    """SEC-4: REQUIRE a positive verification that the staged ``worker/index.ts``
    enforces the admin-token gate BEFORE any real Cloudflare mutation — else
    :class:`DeployRefused` (``WORKER_AUTH_UNVERIFIED``, fail closed). A buggy/malicious
    generated Worker could otherwise ignore ``/admin`` auth or exfiltrate
    ``env.ADMIN_TOKEN``, and the export-readiness gate (GATE 1) does NOT inspect the
    Worker's auth STRUCTURE.

    The verdict reuses the EPIC G ``worker_contract`` static inspector
    (:func:`disco.tools.builtin.verify_appkit_app.inspect_worker`) — the SAME
    battle-tested route-handler/brace-aware parser ``verify_appkit_app`` runs — so the
    deploy can never drift from the verifier. It requires BOTH:

      * ``reads_require_auth`` — GET ``/api/leads`` AND ``/admin`` early-return 401 via the
        auth guard before any DB read, and ``isAuthorized`` actually validates the
        ``Authorization: Bearer`` token against ``env.ADMIN_TOKEN``; and
      * ``fail_closed_without_token`` — the guard denies (``return false``) when
        ADMIN_TOKEN is unset (never defaults open).

    PLUS a focused token-exfiltration scan (:data:`_ADMIN_TOKEN_LEAK_RE`): the Worker must
    never echo ``env.ADMIN_TOKEN`` into a response/serialization sink. The inspector is
    imported FUNCTION-LOCALLY (it pulls the tools layer) so a stale/broken verifier import
    fails the gate CLOSED rather than crashing the deploy."""
    worker_ts = read_workspace_file(workspace / _WORKER_RELPATH, workspace.resolve())
    if not (worker_ts and worker_ts.strip()):
        raise DeployRefused(
            RefusalReason.WORKER_AUTH_UNVERIFIED,
            "No worker/index.ts in the staged deploy tree — cannot verify the admin-token "
            "auth gate. Refusing to deploy an unverified Worker (fail closed).",
        )
    # Security primitives use their authoritative trusted-tree verifiers rather
    # than the legacy lead-specific inspector. The checks repeat post-build.
    if _assert_security_primitive_trusted_trees(workspace):
        return
    try:
        from disco.core.appkit.spec import Entity
        from disco.tools.builtin.verify_appkit_app import inspect_worker
    except ImportError as exc:  # pragma: no cover - defensive; fail CLOSED
        raise DeployRefused(
            RefusalReason.WORKER_AUTH_UNVERIFIED,
            f"The worker-auth verifier could not be loaded ({exc}); refusing to deploy "
            "without a positive auth verdict (fail closed).",
        ) from exc
    # The auth verdict (reads_require_auth / fail_closed_without_token) does not depend on
    # the lead entity — a minimal stand-in is sufficient to drive the static inspection.
    _, auth_model, reasons = inspect_worker(worker_ts, Entity(id="lead", name="Lead"))
    # SEC-4: require the FULL non-bypassable verdict. Beyond the two canonical routes
    # being guarded + fail-closed (reads_require_auth / fail_closed_without_token), the
    # taint/alias-aware checks must pass: env.ADMIN_TOKEN never reaches a leak sink
    # outside isAuthorized (``admin_token_safe`` — supersedes the prior regex scan, which
    # an alias ``const x = env.ADMIN_TOKEN`` bypassed), and EVERY lead-read route is
    # auth-guarded, not just the two canonical ones (``all_lead_reads_guarded`` — closes
    # an extra unauthenticated ``/debug-leads``).
    if not (
        auth_model.reads_require_auth
        and auth_model.fail_closed_without_token
        and auth_model.admin_token_safe
        and auth_model.all_lead_reads_guarded
    ):
        auth_gaps = [
            r
            for r in reasons
            if any(
                k in r
                for k in (
                    "401",
                    "ADMIN_TOKEN",
                    "Bearer",
                    "auth",
                    "guard",
                    "leak",
                    "alias",
                    "debug",
                    "lead",
                )
            )
        ] or reasons
        raise DeployRefused(
            RefusalReason.WORKER_AUTH_UNVERIFIED,
            "The staged worker/index.ts does not provably enforce the admin-token gate on "
            "ALL protected + lead-read routes (fail-closed, with no env.ADMIN_TOKEN leak or "
            f"alias): {'; '.join(auth_gaps)}. Refusing to deploy (fail closed).",
        )


def _normalize_worker_source(src: str) -> str:
    """Normalize a ``worker/index.ts`` for the canonical-match comparison: collapse
    line endings (CRLF/CR → LF), strip per-line TRAILING whitespace, and drop trailing
    blank lines. This tolerates ONLY a benign editor/transport reformat (re-saved with
    CRLF, a trailing newline trimmed); ANY material code change — an added route, a
    leaked-token sink, a reordered statement, a changed identifier — still mismatches.
    Intentionally does NOT normalize interior whitespace/indentation, so a structural
    edit can never be normalized away."""
    lines = src.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    return "\n".join(line.rstrip() for line in lines).rstrip("\n")


def _load_guarded_app_spec(staged: Path) -> AppSpec:
    """Load the frozen staged AppSpec through the deploy path's guarded reader."""
    spec_text = read_workspace_file(staged / _APPSPEC_RELPATH, staged.resolve())
    if not (spec_text and spec_text.strip()):
        raise DeployRefused(
            RefusalReason.WORKER_NOT_CANONICAL,
            "No .disco/appspec.json in the staged deploy tree — cannot validate "
            "the generated Worker or its deployment bindings (fail closed).",
        )
    try:
        return load_app_spec_from_bytes(spec_text)
    except Exception as exc:
        raise DeployRefused(
            RefusalReason.WORKER_NOT_CANONICAL,
            f"Could not validate the staged app spec ({exc}); refusing to deploy an "
            "unverifiable Worker (fail closed).",
        ) from exc


def _guarded_app_is_stripe(staged: Path) -> bool:
    """Best-effort dispatch hint; airtight spec validation still runs later."""
    spec_text = read_workspace_file(staged / _APPSPEC_RELPATH, staged.resolve())
    if not (spec_text and spec_text.strip()):
        return False
    try:
        return load_app_spec_from_bytes(spec_text).stripe is not None
    except Exception:
        return False


def _guarded_app_spec_hint(staged: Path) -> AppSpec | None:
    """Best-effort dispatch only; each selected verifier reloads fail-closed."""
    spec_text = read_workspace_file(staged / _APPSPEC_RELPATH, staged.resolve())
    if not (spec_text and spec_text.strip()):
        return None
    try:
        return load_app_spec_from_bytes(spec_text)
    except Exception:
        return None


def _assert_security_primitive_trusted_trees(staged: Path) -> bool:
    app = _guarded_app_spec_hint(staged)
    if app is None:
        return False
    selected = False
    if app.stripe is not None:
        _assert_stripe_trusted_tree(staged)
        selected = True
    if app.webhooks is not None:
        _assert_webhook_trusted_tree(staged)
        selected = True
    return selected


def _load_guarded_design_spec(staged: Path) -> DesignSpec:
    raw = read_workspace_file(staged / ".disco/designspec.json", staged.resolve())
    if not (raw and raw.strip()):
        raise DeployRefused(
            RefusalReason.STRIPE_TRUSTED_TREE,
            "Stripe deployment requires .disco/designspec.json for trusted reconstruction.",
        )
    try:
        return load_design_spec_from_bytes(raw)
    except Exception as exc:
        raise DeployRefused(
            RefusalReason.STRIPE_TRUSTED_TREE,
            f"Could not validate the staged Stripe design spec ({exc}).",
        ) from exc


def _assert_stripe_trusted_tree(staged: Path) -> None:
    """Run the exact core Stripe verifier over its on-disk trusted path set."""
    app = _load_guarded_app_spec(staged)
    if app.stripe is None:
        return
    design = _load_guarded_design_spec(staged)
    try:
        from disco.core.appkit.generator import generate

        paths = set(generate(app, design))
    except Exception as exc:
        raise DeployRefused(
            RefusalReason.STRIPE_TRUSTED_TREE,
            f"Could not reconstruct the trusted Stripe deploy tree ({exc}).",
        ) from exc
    paths.update({".disco/appspec.json", ".disco/primitives/stripe.json"})
    tree: dict[str, str] = {}
    for relpath in sorted(paths):
        text = read_workspace_file(staged / relpath, staged.resolve())
        if text is not None:
            tree[relpath] = text
    result = stripe_verify(app, design, tree)
    if result.ok:
        return
    failures = [f"{check.name}: {check.evidence}" for check in result.checks if not check.passed]
    raise DeployRefused(
        RefusalReason.STRIPE_TRUSTED_TREE,
        "Stripe trusted-tree verification failed: " + "; ".join(failures),
    )


def _assert_webhook_trusted_tree(staged: Path) -> None:
    """Run the core webhook verifier over its exact on-disk trusted projection."""
    app = _load_guarded_app_spec(staged)
    if app.webhooks is None:
        return
    raw_design = read_workspace_file(staged / ".disco/designspec.json", staged.resolve())
    if not (raw_design and raw_design.strip()):
        raise DeployRefused(
            RefusalReason.WEBHOOK_TRUSTED_TREE,
            "Webhook deployment requires .disco/designspec.json for trusted reconstruction.",
        )
    try:
        design = load_design_spec_from_bytes(raw_design)
        from disco.core.appkit.generator import generate

        paths = set(generate(app, design))
    except Exception as exc:
        raise DeployRefused(
            RefusalReason.WEBHOOK_TRUSTED_TREE,
            f"Could not reconstruct the trusted webhook deploy tree ({exc}).",
        ) from exc
    paths.update({".disco/appspec.json", ".disco/primitives/webhook.json"})
    tree: dict[str, str] = {}
    for relpath in sorted(paths):
        text = read_workspace_file(staged / relpath, staged.resolve())
        if text is not None:
            tree[relpath] = text
    result = webhook_verify(app, design, tree)
    if result.ok:
        return
    failures = [f"{check.name}: {check.evidence}" for check in result.checks if not check.passed]
    raise DeployRefused(
        RefusalReason.WEBHOOK_TRUSTED_TREE,
        "Webhook trusted-tree verification failed: " + "; ".join(failures),
    )


def _assert_worker_is_canonical(staged: Path) -> None:
    """SEC-4 / BUILD-1: REQUIRE the staged ``worker/index.ts`` to BE the canonical Worker
    the AppKit generator deterministically emits from this app's spec — else
    :class:`DeployRefused` (``WORKER_NOT_CANONICAL``, fail closed).

    This is the ROBUST architectural close to the SEC-4 heuristic-taint arms race. Rather
    than try to PROVE arbitrary, custom-authored Worker TypeScript is auth-safe (a losing
    game — every taint/route heuristic can be reworded around: a copy-of-alias token leak,
    a ``url.pathname.startsWith("/debug-leads")`` unauthenticated lead read, switch/regex
    dispatch), we REQUIRE the deployed Worker to equal the ONE Worker the generator
    produces. The generated ``worker/index.ts`` is reconstructed by running the SAME
    registered primitive generator that writes that path for the staged AppSpec. It is
    NAMESPACE-INDEPENDENT (the SEC-30 namespace only renames the Worker/D1 in
    wrangler.toml, never the Worker source). So there is no arbitrary Worker to analyse:
    a leak sink or an extra unauthenticated route is, by construction, NOT the canonical
    Worker and is refused.

    Runs on the POST-BUILD staged tree (after the sandboxed build + dist sync-back, BEFORE
    any Cloudflare mutation), which also closes the BUILD-1 TOCTOU: the pre-build auth gate
    judged the AUTHORED Worker, but the build could EMIT or rewrite ``worker/index.ts`` and
    the sync-back would replace it. The build compiles the FRONTEND into ``dist`` only — it
    must NEVER touch the Worker source — so any post-build deviation (a malicious emit, or
    even a cosmetic rewrite beyond trailing-whitespace/line-ending) → mismatch → refuse.

    The spec is read from the IMMUTABLE STAGED tree (``.disco/appspec.json``, frozen at
    stage time via the guarded reader) through the airtight ``load_app_spec_from_bytes``
    loader (size-capped, schema-validated). A missing/unloadable spec, or a generator that
    can't be imported, fails CLOSED — an unverifiable Worker is never deployed. The
    generator is imported FUNCTION-LOCALLY (it pulls the core layer) so a stale/broken
    import refuses rather than crashing the deploy."""
    workspace_root = staged.resolve()
    try:
        from disco.core.appkit.generator import generate
        from disco.core.appkit.spec import (
            DesignSpec,
            Palette,
            Typography,
        )
    except ImportError as exc:  # pragma: no cover - defensive; fail CLOSED
        raise DeployRefused(
            RefusalReason.WORKER_NOT_CANONICAL,
            f"The AppKit generator could not be loaded ({exc}); refusing to deploy a "
            "Worker we cannot prove canonical (fail closed).",
        ) from exc
    try:
        app_spec = _load_guarded_app_spec(staged)
        # The worker generators do not read design decisions; pass a fixed internal
        # DesignSpec so reconstruction has no staged input beyond the frozen AppSpec.
        canonical_design = DesignSpec(
            schema_version=1,
            typography=Typography(heading_font="Inter", body_font="Inter"),
            palette=Palette(
                primary="#111111",
                surface="#ffffff",
                text="#111111",
                accent="#2563eb",
            ),
            layout_family="canonical",
            component_style="plain",
            density="comfortable",
        )
        canonical_worker = generate(app_spec, canonical_design)["worker/index.ts"]
    except Exception as exc:
        # Malformed/oversize/schema-invalid spec, an unloadable primitive, or a
        # generator failure: we cannot derive the canonical Worker, so we cannot
        # trust the deployed one.
        raise DeployRefused(
            RefusalReason.WORKER_NOT_CANONICAL,
            f"Could not regenerate the canonical Worker from the staged app spec ({exc}); "
            "refusing to deploy an unverifiable Worker (fail closed).",
        ) from exc
    staged_worker = read_workspace_file(staged / _WORKER_RELPATH, workspace_root)
    if not (staged_worker and staged_worker.strip()):
        raise DeployRefused(
            RefusalReason.WORKER_NOT_CANONICAL,
            "No worker/index.ts in the staged deploy tree — refusing to deploy (the "
            "deployed Worker must BE the canonical generated Worker; fail closed).",
        )
    if _normalize_worker_source(staged_worker) != _normalize_worker_source(canonical_worker):
        raise DeployRefused(
            RefusalReason.WORKER_NOT_CANONICAL,
            "The staged worker/index.ts is NOT the canonical Worker the AppKit generator "
            "emits from this app's spec. The deployed Worker must be the pristine generated "
            "one (the build compiles the frontend into ./dist; it must never author or "
            "rewrite the Worker source). Refusing to deploy a non-canonical / custom Worker "
            "(fail closed) — closes the SEC-4 auth-bypass + BUILD-1 post-build-emit vectors.",
        )
    # SEC-1-class: we just proved the STAGED worker/index.ts is canonical — but
    # ``wrangler deploy`` runs whatever the staged wrangler.toml ``main`` points at. TIE
    # them: ``main`` MUST EXACTLY name worker/index.ts, else a look-alike main
    # (``....worker/index.ts``), an absolute path, or a ``..`` escape would deploy a
    # DIFFERENT, UNCHECKED entrypoint while this gate validated the pristine worker
    # (also dodging the SEC-10 ownership preflight). The check uses the immutable STAGED
    # wrangler.toml (TOCTOU-proof) and the SAME pure matcher as export-readiness.
    staged_toml = read_workspace_file(staged / "wrangler.toml", workspace_root)
    try:
        staged_cfg = tomllib.loads(staged_toml) if staged_toml else {}
    except tomllib.TOMLDecodeError:
        staged_cfg = {}
    _assert_main_is_canonical(staged_cfg)


def _parse_targets(wrangler_toml: str) -> tuple[str, str]:
    """Extract the (worker_name, d1_database_name) from wrangler.toml — the exact
    targets the deploy mutates. Both already validated non-empty by
    ``cloudflare_export_ready`` (which gates the plan)."""
    try:
        cfg = tomllib.loads(wrangler_toml)
    except tomllib.TOMLDecodeError:
        # CORR-6: malformed wrangler.toml must NOT raise an uncaught 500 out of the
        # side-effect-free plan build. Return empty targets — cloudflare_export_ready
        # (which also TOML-parses and fails closed) makes the plan not-ready, so the
        # executor refuses at GATE 1 (EXPORT_NOT_READY) rather than crashing.
        return ("", "")
    worker = cfg.get("name")
    d1 = cfg.get("d1_databases")
    db_name = ""
    if isinstance(d1, list):
        for entry in d1:
            if isinstance(entry, dict) and entry.get("binding") == "DB":
                db_name = str(entry.get("database_name") or "")
                break
    return (str(worker or ""), db_name)


# ---- SEC-9: Cloudflare-safe resource-name validation -------------------------

#: A Cloudflare-safe Worker / D1 name: starts with an alphanumeric, then
#: alphanumerics / hyphens / underscores, at most 63 chars total. Cloudflare rejects
#: names outside this shape (and a leading hyphen / odd punctuation could be parsed as
#: a flag or collide); validate BEFORE plan confirmation so a bad name never reaches a
#: wrangler invocation.
_CF_NAME_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9_-]{0,62}$")


def _validate_resource_names(worker: str, db_name: str) -> None:
    """SEC-9: reject a ``worker_name`` / ``db_name`` that is not a Cloudflare-safe
    identifier (charset + length). EMPTY names are left to the export-readiness gate
    (an incomplete export has no targets yet and must surface as EXPORT_NOT_READY, not
    an invalid-name refusal), so this only judges NON-empty names — a non-empty name
    that fails the regex is REFUSED (fail closed)."""
    for label, name in (("worker_name", worker), ("d1 database_name", db_name)):
        if name and not _CF_NAME_RE.match(name):
            raise DeployRefused(
                RefusalReason.INVALID_RESOURCE_NAME,
                f"The {label} {name!r} is not a Cloudflare-safe identifier (allowed: a "
                "leading alphanumeric then letters/digits/'-'/'_', max 63 chars). "
                "Refusing to deploy with an unsafe resource name (fail closed).",
            )


# ---- plan construction (side-effect-free) ------------------------------------


def _plan_hash(
    *, account_id: str | None, worker: str, db_name: str, spec_digest: str, tree_digest: str
) -> str:
    """A hash binding the confirmation phrase to the account, target names, and
    content digests. Any drift (a regenerated app, a different account) changes
    the hash → a previously-shown confirmation phrase no longer matches → refuse."""
    payload = json.dumps(
        {
            "account_id": account_id or "",
            "worker": worker,
            "db_name": db_name,
            "spec_digest": spec_digest,
            "tree_digest": tree_digest,
        },
        sort_keys=True,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def confirmation_phrase(plan_hash: str, worker: str, account_id: str | None) -> str:
    """The EXACT phrase the owner must echo to authorize a real deploy. Includes
    the worker, account, and a slice of the plan_hash so it is unique per
    (target, content) and cannot be a generic 'yes'."""
    acct = account_id or "UNSET"
    return f"DEPLOY {worker} TO {acct} {plan_hash[:16]}"


def build_plan(workspace: Path, store: SecretStore) -> DeployPlan:
    """Build the side-effect-free deploy plan: read the workspace, run
    ``cloudflare_export_ready`` (Epic I), parse the targets, compute digests +
    the plan_hash + the confirmation phrase, and enumerate the deploy steps with
    REDACTED commands. Contacts nothing — safe to call any time to preview."""
    files = _read_export_files(workspace)
    ready = cloudflare_export_ready(files)
    status = connection_status(store)

    wrangler_toml = files.get("wrangler.toml")
    worker, db_name = _parse_targets(wrangler_toml) if wrangler_toml else ("", "")
    # SEC-9: a non-empty but Cloudflare-UNSAFE target name refuses here, BEFORE the
    # confirmation phrase is ever computed/shown (empty names fall through to the
    # EXPORT_NOT_READY gate). Even a dry-run preview surfaces the bad name as a refusal.
    _validate_resource_names(worker, db_name)
    spec_digest = _spec_digest(workspace)
    tree_digest = _tree_digest(workspace)
    phash = _plan_hash(
        account_id=status.account_id,
        worker=worker,
        db_name=db_name,
        spec_digest=spec_digest,
        tree_digest=tree_digest,
    )
    steps = _deploy_steps(worker, db_name)
    return DeployPlan(
        workspace=str(workspace),
        account_id=status.account_id,
        worker_name=worker,
        db_name=db_name,
        spec_digest=spec_digest,
        tree_digest=tree_digest,
        plan_hash=phash,
        export_ready=ready.passed,
        export_detail=ready.evidence,
        connected=status.connected,
        steps=steps,
        confirmation_phrase=confirmation_phrase(phash, worker, status.account_id),
    )


def _deploy_steps(worker: str, db_name: str) -> list[DeployStep]:
    """The canonical, REDACTED deploy step list (no secret appears). ``mutating``
    flags the legs that leave the blast radius — including ``wrangler secret
    put``, which Cloudflare deploys a new Worker version immediately, so it is
    deploy-class, not a benign config write.

    The displayed wrangler commands mirror what :func:`_run_real_deploy` actually
    executes: a TRUSTED resolved ``wrangler`` binary (SEC-3 — NEVER ``npx wrangler``,
    which would run a workspace ``node_modules/.bin/wrangler``), run with ``cwd`` at the
    server-controlled STAGED tree, and the deploy step PINNED to
    ``--config <staged>/wrangler.toml`` (the sole config source). ``<wrangler>`` and
    ``<staged>`` are placeholders for the trusted binary path and the ``mkdtemp`` staging
    dir resolved at execute time; the display intentionally omits the secret-bearing env
    and the ADMIN_TOKEN value (piped on stdin, never argv)."""
    return [
        DeployStep(
            "Build the app",
            "npm ci && npm run build",
            mutating=False,
            note="Runs the workspace-controlled build INSIDE an isolating sandbox "
            "(no host-secret/filesystem access). `npm ci` installs exactly from "
            "the hash-covered lockfile; the built ./dist is synced back for deploy.",
        ),
        DeployStep(
            "Provision D1 (idempotent)",
            f"<wrangler> d1 list --json  ->  adopt or  <wrangler> d1 create {db_name}",
            mutating=True,
            note="Reuses the existing D1 DB by name if present; only creates when absent.",
        ),
        DeployStep(
            "Apply schema migrations",
            f"<wrangler> d1 execute {db_name} --remote --file=./schema.sql",
            mutating=True,
            note="Loads schema.sql into the remote D1 database.",
        ),
        DeployStep(
            "Deploy the Worker",
            "<wrangler> deploy --config <staged>/wrangler.toml",
            mutating=True,
            note="Publishes worker/index.ts + the built assets. Runs a TRUSTED "
            "wrangler binary (never `npx`) with --config pinned to the staged "
            "wrangler.toml (the sole config source).",
        ),
        DeployStep(
            "Set the admin secret",
            "<wrangler> secret put ADMIN_TOKEN  (value via stdin)",
            mutating=True,
            note="ADMIN_TOKEN is piped on stdin, never argv. Deploys a new Worker "
            "version immediately (deploy-class).",
        ),
    ]


# ---- the HARD-GATED executor -------------------------------------------------


def _stripe_lifecycle_for_deploy(
    staged: Path,
    store: SecretStore,
    context: StripeDeployContext | None,
) -> StripeDeploymentLifecycle | None:
    try:
        return stripe_lifecycle_for(
            _load_guarded_app_spec(staged),
            owner_id=context.owner_id if context is not None else None,
            conversation_id=context.conversation_id if context is not None else None,
            secret_store=store,
            dependencies=context.dependencies if context is not None else None,
        )
    except StripeDeployError as exc:
        raise DeployRefused(RefusalReason.STRIPE_RUNTIME_CONFIG, exc.detail) from exc


def _webhook_lifecycle_for_deploy(
    staged: Path,
    store: SecretStore,
    context: WebhookDeployContext | None,
) -> WebhookDeploymentLifecycle | None:
    try:
        return webhook_lifecycle_for(
            _load_guarded_app_spec(staged),
            owner_id=context.owner_id if context is not None else None,
            conversation_id=context.conversation_id if context is not None else None,
            secret_store=store,
            dependencies=context.dependencies if context is not None else None,
        )
    except WebhookDeployError as exc:
        raise DeployRefused(RefusalReason.WEBHOOK_RUNTIME_CONFIG, exc.detail) from exc


def _security_lifecycles_for_deploy(
    staged: Path,
    store: SecretStore,
    stripe_context: StripeDeployContext | None,
    webhook_context: WebhookDeployContext | None,
) -> tuple[StripeDeploymentLifecycle | None, WebhookDeploymentLifecycle | None]:
    stripe = _stripe_lifecycle_for_deploy(staged, store, stripe_context)
    webhook = _webhook_lifecycle_for_deploy(staged, store, webhook_context)
    if stripe is not None and webhook is not None and stripe.audience != webhook.audience:
        raise DeployRefused(
            RefusalReason.WEBHOOK_RUNTIME_CONFIG,
            "Stripe and webhook metadata do not identify the same generated app",
        )
    return stripe, webhook


async def execute_deploy(
    workspace: Path,
    store: SecretStore,
    *,
    dry_run: bool = True,
    confirmation: str | None = None,
    autonomous: bool = False,
    runner: CommandRunner | None = None,
    build_backend: BuildBackend | None = None,
    admin_token: str | None = None,
    stripe_context: StripeDeployContext | None = None,
    webhook_context: WebhookDeployContext | None = None,
    on_mutation_start: DeployMutationHook | None = None,
    on_mutation_finish: DeployMutationHook | None = None,
) -> DeployExecutionResult:
    """The single entry point for a (possibly real) deploy. Enforces the four
    hard gates IN ORDER, then either returns the dry-run plan (default, no side
    effects) or — only when every gate passes and ``dry_run`` is False — drives
    the injected ``runner`` through the real wrangler sequence.

    Raises :class:`DeployRefused` (mapped to a 4xx at the route) on any gate.
    """
    if (on_mutation_start is None) != (on_mutation_finish is None):
        raise ValueError("deploy mutation hooks must be supplied as a start/finish pair")

    # GATE 4: autonomous deploy decisions can never be mistaken for owner approval.
    # Like request_custom_build, a real deploy needs a human.
    if autonomous:
        raise DeployRefused(
            RefusalReason.AUTONOMOUS,
            "A real Cloudflare deploy is never auto-approved in an autonomous run. "
            "An owner must trigger and confirm it explicitly.",
        )

    if not (workspace.exists() and workspace.is_dir()):
        raise DeployRefused(
            RefusalReason.NO_WORKSPACE,
            f"Workspace {workspace} does not exist or is not a directory.",
        )

    # Build the plan fresh so every gate judges the current tree.
    plan = build_plan(workspace, store)

    # GATE 1: Epic I export-readiness must PASS.
    if not plan.export_ready:
        raise DeployRefused(
            RefusalReason.EXPORT_NOT_READY,
            f"cloudflare_export_ready did not pass: {plan.export_detail}",
        )

    # GATE 2: a connected, decryptable account.
    status = connection_status(store)
    if not status.connected:
        raise DeployRefused(RefusalReason.NO_CONNECTED_ACCOUNT, status.detail)

    # DRY-RUN (default): return the plan with ZERO side effects. The runner is
    # never touched; no Cloudflare API is contacted.
    if dry_run:
        return DeployExecutionResult(executed=False, dry_run=True, plan=plan)

    # GATE 3: exact owner confirmation, bound to this fresh plan_hash. A stale
    # phrase (tree/account changed since it was shown) won't match → refuse.
    if not confirmation or confirmation.strip() != plan.confirmation_phrase:
        raise DeployRefused(
            RefusalReason.CONFIRMATION_REQUIRED,
            "A real deploy requires the exact owner confirmation phrase for this "
            "plan. Re-fetch the plan and echo its confirmation_phrase verbatim.",
        )

    if runner is None:
        # CORR-15: the owner DID confirm — the server is just missing its runner. Surface
        # a distinct RUNNER_UNAVAILABLE (an operational fault), not confirmation_required.
        raise DeployRefused(
            RefusalReason.RUNNER_UNAVAILABLE,
            "No command runner is wired for real execution.",
        )

    # GATE 5 (P0-1): the untrusted, workspace-controlled `npm run build` must run
    # in an ISOLATING sandbox. With no build backend wired, or a non-isolating
    # (same-user `process`) backend, REFUSE — never run workspace build code
    # unsandboxed with host-secret access on the deploy path.
    if build_backend is None or not build_backend.isolates:
        raise DeployRefused(
            RefusalReason.BUILD_NOT_SANDBOXED,
            "The workspace-controlled build (npm run build) can only run inside an "
            "isolating sandbox (gVisor/container) so it has no access to host "
            "secrets or the host filesystem. No isolating sandbox backend is "
            "available, so a real deploy is refused.",
        )

    # GATE 6 (P1): a real deploy MUST set the deployed app's ADMIN_TOKEN so its
    # admin endpoint is actually protected. Without an admin_token we cannot run
    # `wrangler secret put ADMIN_TOKEN`, and the deployed Worker would ship an
    # UNAUTHENTICATED admin route. FAIL CLOSED — require it. (The token rides only
    # on stdin at execute time; it never enters the plan_hash, a record, or a log.)
    if not (admin_token and admin_token.strip()):
        raise DeployRefused(
            RefusalReason.ADMIN_TOKEN_REQUIRED,
            "A real deploy requires an admin_token so the deployed app's admin "
            "endpoint is protected via `wrangler secret put ADMIN_TOKEN`. Provide "
            "an admin_token to authorize a real deploy.",
        )

    # GATE 7 (SEC-4): the admin_token must be HIGH-ENTROPY — a short/low-variety token
    # would leave the deployed admin endpoint trivially guessable even with rate
    # limiting. Reject a weak one (fail closed).
    if not _admin_token_strong(admin_token):
        raise DeployRefused(
            RefusalReason.ADMIN_TOKEN_WEAK,
            "The admin_token is too weak to protect the deployed admin endpoint "
            "(need at least 16 characters and at least 8 distinct characters). "
            "Provide a high-entropy admin_token.",
        )

    # P1 (CORR-5): SNAPSHOT the credentials bound to THIS confirmed plan. A
    # ``/connect`` account/token swap between the confirmation and the actual deploy
    # must not be able to redirect the deploy to a DIFFERENT Cloudflare account — the
    # deploy uses the snapshot and refuses on drift. (The account_id is already part
    # of plan_hash, so an account swap also fails GATE 3; this is defence in depth and
    # also covers a token rotation that leaves account_id unchanged.)
    # SEC-30: refuse to USE the deploy credentials under a weak app secret too
    # (defence in depth — a credential stored before the gate, or a key weakened
    # since, must not feed a real Cloudflare mutation).
    try:
        snap_token = store.get_secret(CF_TOKEN_SECRET, strong_required=True)
        snap_account = store.get_secret(CF_ACCOUNT_SECRET, strong_required=True)
    except WeakSecretError as exc:
        raise DeployRefused(RefusalReason.NO_CONNECTED_ACCOUNT, str(exc)) from exc
    if not snap_token:
        raise DeployRefused(
            RefusalReason.NO_CONNECTED_ACCOUNT, "The Cloudflare token vanished before deploy."
        )
    if snap_account != plan.account_id:
        raise DeployRefused(
            RefusalReason.NO_CONNECTED_ACCOUNT,
            "The connected account changed between the plan and the deploy. Re-fetch "
            "the plan and re-confirm.",
        )

    return await _execute_under_deploy_locks(
        workspace,
        store,
        plan,
        runner,
        build_backend,
        admin_token,
        snap_token=snap_token,
        snap_account=snap_account,
        stripe_context=stripe_context,
        webhook_context=webhook_context,
        on_mutation_start=on_mutation_start,
        on_mutation_finish=on_mutation_finish,
    )


async def _execute_under_deploy_locks(
    workspace: Path,
    store: SecretStore,
    plan: DeployPlan,
    runner: CommandRunner,
    build_backend: BuildBackend,
    admin_token: str | None,
    *,
    snap_token: str,
    snap_account: str | None,
    stripe_context: StripeDeployContext | None,
    webhook_context: WebhookDeployContext | None,
    on_mutation_start: DeployMutationHook | None,
    on_mutation_finish: DeployMutationHook | None,
) -> DeployExecutionResult:
    """Serialize, stage, validate, execute, and durably close one real deploy."""

    # The asyncio lock queues local peers; the inner flock refuses a different
    # process. Wrangler only sees the immutable, symlink-free staged copy.
    async with _deploy_lock_for(workspace):
        with _cross_process_deploy_lock(workspace):
            staged = _stage_deploy_tree(workspace)
            deploy_home = Path(tempfile.mkdtemp(prefix="disco-deploy-home-"))
            try:
                _assert_no_plaintext_secrets(staged)
                _assert_no_secret_shaped_files(staged)
                _assert_worker_auth_verified(staged)
                _assert_wrangler_allowlisted(staged)
                _assert_sole_wrangler_config(staged, workspace)
                _assert_no_ancestor_wrangler_config(staged)
                stripe_lifecycle, webhook_lifecycle = _security_lifecycles_for_deploy(
                    staged, store, stripe_context, webhook_context
                )
                mutation_scope_entered = False

                async def begin_mutation() -> None:
                    nonlocal mutation_scope_entered
                    if on_mutation_start is not None:
                        await on_mutation_start()
                    # The successful start callback is the mutation-scope commit
                    # point. A rejected guard must never trigger finalization.
                    mutation_scope_entered = True

                original_escape: BaseException | None = None
                try:
                    return await _run_real_deploy(
                        workspace,
                        staged,
                        store,
                        plan,
                        runner,
                        build_backend,
                        admin_token,
                        snap_token=snap_token,
                        snap_account=snap_account,
                        deploy_home=str(deploy_home),
                        stripe_lifecycle=stripe_lifecycle,
                        webhook_lifecycle=webhook_lifecycle,
                        on_mutation_start=begin_mutation,
                    )
                except BaseException as exc:
                    original_escape = exc
                    raise
                finally:
                    if mutation_scope_entered and on_mutation_finish is not None:
                        try:
                            await _drain_deploy_mutation_finish(on_mutation_finish)
                        except BaseException as finalizer_error:
                            if original_escape is None:
                                raise
                            original_escape.add_note(
                                "A later cancellation or mutation-finalizer failure was "
                                "drained without replacing this original deploy failure."
                            )
                            raise original_escape from finalizer_error
            finally:
                shutil.rmtree(staged, ignore_errors=True)
                shutil.rmtree(deploy_home, ignore_errors=True)


async def _drain_deploy_mutation_finish(callback: DeployMutationHook) -> None:
    """Drain a host-mirror reseal through any number of caller cancellations."""

    task = asyncio.ensure_future(callback())
    cancellation: asyncio.CancelledError | None = None
    while not task.done():
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError as exc:
            if task.cancelled():
                raise
            cancellation = cancellation or exc
    try:
        task.result()
    except Exception:
        _log.exception("deploy mutation finalizer failed while draining cancellation")
        raise
    if cancellation is not None:
        raise cancellation


def _deploy_asset_dir_rel(workspace: Path) -> str:
    """The workspace-relative static-assets directory ``wrangler deploy`` uploads
    (the ``[assets] directory`` in wrangler.toml — e.g. ``./dist``). This is the EXACT
    tree wrangler walks and FOLLOWS symlinks into when it publishes, so the resolution
    here must match wrangler's byte-for-byte (SEC-1/CORR-1/CORR-13).

    The previous ``lstrip("./")`` normalisation was unsafe: it stripped leading ``.``
    and ``/`` *characters*, so ``"../x"`` became ``"x"``, ``"/x"`` became ``"x"``, and
    ``".git"`` became ``"git"`` — DESYNCING our pre-deploy symlink scan from the tree
    wrangler actually reads. This version resolves the value EXACTLY:

      * a leading ``./`` and ``.`` path-segments are dropped (``"./dist"`` == ``dist``);
      * an ABSOLUTE path or any ``..`` segment is REFUSED (fail closed);
      * ``"."`` (the whole workspace) resolves to the root and is scanned as-is;
      * every other name (incl. ``.git``) is kept verbatim.

    Defaults to ``dist`` only when wrangler.toml is absent/unparseable or omits the
    key. Read through the guarded reader so an escaping wrangler.toml symlink still
    fails closed."""
    text = read_workspace_file(workspace / "wrangler.toml", workspace.resolve())
    if not text:
        return "dist"
    try:
        cfg = tomllib.loads(text)
    except tomllib.TOMLDecodeError:
        return "dist"
    assets = cfg.get("assets")
    if not isinstance(assets, dict):
        return "dist"
    directory = assets.get("directory")
    if not isinstance(directory, str) or not directory.strip():
        return "dist"
    raw = directory.strip()
    p = PurePosixPath(raw)
    if p.is_absolute():
        raise DeployRefused(
            RefusalReason.WORKSPACE_SYMLINK_ESCAPE,
            f"The wrangler [assets] directory is an ABSOLUTE path: {raw}. Refusing to "
            "deploy — the assets dir must be inside the workspace (fail closed).",
        )
    parts = [seg for seg in p.parts if seg not in ("", ".")]
    if any(seg == ".." for seg in parts):
        raise DeployRefused(
            RefusalReason.WORKSPACE_SYMLINK_ESCAPE,
            f"The wrangler [assets] directory escapes the workspace via '..': {raw}. "
            "Refusing to deploy (fail closed).",
        )
    if not parts:
        # SEC-1/CORR-16: the assets dir resolves to the workspace ROOT (``.``/``./``).
        # Publishing the whole workspace would ship source + stale/non-built files (and
        # ``wrangler deploy`` would upload them). Refuse — assets must be a real built
        # subdirectory (e.g. ``dist``).
        raise DeployRefused(
            RefusalReason.ASSET_DIR_UNSAFE,
            f"The wrangler [assets] directory resolves to the workspace root ({raw!r}). "
            "Refusing to publish source/stale files — set it to the built output dir "
            "(e.g. ./dist).",
        )
    # SEC-17 (P1, the .disco/public bypass): the served [assets].directory must NOT be —
    # or be NESTED UNDER — an internal/regenerated root the deploy-tree scan globally skips
    # (``_TREE_SKIP_DIRS``: .disco/node_modules/.git/.wrangler). Such a served dir hides the
    # PUBLISHED bytes from :func:`_assert_no_secret_shaped_files` (it walks via
    # :func:`_iter_tree_files`, which prunes those roots), so wrangler could upload a
    # reassembled ``sk-…``/``cfut_…`` token as a PUBLIC asset that the scan never sees. The
    # served assets must be a normal built directory (the generated app uses ``dist``);
    # a served dir buried inside a skipped location is an injection → REFUSE (fail closed).
    if any(seg in _TREE_SKIP_DIRS for seg in parts):
        raise DeployRefused(
            RefusalReason.ASSET_DIR_UNSAFE,
            f"The wrangler [assets] directory is nested under an internal/skipped root "
            f"({raw!r}): served assets must be a normal built directory (e.g. ./dist), not "
            "hidden inside .disco/node_modules/.git/.wrangler — the secret-shaped-file scan "
            "skips those roots, so a served dir there would publish unscanned bytes. "
            "Refusing to deploy (fail closed).",
        )
    return PurePosixPath(*parts).as_posix()


def assert_deploy_tree_symlink_free(workspace: Path, asset_dir_rel: str) -> Path:
    """P0 (round 9): SCAN the EXACT directory ``wrangler deploy`` uploads (the
    ``[assets] directory``, e.g. ``./dist``) for ANY symlink — file OR directory, at
    ANY depth — and REFUSE (:class:`DeployRefused`, fail closed) if one is present.
    Called IMMEDIATELY before invoking ``wrangler deploy`` (after the build + the
    sync-back has regenerated dist), so it judges the tree wrangler actually uploads.

    WHY this is needed despite the read-class (round 6) + write-class (round 8) +
    sync-back (round 7) guards: ``wrangler deploy`` FOLLOWS symlinks when it uploads
    ``./dist``, so a PREEXISTING symlink under dist (planted in the workspace, or in a
    subtree the sync-back never overwrites) would have its TARGET — a host secret —
    published to the PUBLIC Cloudflare site. The plan-time ``_tree_digest`` CANNOT
    catch it: it runs BEFORE the build regenerates dist, and ``os.walk`` with
    ``followlinks=False`` SILENTLY SKIPS symlinked subtrees (never descends, never
    hashes), so the symlink is invisible to both the digest and the containment guard
    yet still uploaded. The dangerous read is performed by wrangler, not our code, so
    only a deploy-time, full-tree scan of the upload directory closes it.

    The scan walks with ``followlinks=False`` but DETECTS symlinks via ``is_symlink``
    (lstat) on BOTH the directory names and the file names at every level — it never
    silently skips a link, it FAILS on it. Each path component of *asset_dir_rel*
    itself is also lstat-checked (a symlinked ``dist`` root, or any intermediate, is
    refused before the walk). A clean, symlink-free dist passes and returns the
    absolute asset dir; ANY symlink anywhere → ``DeployRefused`` and the deploy never
    fires. Done at DEPLOY time (not just plan time) to defeat TOCTOU."""
    workspace_root = workspace.resolve()
    rel = Path(asset_dir_rel)
    if rel.is_absolute() or any(part == ".." for part in rel.parts):
        raise DeployRefused(
            RefusalReason.WORKSPACE_SYMLINK_ESCAPE,
            f"The deploy assets directory is absolute or escapes via '..': "
            f"{asset_dir_rel}. Refusing to deploy — fail closed.",
        )
    # lstat each component of the asset dir path: a symlinked dist root (or any
    # intermediate) is rejected before we even walk it.
    asset_dir = workspace_root
    for part in rel.parts:
        asset_dir = asset_dir / part
        if asset_dir.is_symlink():
            raise DeployRefused(
                RefusalReason.WORKSPACE_SYMLINK_ESCAPE,
                f"The deploy assets directory component is a symlink: {part} "
                f"(in {asset_dir_rel}). wrangler would follow it on upload and "
                "publish the link target to the PUBLIC site — refusing to deploy "
                "(fail closed).",
            )
    # CORR-1: the resolved assets dir must be an EXISTING directory — the scan must
    # run on the EXACT tree wrangler uploads, not a phantom path.
    if not (asset_dir.exists() and asset_dir.is_dir()):
        raise DeployRefused(
            RefusalReason.WORKSPACE_SYMLINK_ESCAPE,
            f"The deploy assets directory does not exist or is not a directory: "
            f"{asset_dir_rel}. Refusing to deploy (fail closed).",
        )
    # Walk the ENTIRE asset tree; reject ANY symlink (dir OR file) at any depth.
    for root, dirs, names in os.walk(asset_dir, followlinks=False):
        root_path = Path(root)
        for entry in (*dirs, *names):
            candidate = root_path / entry
            if candidate.is_symlink():
                kind = "DIRECTORY" if entry in dirs else "FILE"
                try:
                    shown = candidate.relative_to(workspace_root).as_posix()
                except ValueError:
                    shown = candidate.name
                raise DeployRefused(
                    RefusalReason.WORKSPACE_SYMLINK_ESCAPE,
                    f"A symlinked {kind} is present in the deploy tree: {shown}. "
                    "wrangler follows symlinks on upload — it would publish the link "
                    "target (a possible host secret) to the PUBLIC Cloudflare site. "
                    "Refusing to deploy (fail closed); remove the symlink and redeploy.",
                )
    return asset_dir


def _record_deploy_step(
    transcript: list[str],
    label: str,
    res: CommandResult | BuildResult,
    secret_literals: tuple[str, ...],
    *,
    capture_output: bool = True,
) -> None:
    # ``capture_output=False`` records ONLY the label + returncode — used for the
    # `wrangler secret put ADMIN_TOKEN` step, whose stdout/stderr could contain
    # the admin token verbatim and must NEVER land in the transcript/record.
    if not capture_output:
        transcript.append(f"$ {label}\n[exit {res.returncode}]")
        return

    # Scrub literal secret values, then redact secret-shaped patterns, before
    # anything enters the transcript. wrangler may echo ids/urls too.
    out = res.stdout.strip()
    err = res.stderr.strip()
    for secret in secret_literals:
        out = out.replace(secret, "[REDACTED]")
        err = err.replace(secret, "[REDACTED]")
    out = redact_text(out)
    err = redact_text(err)
    transcript.append(f"$ {label}\n[exit {res.returncode}]\n" + out + ("\n" + err if err else ""))


class _DeploySecretWriter:
    """Write one Worker secret and durably record its NAME, never its value/output."""

    def __init__(
        self,
        runner: CommandRunner,
        wrangler_bin: str,
        staged: Path,
        deploy_env: dict[str, str],
        transcript: list[str],
        live_workspace: Path,
        record_rel: Path,
        plan: DeployPlan,
        mutations: list[str],
        store: SecretStore,
    ) -> None:
        self._runner = runner
        self._wrangler_bin = wrangler_bin
        self._staged = staged
        self._deploy_env = deploy_env
        self._transcript = transcript
        self._live_workspace = live_workspace
        self._record_rel = record_rel
        self._plan = plan
        self._mutations = mutations
        self._store = store
        self.effective_digest: str | None = None

    async def __call__(self, name: str, value: str) -> bool:
        self._record_named_mutation(f"secret_put_attempt:{name}")
        result = await self._runner.run(
            [self._wrangler_bin, "secret", "put", name],
            cwd=self._staged,
            env=self._deploy_env,
            stdin=value,
        )
        _record_deploy_step(
            self._transcript,
            f"wrangler secret put {name}",
            result,
            (),
            capture_output=False,
        )
        if not result.ok:
            return False
        self._record_named_mutation(f"secret_put:{name}")
        return True

    def _record_named_mutation(self, name: str) -> None:
        self._mutations.append(name)
        _persist_record(
            self._live_workspace,
            self._record_rel,
            self._plan,
            status="in_progress",
            mutations=self._mutations,
            effective_digest=self.effective_digest,
            store=self._store,
        )

    async def put_legacy_admin(self, value: str) -> CommandResult:
        """Preserve the pre-Stripe ADMIN_TOKEN behavior and mutation label."""
        result = await self._runner.run(
            [self._wrangler_bin, "secret", "put", "ADMIN_TOKEN"],
            cwd=self._staged,
            env=self._deploy_env,
            stdin=value,
        )
        _record_deploy_step(
            self._transcript,
            "wrangler secret put ADMIN_TOKEN",
            result,
            (),
            capture_output=False,
        )
        if result.ok:
            self._mutations.append("secret_put")
        return result

    def record_host_token_mutation(self, name: str) -> None:
        if name not in _HOST_TOKEN_LIFECYCLE_MUTATIONS:
            raise ValueError("invalid host-token lifecycle mutation name")
        self._record_named_mutation(name)


def _recheck_security_bindings_after_build(
    stripe_lifecycle: StripeDeploymentLifecycle | None,
    webhook_lifecycle: WebhookDeploymentLifecycle | None,
    staged: Path,
) -> None:
    app_spec = _load_guarded_app_spec(staged)
    if stripe_lifecycle is not None:
        _assert_stripe_trusted_tree(staged)
        try:
            stripe_lifecycle.recheck_after_build(app_spec)
        except StripeDeployError as exc:
            raise DeployRefused(RefusalReason.STRIPE_RUNTIME_CONFIG, exc.detail) from exc
    if webhook_lifecycle is not None:
        _assert_webhook_trusted_tree(staged)
        try:
            webhook_lifecycle.recheck_after_build(app_spec)
        except WebhookDeployError as exc:
            raise DeployRefused(RefusalReason.WEBHOOK_RUNTIME_CONFIG, exc.detail) from exc


async def _activate_stripe_worker(
    lifecycle: StripeDeploymentLifecycle,
    *,
    deployed_url: str | None,
    admin_token: str,
    fresh_worker: bool,
    writer: _DeploySecretWriter,
    additional_services: frozenset[str] = frozenset(),
) -> StripeDeployError | None:
    try:
        await lifecycle.activate(
            deployed_url=deployed_url,
            admin_token=admin_token,
            fresh_worker=fresh_worker,
            put_secret=writer,
            record_mutation=writer.record_host_token_mutation,
            additional_services=additional_services,
        )
    except StripeDeployError as exc:
        if await lifecycle.fail_closed(writer):
            return exc
        return StripeDeployError(
            "stripe_runtime_state_uncertain",
            "Stripe activation state is uncertain; manual intervention is required.",
        )
    except Exception:
        if await lifecycle.fail_closed(writer):
            return StripeDeployError(
                "stripe_runtime_activation",
                "Stripe activation failed unexpectedly and was disabled",
            )
        return StripeDeployError(
            "stripe_runtime_state_uncertain",
            "Stripe activation state is uncertain; manual intervention is required.",
        )
    return None


async def _install_worker_bindings(
    stripe_lifecycle: StripeDeploymentLifecycle | None,
    webhook_lifecycle: WebhookDeploymentLifecycle | None,
    deployed_url: str | None,
    admin_token: str | None,
    fresh_worker: bool,
    writer: _DeploySecretWriter,
) -> StripeDeployError | WebhookDeployError | None:
    if webhook_lifecycle is not None and fresh_worker:
        try:
            await webhook_lifecycle.establish_disabled(writer)
        except WebhookDeployError as exc:
            if await webhook_lifecycle.fail_closed(writer):
                return exc
            return WebhookDeployError(
                "webhook_runtime_state_uncertain",
                "Webhook activation state is uncertain; manual intervention is required.",
            )
    if stripe_lifecycle is not None and admin_token is not None:
        if webhook_lifecycle is not None:
            try:
                # Stripe owns the shared bus binding and the one candidate token;
                # webhook contributes only its inbound key and service scope.
                await webhook_lifecycle.install_fixed_bindings(writer, include_bus=False)
            except WebhookDeployError as exc:
                stripe_safe = await stripe_lifecycle.fail_closed(writer)
                webhook_safe = await webhook_lifecycle.fail_closed(writer)
                if stripe_safe and webhook_safe:
                    return exc
                return WebhookDeployError(
                    "webhook_runtime_state_uncertain",
                    "Webhook/Stripe activation state is uncertain; manual intervention "
                    "is required.",
                )
            except Exception:
                stripe_safe = await stripe_lifecycle.fail_closed(writer)
                webhook_safe = await webhook_lifecycle.fail_closed(writer)
                if stripe_safe and webhook_safe:
                    return WebhookDeployError(
                        "webhook_runtime_activation",
                        "Webhook activation failed unexpectedly and Stripe remained disabled",
                    )
                return WebhookDeployError(
                    "webhook_runtime_state_uncertain",
                    "Webhook/Stripe activation state is uncertain; manual intervention "
                    "is required.",
                )
        stripe_error = await _activate_stripe_worker(
            stripe_lifecycle,
            deployed_url=deployed_url,
            admin_token=admin_token,
            fresh_worker=fresh_worker,
            writer=writer,
            additional_services=(
                webhook_lifecycle.required_services
                if webhook_lifecycle is not None
                else frozenset()
            ),
        )
        if stripe_error is not None:
            if webhook_lifecycle is not None and not await webhook_lifecycle.fail_closed(writer):
                return WebhookDeployError(
                    "webhook_runtime_state_uncertain",
                    "Webhook activation state is uncertain; manual intervention is required.",
                )
            return stripe_error
        if webhook_lifecycle is not None:
            try:
                await webhook_lifecycle.enable(writer)
            except Exception:
                stripe_safe = await stripe_lifecycle.fail_closed(writer)
                webhook_safe = await webhook_lifecycle.fail_closed(writer)
                if stripe_safe and webhook_safe:
                    return WebhookDeployError(
                        "wrangler secret put WEBHOOK_RUNTIME_READY",
                        "the verified combined Worker could not enable webhook delivery",
                    )
                return WebhookDeployError(
                    "webhook_runtime_state_uncertain",
                    "Webhook/Stripe activation state is uncertain; manual intervention "
                    "is required.",
                )
        return None
    if admin_token is not None:
        result = await writer.put_legacy_admin(admin_token)
        if not result.ok:
            if webhook_lifecycle is not None and not await webhook_lifecycle.fail_closed(writer):
                return WebhookDeployError(
                    "webhook_runtime_state_uncertain",
                    "Webhook activation state is uncertain; manual intervention is required.",
                )
            return StripeDeployError(
                "wrangler secret put ADMIN_TOKEN",
                f"secret put failed (exit {result.returncode})",
            )
    if webhook_lifecycle is not None:
        try:
            await webhook_lifecycle.install_fixed_bindings(writer)
            await webhook_lifecycle.rotate_outbound_token(
                deployed_url=deployed_url,
                put_secret=writer,
                record_mutation=writer.record_host_token_mutation,
            )
            await webhook_lifecycle.enable(writer)
        except WebhookDeployError as exc:
            if await webhook_lifecycle.fail_closed(writer):
                return exc
            return WebhookDeployError(
                "webhook_runtime_state_uncertain",
                "Webhook activation state is uncertain; manual intervention is required.",
            )
        except Exception:
            if await webhook_lifecycle.fail_closed(writer):
                return WebhookDeployError(
                    "webhook_runtime_activation", "Webhook activation failed unexpectedly"
                )
            return WebhookDeployError(
                "webhook_runtime_state_uncertain",
                "Webhook activation state is uncertain; manual intervention is required.",
            )
    return None


async def _quiesce_stripe_worker(
    lifecycle: StripeDeploymentLifecycle | None,
    worker_exists: bool,
    writer: _DeploySecretWriter,
) -> StripeDeployError | None:
    if lifecycle is None or not worker_exists:
        return None
    try:
        await lifecycle.quiesce_existing_worker(writer)
    except StripeDeployError as exc:
        if await lifecycle.fail_closed(writer):
            return exc
        return StripeDeployError(
            "stripe_runtime_state_uncertain",
            "Stripe activation state is uncertain; manual intervention is required.",
        )
    except Exception:
        if await lifecycle.fail_closed(writer):
            return StripeDeployError(
                "stripe_runtime_quiesce", "Stripe could not be disabled before deployment"
            )
        return StripeDeployError(
            "stripe_runtime_state_uncertain",
            "Stripe activation state is uncertain; manual intervention is required.",
        )
    return None


async def _quiesce_security_workers(
    stripe_lifecycle: StripeDeploymentLifecycle | None,
    webhook_lifecycle: WebhookDeploymentLifecycle | None,
    worker_exists: bool,
    writer: _DeploySecretWriter,
) -> StripeDeployError | WebhookDeployError | None:
    stripe_error = await _quiesce_stripe_worker(stripe_lifecycle, worker_exists, writer)
    if stripe_error is not None:
        return stripe_error
    if webhook_lifecycle is None or not worker_exists:
        return None
    try:
        await webhook_lifecycle.establish_disabled(writer)
    except WebhookDeployError as exc:
        if await webhook_lifecycle.fail_closed(writer):
            return exc
        return WebhookDeployError(
            "webhook_runtime_state_uncertain",
            "Webhook activation state is uncertain; manual intervention is required.",
        )
    except Exception:
        if await webhook_lifecycle.fail_closed(writer):
            return WebhookDeployError(
                "webhook_runtime_quiesce", "Webhook could not be disabled before deployment"
            )
        return WebhookDeployError(
            "webhook_runtime_state_uncertain",
            "Webhook activation state is uncertain; manual intervention is required.",
        )
    return None


async def _begin_deploy_record_scope(
    callback: DeployMutationHook | None,
    workspace: Path,
    plan: DeployPlan,
    store: SecretStore,
) -> tuple[Path, list[str], str]:
    """Cross the host-mutation fence immediately before the attempt record."""

    if callback is not None:
        await callback()
    record_rel = _new_record_rel()
    mutations: list[str] = []
    attempt_record_path = _persist_record(
        workspace,
        record_rel,
        plan,
        status="in_progress",
        mutations=mutations,
        exclusive=True,
        store=store,
    )
    return record_rel, mutations, attempt_record_path


@contextlib.contextmanager
def _close_deploy_record_on_escape(
    workspace: Path,
    record_rel: Path,
    plan: DeployPlan,
    mutations: list[str],
    store: SecretStore,
    *,
    effective_digest: Callable[[], str | None],
    deployed_url: Callable[[], str | None],
) -> Iterator[None]:
    """Close the exact attempt record before an escaping failure reaches reseal."""

    try:
        yield
    except BaseException as original:
        detail = (
            "Deploy execution was cancelled after the mutation fence."
            if isinstance(original, asyncio.CancelledError)
            else "Deploy execution exited unexpectedly after the mutation fence."
        )
        try:
            _persist_record(
                workspace,
                record_rel,
                plan,
                status="partial_failure" if mutations else "failed",
                mutations=mutations,
                deployed_url=deployed_url(),
                effective_digest=effective_digest(),
                attempted_step="deploy interrupted",
                error_detail=detail,
                store=store,
            )
        except BaseException as close_error:
            original.add_note("The deployment attempt record could not be closed.")
            raise original from close_error
        raise


def _failed_deploy_result(
    workspace: Path,
    record_rel: Path,
    plan: DeployPlan,
    mutations: list[str],
    transcript: list[str],
    effective_digest: str | None,
    store: SecretStore,
    step: str,
    detail: str,
) -> DeployExecutionResult:
    """Durably close an ordinary failed step and return its structured result."""

    record_path = _persist_record(
        workspace,
        record_rel,
        plan,
        status="partial_failure" if mutations else "failed",
        mutations=mutations,
        attempted_step=step,
        error_detail=detail,
        effective_digest=effective_digest,
        store=store,
    )
    return DeployExecutionResult(
        executed=False,
        dry_run=False,
        plan=plan,
        transcript=transcript,
        succeeded=False,
        failed_step=step,
        error_detail=detail,
        mutations=list(mutations),
        attempt_record_path=record_path,
    )


async def _run_real_deploy(
    live_workspace: Path,
    staged: Path,
    store: SecretStore,
    plan: DeployPlan,
    runner: CommandRunner,
    build_backend: BuildBackend,
    admin_token: str | None,
    *,
    snap_token: str,
    snap_account: str | None,
    deploy_home: str,
    stripe_lifecycle: StripeDeploymentLifecycle | None = None,
    webhook_lifecycle: WebhookDeploymentLifecycle | None = None,
    on_mutation_start: DeployMutationHook | None = None,
) -> DeployExecutionResult:
    """Drive the deploy sequence (idempotent) against the IMMUTABLE *staged* copy —
    NEVER the live mutable workspace (SEC-6/SEC-7/CORR-2). The UNTRUSTED build runs in
    an isolating SANDBOX (no host-secret access); the trusted wrangler steps run on the
    host with a TRUSTED wrangler binary (SEC-3), the SNAPSHOTTED token via a MINIMISED
    ENV only (SEC-32), and ADMIN_TOKEN via stdin only. Every captured output is redacted
    before it enters the transcript or the record. The deployment record is written to
    the LIVE workspace so it persists for idempotency/audit."""
    token = snap_token
    account_id = snap_account

    # SEC-3/CORR-3: resolve a TRUSTED wrangler binary (NOT `npx wrangler`, which would
    # run a workspace ``node_modules/.bin/wrangler``). The staged tree carries no
    # node_modules, and both the live workspace + staged copy are passed as unsafe
    # roots so neither PATH resolution nor the binary itself can come from them.
    wrangler_bin = _resolve_trusted_wrangler(live_workspace, staged)

    def _wr(*args: str) -> list[str]:
        return [wrangler_bin, *args]

    # Trusted wrangler receives the token only in its minimized, sanitized environment.
    # NEVER reaches the build: the build runs in the sandbox (below), which has no
    # host env / host FS at all.
    deploy_env = _deploy_env(
        token, account_id, home=deploy_home, unsafe_roots=(live_workspace, staged)
    )

    transcript: list[str] = []

    # Scrub literals before pattern redaction and never record secret-put output.
    secret_literals = tuple(s for s in (token, admin_token) if s and s.strip())

    def record(
        label: str, res: CommandResult | BuildResult, *, capture_output: bool = True
    ) -> None:
        _record_deploy_step(
            transcript,
            label,
            res,
            secret_literals,
            capture_output=capture_output,
        )

    # Validate [assets].directory early, before any Cloudflare mutation.
    asset_dir_rel = _deploy_asset_dir_rel(staged)

    # 1. Build the UNTRUSTED, workspace-controlled app INSIDE an isolating sandbox
    #    (P0-1 — no host-secret/filesystem access) with a DETERMINISTIC `npm ci`
    #    from the hash-covered lockfile (P0-2). The built ./dist is synced back into
    #    the STAGED copy (never the live tree). ABORT on failure — never mutate
    #    Cloudflare off a broken (or un-isolated) build.
    # CORR-16/SEC-1: build + sync the EXACT `[assets].directory` wrangler will publish
    # (a non-`dist` app would otherwise sync the wrong dir). The BuildBackend Protocol
    # predates this optional param (owned by wrangler.py — not editable here), so the
    # call carries the kwarg with a targeted ignore; the real SandboxBuildBackend.build
    # accepts ``asset_dir``.
    build = await build_backend.build(
        staged,
        install_cmd="npm ci",
        build_cmd="npm run build",
        asset_dir=asset_dir_rel,
    )
    record("npm ci && npm run build  (sandboxed)", build)
    if not build.ok:
        return _aborted(plan, "npm ci && npm run build", build, transcript)

    # CORR-5: re-validate the snapshotted credentials AFTER the build — a `/connect`
    # account/token swap during the deploy must not redirect to a different account.
    if (
        store.get_secret(CF_ACCOUNT_SECRET) != snap_account
        or store.get_secret(CF_TOKEN_SECRET) != snap_token
    ):
        raise DeployRefused(
            RefusalReason.NO_CONNECTED_ACCOUNT,
            "The connected Cloudflare credentials changed mid-deploy. Refusing to "
            "continue with drifted credentials (fail closed); re-fetch the plan and "
            "re-confirm.",
        )

    # SEC-17/18 (POST-BUILD rescan): the pre-build scans (in `execute_deploy`) ran on the
    # AUTHORED tree, BEFORE the build regenerated the output dir. The sandbox build can
    # EMIT a secret into the synced-back ``./dist`` (a leaked .env baked into a bundle, a
    # key written into a generated config). Re-run the deployable-file denylist + the
    # secret-SHAPED VALUE scan on the STAGED post-build tree, AFTER sync-back and BEFORE any
    # Cloudflare mutation — fail closed (no mutation runs) if the build emitted a secret.
    _assert_no_plaintext_secrets(staged)
    _assert_no_secret_shaped_files(staged)

    # SEC-4/BUILD-1 (POST-build canonical match): REQUIRE the staged worker/index.ts to BE
    # the canonical Worker the AppKit generator emits from this app's spec. The pre-build
    # heuristic auth gate (_assert_worker_auth_verified) judged the AUTHORED Worker; the
    # sandbox build could EMIT/rewrite worker/index.ts and the sync-back replaces it. This
    # gate runs HERE — POST sync-back, BEFORE the first Cloudflare mutation — and refuses
    # (WORKER_NOT_CANONICAL) any Worker that is not the pristine generated one, ending the
    # heuristic-taint arms race (no arbitrary Worker auth to analyse) and closing the
    # BUILD-1 TOCTOU (the deployed Worker == the regenerated canonical Worker).
    _assert_worker_is_canonical(staged)

    # SEC (config-source bypass, POST-build): the pre-build alt-config gate scanned the
    # AUTHORED tree; the sandbox build could EMIT a wrangler.json/.jsonc or a
    # .wrangler/deploy/config.json into the synced-back staged tree (the bytes wrangler runs
    # against). Re-scan the staged tree HERE — AFTER sync-back, BEFORE the first Cloudflare
    # mutation — and refuse (ALT_WRANGLER_CONFIG) so a build-emitted alt config never deploys.
    _assert_sole_wrangler_config(staged)
    _recheck_security_bindings_after_build(stripe_lifecycle, webhook_lifecycle, staged)

    # 2. Decide D1 provisioning from the EXACT-parsed `d1 list` (SEC-15/CORR-8): match
    #    the database by EXACT name (no substring, no first-UUID fallback). The `d1 list`
    #    itself is READ-ONLY — no mutation yet.
    listing = await runner.run(_wr("d1", "list", "--json"), cwd=staged, env=deploy_env)
    record("wrangler d1 list --json", listing)
    if not listing.ok:
        return _aborted(plan, "wrangler d1 list", listing, transcript)
    # SEC-15/CORR-8: a SUCCESSFUL list whose body is not a JSON array must ABORT — never
    # fall open to creating a fresh DB off unparseable output.
    try:
        existing_entry = _d1_entry_from_list(listing.stdout, plan.db_name)
    except _ListParseError as exc:
        return _aborted_msg(
            plan,
            "wrangler d1 list",
            transcript,
            f"malformed `wrangler d1 list --json` output ({exc}); refusing to fall open "
            "to creating a fresh database (fail closed).",
        )
    creating = existing_entry is None
    if not creating:
        # SEC-10: the D1 already exists by name. ADOPT it ONLY if a SIGNED, server-trusted
        # prior Disco deploy record proves we own it (SEC-10-B) — otherwise it is an
        # UNRELATED resource and migrating our schema into it could corrupt someone else's
        # data. Refuse (fail closed).
        if not _has_ownership_record(live_workspace, plan, store, kind="d1"):
            raise DeployRefused(
                RefusalReason.UNRELATED_RESOURCE,
                f"A D1 database named {plan.db_name!r} already exists on this account "
                "but no prior Disco deploy record proves we own it. Refusing to adopt/"
                "migrate an unrelated database (fail closed).",
            )
        # SEC-11/CORR-9: never run destructive/non-CREATE schema SQL against an adopted
        # (owner-owned, pre-existing) database.
        _assert_schema_safe_for_adopt(staged)

    # SEC-10: the read-only preflight may proceed only on a parsed existence
    # verdict or Cloudflare's documented script-not-found signal. Ambiguity fails
    # closed; an existing Worker additionally requires a signed ownership record.
    preflight = await runner.run(
        _wr("deployments", "list", "--name", plan.worker_name, "--json"),
        cwd=staged,
        env=deploy_env,
    )
    record(f"wrangler deployments list --name {plan.worker_name} --json", preflight)
    if preflight.ok:
        try:
            worker_exists = _worker_exists_from_deployments(preflight.stdout)
        except _ListParseError as exc:
            return _aborted_msg(
                plan,
                "wrangler deployments list",
                transcript,
                f"malformed `wrangler deployments list --json` output ({exc}); refusing "
                "to fall open and overwrite a possibly-unrelated Worker (fail closed).",
            )
    elif _preflight_signals_worker_absent(preflight):
        # The ONLY nonzero outcome we accept as trustworthy: a documented not-found result.
        worker_exists = False
    else:
        # SEC-10-A: any OTHER nonzero/errored preflight is AMBIGUOUS — we cannot prove the
        # Worker is absent, and proceeding could OVERWRITE an existing Worker. Fail closed
        # BEFORE any mutation (the preflight is still read-only at this point).
        raise DeployRefused(
            RefusalReason.WORKER_PREFLIGHT_FAILED,
            f"The Worker-existence preflight (`wrangler deployments list --name "
            f"{plan.worker_name}`) failed (exit {preflight.returncode}) without a "
            "documented 'script not found' result. Refusing to deploy — cannot prove the "
            "Worker is absent, and proceeding could overwrite an existing (possibly "
            "unrelated) Worker (fail closed). Check the wrangler version / API token "
            "permissions and retry.",
        )
    if worker_exists and not _has_ownership_record(live_workspace, plan, store, kind="worker"):
        raise DeployRefused(
            RefusalReason.UNRELATED_RESOURCE,
            f"A Worker named {plan.worker_name!r} already exists on this account but no "
            "signed prior Disco deploy record proves we own it. Refusing to overwrite an "
            "unrelated Worker (fail closed).",
        )

    # SEC-12/SEC-13/CORR-10/CORR-28: write a DURABLE pre-mutation 'attempt' record BEFORE
    # the first Cloudflare mutation, then UPDATE it after each mutating leg, so a failure
    # part-way through is persisted (the partial side effects are never lost). Collision-
    # proof filename (SEC-16): timestamp + random suffix.
    record_rel, mutations, attempt_record_path = await _begin_deploy_record_scope(
        on_mutation_start,
        live_workspace,
        plan,
        store,
    )
    # CORR-7: the EFFECTIVE deployed-artifact digest (post-build, post-D1-substitution),
    # computed just before `wrangler deploy` and persisted FROM the worker_deploy step on —
    # so even a partial failure AFTER the deploy durably records what was published. None
    # until then.
    effective_digest: str | None = None
    deployed_url: str | None = None

    def _fail(step: str, detail: str) -> DeployExecutionResult:
        return _failed_deploy_result(
            live_workspace,
            record_rel,
            plan,
            mutations,
            transcript,
            effective_digest,
            store=store,
            step=step,
            detail=detail,
        )

    with _close_deploy_record_on_escape(
        live_workspace,
        record_rel,
        plan,
        mutations,
        store,
        effective_digest=lambda: effective_digest,
        deployed_url=lambda: deployed_url,
    ):
        secret_writer = _DeploySecretWriter(
            runner,
            wrangler_bin,
            staged,
            deploy_env,
            transcript,
            live_workspace,
            record_rel,
            plan,
            mutations,
            store,
        )
        quiesce_error = await _quiesce_security_workers(
            stripe_lifecycle, webhook_lifecycle, worker_exists, secret_writer
        )
        if quiesce_error is not None:
            return _fail(quiesce_error.step, quiesce_error.detail)

        database_id: str | None
        if creating:
            created = await runner.run(
                _wr("d1", "create", plan.db_name), cwd=staged, env=deploy_env
            )
            record(f"wrangler d1 create {plan.db_name}", created)
            if not created.ok:
                return _fail("wrangler d1 create", f"d1 create failed (exit {created.returncode})")
            mutations.append(f"d1_create:{plan.db_name}")
            _persist_record(
                live_workspace,
                record_rel,
                plan,
                status="in_progress",
                mutations=mutations,
                store=store,
            )
            database_id = _extract_database_id(created.stdout)
        else:
            database_id = _database_id_from_list(listing.stdout, plan.db_name)

        # P1-3: substitute the REAL database_id into the STAGED wrangler.toml BEFORE deploy,
        # so the D1 binding is actually wired (a fresh deploy ships the placeholder
        # otherwise). FAIL CLOSED if we cannot resolve it — never deploy a Worker with a
        # non-functional D1 binding.
        if database_id:
            _substitute_database_id(staged, database_id)
        if _D1_ID_PLACEHOLDER in (
            read_workspace_file(staged / "wrangler.toml", staged.resolve()) or ""
        ):
            return _fail(
                "resolve d1 database_id",
                "could not resolve a valid (UUID) D1 database_id from wrangler output; "
                "refusing to deploy a Worker with an unbound (placeholder) D1 database.",
            )

        # 3. Apply schema to the remote D1. ABORT before deploy if the migration fails.
        migrate = await runner.run(
            _wr("d1", "execute", plan.db_name, "--remote", "--file=./schema.sql"),
            cwd=staged,
            env=deploy_env,
        )
        record(f"wrangler d1 execute {plan.db_name} --remote", migrate)
        if not migrate.ok:
            return _fail(
                "wrangler d1 execute (migrate)", f"migration failed (exit {migrate.returncode})"
            )
        mutations.append("d1_migrate")
        _persist_record(
            live_workspace,
            record_rel,
            plan,
            status="in_progress",
            mutations=mutations,
            store=store,
        )

        # P0 (round 9): IMMEDIATELY before `wrangler deploy` — which FOLLOWS symlinks
        # when it uploads ./dist — scan the EXACT asset tree wrangler will publish and
        # REFUSE if ANY entry (dir or file, any depth) is a symlink. The build regenerates
        # dist AFTER staging, so a symlink the build emits is caught only here. Fail closed
        # BEFORE the deploy command runs (no upload on refusal).
        assert_deploy_tree_symlink_free(staged, asset_dir_rel)

        # CORR-7: bind the audit record to the EFFECTIVE deployed artifact — the digest of
        # the staged tree POST-build and POST-D1-substitution (what wrangler actually
        # uploads), not just the plan-time pre-build digest.
        effective_digest = _tree_digest(staged)
        secret_writer.effective_digest = effective_digest

        # 4. Deploy the Worker + assets. ABORT before the secret-put on failure.
        # SEC (config-source bypass): PIN the config explicitly to the staged wrangler.toml so
        # wrangler can NEVER pick an alt source by precedence (wrangler.json/.jsonc) at exec
        # time. Belt-and-suspenders alongside the alt-config REFUSAL (the primary guard — a
        # .wrangler/deploy/config.json redirect may not be overridden by --config) + the staging
        # exclusion of .wrangler; together they guarantee wrangler.toml is the sole config source.
        deployed = await runner.run(
            _wr("deploy", "--config", str(staged / "wrangler.toml")),
            cwd=staged,
            env=deploy_env,
        )
        record("wrangler deploy", deployed)
        if not deployed.ok:
            return _fail("wrangler deploy", f"worker deploy failed (exit {deployed.returncode})")
        mutations.append("worker_deploy")
        # CORR-7: persist the effective digest WITH the worker_deploy step, so a partial
        # failure AFTER the deploy (e.g. secret put) still records what was published.
        _persist_record(
            live_workspace,
            record_rel,
            plan,
            status="in_progress",
            mutations=mutations,
            deployed_url=_extract_url(deployed.stdout),
            effective_digest=effective_digest,
            store=store,
        )

        deployed_url = _extract_url(deployed.stdout)
        binding_error = await _install_worker_bindings(
            stripe_lifecycle,
            webhook_lifecycle,
            deployed_url,
            admin_token,
            not worker_exists,
            secret_writer,
        )
        if binding_error is not None:
            return _fail(binding_error.step, binding_error.detail)

        # Finalize the SAME durable record (persists for idempotency/audit); the staged copy
        # is torn down by the caller.
        record_path = _persist_record(
            live_workspace,
            record_rel,
            plan,
            status="succeeded",
            mutations=mutations,
            deployed_url=deployed_url,
            effective_digest=effective_digest,
            store=store,
        )
        return DeployExecutionResult(
            executed=True,
            dry_run=False,
            plan=plan,
            deployed_url=deployed_url,
            record_path=record_path,
            transcript=transcript,
            succeeded=True,
            mutations=list(mutations),
            attempt_record_path=attempt_record_path,
        )


def _aborted(
    plan: DeployPlan, step: str, res: CommandResult | BuildResult, transcript: list[str]
) -> DeployExecutionResult:
    """A subprocess/build step failed — HALT the sequence BEFORE any further
    mutation. No deployment record is written (the deploy did not complete).
    Returns a clear failure result carrying the failed step + the (redacted)
    transcript so far."""
    return _aborted_msg(
        plan,
        step,
        transcript,
        f"step '{step}' failed (exit {res.returncode}); aborted before later steps.",
    )


def _aborted_msg(
    plan: DeployPlan, step: str, transcript: list[str], detail: str
) -> DeployExecutionResult:
    """An abort with an explicit detail message (e.g. an unresolved D1 id) rather
    than a subprocess exit code."""
    return DeployExecutionResult(
        executed=False,
        dry_run=False,
        plan=plan,
        transcript=transcript,
        succeeded=False,
        failed_step=step,
        error_detail=detail,
    )


def _validated_uuid(val: object) -> str | None:
    """Return *val* iff it is a string that is EXACTLY a UUID (fullmatch), else None.
    SEC-15: never accept a partial/garbage id as a database_id — a non-UUID is rejected
    so the wrangler.toml placeholder is never substituted with junk."""
    if not isinstance(val, str):
        return None
    s = val.strip()
    return s if _UUID_RE.fullmatch(s) else None


def _extract_database_id(create_stdout: str) -> str | None:
    """Pull the real D1 database_id (a UUID) out of ``wrangler d1 create`` output.
    Accepts ONLY a ``database_id``-labelled line whose value is a valid UUID — no bare
    first-UUID fallback (SEC-15: a stray UUID elsewhere in the output must not be
    mistaken for the database id)."""
    for line in create_stdout.splitlines():
        if "database_id" in line:
            m = _UUID_RE.search(line)
            if m and _validated_uuid(m.group(0)):
                return m.group(0)
    return None


class _ListParseError(Exception):
    """A SUCCESSFUL wrangler list command returned output that is NOT a JSON array
    (SEC-15/CORR-8). The caller must ABORT — never fall open to 'create a fresh DB' /
    'no existing Worker' off unparseable output."""


def _d1_entries_strict(list_stdout: str) -> list[dict]:
    """Parse ``wrangler d1 list --json`` to the list of entries, STRICTLY (SEC-15/CORR-8):
    a successful list whose stdout is not a JSON array raises :class:`_ListParseError` so
    the deploy ABORTS rather than falling open to creating a fresh DB. An empty array
    (``[]``) is a valid 'no databases' answer."""
    try:
        data = json.loads(list_stdout)
    except ValueError as exc:
        raise _ListParseError("d1 list output is not valid JSON") from exc
    if not isinstance(data, list):
        raise _ListParseError("d1 list output is not a JSON array")
    return [e for e in data if isinstance(e, dict)]


def _d1_entry_from_list(list_stdout: str, db_name: str) -> dict | None:
    """EXACT-parse ``wrangler d1 list --json`` and return the entry whose ``name``
    EQUALS *db_name* (SEC-15/CORR-8 — no substring match, no first-UUID fallback). Returns
    None when the database is absent. Raises :class:`_ListParseError` on a non-array body so
    the caller fails closed instead of treating malformed output as 'database absent'."""
    for entry in _d1_entries_strict(list_stdout):
        if entry.get("name") == db_name:
            return entry
    return None


def _worker_exists_from_deployments(stdout: str) -> bool:
    """SEC-10: from a SUCCESSFUL ``wrangler deployments list --name <worker> --json``,
    decide whether a Worker of that name already exists on the account. True iff the body
    is a NON-EMPTY JSON array of deployments. An empty array means 'no published
    deployments' (treat as absent → fresh deploy). A non-array body raises
    :class:`_ListParseError` so the caller ABORTS rather than overwriting a possibly
    unrelated Worker off unparseable output (fail closed)."""
    try:
        data = json.loads(stdout)
    except ValueError as exc:
        raise _ListParseError("deployments list output is not valid JSON") from exc
    if not isinstance(data, list):
        raise _ListParseError("deployments list output is not a JSON array")
    return len(data) > 0


#: SEC-10-A: the ONLY nonzero ``wrangler deployments list`` outcomes we accept as a
#: TRUSTWORTHY "Worker absent" result — a DOCUMENTED Cloudflare not-found signal. Anything
#: else (auth/permission/transport failure, an old/!json-capable wrangler, a malformed
#: invocation) is AMBIGUOUS and must fail closed rather than be read as "no Worker exists".
#: Cloudflare error code 10007 is ``workers.api.error.script_not_found``; the textual
#: variants cover wrangler's human-readable not-found phrasings.
_WORKER_NOT_FOUND_RE = re.compile(
    r"(?:"
    r"workers\.api\.error\.script_not_found"
    r"|script_not_found"
    r"|\[code:\s*10007\]"
    r"|\bcode[:\s]+10007\b"
    r"|(?:script|worker)\b[^.\n]{0,16}?\bnot\s+found"
    r"|could\s+not\s+find\s+(?:a\s+)?(?:script|worker)"
    r")",
    re.IGNORECASE,
)


def _preflight_signals_worker_absent(res: CommandResult) -> bool:
    """SEC-10-A: True ONLY when a NONZERO (errored) ``wrangler deployments list --name
    <w>`` carries a DOCUMENTED "script not found" signal (Cloudflare code 10007 /
    ``script_not_found`` / an explicit not-found phrase). That is the single nonzero
    outcome we trust to mean the Worker really does not exist (→ proceed to create). Any
    OTHER nonzero exit is ambiguous and must NOT be read as 'absent' — the caller fails
    closed (:class:`DeployRefused` ``WORKER_PREFLIGHT_FAILED``) instead of risking an
    overwrite."""
    blob = f"{res.stdout}\n{res.stderr}"
    return bool(_WORKER_NOT_FOUND_RE.search(blob))


def _database_id_from_list(list_stdout: str, db_name: str) -> str | None:
    """The validated (UUID) database_id for the EXACT-named *db_name* entry in
    ``wrangler d1 list --json``, or None if absent / not a valid UUID. No first-UUID
    fallback (SEC-15/CORR-8)."""
    entry = _d1_entry_from_list(list_stdout, db_name)
    if entry is None:
        return None
    return _validated_uuid(entry.get("uuid") or entry.get("database_id"))


def _substitute_database_id(workspace: Path, database_id: str) -> None:
    """Replace the ``REPLACE_WITH_D1_DATABASE_ID`` placeholder in wrangler.toml with
    the real *database_id* (P1-3), in the deploy working copy so ``wrangler deploy``
    binds D1. Idempotent: a re-deploy whose placeholder is already substituted is a
    no-op."""
    workspace_root = workspace.resolve()
    # Guarded read (escaping symlink → WORKSPACE_SYMLINK_ESCAPE) BEFORE the write,
    # so we never dereference an escaping link nor write through it.
    current = read_workspace_file(workspace / "wrangler.toml", workspace_root)
    if current is None or _D1_ID_PLACEHOLDER not in current:
        return
    # Guarded write: a wrangler.toml that is (or sits under) a symlinked component is
    # REFUSED here too — the write-back can never redirect through an in-workspace
    # symlink that resolve() would have masked.
    write_workspace_file(
        "wrangler.toml", current.replace(_D1_ID_PLACEHOLDER, database_id), workspace
    )


def _extract_url(stdout: str) -> str | None:
    for token in stdout.split():
        if token.startswith("https://") and "workers.dev" in token:
            return token.rstrip(".,")
    return None


def _new_record_rel() -> Path:
    """A COLLISION-PROOF deployment-record relative path (SEC-16): a timestamp (down to
    microseconds) PLUS a random suffix, so two deploys in the same second — or a re-run —
    never clobber each other's record."""
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S_%fZ")
    return Path(_DEPLOY_RECORD_DIR) / f"{stamp}-{_secrets.token_hex(6)}.json"


def _persist_record(
    workspace: Path,
    rec_rel: Path,
    plan: DeployPlan,
    *,
    status: str,
    mutations: list[str],
    deployed_url: str | None = None,
    effective_digest: str | None = None,
    attempted_step: str | None = None,
    error_detail: str | None = None,
    exclusive: bool = False,
    store: SecretStore | None = None,
) -> str:
    """Persist / UPDATE the NON-SECRET durable deployment record (idempotency + teardown
    audit + PARTIAL-STATE durability). No token, no ADMIN_TOKEN — only target names,
    digests, the deployed URL, and which mutating legs completed (``mutations``).

    Called BEFORE the first Cloudflare mutation (``status="in_progress"``, empty
    mutations), UPDATED after each mutating leg, and finalized on success
    (``status="succeeded"``) or partial failure (``status="partial_failure"``/``failed``
    with ``attempted_step``/``error_detail``) — so a deploy that fails after a real
    side effect (D1 create/migrate, Worker deploy, secret put) is DURABLY recorded, not
    lost (SEC-12/SEC-13/CORR-10/CORR-28). Rewrites the SAME ``rec_rel`` each time.

    SECURITY (P0, round 8): the record lives under ``.disco/cloudflare/deployments``
    (digest-SKIPPED), so directory creation and publication go through the single
    descriptor-rooted writer (:func:`write_workspace_file`). It opens every component
    with ``O_NOFOLLOW`` and revalidates identities through publication, so neither a
    static symlink nor an ancestor-swap race can redirect the record.

    SEC-10-B: when a ``store`` is supplied, the record is SIGNED — an HMAC over the
    ownership-bearing fields (account_id, worker_name, db_name, mutations) keyed by the
    server-controlled signing key. A later deploy trusts this record (for adoption /
    overwrite) ONLY if that signature verifies, so a record planted in the workspace's
    digest-SKIPPED ``.disco`` dir by the untrusted build agent — which cannot read the
    server key — authorizes nothing."""
    # SEC-10-B: sign the ownership-bearing fields with the server-controlled key so this
    # record can be TRUSTED by a later deploy (and only this server could have written it).
    signature = (
        _ownership_signature(
            store,
            account_id=plan.account_id,
            worker_name=plan.worker_name,
            db_name=plan.db_name,
            mutations=mutations,
            create=True,
        )
        if store is not None
        else None
    )
    payload = json.dumps(
        {
            "status": status,
            "worker_name": plan.worker_name,
            "db_name": plan.db_name,
            "account_id": plan.account_id,
            "spec_digest": plan.spec_digest,
            "tree_digest": plan.tree_digest,
            "plan_hash": plan.plan_hash,
            "effective_digest": effective_digest,
            "deployed_url": deployed_url,
            "mutations": list(mutations),
            "signature": signature,
            "attempted_step": attempted_step,
            "error_detail": error_detail,
            "updated_at": datetime.now(UTC).strftime("%Y%m%dT%H%M%S_%fZ"),
        },
        indent=2,
    )
    rec_path = write_workspace_file(rec_rel, payload, workspace, exclusive=exclusive)
    return str(rec_path)


__all__ = [
    "CF_ACCOUNT_SECRET",
    "CF_TOKEN_SECRET",
    "assert_deploy_tree_symlink_free",
    "build_plan",
    "confirmation_phrase",
    "connect_account",
    "connection_status",
    "disconnect_account",
    "ensure_workspace_dir",
    "execute_deploy",
    "read_workspace_file",
    "workspace_contained",
    "write_workspace_file",
]
