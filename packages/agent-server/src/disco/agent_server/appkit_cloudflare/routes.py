"""AppKit EPIC O — owner-only Cloudflare deploy API.

The real-deploy mutation path sits OUTSIDE the LLM tool loop (codex's safety
adjustment): these are owner-triggered HTTP endpoints, not model-callable tools.
The model can build + verify an export (Epics E/G/I); only the human owner, via
these routes, connects an account and authorizes a real deploy.

  GET  /api/appkit/cloudflare/status            — connection state (never the token)
  POST /api/appkit/cloudflare/connect           — store API token (encrypted) + account
  POST /api/appkit/cloudflare/disconnect        — clear the stored credential
  POST /api/appkit/cloudflare/connection-test   — verify the token is active (read-only)
  POST /api/appkit/cloudflare/deploy-plan        — side-effect-free dry-run plan
  POST /api/appkit/cloudflare/deploy             — gated; dry-run DEFAULT, real on confirm

Every real-deploy refusal (export-not-ready / no-account / no-confirmation /
autonomous) returns a named 4xx, never a fake success. The token is never
echoed back, never logged.

CLUSTER C (deploy-security) hardening, this file:
  * SEC-25 a per-conversation HTTP deploy lock serialises owner deploys (a second
    in-flight deploy for the same workspace gets 409, never an interleaved run);
  * CORR-25 accurate HTTP status mapping — bad input→400, missing workspace→404,
    refused-by-gate→409 (named), an ABORTED/failed deploy step→502 (NEVER 200),
    unexpected→500;
  * CORR-26 the strict-CORS middleware strips ANY preexisting deploy
    Access-Control-Allow-Origin that is wildcard OR reflects a non-allowlisted
    (arbitrary) origin — not just the literal ``*``;
  * SEC-27 ``admin_token`` (and the CF token) are bounded ``SecretStr`` inputs so a
    validation error never echoes the secret, and an over-long value is a 400;
  * SEC-28 owner-token auth FAILURES are rate-limited per client + every auth
    attempt is logged WITHOUT the token value;
  * SEC-26 responses carry NO absolute host paths — the workspace is referenced by
    its opaque conversation id and the deployment record by a workspace-relative path;
  * CORR-27 ``/connect`` verifies the token with the existing verifier BEFORE it is
    stored / marked connected (never persist an unverified credential).

EPIC O wave-2 follow-ups, this file:
  * SEC-29 ``/connect`` VALIDATES the account id (a bounded Cloudflare-id-shaped
    token — non-empty, no whitespace / path / URL metacharacters, length-capped)
    BEFORE it is stored, so an unbounded/garbage account id is never persisted; and
    the verify-before-store gate is bound to that account — when the injected
    verifier exposes an account-scoped check (``verify_for_account``) the route uses
    it so a token that is not valid FOR the given account (lacks the scope) is
    refused, never connected. (The bundled ``HttpTokenVerifier`` hits Cloudflare's
    account-agnostic token-verify endpoint, so it confirms the token is active; a
    deployment that injects an account-scoped verifier gets full scope binding.)
  * SEC-26 (boundary) every client-facing refusal/error ``message`` is SCRUBBED of
    any absolute host path at the route boundary (mapped to an opaque ``<path>``
    marker) — defense in depth so that even if an inner layer were to include a host
    path in a ``DeployRefused``/spawn detail, it never reaches the client.
"""

from __future__ import annotations

import hmac
import logging
import os
import re
import time
from collections.abc import Awaitable
from pathlib import Path
from typing import TYPE_CHECKING, cast

from disco.core.llm.secrets import WeakSecretError
from disco.core.store.sqlite import SqliteEventStore
from disco.tools.projects import StorageStatus
from fastapi import APIRouter, Depends, Header, HTTPException
from pydantic import BaseModel, SecretStr
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.requests import Request
from starlette.responses import Response

from ..workspace_commit import CommittedWorkspaceView, WorkspaceCommitUnavailable
from ..workspace_process_fence import (
    WorkspaceProcessBusy,
    WorkspaceProcessFenceUnavailable,
    workspace_process_fence,
)
from . import deploy as cf
from .models import DeployPlan, DeployRefused, RefusalReason
from .sandbox_build import build_backend_for_runtime
from .stripe_deploy import StripeDeployContext, StripeDeployDependencies
from .webhook_deploy import WebhookDeployContext, WebhookDeployDependencies
from .wrangler import (
    BuildBackend,
    CommandRunner,
    HttpTokenVerifier,
    SubprocessCommandRunner,
    TokenVerifier,
)

if TYPE_CHECKING:
    from ..runtime import ConversationRuntime

logger = logging.getLogger(__name__)

