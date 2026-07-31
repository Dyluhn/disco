"""Webhook-lifecycle owner — prove the live bundle cannot resurrect a revoked entitlement.

This module owns the ``_check_webhook_lifecycle`` check and its helpers.  The
check deliberately retains the lifecycle proof that complements the five
mandatory exploit checks: an authentic but unpaid event is a no-op, an
unrelated correlation is a no-op, revocation wins a same-timestamp stale
completion, and an async-payment success can re-grant afterwards.
"""

from __future__ import annotations

import json
import time
from collections.abc import Mapping

from disco.core.appkit.primitives import VerifyCheck

from .evidence import (
    _check_result,
    _signed_header,
    _stripe_event_body,
    _stripe_metadata,
)
from .worker_endpoint import WorkerEndpoint


class _LifecycleContext:
    """Per-run mutable state for the webhook-lifecycle check."""

    def __init__(
        self,
        worker: WorkerEndpoint,
        webhook_secret: str,
        app_binding: str,
        plan_selector: str,
        binding_secret: str,
    ) -> None:
        self._worker = worker
        self._webhook_secret = webhook_secret
        self._metadata = _stripe_metadata(app_binding, plan_selector, binding_secret, 1)
        self._now = int(time.time())

    def _checkout_body(
        self,
        event_id: str,
        created: int,
        *,
        event_type: str = "checkout.session.completed",
        payment_status: str = "paid",
        event_metadata: Mapping[str, str] | None = None,
    ) -> bytes:
        metadata = event_metadata if event_metadata is not None else self._metadata
        return _stripe_event_body(
            event_id,
            event_type,
            created,
            {
                "id": "cs_live_session",
                "subscription": "sub_live_subscription",
                "client_reference_id": "1",
                "payment_status": payment_status,
                "metadata": dict(metadata),
            },
        )

    async def _deliver(self, body: bytes) -> int:
        status, _ = await self._worker.request_async(
            "POST",
            "/api/stripe/webhook",
            body=body,
            headers={
                "Content-Type": "application/json",
                "Stripe-Signature": _signed_header(self._webhook_secret, body, self._now),
            },
        )
        return status

    async def _entitled(self) -> bool:
        status, body = await self._worker.get_async("/api/stripe/status")
        if status != 200:
            return False
        try:
            payload = json.loads(body)
        except json.JSONDecodeError:
            return False
        return isinstance(payload, dict) and payload.get("entitled") is True

    async def _counts(self) -> tuple[int, int, int]:
        worker = self._worker
        return (
            await worker.d1_count_async("stripe_events"),
            await worker.d1_count_async("stripe_fulfillments"),
            await worker.d1_count_async("user_role_grants"),
        )


async def _check_webhook_lifecycle(
    worker: WorkerEndpoint,
    webhook_secret: str,
    app_binding: str,
    plan_selector: str,
    binding_secret: str,
) -> VerifyCheck:
    """Prove the live bundle cannot resurrect a revoked entitlement.

    This deliberately retains the lifecycle proof that complements the five
    mandatory exploit checks: an authentic but unpaid event is a no-op, an
    unrelated correlation is a no-op, revocation wins a same-timestamp stale
    completion, and an async-payment success can re-grant afterwards.
    """
    ctx = _LifecycleContext(worker, webhook_secret, app_binding, plan_selector, binding_secret)
    now = ctx._now

    initial = await ctx._counts()
    unpaid_status = await ctx._deliver(
        ctx._checkout_body("evt_lifecycleunpaid", now + 10, payment_status="unpaid")
    )
    mismatched_status = await ctx._deliver(
        ctx._checkout_body(
            "evt_lifecyclemismatch",
            now + 20,
            event_metadata={
                "disco_app_binding": app_binding,
                "disco_plan_selector": plan_selector,
                "disco_correlation": "0" * 64,
            },
        )
    )
    after_noops = await ctx._counts()
    paid_status = await ctx._deliver(ctx._checkout_body("evt_lifecyclepaid", now + 100))
    after_paid = await ctx._entitled()

    revoked_body = _stripe_event_body(
        "evt_lifecyclerevoked",
        "customer.subscription.deleted",
        now + 200,
        {"id": "sub_live_subscription"},
    )
    revoked_status = await ctx._deliver(revoked_body)
    after_revoke = await ctx._entitled()

    stale_status = await ctx._deliver(ctx._checkout_body("evt_lifecyclestale", now + 200))
    after_stale = await ctx._entitled()
    async_status = await ctx._deliver(
        ctx._checkout_body(
            "evt_lifecycleasyncsuccess",
            now + 300,
            event_type="checkout.session.async_payment_succeeded",
        )
    )
    after_async = await ctx._entitled()
    final = await ctx._counts()
    statuses = (
        unpaid_status,
        mismatched_status,
        paid_status,
        revoked_status,
        stale_status,
        async_status,
    )
    expected_final = (initial[0] + 4, initial[1], initial[2])
    if (
        unpaid_status == 200
        and mismatched_status == 200
        and after_noops == initial
        and paid_status == 200
        and after_paid
        and revoked_status == 200
        and not after_revoke
        and stale_status == 200
        and not after_stale
        and async_status == 200
        and after_async
        and final == expected_final
    ):
        return _check_result(
            "webhook_lifecycle",
            True,
            (
                "unpaid/unrelated events were no-ops; revoke resisted stale completion; "
                "async success re-granted"
            ),
        )
    return _check_result(
        "webhook_lifecycle",
        False,
        (
            f"statuses={statuses}, "
            f"entitled={(after_paid, after_revoke, after_stale, after_async)}, "
            f"counts={(initial, after_noops, final)}, expected_final={expected_final}"
        ),
    )
