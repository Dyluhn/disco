"""Stripe live verifier exploit checks — forged-signature, replay, and secret-absence.

This module owns the individual check functions that do not require
monkeypatched host-service imports: forged-signature-rejected, replay-deduped,
and secret-absence.  The restricted-key-only and price-injection-refused
checks remain in ``stripe_live_verifier.py`` because their tests monkeypatch
host-service imports at that module's namespace.  The webhook-lifecycle check
lives in ``webhook_lifecycle.py``.
"""

from __future__ import annotations

import json
import re as _re
import time
from collections.abc import Mapping, Sequence
from pathlib import Path

from disco.core.appkit.primitives import VerifyCheck

from .evidence import (
    _SECRET_PATTERNS,
    _SIGNATURE_TOLERANCE_S,
    _check_result,
    _checkout_event,
    _scan_for_secrets,
    _signed_header,
)
from .worker_endpoint import WorkerEndpoint
from .workerd_lifecycle import _WorkerdApp


async def _check_forged_signature_rejected(
    worker: WorkerEndpoint,
    webhook_secret: str,
    app_binding: str,
    plan_selector: str,
    binding_secret: str,
) -> VerifyCheck:
    """Forged or stale signatures must return 400 with zero writes."""
    now = int(time.time())
    forged_body = _checkout_event("evt_forged", now, app_binding, plan_selector, binding_secret, 1)
    forged_status, _ = await worker.request_async(
        "POST",
        "/api/stripe/webhook",
        body=forged_body,
        headers={
            "Content-Type": "application/json",
            "Stripe-Signature": f"t={now},v1={'0' * 64}",
        },
    )
    stale_ts = now - _SIGNATURE_TOLERANCE_S - 1
    stale_body = _checkout_event(
        "evt_stale", stale_ts, app_binding, plan_selector, binding_secret, 1
    )
    stale_status, _ = await worker.request_async(
        "POST",
        "/api/stripe/webhook",
        body=stale_body,
        headers={
            "Content-Type": "application/json",
            "Stripe-Signature": _signed_header(webhook_secret, stale_body, stale_ts),
        },
    )
    events = await worker.d1_count_async("stripe_events")
    fulfillments = await worker.d1_count_async("stripe_fulfillments")
    grants = await worker.d1_count_async("user_role_grants")
    if (
        forged_status == 400
        and stale_status == 400
        and events == 0
        and fulfillments == 0
        and grants == 0
    ):
        return _check_result(
            "forged_signature_rejected",
            True,
            "forged and stale signatures returned 400 with zero fulfillment or grant rows",
        )
    return _check_result(
        "forged_signature_rejected",
        False,
        (
            f"forged={forged_status}, stale={stale_status}, "
            f"stripe_events={events}, stripe_fulfillments={fulfillments}, "
            f"user_role_grants={grants}"
        ),
    )


async def _check_replay_deduped(
    worker: WorkerEndpoint,
    webhook_secret: str,
    app_binding: str,
    plan_selector: str,
    binding_secret: str,
    admin_token: str,
) -> VerifyCheck:
    """A valid signed event replayed once must produce exactly one grant."""
    status, _ = await worker.post_json_async(
        "/api/register",
        {
            "email": "payer@example.com",
            "password": "correct horse battery staple",
            "role": "member",
        },
        token=admin_token,
    )
    if status != 201:
        return _check_result(
            "replay_deduped",
            False,
            f"test user registration failed (status {status})",
        )
    login_status, _ = await worker.post_json_async(
        "/api/login",
        {"email": "payer@example.com", "password": "correct horse battery staple"},
    )
    if login_status != 200:
        return _check_result(
            "replay_deduped",
            False,
            f"test user login failed (status {login_status})",
        )
    now = int(time.time())
    body = _checkout_event("evt_replay", now, app_binding, plan_selector, binding_secret, 1)
    header = _signed_header(webhook_secret, body, now)
    first_status, _ = await worker.request_async(
        "POST",
        "/api/stripe/webhook",
        body=body,
        headers={"Content-Type": "application/json", "Stripe-Signature": header},
    )
    first_events = await worker.d1_count_async("stripe_events")
    first_fulfillments = await worker.d1_count_async("stripe_fulfillments")
    first_grants = await worker.d1_count_async("user_role_grants")
    second_status, _ = await worker.request_async(
        "POST",
        "/api/stripe/webhook",
        body=body,
        headers={"Content-Type": "application/json", "Stripe-Signature": header},
    )
    status, text = await worker.get_async("/api/stripe/status")
    entitled = False
    if status == 200:
        try:
            data = json.loads(text)
            entitled = bool(data.get("entitled"))
        except (json.JSONDecodeError, AttributeError):
            pass
    events = await worker.d1_count_async("stripe_events")
    fulfillments = await worker.d1_count_async("stripe_fulfillments")
    grants = await worker.d1_count_async("user_role_grants")
    if (
        first_status == 200
        and second_status == 200
        and entitled
        and (first_events, first_fulfillments, first_grants) == (1, 1, 1)
        and events == 1
        and fulfillments == 1
        and grants == 1
    ):
        return _check_result(
            "replay_deduped",
            True,
            (
                "valid signed event replayed once produced exactly one event, "
                "one fulfillment, and one grant"
            ),
        )
    return _check_result(
        "replay_deduped",
        False,
        f"first={first_status}, second={second_status}, entitled={entitled}, "
        f"first_counts={(first_events, first_fulfillments, first_grants)}, "
        f"stripe_events={events}, stripe_fulfillments={fulfillments}, user_role_grants={grants}",
    )


async def _check_secret_absence(
    tree: Mapping[str, str],
    worker: _WorkerdApp,
    exact_secrets: Sequence[str],
    private_root: Path,
) -> VerifyCheck:
    """No Stripe secret value or secret-shaped pattern may leak to tree/bundle/logs/state."""
    scan_paths: list[Path] = [worker.app_dir, private_root]
    if worker.bundle_dir is not None:
        scan_paths.append(worker.bundle_dir)
    if worker.dev_log_path is not None:
        scan_paths.append(worker.dev_log_path)
    hits = _scan_for_secrets(scan_paths, exact_secrets, _SECRET_PATTERNS)
    # Also scan the emitted tree directly (it is already in app_dir, but the caller's
    # tree mapping is the authoritative source before materialization).
    for contents in tree.values():
        for secret in exact_secrets:
            if secret and secret in contents:
                hits.append("tree: exact secret value")
                break
        for pattern in _SECRET_PATTERNS:
            if _re.search(pattern, contents):
                hits.append(f"tree: pattern {pattern!r}")
    if not hits:
        return _check_result(
            "secret_absence",
            True,
            (
                "no Stripe secret values or secret-shaped patterns in "
                "tree, bundle, logs, or local state"
            ),
        )
    return _check_result("secret_absence", False, "; ".join(sorted(set(hits))))