# ---- owner-only HTTP authority (P0-3) ---------------------------------------

#: The server-side owner token. The deploy API connects a LIVE Cloudflare account
#: and can mutate it, so the HTTP layer must itself be authoritative — the in-loop
#: gates can't be relied on if an unauthorized caller can reach the route. The
#: operator sets this out-of-band; callers present it in ``X-Disco-Owner-Token``.
_OWNER_TOKEN_ENV = "DISCO_ADMIN_TOKEN"
#: Optional CORS allowlist for the deploy surface (comma-separated origins). The
#: deploy routes are NEVER wildcard-CORS reachable; an Origin is reflected only if
#: it is listed here. Default (unset) → no cross-origin access at all.
_OWNER_ORIGINS_ENV = "DISCO_OWNER_ORIGIN"
#: Path prefix the strict-CORS middleware guards.
_CF_DEPLOY_PATH_PREFIX = "/api/appkit/cloudflare/"

#: SEC-27 input bounds. A real Cloudflare API token / owner-provided ADMIN_TOKEN is
#: short; an over-long body field is rejected (400) before it is parsed/echoed.
_MAX_SECRET_LEN = 8192

#: SEC-29 account-id shape. A Cloudflare account id is an opaque hex token (32 hex
#: chars in production); we accept a bounded alphanumeric token (``[A-Za-z0-9_-]``,
#: 8–64 chars) so a malformed/unbounded value — empty, whitespace, a path fragment
#: (``../``, ``/etc/...``), a URL, or a multi-kilobyte blob — is REJECTED before it
#: is ever stored as the connected account. A real 32-hex CF id is a strict subset.
_ACCOUNT_ID_RE = re.compile(r"[A-Za-z0-9_-]{8,64}")

#: SEC-26 (boundary): an absolute POSIX host path embedded in a message — a ``/`` at
#: a path boundary (NOT preceded by a path character, so it is a real root ``/`` and
#: not an internal separator of a relative path) followed by at least one
#: ``segment/`` and a trailing segment. Matched so the route can replace it with an
#: opaque marker before any refusal/error reaches the client. A relative path (e.g. a
#: workspace-relative record path like ``.disco/cloudflare/rec.json``) is NOT matched.
_ABS_PATH_RE = re.compile(r"(?<![A-Za-z0-9._-])/(?:[A-Za-z0-9._-]+/)+[A-Za-z0-9._-]*")

#: SEC-26 (remainder): a WORKSPACE-INTERNAL relative path embedded in a FREE-TEXT
#: response field (a transcript line, a result ``error_detail``/``failed_step``, the
#: plan ``export_detail``). The internal Disco state dir ``.disco/...`` exposes the
#: private workspace layout (deployment-record locations, internal state files); it
#: must NOT leak inside arbitrary free text. The dedicated ``record_path`` field is a
#: deliberate workspace-relative reference (built by :func:`_relative_record_path`)
#: and does NOT pass through this — only opaque free-text fields do.
_REL_INTERNAL_PATH_RE = re.compile(r"(?<![A-Za-z0-9._-])\.disco(?:/[A-Za-z0-9._-]+)+")

#: SEC-29 (remainder): when set truthy, a real ``/connect`` REQUIRES an account-scoped
#: verifier (one exposing ``verify_for_account``). If only the account-agnostic
#: verifier is available the connect is REFUSED (the token cannot be proven bound to
#: the target account) rather than degrading. Default (unset) → degrade-but-surface:
#: connect succeeds and the response flags ``account_scoped: false`` so the caller is
#: never silently told an agnostically-verified token is account-bound.
_REQUIRE_SCOPED_VERIFY_ENV = "DISCO_REQUIRE_ACCOUNT_SCOPED_VERIFY"

#: SEC-28 owner-auth failure rate limit: at most this many FAILED owner-token
#: attempts per client within the window before further attempts are throttled
#: (429). A successful auth clears the client's failure tally. The unit is failures
#: in a fixed sliding window; this caps online token guessing without affecting a
#: legitimate operator (who authenticates successfully and is cleared).
_AUTH_FAIL_LIMIT = 8
_AUTH_FAIL_WINDOW_S = 60.0
#: client-key -> list of monotonic failure timestamps (pruned to the window).
_auth_failures: dict[str, list[float]] = {}


def _reset_owner_auth_throttle() -> None:
    """Clear the in-memory owner-auth failure tally. Called when a router is built
    (process start) so the limiter starts clean; also lets tests reset state."""
    _auth_failures.clear()


def _client_key(request: Request) -> str:
    client = request.client
    return client.host if client is not None else "unknown"


