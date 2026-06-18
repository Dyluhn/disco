"""Config-matrix routes — sandbox / encoders / tts / data-sources / project storage.

The deterministic settings surfaces: each is a simple GET (current value) + PUT
(replace) over `ConfigState`. The project-storage PUT additionally validates the
path server-side and returns a typed 400 `reason` the UI can branch on.
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException

from ..config.dtos import (
    DataSourcesConfigDTO,
    EncodersConfigDTO,
    ImageGenConfigDTO,
    ProjectStorageConfigDTO,
    SandboxConfigDTO,
    TtsConfigDTO,
)
from ..config_state import ConfigState, ConfigValidationError


def make_config_router(state: ConfigState) -> APIRouter:
    router = APIRouter()

    @router.get("/api/sandbox/config")
    async def get_sandbox_config() -> SandboxConfigDTO:
        return state.sandbox_config()

    @router.put("/api/sandbox/config")
    async def put_sandbox_config(dto: SandboxConfigDTO) -> SandboxConfigDTO:
        return state.update_sandbox_config(dto)

    @router.get("/api/encoders/config")
    async def get_encoders_config() -> EncodersConfigDTO:
        return state.encoders_config()

    @router.put("/api/encoders/config")
    async def put_encoders_config(dto: EncodersConfigDTO) -> EncodersConfigDTO:
        return state.update_encoders_config(dto)

    @router.get("/api/tts/config")
    async def get_tts_config() -> TtsConfigDTO:
        return state.tts_config()

    @router.put("/api/tts/config")
    async def put_tts_config(dto: TtsConfigDTO) -> TtsConfigDTO:
        return state.update_tts_config(dto)

    @router.get("/api/image-gen/config")
    async def get_image_gen_config() -> ImageGenConfigDTO:
        return state.image_gen_config()

    @router.put("/api/image-gen/config")
    async def put_image_gen_config(dto: ImageGenConfigDTO) -> ImageGenConfigDTO:
        return state.update_image_gen_config(dto)

    @router.get("/api/data-sources/config")
    async def get_data_sources_config() -> DataSourcesConfigDTO:
        return state.data_sources_config()

    @router.put("/api/data-sources/config")
    async def put_data_sources_config(dto: DataSourcesConfigDTO) -> DataSourcesConfigDTO:
        return state.update_data_sources_config(dto)

    @router.get("/api/projects/storage/config")
    async def get_projects_config() -> ProjectStorageConfigDTO:
        return state.projects_config()

    @router.put("/api/projects/storage/config")
    async def put_projects_config(
        dto: ProjectStorageConfigDTO,
    ) -> ProjectStorageConfigDTO:
        """Persist the Build-project storage path. A non-empty path is validated
        server-side; a bad path returns 400 with a typed `reason` so the UI shows
        a specific error ("not_found" / "not_a_directory" / "not_writable")."""
        try:
            return state.update_projects_config(dto)
        except ConfigValidationError as exc:
            raise HTTPException(
                status_code=400,
                detail={"reason": exc.reason, "message": exc.detail or exc.reason},
            ) from exc

    return router
