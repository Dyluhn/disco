"""AppKit EPIC O — typed data models for the Cloudflare deploy capability.

Pure data + the one refusal exception. No IO, no network, no subprocess — the
planner/executor (``deploy.py``) and the owner routes (``routes.py``) build and
consume these. Keeping the models pure makes the gate logic unit-testable
without a real Cloudflare account or a wrangler binary.

SAFETY NOTE: none of these structures ever carry the API token or the
ADMIN_TOKEN secret. A ``DeployStep.command`` is the REDACTED display form of a
wrangler invocation; the real token rides only in the subprocess ENV at execute
time (see ``deploy.py``), never in argv, a model field, a plan, a transcript, or
a persisted deployment record.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum


class RefusalReason(str, Enum):
    """Why a real (non-dry-run) deploy was refused. Each maps to a named HTTP
    reason at the route so the owner sees exactly which gate blocked them and
    never a generic 500. Every value here is a HARD gate — a real Cloudflare
    mutation can fire ONLY when none of these apply."""

    #: The autonomous-run refusal — like ``request_custom_build``, a real deploy
    #: is NEVER auto-approved without a human. An autonomous caller is refused
    #: outright, before any readiness/credential work.
    AUTONOMOUS = "autonomous_refused"
    #: Epic I ``cloudflare_export_ready`` did not PASS (or the workspace is not a
    #: complete CF export). Fail closed — never deploy a half-built export.
    EXPORT_NOT_READY = "export_not_ready"
    #: No connected account: the API token is absent from the SecretStore or
    #: cannot be decrypted (missing/wrong app secret).
    NO_CONNECTED_ACCOUNT = "no_connected_account"
    #: The owner confirmation phrase was missing or did not EXACTLY match the
    #: phrase bound to this freshly-computed plan (covers a stale plan whose
    #: digests/hash changed since the phrase was shown).
    CONFIRMATION_REQUIRED = "confirmation_required"
    #: The workspace could not be resolved / is not a Cloudflare export tree.
    NO_WORKSPACE = "no_workspace"
    #: P0-1: the untrusted, workspace-controlled ``npm run build`` cannot be run in
    #: an isolating sandbox (no build backend wired, or the active backend is the
    #: same-user ``process`` backend with host-secret/filesystem access). FAIL
    #: CLOSED — never run workspace-controlled build code unsandboxed on the deploy
    #: path, where it could read host secrets (the SecretStore, ``~/.config/disco``).
    BUILD_NOT_SANDBOXED = "build_not_sandboxed"
    #: P1: a real deploy requires an ``admin_token`` so the deployed app's admin
    #: endpoint is actually protected (``wrangler secret put ADMIN_TOKEN``). Without
    #: one we cannot set the secret and would publish a Worker whose admin route is
    #: UNAUTHENTICATED. FAIL CLOSED — never deploy an unprotected admin endpoint.
    ADMIN_TOKEN_REQUIRED = "admin_token_required"
    #: P0 (round 5): a workspace path resolves OUTSIDE the workspace after following
    #: symlinks — i.e. an escaping symlink pointing at a host file (a SecretStore
    #: file, ``~/.config/disco``, a provider key). Reading/hashing it would
    #: dereference the link and pull host-secret content into the plan_hash/tree
    #: digest (the planner side) or the build sandbox/artifact (the push side). FAIL
    #: CLOSED — the plan build itself refuses, BEFORE any byte is read through the
    #: link, so the secret is never dereferenced on the deploy path.
    WORKSPACE_SYMLINK_ESCAPE = "workspace_symlink_escape"
    #: WAVE 2 (SEC-3/CORR-3/SEC-32): no TRUSTED ``wrangler`` binary could be resolved
    #: from OUTSIDE the untrusted workspace/staging tree (no pinned ``DISCO_WRANGLER_BIN``
    #: and nothing absolute+outside on ``PATH``). FAIL CLOSED rather than fall back to a
    #: bare ``wrangler`` that a build-controlled ``dist/wrangler`` could shadow.
    WRANGLER_NOT_TRUSTED = "wrangler_not_trusted"
    #: WAVE 2 (SEC-5): wrangler.toml carries an unexpected top-level key/binding that
    #: would broaden the deploy blast radius (``routes``/custom domains/``build`` hooks/
    #: cron ``triggers``/extra ``vars``/extra bindings). Only an allowlisted set of keys
    #: is permitted — anything else is refused before deploy.
    WRANGLER_CONFIG_REJECTED = "wrangler_config_rejected"
    #: WAVE 2 (SEC-1/CORR-16): the resolved ``[assets] directory`` is the workspace
    #: ROOT (``.``) — deploying it would publish source/stale files. Refuse.
    ASSET_DIR_UNSAFE = "asset_dir_unsafe"
    #: WAVE 2 (SEC-11/CORR-9): the ``schema.sql`` applied to an ADOPTED (pre-existing)
    #: D1 database contains a destructive / non-``CREATE`` statement (DROP/DELETE/ALTER/
    #: UPDATE/…) that could mutate or wipe owner data. Refuse before the migration runs.
    SCHEMA_UNSAFE = "schema_unsafe"
    #: WAVE 2 (SEC-4): the owner-supplied ``admin_token`` is too weak (too short / too
    #: little variety) to protect the deployed admin endpoint. Require a high-entropy one.
    ADMIN_TOKEN_WEAK = "admin_token_weak"
    #: WAVE 2 (SEC-10): a Cloudflare resource with the target name already exists but is
    #: NOT proven owned by a prior Disco deploy record — adopting it could overwrite an
    #: UNRELATED resource. Refuse rather than silently adopt/overwrite.
    UNRELATED_RESOURCE = "unrelated_resource_adopt_refused"
    #: WAVE 3 (SEC-4): no POSITIVE worker-auth verification for the staged tree. A real
    #: deploy must prove the generated ``worker/index.ts`` enforces the admin-token check
    #: on the protected routes (``/admin`` + GET ``/api/leads``), fails closed when
    #: ADMIN_TOKEN is unset, AND never echoes ``env.ADMIN_TOKEN`` into a response body.
    #: Without a passing verdict (missing worker, unguarded route, or a token leak) the
    #: deploy is REFUSED before any Cloudflare mutation (fail closed) — never publish a
    #: Worker whose admin gate is unverified or that exfiltrates the token.
    WORKER_AUTH_UNVERIFIED = "worker_auth_unverified"
    #: WAVE 3 (SEC-9): the resolved ``worker_name`` / ``db_name`` is not a
    #: Cloudflare-safe identifier (charset / length). Refuse before plan confirmation —
    #: an invalid name would either fail at wrangler or collide unexpectedly.
    INVALID_RESOURCE_NAME = "invalid_resource_name"
    #: WAVE 3 (CORR-15): a real deploy was authorized but NO command runner is wired to
    #: execute the wrangler sequence. Distinct from ``confirmation_required`` — the owner
    #: DID confirm; the server is simply missing its runner (an operational fault).
    RUNNER_UNAVAILABLE = "runner_unavailable"
    #: WAVE 4 (SEC-10-A): the Worker-existence PREFLIGHT (``wrangler deployments list
    #: --name <w>``) could not produce a TRUSTWORTHY verdict — it errored (nonzero exit:
    #: old wrangler, auth/permission/transport failure, malformed invocation) WITHOUT a
    #: documented "script not found" result. FAIL CLOSED: we cannot prove the Worker is
    #: absent, and proceeding could OVERWRITE an existing (possibly unrelated) Worker, so
    #: the deploy is refused rather than falling open to "no Worker exists".
    WORKER_PREFLIGHT_FAILED = "worker_preflight_failed"
    #: SEC-25 (cross-process): another SERVER PROCESS already holds the cross-process
    #: advisory deploy lock for this workspace (a file ``flock`` keyed by the resolved
    #: workspace path). The in-process asyncio mutex + the routes per-conversation
    #: in-flight set only serialise WITHIN one process; under a multi-worker server
    #: (e.g. gunicorn) a second worker could otherwise interleave a deploy of the SAME
    #: workspace. FAIL CLOSED — refuse the concurrent deploy rather than risk a torn
    #: build/D1/wrangler.toml/record. A transient condition: retry once the other
    #: process finishes.
    DEPLOY_IN_PROGRESS = "deploy_in_progress"
    #: WAVE 5 (SEC-4 / BUILD-1): the staged ``worker/index.ts`` does NOT MATCH the
    #: canonical Worker the AppKit generator deterministically emits from this app's
    #: spec. The generated Worker is a pure function of the app spec, so the deployed
    #: Worker is REQUIRED to BE that canonical Worker — closing the SEC-4 heuristic-taint
    #: arms race (there is no arbitrary, custom-authored Worker to prove auth-safe) AND
    #: the BUILD-1 TOCTOU (the check runs POST-build, on the synced-back staged tree, so
    #: a build that EMITS or rewrites ``worker/index.ts`` is caught before any Cloudflare
    #: mutation). The build compiles the FRONTEND into ``dist``; it must NEVER touch the
    #: Worker source. A missing/unloadable spec → fail closed (refuse) — an unverifiable
    #: Worker is never deployed.
    WORKER_NOT_CANONICAL = "worker_not_canonical"
    #: SEC-1-class: wrangler.toml ``main`` does NOT EXACTLY name the canonical worker
    #: entry (``worker/index.ts``). ``wrangler deploy`` runs whatever ``main`` points at,
    #: but the canonical-worker match gate validates ``worker/index.ts`` — so a ``main``
    #: that resolves elsewhere (an absolute path, a ``..`` escape, or a look-alike dir
    #: like ``....worker/index.ts`` that a naive ``lstrip("./")`` would collapse to the
    #: canonical name) would deploy a DIFFERENT, UNCHECKED Worker (and dodge the SEC-10
    #: ownership preflight). REQUIRE ``main`` to be EXACTLY the file the canonical match
    #: validates — fail closed otherwise.
    MAIN_NOT_CANONICAL = "main_not_canonical"
    #: SEC (config-source bypass): an ALTERNATE wrangler config source is present in the
    #: deploy tree. The deploy gates (SEC-5 allowlist, canonical-worker match, SEC-10)
    #: read/validate ONLY ``wrangler.toml``, but ``wrangler deploy`` resolves config with
    #: the precedence ``wrangler.json`` → ``wrangler.jsonc`` → ``wrangler.toml`` AND honors
    #: a redirect file ``.wrangler/deploy/config.json`` pointing at an ARBITRARY config. A
    #: planted ``wrangler.json``/``wrangler.jsonc`` (different ``main``/``name``/routes/
    #: account) or a ``.wrangler`` redirect would deploy UNCHECKED bytes/name/account,
    #: bypassing every gate. AppKit generates ONLY ``wrangler.toml``, so any alt config is an
    #: injection — REFUSE (fail closed). ``wrangler.toml`` is the sole config source.
    ALT_WRANGLER_CONFIG = "alt_wrangler_config"
    #: SEC (out-of-tree config-source bypass): a ``.wrangler`` (esp. a
    #: ``.wrangler/deploy/config.json`` redirect) exists at an ANCESTOR of the staged deploy
    #: cwd — OUTSIDE both the staged copy and the live workspace (the trees the
    #: ``ALT_WRANGLER_CONFIG`` scan covers). ``wrangler deploy`` discovers a deploy-config
    #: redirect by walking UP from its cwd, so a PRE-EXISTING ancestor redirect (e.g. a
    #: planted ``/tmp/.wrangler/deploy/config.json`` above the ``mkdtemp`` staging dir) would
    #: re-point wrangler at an ARBITRARY config/main/account that ``--config`` may not
    #: override — bypassing every gate. The deploy walks every ancestor of the staged cwd up
    #: to the filesystem root and REFUSES (fail closed) if any holds a ``.wrangler``.
    ANCESTOR_WRANGLER_CONFIG = "ancestor_wrangler_config"
    #: SEC-17/SEC-18: a PLAINTEXT secret is present in the deploy tree — a secret-NAMED
    #: ``.env`` / ``wrangler.toml`` ``[vars]`` assignment (``_assert_no_plaintext_secrets``),
    #: a high-confidence secret-SHAPED value in a scanned file, or a credential-NAMED file
    #: (``_assert_no_secret_shaped_files``). Such a value belongs in ``wrangler secret put``
    #: (encrypted at the edge), never as a public/build-time-inlined plaintext that would
    #: ship to the public edge. FAIL CLOSED — refuse before any Cloudflare mutation. This is
    #: DISTINCT from ``WORKSPACE_SYMLINK_ESCAPE`` (an escaping-symlink refusal); the
    #: secret-scan gates carry this dedicated reason so the owner sees exactly which gate
    #: blocked them.
    PLAINTEXT_SECRET_REFUSED = "plaintext_secret_refused"
    #: WO-F4.1: host-owned Stripe config, scoped secrets, the public A2 bus,
    #: and deployed-token stores must all be present and mutually consistent.
    STRIPE_RUNTIME_CONFIG = "stripe_runtime_config_refused"


class DeployRefused(Exception):
    """A real deploy was blocked by a hard gate. Carries the machine ``reason``
    and a human ``detail``; the route maps it to a 4xx with the reason code."""

    def __init__(self, reason: RefusalReason, detail: str) -> None:
        super().__init__(f"{reason.value}: {detail}")
        self.reason = reason
        self.detail = detail


@dataclass(frozen=True)
class ConnectionStatus:
    """The O1 account-connection state, derived from the SecretStore — NEVER the
    token itself, only whether one is present and usable."""

    connected: bool
    token_present: bool
    decryptable: bool
    account_id: str | None
    detail: str


@dataclass(frozen=True)
class DeployStep:
    """One step of the deploy plan. ``command`` is the human-readable, REDACTED
    display of the wrangler/npm invocation (token never appears). ``mutating``
    distinguishes a real outward change (d1 create, migrations, deploy, secret
    put) from a read/build step, so the dry-run plan can call out exactly which
    legs leave the blast radius."""

    title: str
    command: str
    mutating: bool
    note: str = ""


@dataclass(frozen=True)
class DeployPlan:
    """The complete, side-effect-free deploy plan for an AppKit export. Produced
    by reading the workspace + running ``cloudflare_export_ready``; building it
    never contacts Cloudflare. ``confirmation_phrase`` is the exact string the
    owner must echo to authorize a real deploy — it is bound to the account,
    target names, and the content digests via ``plan_hash`` so it cannot be
    replayed against a changed tree."""

    workspace: str
    account_id: str | None
    worker_name: str
    db_name: str
    spec_digest: str
    tree_digest: str
    plan_hash: str
    export_ready: bool
    export_detail: str
    connected: bool
    steps: list[DeployStep] = field(default_factory=list)
    confirmation_phrase: str = ""


@dataclass(frozen=True)
class DeployExecutionResult:
    """The outcome of an ``execute_deploy`` call. In dry-run (the default) it
    carries the plan and ``executed=False`` with zero side effects. After a real
    owner-confirmed deploy it carries the REDACTED transcript, the deployed URL,
    and the path of the persisted (non-secret) deployment record.

    ``succeeded`` distinguishes a clean run from an ABORTED one: any non-zero
    subprocess step (build/migration/deploy) halts the sequence BEFORE the next
    mutation, returning ``executed=False, succeeded=False`` with the ``failed_step``
    + ``error_detail``.

    PARTIAL STATE (CORR-28/SEC-12/SEC-13): ``succeeded`` alone cannot tell whether a
    FAILED deploy left side effects on Cloudflare. ``mutations`` enumerates the
    mutating legs that actually completed (``d1_create``/``d1_migrate``/``worker_deploy``/
    ``secret_put``), so a failure AFTER one or more of them is durably modelled (a
    half-applied deploy is partial, not 'nothing happened'). ``attempt_record_path`` is
    the durable on-disk record written BEFORE the first Cloudflare mutation and updated
    per step — present on BOTH a clean run and a partial failure, so the side effects
    are never lost to an in-memory-only result. A pre-mutation refusal (export/auth/
    confirmation/build) leaves ``mutations`` empty and no ``attempt_record_path``."""

    executed: bool
    dry_run: bool
    plan: DeployPlan
    deployed_url: str | None = None
    record_path: str | None = None
    transcript: list[str] = field(default_factory=list)
    succeeded: bool = True
    failed_step: str | None = None
    error_detail: str | None = None
    #: The mutating Cloudflare legs that ACTUALLY completed (in order). Empty on a
    #: pre-mutation refusal/abort; partial on a mid-sequence failure; full on success.
    mutations: list[str] = field(default_factory=list)
    #: The durable deploy record path written BEFORE the first mutation and updated per
    #: step (persists the partial state on failure). ``None`` only when no mutation was
    #: ever attempted.
    attempt_record_path: str | None = None


__all__ = [
    "ConnectionStatus",
    "DeployExecutionResult",
    "DeployPlan",
    "DeployRefused",
    "DeployStep",
    "RefusalReason",
]