async def _require_owner(
    request: Request,
    x_disco_owner_token: str | None = Header(default=None),
) -> None:
    """Owner-only guard applied to EVERY Cloudflare-deploy route. The caller must
    present the server's ``DISCO_ADMIN_TOKEN`` in the ``X-Disco-Owner-Token``
    header. FAIL CLOSED: if the server has no owner token configured, NO request is
    authorized — the deploy surface is unreachable until the operator opts in by
    setting the token. The phrase + plan are thus never served to an
    unauthenticated caller.

    SEC-28: repeated owner-token FAILURES from one client are rate-limited (429),
    and every attempt is logged WITHOUT the token value (the secret never reaches a
    log line)."""
    key = _client_key(request)
    now = time.monotonic()
    fails = [t for t in _auth_failures.get(key, ()) if now - t < _AUTH_FAIL_WINDOW_S]

    if len(fails) >= _AUTH_FAIL_LIMIT:
        _auth_failures[key] = fails  # keep the pruned window; do NOT extend on a throttled hit
        logger.warning(
            "cloudflare deploy owner-auth throttled for client=%s (%d failures in window)",
            key,
            len(fails),
        )
        raise HTTPException(
            status_code=429,
            detail={
                "reason": "too_many_auth_failures",
                "message": (
                    "Too many failed owner-token attempts. Wait and retry with the "
                    "correct DISCO_ADMIN_TOKEN."
                ),
            },
        )

    expected = os.environ.get(_OWNER_TOKEN_ENV, "").strip()
    presented = (x_disco_owner_token or "").strip()
    if not expected or not presented or not hmac.compare_digest(expected, presented):
        fails.append(now)
        _auth_failures[key] = fails
        # NEVER log the presented/expected token value — only the outcome + client.
        logger.warning(
            "cloudflare deploy owner-auth FAILURE for client=%s (%d/%d in window)",
            key,
            len(fails),
            _AUTH_FAIL_LIMIT,
        )
        raise HTTPException(
            status_code=401,
            detail={
                "reason": "owner_auth_required",
                "message": (
                    "The Cloudflare deploy API is owner-only. Present the server's "
                    "DISCO_ADMIN_TOKEN in the X-Disco-Owner-Token header."
                ),
            },
        )
    # Success clears the failure tally so a legitimate operator is never throttled.
    _auth_failures.pop(key, None)


def _owner_allowed_origins() -> frozenset[str]:
    raw = os.environ.get(_OWNER_ORIGINS_ENV, "")
    return frozenset(o.strip() for o in raw.split(",") if o.strip())


class CloudflareDeployCorsMiddleware(BaseHTTPMiddleware):
    """Strict CORS for the owner-only Cloudflare deploy surface (P0-3 / CORR-26).
    The app's global CORS is permissive (``*`` or origin-reflecting) for dev, but
    the deploy routes mutate a live account, so they must NEVER be cross-origin
    reachable by an arbitrary site. Installed OUTERMOST so it post-processes
    responses: for the deploy paths it STRIPS any preexisting
    ``Access-Control-Allow-Origin`` that is wildcard OR reflects an origin NOT in
    the operator's ``DISCO_OWNER_ORIGIN`` allowlist, then reflects an Origin ONLY
    when it is allowlisted (default: none)."""

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        response = await call_next(request)
        if request.url.path.startswith(_CF_DEPLOY_PATH_PREFIX):
            allowed = _owner_allowed_origins()
            # Strip ANY preexisting ACAO that is not an allowlisted origin — this
            # covers the literal wildcard AND an upstream layer reflecting an
            # arbitrary Origin. Only an explicitly allowlisted origin survives.
            existing = response.headers.get("access-control-allow-origin")
            if existing is not None and existing not in allowed:
                del response.headers["access-control-allow-origin"]
                if "access-control-allow-credentials" in response.headers:
                    del response.headers["access-control-allow-credentials"]
            origin = request.headers.get("origin")
            if origin and origin in allowed:
                response.headers["access-control-allow-origin"] = origin
                response.headers["access-control-allow-credentials"] = "true"
                vary = response.headers.get("vary")
                response.headers["vary"] = f"{vary}, Origin" if vary else "Origin"
        return response


class ConnectBody(BaseModel):
    # SEC-27: the CF API token is a SecretStr so it is never echoed in a validation
    # error / model repr; the raw value is pulled only at use via get_secret_value.
    token: SecretStr
    account_id: str


class ConnectionTestBody(BaseModel):
    # Optional ad-hoc token to test BEFORE storing; falls back to the stored one.
    token: SecretStr | None = None


class DeployPlanBody(BaseModel):
    conversation_id: str


