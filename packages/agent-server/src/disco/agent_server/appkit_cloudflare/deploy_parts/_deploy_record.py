"""The durable (non-secret) deployment record: collision-proof naming, signed
persistence, the pre-mutation record-scope open, and the escape-safe close.

Extracted from ``deploy.py`` to reduce module complexity; the public facade
re-imports these names unchanged.

``write_workspace_file`` is reached through the ``deploy`` facade (a lazy,
function-local import) rather than a sibling module: it is DEFINED on
``deploy.py`` itself (see ``_workspace.py``'s docstring for why), and deploy.py
imports FROM this module at its own top level, so a module-level reverse import
here would be circular.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import secrets as _secrets
from collections.abc import Callable, Iterator
from datetime import UTC, datetime
from pathlib import Path

from disco.core.llm.secrets import SecretStore

from ..models import DeployExecutionResult, DeployPlan
from ._constants import _DEPLOY_RECORD_DIR, DeployMutationHook
from ._ownership import _ownership_signature


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
    from disco.agent_server.appkit_cloudflare import deploy as _deploy

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
    rec_path = _deploy.write_workspace_file(rec_rel, payload, workspace, exclusive=exclusive)
    return str(rec_path)


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
