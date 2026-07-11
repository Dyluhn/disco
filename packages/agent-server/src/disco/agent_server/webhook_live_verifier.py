"""Mandatory live exploit verifier for the WO-F3.3 webhook primitive."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import secrets
import shutil
import socket
import tempfile
import time
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from disco.core.appkit.primitives import PrimitiveVerifyResult, VerifyCheck
from disco.core.appkit.spec import AppSpec, DesignSpec
from disco.core.host_egress import validate_untrusted_url
from disco.core.host_services import HostServiceContext, call_host_service
from disco.core.llm.config_store import ConfigStore
from disco.core.llm.secrets import SecretBox, SecretStore
from disco.core.webhook_host_service import (
    WEBHOOK_EMIT_SERVICE_NAME,
    WEBHOOK_PURPOSE,
    WebhookAppConfigStore,
)

from .stripe_live_verifier import (
    LiveWorkerdApp,
    find_wrangler,
    free_loopback_port,
    make_loopback_tls_pair,
    scan_live_paths_for_secrets,
)

_LOG = logging.getLogger(__name__)
_LIVE_ID = "webhook.security.v1"
_REQUIRED_CHECKS = (
    "forged_signature_rejected",
    "replay_deduped",
    "egress_blocked",
    "secret_absence",
)
_SECRET_PATTERNS = (
    r"(?i)(?:sk_(?:live|test)|rk_(?:live|test)|whsec_)[A-Za-z0-9_-]{4,}",
    r"(?i)webhook_secret_[A-Za-z0-9_-]{4,}",
)


def _check(name: str, passed: bool, evidence: str) -> VerifyCheck:
    return VerifyCheck(name=name, passed=passed, evidence=evidence)


def _result(checks: list[VerifyCheck]) -> PrimitiveVerifyResult:
    by_name = {item.name: item for item in checks}
    ordered = tuple(
        by_name.get(name, _check(name, False, "check missing")) for name in _REQUIRED_CHECKS
    )
    return PrimitiveVerifyResult(
        ok=all(item.passed for item in ordered),
        detail=f"{sum(item.passed for item in ordered)}/{len(ordered)} checks passed",
        checks=ordered,
    )


def _signed_header(secret: str, endpoint_id: str, body: bytes, timestamp: int) -> str:
    signed = f"{timestamp}.{endpoint_id}.".encode() + body
    digest = hmac.new(secret.encode(), signed, hashlib.sha256).hexdigest()
    return f"t={timestamp},v1={digest}"


class _WebhookVerifyRun:
    def __init__(self, app: AppSpec, design: DesignSpec, tree: Mapping[str, str]) -> None:
        self.app = app
        self.design = design
        self.tree = tree
        self.tmp_root: Path | None = None
        self.worker: LiveWorkerdApp | None = None
        self.secret_store: SecretStore | None = None
        self.config_store: ConfigStore | None = None
        self.webhook_configs: WebhookAppConfigStore | None = None
        self.signing_secret = ""
        self.outbound_secret = ""
        self.owner_id = "webhook-live-owner"

    async def run(self) -> PrimitiveVerifyResult:
        checks: list[VerifyCheck] = []
        try:
            endpoint_id = self._ensure_prerequisites()
            self._setup()
            assert self.tmp_root is not None
            assert self.worker is not None
            self.worker.__enter__()
            try:
                self.worker.boot(free_loopback_port())
                checks.append(await self._forged_check(endpoint_id))
                checks.append(await self._replay_check(endpoint_id))
                checks.append(await self._egress_check())
                checks.append(self._secret_check())
            finally:
                try:
                    self.worker.__exit__(None, None, None)
                finally:
                    self.worker = None
        except Exception as exc:  # noqa: BLE001 - host verifier fails closed, sanitized
            _LOG.exception("Webhook live verifier failed")
            safe = f"host live verifier raised {type(exc).__name__} (fail-closed)"
            checks.extend(_check(name, False, safe) for name in _REQUIRED_CHECKS)
        finally:
            self._cleanup()
        return _result(checks)

    def _ensure_prerequisites(self) -> str:
        meta = self.app.webhooks
        if self.app.app_kind != "records" or meta is None:
            raise RuntimeError("Webhook live verifier requires records webhook metadata")
        inbound = [item.endpoint_id for item in meta.endpoints if item.direction == "inbound"]
        if not inbound:
            raise RuntimeError("Webhook live verifier requires an inbound endpoint")
        if find_wrangler(Path.cwd()) is None:
            raise RuntimeError("wrangler executable is unavailable")
        return inbound[0]

    def _setup(self) -> None:
        meta = self.app.webhooks
        assert meta is not None
        self.tmp_root = Path(tempfile.mkdtemp(prefix="disco-webhook-live-"))
        app_secret = base64.b64encode(secrets.token_bytes(32)).decode("ascii")
        self.secret_store = SecretStore(
            path=self.tmp_root / "secrets.json", box=SecretBox(app_secret)
        )
        self.config_store = ConfigStore(path=self.tmp_root / "config.json")
        self.webhook_configs = WebhookAppConfigStore(self.tmp_root / "webhooks.db")
        self.signing_secret = "inbound_" + secrets.token_urlsafe(40)
        self.outbound_secret = "outbound_" + secrets.token_urlsafe(40)
        ca_cert, _ca_key = make_loopback_tls_pair(self.tmp_root)
        self.worker = LiveWorkerdApp(
            self.tree,
            {
                "WEBHOOK_SIGNING_SECRET": self.signing_secret,
                "WEBHOOK_RUNTIME_READY": "1",
            },
            self.tmp_root,
            ca_cert,
        )

    async def _forged_check(self, endpoint_id: str) -> VerifyCheck:
        assert self.worker is not None
        body = json.dumps(
            {"id": "evt_forged", "type": self._event_type(endpoint_id), "data": {}},
            separators=(",", ":"),
        ).encode()
        path = f"/api/webhooks/{endpoint_id}"
        before_events = await self.worker.d1_count_async("webhook_events")
        before_effects = await self.worker.d1_count_async("webhook_effects")
        now = int(time.time())
        forged_status, _ = await self.worker.request_async(
            "POST",
            path,
            body,
            {
                "Content-Type": "application/json",
                "Disco-Webhook-Signature": _signed_header(
                    "wrong-" + self.signing_secret, endpoint_id, body, now
                ),
            },
        )
        stale_status, _ = await self.worker.request_async(
            "POST",
            path,
            body,
            {
                "Content-Type": "application/json",
                "Disco-Webhook-Signature": _signed_header(
                    self.signing_secret, endpoint_id, body, now - 301
                ),
            },
        )
        after_events = await self.worker.d1_count_async("webhook_events")
        after_effects = await self.worker.d1_count_async("webhook_effects")
        passed = (
            forged_status == 401
            and stale_status == 401
            and (before_events, before_effects) == (after_events, after_effects) == (0, 0)
        )
        return _check(
            "forged_signature_rejected",
            passed,
            "forged and stale signatures returned 401 with zero event/effect rows"
            if passed
            else (
                f"statuses={(forged_status, stale_status)} "
                f"counts={(before_events, before_effects, after_events, after_effects)}"
            ),
        )

    async def _replay_check(self, endpoint_id: str) -> VerifyCheck:
        assert self.worker is not None
        body = json.dumps(
            {"id": "evt_replay", "type": self._event_type(endpoint_id), "data": {"n": 1}},
            separators=(",", ":"),
        ).encode()
        headers = {
            "Content-Type": "application/json",
            "Disco-Webhook-Signature": _signed_header(
                self.signing_secret, endpoint_id, body, int(time.time())
            ),
        }
        path = f"/api/webhooks/{endpoint_id}"
        first, _ = await self.worker.request_async("POST", path, body, headers)
        second, _ = await self.worker.request_async("POST", path, body, headers)
        events = await self.worker.d1_count_async("webhook_events")
        effects = await self.worker.d1_count_async("webhook_effects")
        passed = first == second == 200 and events == effects == 1
        return _check(
            "replay_deduped",
            passed,
            "the valid replay was acknowledged twice with exactly one atomic effect"
            if passed
            else f"statuses={(first, second)} counts={(events, effects)}",
        )

    async def _egress_check(self) -> VerifyCheck:
        assert self.app.webhooks is not None
        assert self.secret_store is not None
        assert self.config_store is not None
        assert self.webhook_configs is not None
        endpoint = next(
            (item for item in self.app.webhooks.endpoints if item.direction == "outbound"),
            None,
        )
        endpoint_id = endpoint.endpoint_id if endpoint is not None else "live_outbound"
        target = "https://169.254.169.254/latest/meta-data/"
        config = self.webhook_configs.configure(
            owner_id=self.owner_id,
            audience=self.app.webhooks.app_binding,
            endpoint_id=endpoint_id,
            target_url=target,
            signing_secret=self.outbound_secret,
            event_types=frozenset(endpoint.event_types if endpoint is not None else ("live.test",)),
            enabled=True,
            secret_store=self.secret_store,
        )
        approvals = self.config_store.approval_store(secret_store=self.secret_store)
        approvals.approve(target, WEBHOOK_PURPOSE, config.secret_ref)
        ctx = HostServiceContext(
            secret_store=self.secret_store,
            approvals=approvals,
            allow_hosts=None,
            app_id=self.app.webhooks.app_binding,
            owner_id=self.owner_id,
            allowed_services=frozenset({WEBHOOK_EMIT_SERVICE_NAME}),
            webhook_config_store=self.webhook_configs,
        )
        payload = {
            "app_binding": self.app.webhooks.app_binding,
            "endpoint_id": endpoint_id,
            "event_type": endpoint.event_types[0] if endpoint is not None else "live.test",
            "data": {},
        }
        metadata = await call_host_service(WEBHOOK_EMIT_SERVICE_NAME, payload, ctx)

        # Controlled rebinding proof: the same hostname first validates to a
        # global address, then resolves private at the actual guarded request.
        rebind_target = "https://rebind.invalid/hook"
        config = self.webhook_configs.configure(
            owner_id=self.owner_id,
            audience=self.app.webhooks.app_binding,
            endpoint_id=endpoint_id,
            target_url=rebind_target,
            signing_secret=self.outbound_secret,
            event_types=frozenset(endpoint.event_types if endpoint is not None else ("live.test",)),
            enabled=True,
            secret_store=self.secret_store,
        )
        approvals.approve(rebind_target, WEBHOOK_PURPOSE, config.secret_ref)
        original = socket.getaddrinfo
        calls = 0

        def rebinding_resolver(
            host: str, port: int, *args: Any, **kwargs: Any
        ) -> list[tuple[Any, ...]]:
            nonlocal calls
            if host != "rebind.invalid":
                return original(host, port, *args, **kwargs)
            calls += 1
            address = "93.184.216.34" if calls == 1 else "127.0.0.1"
            return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (address, port))]

        socket.getaddrinfo = rebinding_resolver
        try:
            validate_untrusted_url(rebind_target)
            rebound = await call_host_service(WEBHOOK_EMIT_SERVICE_NAME, payload, ctx)
        finally:
            socket.getaddrinfo = original
        passed = (
            metadata == {"ok": False, "error": "webhook_egress_denied"}
            and rebound == {"ok": False, "error": "webhook_egress_denied"}
            and calls == 2
        )
        return _check(
            "egress_blocked",
            passed,
            "metadata IP and public-then-private DNS rebind were blocked by guarded_request"
            if passed
            else f"metadata={metadata!r} rebound={rebound!r} resolver_calls={calls}",
        )

    def _secret_check(self) -> VerifyCheck:
        assert self.worker is not None
        assert self.tmp_root is not None
        paths = [self.worker.app_dir, self.tmp_root]
        if self.worker.bundle_dir is not None:
            paths.append(self.worker.bundle_dir)
        if self.worker.dev_log_path is not None:
            paths.append(self.worker.dev_log_path)
        exact = [
            self.signing_secret,
            self.outbound_secret,
            self.secret_store.signing_secret if self.secret_store is not None else "",
        ]
        hits = scan_live_paths_for_secrets(paths, exact, _SECRET_PATTERNS)
        for contents in self.tree.values():
            if any(value and value in contents for value in exact):
                hits.append("tree: exact secret")
        return _check(
            "secret_absence",
            not hits,
            "no webhook secret in emitted tree, dry-run bundle, logs, or local state"
            if not hits
            else "; ".join(sorted(set(hits))),
        )

    def _event_type(self, endpoint_id: str) -> str:
        assert self.app.webhooks is not None
        endpoint = next(
            item for item in self.app.webhooks.endpoints if item.endpoint_id == endpoint_id
        )
        return endpoint.event_types[0]

    def _cleanup(self) -> None:
        if self.worker is not None:
            try:
                self.worker.__exit__(None, None, None)
            except Exception:  # noqa: BLE001 - best effort after recorded failure
                pass
            self.worker = None
        if self.webhook_configs is not None:
            self.webhook_configs.close()
            self.webhook_configs = None
        if self.tmp_root is not None:
            shutil.rmtree(self.tmp_root, ignore_errors=True)
            self.tmp_root = None


async def _webhook_live_verify(
    app: AppSpec, design: DesignSpec, tree: Mapping[str, str]
) -> PrimitiveVerifyResult:
    return await _WebhookVerifyRun(app, design, tree).run()


def make_webhook_live_verifier() -> Any:
    async def verifier(
        live_id: str, app: AppSpec, design: DesignSpec, tree: Mapping[str, str]
    ) -> PrimitiveVerifyResult:
        if live_id != _LIVE_ID:
            return _result(
                [
                    _check(name, False, f"unknown live_verify_id {live_id!r}")
                    for name in _REQUIRED_CHECKS
                ]
            )
        return await _webhook_live_verify(app, design, tree)

    return verifier


__all__ = ["make_webhook_live_verifier"]