class DeployBody(BaseModel):
    conversation_id: str
    dry_run: bool = True  # dry-run is the DEFAULT — a real deploy is opt-in.
    confirmation: str | None = None
    # An explicit autonomous flag also forces refusal; the server env override
    # (DISCO_AUTONOMOUS) takes precedence so a headless run can never deploy.
    autonomous: bool = False
    # The secret to set as the deployed app's ADMIN_TOKEN (protects its admin
    # endpoint). REQUIRED for a real (non-dry-run) deploy — without it the deploy
    # fails closed (RefusalReason.ADMIN_TOKEN_REQUIRED). SEC-27: a bounded SecretStr
    # so a validation error never echoes it. Rides only to `wrangler secret put
    # ADMIN_TOKEN` on stdin; never echoed back, never logged.
    admin_token: SecretStr | None = None


#: CORR-25 — distinct HTTP status per refusal reason. Every hard gate is a 409
#: Conflict (a named precondition the owner must resolve), EXCEPT a genuinely
#: missing/unresolvable workspace, which is a 404 so a client can distinguish it
#: from a refused-but-present export.
_REFUSAL_STATUS: dict[RefusalReason, int] = {
    RefusalReason.NO_WORKSPACE: 404,
    # SEC-25 (cross-process): a deploy refused because ANOTHER server process holds the
    # cross-process advisory lock is a 409 Conflict (transient — retry once it frees),
    # mirroring the per-conversation in-flight 409 below. (409 is also the default, but
    # this is pinned so the multi-worker backstop's status is explicit.)
    RefusalReason.DEPLOY_IN_PROGRESS: 409,
}


def _refusal_status(reason: RefusalReason) -> int:
    return _REFUSAL_STATUS.get(reason, 409)


def _server_autonomous() -> bool:
    return os.environ.get("DISCO_AUTONOMOUS", "").strip().lower() in ("1", "true", "yes", "on")


def _secret_value(secret: SecretStr | None) -> str | None:
    return secret.get_secret_value() if secret is not None else None


def _check_secret_len(value: str | None, field: str) -> None:
    """CORR-25 + SEC-27: reject an over-long secret body field as bad input (400)
    WITHOUT echoing the value (only the field name + the limit appear)."""
    if value is not None and len(value) > _MAX_SECRET_LEN:
        raise HTTPException(
            status_code=400,
            detail={
                "reason": "invalid_input",
                "message": f"{field} exceeds the maximum length of {_MAX_SECRET_LEN}.",
            },
        )


def _validate_account_id(account_id: str) -> str:
    """SEC-29: validate the ``/connect`` account id BEFORE it is stored. Returns the
    cleaned (stripped) id, or raises a 400 ``invalid_account_id`` (the value is NOT
    echoed) for anything that is not a bounded Cloudflare-id-shaped token — empty,
    whitespace, a path/URL fragment, or an over-long blob. Fail closed: a malformed
    account id is never persisted as the connected account."""
    cleaned = (account_id or "").strip()
    if not _ACCOUNT_ID_RE.fullmatch(cleaned):
        raise HTTPException(
            status_code=400,
            detail={
                "reason": "invalid_account_id",
                "message": (
                    "account_id must be a Cloudflare account id — a bounded "
                    "alphanumeric token (letters, digits, '-' or '_'; 8–64 chars)."
                ),
            },
        )
    return cleaned


async def _verify_token_for_account(
    verifier: TokenVerifier, token: str, account_id: str
) -> tuple[bool, str, bool]:
    """SEC-29: verify the token, BOUND to the target account when possible. Returns
    ``(ok, detail, account_scoped)``. If the injected verifier exposes an account-scoped
    check (``verify_for_account(token, account_id)``) the route uses it — a token that
    is not valid FOR this account (lacks the scope) is refused — and ``account_scoped``
    is ``True``. Otherwise it falls back to the account-agnostic ``verify(token)`` (the
    bundled :class:`HttpTokenVerifier`, which hits Cloudflare's account-agnostic
    token-verify endpoint and confirms only that the token is ACTIVE, not that it is
    bound to this account) and ``account_scoped`` is ``False`` so the caller can refuse
    or surface the weaker guarantee rather than silently treat it as account-bound."""
    scoped = getattr(verifier, "verify_for_account", None)
    if callable(scoped):
        ok, detail = await cast("Awaitable[tuple[bool, str]]", scoped(token, account_id))
        return ok, detail, True
    ok, detail = await verifier.verify(token)
    return ok, detail, False


def _require_account_scoped_verify() -> bool:
    """SEC-29 (remainder): the operator policy for an account-agnostic-only verify.
    When ``DISCO_REQUIRE_ACCOUNT_SCOPED_VERIFY`` is truthy a connect that can only be
    verified account-agnostically is REFUSED; default is degrade-but-surface."""
    return os.environ.get(_REQUIRE_SCOPED_VERIFY_ENV, "").strip().lower() in (
        "1",
        "true",
        "yes",
        "on",
    )


