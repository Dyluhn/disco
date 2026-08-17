"""Owner-admin Stripe configuration; credential input is strictly write-only."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request

from ..auth import current_owner_id
from ..config.dtos import StripeConfigBody, StripeConfigStatus
from ..config_state import ConfigState


def make_stripe_router(state: ConfigState) -> APIRouter:
    router = APIRouter()

    @router.put("/api/stripe/config/{audience}")
    async def configure_stripe(
        audience: str,
        body: StripeConfigBody,
        request: Request,
    ) -> StripeConfigStatus:
        try:
            config = state.integrations.configure_stripe_app(
                owner_id=current_owner_id(request),
                audience=audience,
                restricted_key=body.restricted_key.get_secret_value(),
                webhook_secret=body.webhook_secret.get_secret_value(),
                plan_selector=body.plan_selector,
                stripe_price_id=body.stripe_price_id,
                allowed_return_origins=frozenset(body.allowed_return_origins),
                enabled=body.enabled,
            )
        except (RuntimeError, ValueError) as exc:
            raise HTTPException(
                status_code=400,
                detail={"reason": "invalid_stripe_config", "message": str(exc)},
            ) from exc
        return StripeConfigStatus(
            audience=config.audience,
            plan_selector=config.plan_selector,
            allowed_return_origins=sorted(config.allowed_return_origins),
            enabled=config.enabled,
            credential_configured=True,
            webhook_configured=True,
        )

    return router


__all__ = ["make_stripe_router"]
