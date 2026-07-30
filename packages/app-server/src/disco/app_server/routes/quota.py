"""Owner-admin configuration and safe status for per-app host-service quotas."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Annotated, Literal

from disco.core.quota import (
    QuotaConfig,
    QuotaConfigurationError,
    QuotaUsage,
    SqliteQuotaStore,
    StoredQuotaConfig,
)
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


@dataclass(frozen=True)
class _QuotaStatusDecision:
    audience: str
    service: str | None
    source: Literal["configured", "inherited", "default"]
    limits: QuotaConfig
    updated_at: datetime | None
    usage: QuotaUsage


class _QuotaApplicationService:
    """Narrow quota port; routes never reconstruct policy from mutable state."""

    def __init__(self, store: Callable[[], SqliteQuotaStore]) -> None:
        self._store = store

    def configure(
        self,
        owner_id: str,
        audience: str,
        service: str | None,
        limits: QuotaConfig,
    ) -> StoredQuotaConfig:
        return self._store().configure(
            owner_id=owner_id,
            audience=audience,
            service=service,
            limits=limits,
        )

    def configured(
        self,
        owner_id: str,
        audience: str,
        service: str | None,
    ) -> StoredQuotaConfig | None:
        return self._store().get_config(owner_id, audience, service=service)

    def delete(
        self,
        owner_id: str,
        audience: str,
        service: str | None,
    ) -> bool:
        return self._store().delete_config(owner_id, audience, service=service)

    def status(
        self,
        owner_id: str,
        audience: str,
        service: str | None,
    ) -> _QuotaStatusDecision:
        store = self._store()
        exact = store.get_config(owner_id, audience, service=service)
        aggregate = (
            store.get_config(owner_id, audience) if service is not None and exact is None else None
        )
        usage = store.get_usage(
            owner_id=owner_id,
            audience=audience,
            service=service,
        )
        if exact is not None:
            return _QuotaStatusDecision(
                audience,
                service,
                "configured",
                exact.limits,
                exact.updated_at,
                usage,
            )
        if aggregate is not None:
            return _QuotaStatusDecision(
                audience,
                service,
                "inherited",
                aggregate.limits,
                aggregate.updated_at,
                usage,
            )
        return _QuotaStatusDecision(
            audience,
            service,
            "default",
            store.default_config,
            None,
            usage,
        )


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


def _register_quota_config_routes(
    router: APIRouter,
    service_port: _QuotaApplicationService,
) -> None:
    @router.put("/api/quota/config/{audience}")
    async def configure_quota(
        audience: str,
        body: QuotaConfigBody,
        request: Request,
        service: ServiceQuery = None,
    ) -> QuotaConfigStatus:
        try:
            stored = service_port.configure(
                current_owner_id(request),
                audience,
                service,
                QuotaConfig(
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
            stored = service_port.configured(current_owner_id(request), audience, service)
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
            deleted = service_port.delete(current_owner_id(request), audience, service)
        except QuotaConfigurationError as exc:
            raise HTTPException(status_code=400, detail={"reason": "invalid_quota_scope"}) from exc
        except RuntimeError as exc:
            raise _store_unavailable(exc) from exc
        if not deleted:
            raise HTTPException(status_code=404, detail={"reason": "quota_config_not_found"})
        return Response(status_code=status.HTTP_204_NO_CONTENT)


def _register_quota_status_route(
    router: APIRouter,
    service_port: _QuotaApplicationService,
) -> None:
    @router.get("/api/quota/status/{audience}")
    async def quota_status(
        audience: str,
        request: Request,
        service: ServiceQuery = None,
    ) -> QuotaStatusDTO:
        owner_id = current_owner_id(request)
        try:
            decision = service_port.status(owner_id, audience, service)
        except QuotaConfigurationError as exc:
            raise HTTPException(status_code=400, detail={"reason": "invalid_quota_scope"}) from exc
        except RuntimeError as exc:
            raise _store_unavailable(exc) from exc
        return QuotaStatusDTO(
            config=QuotaConfigStatus(
                audience=decision.audience,
                service=decision.service,
                source=decision.source,
                limits=_limits_dto(decision.limits),
                updated_at=decision.updated_at,
            ),
            usage=QuotaUsageDTO(
                request_count=decision.usage.request_count,
                input_tokens=decision.usage.input_tokens,
                output_tokens=decision.usage.output_tokens,
                total_tokens=decision.usage.total_tokens,
                window_start=decision.usage.window_start,
                window_end=decision.usage.window_end,
            ),
        )


def make_quota_router(state: ConfigState) -> APIRouter:
    router = APIRouter()
    service_port = _QuotaApplicationService(lambda: app_quota_store(state))
    _register_quota_config_routes(router, service_port)
    _register_quota_status_route(router, service_port)
    return router


__all__ = ["make_quota_router"]