def _scrub_paths(message: str | None) -> str | None:
    """SEC-26 (boundary): replace any absolute host path in a client-facing message
    with an opaque ``<path>`` marker, so no host filesystem layout leaks on a
    refusal/error path even if an inner layer included one. ``None`` passes through.
    This is the absolute-only primitive; free-text response fields go through
    :func:`_scrub_text` (a superset that also strips internal relative paths)."""
    if not message:
        return message
    return _ABS_PATH_RE.sub("<path>", message)


def _scrub_text(message: str | None) -> str | None:
    """SEC-26 (remainder): the COMPREHENSIVE client-facing scrub applied to EVERY
    free-text response field (refusal/error ``message``, ``failed_step``/``error_detail``,
    every transcript line, the plan ``export_detail`` + status details) on BOTH the
    success and the failure paths. Strips absolute host paths AND workspace-internal
    relative path structure (``.disco/...``), so neither the host filesystem layout nor
    the private workspace layout leaks in arbitrary free text. ``None``/empty pass
    through. The dedicated ``record_path`` reference is NOT routed through here — it is
    the deliberate workspace-relative handle built by :func:`_relative_record_path`."""
    scrubbed = _scrub_paths(message)
    if not scrubbed:
        return scrubbed
    return _REL_INTERNAL_PATH_RE.sub("<path>", scrubbed)


def _scrub_transcript(transcript: list[str]) -> list[str]:
    """SEC-26 (remainder): scrub EVERY transcript line before it reaches the client
    (the transcript is REDACTED of secrets upstream, but may still carry host/internal
    paths from a wrangler/npm step). An empty result stays an empty string."""
    return [_scrub_text(line) or "" for line in transcript]


def _plan_public(plan: DeployPlan, conversation_id: str) -> dict:
    """The plan as a wire dict — no secret fields exist on a DeployPlan. SEC-26: the
    absolute host ``workspace`` path is NEVER serialised; the plan is referenced by
    its opaque conversation id instead."""
    return {
        "conversation_id": conversation_id,
        "account_id": plan.account_id,
        "worker_name": plan.worker_name,
        "db_name": plan.db_name,
        "spec_digest": plan.spec_digest,
        "tree_digest": plan.tree_digest,
        "plan_hash": plan.plan_hash,
        "export_ready": plan.export_ready,
        # SEC-26 (remainder): export_detail is free text (readiness evidence) that can
        # carry host/internal paths — scrub it before it ships in the plan.
        "export_detail": _scrub_text(plan.export_detail),
        "connected": plan.connected,
        "confirmation_phrase": plan.confirmation_phrase,
        "steps": [
            {"title": s.title, "command": s.command, "mutating": s.mutating, "note": s.note}
            for s in plan.steps
        ],
    }


def _relative_record_path(record_path: str | None, workspace: Path) -> str | None:
    """SEC-26: return the deployment record as a workspace-relative path (an opaque,
    host-anonymous reference) instead of the absolute host path."""
    if not record_path:
        return None
    try:
        return str(Path(record_path).resolve().relative_to(workspace.resolve()))
    except ValueError:
        # Defense in depth: if it somehow is not under the workspace, expose only
        # the basename, never the absolute host path.
        return Path(record_path).name


def _stripe_deploy_context(
    dependencies: StripeDeployDependencies | None, owner_id: str | None, cid: str
) -> StripeDeployContext | None:
    return StripeDeployContext(dependencies, owner_id, cid) if dependencies is not None else None


def _webhook_deploy_context(
    dependencies: WebhookDeployDependencies | None, owner_id: str | None, cid: str
) -> WebhookDeployContext | None:
    return WebhookDeployContext(dependencies, owner_id, cid) if dependencies is not None else None


