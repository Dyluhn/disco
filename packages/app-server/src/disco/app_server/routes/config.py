"""Config-matrix routes — sandbox / encoders / tts / data-sources / project storage.

The deterministic settings surfaces: each is a simple GET (current value) + PUT
(replace) over `ConfigState`. The project-storage PUT additionally validates the
path server-side and returns a typed 400 `reason` the UI can branch on.
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException

from ..config.dtos import (
    BuildKernelConfigDTO,
    DataSourcesConfigDTO,
    EncodersConfigDTO,
    ImageGenConfigDTO,
    LiveBrowserConfigDTO,
    ProbeResult,
    ProjectStorageConfigDTO,
    RoleFallbackConfigDTO,
    SandboxConfigDTO,
    SandboxHealthDTO,
    TtsConfigDTO,
)
from ..config_state import ConfigState, ConfigValidationError


def _validation_error(exc: ConfigValidationError) -> HTTPException:
    return HTTPException(
        status_code=400,
        detail={"reason": exc.reason, "message": exc.detail or exc.reason},
    )


def _put_sandbox_config(state: ConfigState, dto: SandboxConfigDTO) -> SandboxConfigDTO:
    """Persist the active backend + connection. Per-backend connection blocks are
    retained by the store, so switching the active backend never clears the others'
    setup. A structurally unrunnable config (gvisor/podman with an empty/host-less
    endpoint) is rejected 400 with a typed reason rather than silently saved."""
    try:
        return state.update_sandbox_config(dto)
    except ConfigValidationError as exc:
        raise _validation_error(exc) from exc


async def _test_sandbox(state: ConfigState, dto: SandboxConfigDTO) -> ProbeResult:
    """W-48: connectivity PREFLIGHT for the given sandbox backend config — a real,
    bounded probe of the configured endpoint, returning a typed host-naming verdict
    (HTTP 200 with ok=False on an expected failure, never a 500)."""
    return await state.test_sandbox(dto)


async def _sandbox_health(state: ConfigState) -> SandboxHealthDTO:
    """Reachability of the ACTIVE (persisted) sandbox backend — the cheap signal the
    app shell polls to surface an unreachable sandbox BEFORE a run is started. Same
    probe the run path hits, so the banner and the real run agree."""
    return await state.sandbox_health()


def _put_role_fallback_config(
    state: ConfigState, dto: RoleFallbackConfigDTO
) -> RoleFallbackConfigDTO:
    try:
        return state.update_role_fallback_config(dto)
    except ConfigValidationError as exc:
        raise _validation_error(exc) from exc


async def _test_data_source(state: ConfigState, kind: str) -> ProbeResult:
    """Probe T4.2 — reachability of the configured search/extraction endpoint.
    ``kind`` is 'search' or 'extraction'. Bundled tiers report honestly that
    there's nothing to reach; remote tiers do a real GET. Always 200."""
    return await state.test_data_source(kind)


def _put_projects_config(
    state: ConfigState, dto: ProjectStorageConfigDTO
) -> ProjectStorageConfigDTO:
    """Persist the Build-project storage path. A non-empty path is validated
    server-side; a bad path returns 400 with a typed `reason` so the UI shows
    a specific error ("not_found" / "not_a_directory" / "not_writable")."""
    try:
        return state.update_projects_config(dto)
    except ConfigValidationError as exc:
        raise _validation_error(exc) from exc


def _put_live_browser_config(
    state: ConfigState, dto: LiveBrowserConfigDTO
) -> LiveBrowserConfigDTO:
    """Persist the noVNC toggle. Enabling it on a sandbox backend that can't run the
    live stack (anything but gVisor) is rejected with 400 + a typed reason so the UI
    flags it instead of silently persisting a setting that only ever fails at runtime."""
    try:
        return state.update_live_browser_config(dto)
    except ConfigValidationError as exc:
        raise _validation_error(exc) from exc


def _put_build_kernel_config(
    state: ConfigState, dto: BuildKernelConfigDTO
) -> BuildKernelConfigDTO:
    """Persist the vestigial Build kernel selector."""
    try:
        return state.update_build_kernel_config(dto)
    except ConfigValidationError as exc:
        raise _validation_error(exc) from exc


def make_config_router(state: ConfigState) -> APIRouter:
    router = APIRouter()

    @router.get("/api/sandbox/config")
    async def get_sandbox_config() -> SandboxConfigDTO:
        return state.sandbox_config()

    @router.put("/api/sandbox/config")
    async def put_sandbox_config(dto: SandboxConfigDTO) -> SandboxConfigDTO:
        return _put_sandbox_config(state, dto)

    @router.post("/api/sandbox/test")
    async def test_sandbox(dto: SandboxConfigDTO) -> ProbeResult:
        return await _test_sandbox(state, dto)

    @router.get("/api/sandbox/health")
    async def sandbox_health() -> SandboxHealthDTO:
        return await _sandbox_health(state)

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

    @router.get("/api/role-fallback/config")
    async def get_role_fallback_config() -> RoleFallbackConfigDTO:
        return state.role_fallback_config()

    @router.put("/api/role-fallback/config")
    async def put_role_fallback_config(dto: RoleFallbackConfigDTO) -> RoleFallbackConfigDTO:
        return _put_role_fallback_config(state, dto)

    @router.post("/api/data-sources/{kind}/test")
    async def test_data_source(kind: str) -> ProbeResult:
        return await _test_data_source(state, kind)

    @router.get("/api/projects/storage/config")
    async def get_projects_config() -> ProjectStorageConfigDTO:
        return state.projects_config()

    @router.put("/api/projects/storage/config")
    async def put_projects_config(dto: ProjectStorageConfigDTO) -> ProjectStorageConfigDTO:
        return _put_projects_config(state, dto)

    @router.get("/api/live-browser/config")
    async def get_live_browser_config() -> LiveBrowserConfigDTO:
        return state.live_browser_config()

    @router.put("/api/live-browser/config")
    async def put_live_browser_config(dto: LiveBrowserConfigDTO) -> LiveBrowserConfigDTO:
        return _put_live_browser_config(state, dto)

    @router.get("/api/build-kernel/config")
    async def get_build_kernel_config() -> BuildKernelConfigDTO:
        return state.build_kernel_config()

    @router.put("/api/build-kernel/config")
    async def put_build_kernel_config(dto: BuildKernelConfigDTO) -> BuildKernelConfigDTO:
        return _put_build_kernel_config(state, dto)

    return router
