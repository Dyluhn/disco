"""Security administration routes."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException

from ..config.dtos import OriginApprovalDTO, OriginApprovalResultDTO
from ..config_state import ConfigState


def make_security_router(state: ConfigState) -> APIRouter:
    router = APIRouter()

    @router.post("/api/security/approve-origin")
    async def approve_origin(body: OriginApprovalDTO) -> OriginApprovalResultDTO:
        origin = body.origin.strip()
        purpose = body.purpose.strip()
        if not origin or not purpose:
            raise HTTPException(
                status_code=400,
                detail={"reason": "invalid_approval", "message": "origin and purpose are required"},
            )
        try:
            state.approve_origin(origin, purpose, body.secret_ref)
        except (RuntimeError, ValueError) as exc:
            raise HTTPException(
                status_code=400,
                detail={"reason": "invalid_approval", "message": str(exc)},
            ) from exc
        return OriginApprovalResultDTO(
            approved=True,
            origin=origin,
            purpose=purpose,
            secret_ref=body.secret_ref.strip(),
        )

    return router