class _CommittedWorkspaceGate:
    """Bind Cloudflare reads and mutations to one current immutable workspace head."""

    def __init__(self, runtime: ConversationRuntime | None) -> None:
        self._runtime = runtime

    def runtime_required(self) -> ConversationRuntime:
        if self._runtime is None:
            raise HTTPException(
                status_code=409,
                detail={
                    "reason": "workspace_not_committed",
                    "message": "A current immutable workspace commit is required.",
                },
            )
        return self._runtime

    def resolve_workspace(self, conversation_id: str) -> Path:
        runtime = self.runtime_required()
        ps = runtime.project_store()
        if ps is None or ps.status() != StorageStatus.OK:
            raise HTTPException(status_code=404, detail={"reason": "storage_unavailable"})
        try:
            workspace = ps.path_for(conversation_id)
        except Exception as exc:  # noqa: BLE001 — poisoned id → 400
            raise HTTPException(
                status_code=400,
                detail={"reason": "bad_conversation_id", "message": _scrub_text(str(exc))},
            ) from exc
        if not workspace.exists() or not workspace.is_dir():
            raise HTTPException(status_code=404, detail={"reason": "no_workspace"})
        return workspace

    async def require_locked(self, conversation_id: str) -> CommittedWorkspaceView:
        try:
            return await self.runtime_required()._workspace.require_committed_host_mirror_locked(
                conversation_id
            )
        except (WorkspaceCommitUnavailable, RuntimeError) as exc:
            raise HTTPException(
                status_code=409,
                detail={
                    "reason": "workspace_not_committed",
                    "message": _scrub_text(str(exc)),
                },
            ) from exc


async def _build_committed_plan(
    gate: _CommittedWorkspaceGate,
    conversation_id: str,
    secret_store: cf.SecretStore,
) -> DeployPlan:
    runtime = gate.runtime_required()
    workspace = gate.resolve_workspace(conversation_id)
    try:
        async with runtime.workspace_lock(conversation_id):
            async with workspace_process_fence(workspace, wait=False):
                before = await gate.require_locked(conversation_id)
                try:
                    plan = cf.build_plan(workspace, secret_store)
                except DeployRefused as exc:
                    raise HTTPException(
                        status_code=_refusal_status(exc.reason),
                        detail={"reason": exc.reason.value, "message": _scrub_text(exc.detail)},
                    ) from exc
                after = await gate.require_locked(conversation_id)
                if after != before:
                    raise HTTPException(
                        status_code=409,
                        detail={
                            "reason": "workspace_not_committed",
                            "message": "The committed workspace changed while planning; retry.",
                        },
                    )
                return plan
    except WorkspaceProcessBusy as exc:
        raise HTTPException(
            status_code=409,
            detail={"reason": "workspace_busy", "message": "The workspace is changing; retry."},
        ) from exc
    except WorkspaceProcessFenceUnavailable as exc:
        raise HTTPException(
            status_code=503,
            detail={"reason": "workspace_fence_unavailable", "message": _scrub_text(str(exc))},
        ) from exc


async def _execute_committed_deploy(
    gate: _CommittedWorkspaceGate,
    workspace: Path,
    body: DeployBody,
    secret_store: cf.SecretStore,
    *,
    runner: CommandRunner,
    build_backend: BuildBackend | None,
    admin_token: str | None,
    owner_id: str | None,
    stripe_dependencies: StripeDeployDependencies | None,
    webhook_dependencies: WebhookDeployDependencies | None,
) -> cf.DeployExecutionResult:
    cid = body.conversation_id
    runtime = gate.runtime_required()
    try:
        async with runtime.workspace_lock(cid):
            async with workspace_process_fence(workspace, wait=False):
                await gate.require_locked(cid)

                async def begin_mutation() -> None:
                    await gate.require_locked(cid)
                    await runtime.record_workspace_mutation_locked(
                        cid,
                        "cloudflare.deploy-record",
                        paths=(".disco/cloudflare/deployments",),
                    )

                async def finish_mutation() -> None:
                    await runtime.finalize_host_mirror_change_locked(
                        cid, "cloudflare.deploy-record"
                    )

                try:
                    return await cf.execute_deploy(
                        workspace,
                        secret_store,
                        dry_run=body.dry_run,
                        confirmation=body.confirmation,
                        autonomous=body.autonomous or _server_autonomous(),
                        runner=runner,
                        build_backend=build_backend,
                        admin_token=admin_token,
                        stripe_context=_stripe_deploy_context(stripe_dependencies, owner_id, cid),
                        webhook_context=_webhook_deploy_context(
                            webhook_dependencies, owner_id, cid
                        ),
                        on_mutation_start=begin_mutation,
                        on_mutation_finish=finish_mutation,
                    )
                except DeployRefused as exc:
                    raise HTTPException(
                        status_code=_refusal_status(exc.reason),
                        detail={
                            "reason": exc.reason.value,
                            "message": _scrub_text(exc.detail),
                        },
                    ) from exc
                except WorkspaceCommitUnavailable as exc:
                    raise HTTPException(
                        status_code=503,
                        detail={
                            "reason": "workspace_reseal_failed",
                            "message": _scrub_text(str(exc)),
                        },
                    ) from exc
    except WorkspaceProcessBusy as exc:
        raise HTTPException(
            status_code=409,
            detail={"reason": "workspace_busy", "message": "The workspace is changing; retry."},
        ) from exc
    except WorkspaceProcessFenceUnavailable as exc:
        raise HTTPException(
            status_code=503,
            detail={"reason": "workspace_fence_unavailable", "message": _scrub_text(str(exc))},
        ) from exc


