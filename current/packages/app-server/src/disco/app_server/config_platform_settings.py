"""Build-platform settings repository split out of ConfigState (PY-0365): the
live-browser (noVNC) toggle, the vestigial Build kernel selector, and the
Build-project storage path — each a simple persisted GET/PUT DTO pair.
`ConfigState` exposes an instance of this class as the plain `platform`
attribute.

Sibling of `config_features.py` / `ConfigFeatures` (`ConfigState.features`):
the seam evaluation's "feature config" grouping (17 methods) exceeds this
decomposition's own 12-method cap as a single class, so it is split along the
LLM-pipeline (`features`) vs. Build-platform (`platform`) seam — see
`config_features.py`'s module docstring for why.
"""

from __future__ import annotations

from disco.core.llm import ConfigStore

from .config.dtos import (
    BuildKernelConfigDTO,
    LiveBrowserConfigDTO,
    ProjectStorageConfigDTO,
)
from .config.mappers import _build_kernel_from, _live_browser_from, _projects_from
from .config_state_errors import ConfigValidationError


class ConfigPlatformSettings:
    """Live browser, Build kernel, and Build-project storage settings."""

    def __init__(self, store: ConfigStore) -> None:
        self._store = store

    # Live browser (noVNC) toggle (persisted; agent-server reads per request) ------

    def live_browser_config(self) -> LiveBrowserConfigDTO:
        return _live_browser_from(self._store.load())

    def update_live_browser_config(self, dto: LiveBrowserConfigDTO) -> LiveBrowserConfigDTO:
        """Persist the live-browser toggle. Affects the next /browser/live-url call.

        Defense in depth: REJECT enabling noVNC when the configured sandbox backend
        can't run the live stack (only gVisor ships Xvfb/x11vnc/websockify — see
        LIVE_VIEW_BACKENDS). The Settings UI already greys the toggle on an unsupported
        backend, but a stale persisted flag or a direct API call must not be able to set
        enabled=true for a backend that can't stream it (it would only ever produce the
        live_start_failed error at runtime). Turning it OFF is always allowed."""
        from disco.core.llm.config import LiveBrowserSettings
        from disco.tools.sandbox._container import LIVE_VIEW_BACKENDS

        if dto.enabled:
            backend = self._store.load().sandbox.backend
            if backend not in LIVE_VIEW_BACKENDS:
                raise ConfigValidationError(
                    "unsupported_backend",
                    detail=(
                        f"Live browser needs a containerized gVisor sandbox — it can't run "
                        f"on the {backend} sandbox. Switch the sandbox backend to gVisor first."
                    ),
                )

        self._store.sections.save_live_browser(LiveBrowserSettings(enabled=dto.enabled))
        return _live_browser_from(self._store.load())

    # Build kernel selector (persisted; agent-server reads it per request) --------

    def build_kernel_config(self) -> BuildKernelConfigDTO:
        """The vestigial Build kernel config. Legacy values load as Disco."""
        return _build_kernel_from(self._store.load())

    def update_build_kernel_config(self, dto: BuildKernelConfigDTO) -> BuildKernelConfigDTO:
        """Persist the vestigial Build kernel selector."""
        self._store.sections.save_build_kernel(dto.kind)
        return self.build_kernel_config()

    # Build-project storage path (persisted; agent-server reads it per request) ---

    def projects_config(self) -> ProjectStorageConfigDTO:
        return _projects_from(self._store.load())

    def update_projects_config(self, dto: ProjectStorageConfigDTO) -> ProjectStorageConfigDTO:
        """Persist the chosen projects_root after validation. An empty path
        unsets it (allowed). A non-empty path must be an existing, writable
        directory or this raises ConfigValidationError; the endpoint maps that
        to a 400 with a typed reason so the UI can show a specific error."""
        from disco.core.llm import ProjectStorageSettings
        from disco.tools.projects import (
            StorageStatus,
            resolve_projects_root,
            validate_root,
        )

        raw = dto.projects_root.strip()
        if raw:
            # E4: a configured-but-missing path that's CREATABLE is mkdir -p'd and
            # accepted ("works every time"); only a genuinely unreachable path
            # (mkdir fails) stays NOT_FOUND and is rejected with a typed reason.
            status = validate_root(resolve_projects_root(raw))
            if status != StorageStatus.OK:
                raise ConfigValidationError(
                    reason=status.value,
                    detail=f"projects_root {raw!r}: {status.value}",
                )
        self._store.sections.save_projects(ProjectStorageSettings(projects_root=raw))
        return _projects_from(self._store.load())
