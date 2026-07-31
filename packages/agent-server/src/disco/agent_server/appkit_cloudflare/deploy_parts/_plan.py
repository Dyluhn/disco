"""O1 account connection, the side-effect-free deploy plan builder, and the
Stripe/webhook deployment-lifecycle resolution consumed by the executor.

Extracted from ``deploy.py`` to reduce module complexity; the public facade
re-imports these names unchanged.
"""

from __future__ import annotations

import hashlib
import json
import tomllib
from pathlib import Path

from disco.core.appkit.local_verify import cloudflare_export_ready
from disco.core.llm.secrets import SecretStore

from ..models import ConnectionStatus, DeployPlan, DeployRefused, DeployStep, RefusalReason
from ..stripe_deploy import (
    StripeDeployContext,
    StripeDeployError,
    StripeDeploymentLifecycle,
    stripe_lifecycle_for,
)
from ..webhook_deploy import (
    WebhookDeployContext,
    WebhookDeployError,
    WebhookDeploymentLifecycle,
    webhook_lifecycle_for,
)
from ._constants import _CF_NAME_RE, CF_ACCOUNT_SECRET, CF_TOKEN_SECRET
from ._staging import _read_export_files, _spec_digest, _tree_digest
from ._worker import _load_guarded_app_spec

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


# ---- security-primitive deployment lifecycles ---------------------------------


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