def make_cloudflare_router(
    store: SqliteEventStore,
    runtime: ConversationRuntime | None,
    *,
    verifier: TokenVerifier | None = None,
    runner: CommandRunner | None = None,
    build_backend: BuildBackend | None = None,
    stripe_dependencies: StripeDeployDependencies | None = None,
    webhook_dependencies: WebhookDeployDependencies | None = None,
) -> APIRouter:
    """Owner Cloudflare-deploy endpoints. ``runner``/``verifier``/``build_backend``
    are injectable so tests drive fakes; in production they default to the REAL
    subprocess / HTTP / sandbox implementations so a fully-gated, owner-authed,
    confirmed deploy can actually run (P1-4 — the owner path is functional, not a
    dead stub). Every route is owner-gated by ``_require_owner`` (P0-3)."""
    _reset_owner_auth_throttle()
    router = APIRouter(dependencies=[Depends(_require_owner)])
    token_verifier = verifier or HttpTokenVerifier()
    # P1-4: wire the REAL runner by default. Dry-run never touches it, and the four
    # hard gates still bind a real run; tests inject a FakeRunner (no real deploy).
    command_runner: CommandRunner = runner or SubprocessCommandRunner()
    # P0-1: the untrusted `npm run build` runs in an isolating sandbox. Default to
    # the production sandbox backend built from the runtime; tests inject a fake.
    # A real deploy is REFUSED when no isolating backend is available.
    deploy_build_backend: BuildBackend | None = build_backend or build_backend_for_runtime(runtime)
    workspace_gate = _CommittedWorkspaceGate(runtime)
    # SEC-25: per-conversation deploy lock. Two owner deploy requests for the SAME
    # conversation/workspace must NOT interleave (they'd race the build sync-back,
    # D1 provisioning, and wrangler.toml substitution). A conversation id is held
    # for the duration of its deploy; a second concurrent request gets 409. Lives in
    # the router closure (per app), and asyncio's single-threaded scheduling makes
    # the check-then-add atomic (no await between them).
    _deploys_in_flight: set[str] = set()

    def _secret_store() -> cf.SecretStore:
        return cf.SecretStore()

    @router.get("/api/appkit/cloudflare/status")
    async def status() -> dict:
        st = cf.connection_status(_secret_store())
        return {
            "connected": st.connected,
            "token_present": st.token_present,
            "decryptable": st.decryptable,
            "account_id": st.account_id,
            # SEC-26 (remainder): scrub the free-text detail on every client-facing path.
            "detail": _scrub_text(st.detail),
        }

    @router.post("/api/appkit/cloudflare/connect")
    async def connect(body: ConnectBody) -> dict:
        store_ = _secret_store()
        if not store_.can_store:
            raise HTTPException(
                status_code=400,
                detail={
                    "reason": "secret_store_locked",
                    "message": "DISCO_SECRET_KEY is unset — cannot encrypt the token.",
                },
            )
        # SEC-29: validate + bound the account id BEFORE any store. A malformed /
        # unbounded account id is rejected (400) and never persisted.
        account_id = _validate_account_id(body.account_id)
        token = _secret_value(body.token)
        _check_secret_len(token, "token")
        # CORR-27 + SEC-29: VERIFY the token — bound to THIS account when the verifier
        # supports it — BEFORE storing it. Never mark an account "connected" on an
        # unverified credential, or on a token that is not valid for this account.
        ok, detail, account_scoped = await _verify_token_for_account(
            token_verifier, token or "", account_id
        )
        if not ok:
            raise HTTPException(
                status_code=400,
                detail={"reason": "token_verification_failed", "message": _scrub_text(detail)},
            )
        # SEC-29 (remainder): an account-agnostic-only verify proves the token is ACTIVE
        # but NOT that it is bound to THIS account. Per operator policy either REFUSE the
        # connect (require an account-scoped verifier) or degrade-but-SURFACE — never
        # silently treat the agnostic result as account-bound.
        if not account_scoped and _require_account_scoped_verify():
            raise HTTPException(
                status_code=400,
                detail={
                    "reason": "account_scope_unverifiable",
                    "message": (
                        "The token verified as active but could NOT be verified as scoped "
                        "to this account (no account-scoped verifier is available, and "
                        "DISCO_REQUIRE_ACCOUNT_SCOPED_VERIFY is set). Refusing to connect."
                    ),
                },
            )
        try:
            st = cf.connect_account(store_, token=token or "", account_id=account_id)
        except WeakSecretError as exc:
            # SEC-30: deploy credentials are refused under a weak/absent app secret.
            raise HTTPException(
                status_code=400,
                detail={"reason": "weak_app_secret", "message": _scrub_text(str(exc))},
            ) from exc
        except ValueError as exc:
            raise HTTPException(
                status_code=400,
                detail={"reason": "invalid_input", "message": _scrub_text(str(exc))},
            ) from exc
        # NOTE: never echo the token back. ``account_scoped`` surfaces whether the token
        # was verified AS bound to this account (True) or only account-agnostically
        # active (False) — the caller is never silently told an agnostic verify is bound.
        return {
            "connected": st.connected,
            "account_id": st.account_id,
            "detail": _scrub_text(st.detail),
            "account_scoped": account_scoped,
        }

    @router.post("/api/appkit/cloudflare/disconnect")
    async def disconnect() -> dict:
        st = cf.disconnect_account(_secret_store())
        return {"connected": st.connected, "detail": _scrub_text(st.detail)}

    @router.post("/api/appkit/cloudflare/connection-test")
    async def connection_test(body: ConnectionTestBody) -> dict:
        store_ = _secret_store()
        ad_hoc = _secret_value(body.token)
        _check_secret_len(ad_hoc, "token")
        token = ad_hoc or store_.get_secret(cf.CF_TOKEN_SECRET)
        if not token:
            raise HTTPException(
                status_code=400,
                detail={"reason": "no_token", "message": "No token to test — connect first."},
            )
        ok, detail = await token_verifier.verify(token)
        return {"ok": ok, "detail": _scrub_text(detail)}

    @router.post("/api/appkit/cloudflare/deploy-plan")
    async def deploy_plan(body: DeployPlanBody) -> dict:
        plan = await _build_committed_plan(
            workspace_gate,
            body.conversation_id,
            _secret_store(),
        )
        return _plan_public(plan, body.conversation_id)

    @router.post("/api/appkit/cloudflare/deploy")
    async def deploy_(body: DeployBody) -> dict:
        cid = body.conversation_id
        # SEC-25: refuse a second concurrent deploy for the same conversation. The
        # check-then-add pair runs with no await between them, so it is atomic under
        # asyncio — exactly one in-flight deploy per conversation.
        if cid in _deploys_in_flight:
            raise HTTPException(
                status_code=409,
                detail={
                    "reason": "deploy_in_progress",
                    "message": (
                        "A deploy is already in progress for this conversation. Wait "
                        "for it to finish before starting another."
                    ),
                },
            )
        _deploys_in_flight.add(cid)
        try:
            workspace = workspace_gate.resolve_workspace(cid)
            owner_id = await store.conversation_owner_id(cid)
            admin_token = _secret_value(body.admin_token)
            _check_secret_len(admin_token, "admin_token")
            result = await _execute_committed_deploy(
                workspace_gate,
                workspace,
                body,
                _secret_store(),
                runner=command_runner,
                build_backend=deploy_build_backend,
                admin_token=admin_token,
                owner_id=owner_id,
                stripe_dependencies=stripe_dependencies,
                webhook_dependencies=webhook_dependencies,
            )

            # CORR-25 — a real deploy whose step ABORTED/failed must NOT be 200. It
            # is an upstream (wrangler/sandbox) failure → 502, carrying the failed
            # step + detail so the client can distinguish a partial failure from a
            # clean run or a refusal. SEC-26 (boundary): the failed-step / error
            # detail are scrubbed of any absolute host path before they reach the client.
            if not result.dry_run and not result.succeeded:
                raise HTTPException(
                    status_code=502,
                    detail={
                        "reason": "deploy_failed",
                        "message": "A deploy step failed; the deploy was aborted.",
                        "failed_step": _scrub_text(result.failed_step),
                        "error_detail": _scrub_text(result.error_detail),
                    },
                )

            return {
                "executed": result.executed,
                "dry_run": result.dry_run,
                "succeeded": result.succeeded,
                # SEC-26 (remainder): scrub the free-text result fields on the SUCCESS
                # path too — a host/internal path in a step label or detail must not leak.
                "failed_step": _scrub_text(result.failed_step),
                "error_detail": _scrub_text(result.error_detail),
                "deployed_url": result.deployed_url,
                # SEC-26: a workspace-relative record path, never the absolute host path.
                "record_path": _relative_record_path(result.record_path, workspace),
                "plan": _plan_public(result.plan, cid),
                # SEC-26 (remainder): scrub every transcript line (host/internal paths).
                "transcript": _scrub_transcript(result.transcript),
            }
        finally:
            _deploys_in_flight.discard(cid)

    return router


__all__ = ["CloudflareDeployCorsMiddleware", "make_cloudflare_router"]
