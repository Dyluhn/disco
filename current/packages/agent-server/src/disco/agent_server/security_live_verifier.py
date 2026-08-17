"""Dispatch mandatory AppKit exploit verifiers by their exact versioned id."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from disco.core.appkit.primitives import PrimitiveVerifyResult, VerifyCheck
from disco.core.appkit.spec import AppSpec, DesignSpec

from .stripe_live_verifier import make_stripe_live_verifier
from .webhook_live_verifier import make_webhook_live_verifier


def make_security_live_verifier() -> Any:
    stripe = make_stripe_live_verifier()
    webhook = make_webhook_live_verifier()

    async def verifier(
        live_id: str,
        app: AppSpec,
        design: DesignSpec,
        tree: Mapping[str, str],
    ) -> PrimitiveVerifyResult:
        if live_id == "stripe.security.v1":
            return await stripe(live_id, app, design, tree)
        if live_id == "webhook.security.v1":
            return await webhook(live_id, app, design, tree)
        return PrimitiveVerifyResult(
            ok=False,
            detail="unknown mandatory live verifier (fail-closed)",
            checks=(
                VerifyCheck(
                    name="unknown_live_verify_id",
                    passed=False,
                    evidence=f"unknown live_verify_id {live_id!r}",
                ),
            ),
        )

    return verifier


__all__ = ["make_security_live_verifier"]
