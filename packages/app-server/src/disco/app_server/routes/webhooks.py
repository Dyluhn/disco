"""Owner-admin generic webhook configuration; all credentials are write-only."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request

from ..auth import current_owner_id
from ..config.dtos import (
    WebhookInboundConfigBody,
    WebhookInboundConfigStatus,
    WebhookOutboundConfigBody,
    WebhookOutboundConfigStatus,
)
from ..config_state import ConfigState


def make_webhooks_router(state: ConfigState) -> APIRouter:
    router = APIRouter()

    @router.put("/api/webhooks/config/{audience}/inbound")
    async def configure_inbound(
        audience: str,
        body: WebhookInboundConfigBody,
        request: Request,
    ) -> WebhookInboundConfigStatus:
        try:
            state.integrations.configure_webhook_inbound(
                owner_id=current_owner_id(request),
                audience=audience,
                signing_secret=body.signing_secret.get_secret_value(),
            )
        except (RuntimeError, ValueError) as exc:
            raise HTTPException(
                status_code=400,
                detail={"reason": "invalid_webhook_config", "message": str(exc)},
            ) from exc
        return WebhookInboundConfigStatus(audience=audience, inbound_configured=True)

    @router.put("/api/webhooks/config/{audience}/outbound/{endpoint_id}")
    async def configure_outbound(
        audience: str,
        endpoint_id: str,
        body: WebhookOutboundConfigBody,
        request: Request,
    ) -> WebhookOutboundConfigStatus:
        try:
            config = state.integrations.configure_webhook_outbound(
                owner_id=current_owner_id(request),
                audience=audience,
                endpoint_id=endpoint_id,
                target_url=body.target_url,
                signing_secret=body.signing_secret.get_secret_value(),
                event_types=frozenset(body.event_types),
                enabled=body.enabled,
            )
        except (RuntimeError, ValueError) as exc:
            raise HTTPException(
                status_code=400,
                detail={"reason": "invalid_webhook_config", "message": str(exc)},
            ) from exc
        return WebhookOutboundConfigStatus(
            audience=config.audience,
            endpoint_id=config.endpoint_id,
            enabled=config.enabled,
            credential_configured=True,
        )

    return router


__all__ = ["make_webhooks_router"]
