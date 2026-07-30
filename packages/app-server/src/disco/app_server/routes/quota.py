"""Owner-admin configuration and safe status for per-app host-service quotas."""

from __future__ import annotations

from typing import Annotated, Literal

from disco.core.quota import QuotaConfig, QuotaConfigurationError, StoredQuotaConfig
from fastapi import APIRouter, HTTPException, Query, Request, Response, status

from ..auth import current_owner_id
from ..config.dtos import (
    QuotaConfigBody,
    QuotaConfigStatus,
    QuotaLimitsDTO,
    QuotaStatusDTO,
    QuotaUsageDTO,
)
from ..config_state import ConfigState, app_quota_store

_SERVICE_PATTERN = r"^[a-z][a-z0-9_]*(?:\.[a-z][a-z0-9_]*)+$"
ServiceQuery = Annotated[
    str | None,
    Query(min_length=3, max_length=128, pattern=_SERVICE_PATTERN),
]


def _limits_dto(limits: QuotaConfig) -> QuotaLimitsDTO:
    return QuotaLimitsDTO(
        window_seconds=limits.window_seconds,
        max_requests=limits.max_requests,
        max_input_tokens=limits.max_input_tokens,
        max_output_tokens=limits.max_output_tokens,
        max_total_tokens=limits.max_total_tokens,
    )


def _config_dto(
    stored: StoredQuotaConfig,
    *,
    source: Literal["configured", "inherited", "default"] = "configured",
) -> QuotaConfigStatus:
    return QuotaConfigStatus(
        audience=stored.audience,
        service=stored.service,
        source=source,
        limits=_limits_dto(stored.limits),
        updated_at=stored.updated_at,
    )


def _store_unavailable(exc: RuntimeError) -> HTTPException:
    return HTTPException(status_code=503, detail={"reason": "quota_store_unavailable"})


def _register_quota_config_routes(router: APIRouter, state: ConfigState) -> None:
    @router.put("/api/quota/config/{audience}")
    async def configure_quota(
        audience: str,
        body: QuotaConfigBody,
        request: Request,
        service: ServiceQuery = None,
    ) -> QuotaConfigStatus:
        try:
            stored = app_quota_store(state).configure(
                owner_id=current_owner_id(request),
                audience=audience,
                service=service,
                limits=QuotaConfig(
                    window_seconds=body.window_seconds,
                    max_requests=body.max_requests,
                    max_input_tokens=body.max_input_tokens,
                    max_output_tokens=body.max_output_tokens,
                    max_total_tokens=body.max_total_tokens,
                ),
            )
        except QuotaConfigurationError as exc:
            raise HTTPException(
                status_code=400,
                detail={"reason": "invalid_quota_config", "message": str(exc)},
            ) from exc
        except RuntimeError as exc:
            raise _store_unavailable(exc) from exc
        return _config_dto(stored)

    @router.get("/api/quota/config/{audience}")
    async def get_quota_config(
        audience: str,
        request: Request,
        service: ServiceQuery = None,
    ) -> QuotaConfigStatus:
        try:
            stored = app_quota_store(state).get_config(
                current_owner_id(request), audience, service=service
            )
        except QuotaConfigurationError as exc:
            raise HTTPException(status_code=400, detail={"reason": "invalid_quota_scope"}) from exc
        except RuntimeError as exc:
            raise _store_unavailable(exc) from exc
        if stored is None:
            raise HTTPException(status_code=404, detail={"reason": "quota_config_not_found"})
        return _config_dto(stored)

    @router.delete(
        "/api/quota/config/{audience}",
        status_code=status.HTTP_204_NO_CONTENT,
    )
    async def delete_quota_config(
        audience: str,
        request: Request,
        service: ServiceQuery = None,
    ) -> Response:
        try:
            deleted = app_quota_store(state).delete_config(
                current_owner_id(request), audience, service=service
            )
        except QuotaConfigurationError as exc:
            raise HTTPException(status_code=400, detail={"reason": "invalid_quota_scope"}) from exc
        except RuntimeError as exc:
            raise _store_unavailable(exc) from exc
        if not deleted:
            raise HTTPException(status_code=404, detail={"reason": "quota_config_not_found"})
        return Response(status_code=status.HTTP_204_NO_CONTENT)


def _register_quota_status_route(router: APIRouter, state: ConfigState) -> None:
    @router.get("/api/quota/status/{audience}")
    async def quota_status(
        audience: str,
        request: Request,
        service: ServiceQuery = None,
    ) -> QuotaStatusDTO:
        owner_id = current_owner_id(request)
        try:
            store = app_quota_store(state)
            exact = store.get_config(owner_id, audience, service=service)
            aggregate = (
                store.get_config(owner_id, audience)
                if service is not None and exact is None
                else None
            )
            usage = store.get_usage(owner_id=owner_id, audience=audience, service=service)
            if exact is not None:
                config = _config_dto(exact)
            elif aggregate is not None:
                config = QuotaConfigStatus(
                    audience=audience,
                    service=service,
                    source="inherited",
                    limits=_limits_dto(aggregate.limits),
                    updated_at=aggregate.updated_at,
                )
            else:
                config = QuotaConfigStatus(
                    audience=audience,
                    service=service,
                    source="default",
                    limits=_limits_dto(store.default_config),
                )
        except QuotaConfigurationError as exc:
            raise HTTPException(status_code=400, detail={"reason": "invalid_quota_scope"}) from exc
        except RuntimeError as exc:
            raise _store_unavailable(exc) from exc
        return QuotaStatusDTO(
            config=config,
            usage=QuotaUsageDTO(
                request_count=usage.request_count,
                input_tokens=usage.input_tokens,
                output_tokens=usage.output_tokens,
                total_tokens=usage.total_tokens,
                window_start=usage.window_start,
                window_end=usage.window_end,
            ),
        )


def make_quota_router(state: ConfigState) -> APIRouter:
    router = APIRouter()
    _register_quota_config_routes(router, state)
    _register_quota_status_route(router, state)
    return router


__all__ = ["make_quota_router"]
