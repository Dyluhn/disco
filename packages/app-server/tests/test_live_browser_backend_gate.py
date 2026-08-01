"""Settings-level gate: enabling noVNC is rejected unless the sandbox backend can run
the live stack (only gVisor — LIVE_VIEW_BACKENDS). Defense in depth so a stale flag or a
direct API call can't persist enabled=true for a backend that would only ever fail at
runtime with "live_start_failed". Disabling is always allowed.

Uses an ISOLATED on-disk ConfigStore (tmp_path) seeded via save_sandbox so the gate reads
the backend WE set — never the shared dev config on disk (hermetic)."""

import pytest
from disco.app_server.config.dtos import LiveBrowserConfigDTO
from disco.app_server.config_state import ConfigState, ConfigValidationError
from disco.core.llm.config import LiveBrowserSettings, SandboxSettings
from disco.core.llm.config_store import ConfigStore


def _state(tmp_path, *, backend: str, enabled: bool = False) -> ConfigState:
    store = ConfigStore(tmp_path / "config.json")
    store.sections.save_sandbox(SandboxSettings(backend=backend))
    store.sections.save_live_browser(LiveBrowserSettings(enabled=enabled))
    return ConfigState(store=store)


def test_enable_rejected_on_local_backend(tmp_path):
    """Turning noVNC ON while on the local (shared-kernel) backend is rejected with a
    typed reason — and NOT persisted."""
    state = _state(tmp_path, backend="local")
    with pytest.raises(ConfigValidationError) as exc:
        state.update_live_browser_config(LiveBrowserConfigDTO(enabled=True))
    assert exc.value.reason == "unsupported_backend"
    # not persisted: it still reads disabled.
    assert state.live_browser_config().enabled is False


def test_enable_rejected_on_process_and_podman_backends(tmp_path):
    for backend in ("process", "podman"):
        state = _state(tmp_path, backend=backend)
        with pytest.raises(ConfigValidationError):
            state.update_live_browser_config(LiveBrowserConfigDTO(enabled=True))


def test_enable_allowed_on_gvisor_backend(tmp_path):
    """gVisor ships the stack + the accepted live-jail model → enabling persists."""
    state = _state(tmp_path, backend="gvisor")
    out = state.update_live_browser_config(LiveBrowserConfigDTO(enabled=True))
    assert out.enabled is True
    assert state.live_browser_config().enabled is True


def test_disable_always_allowed_even_on_local(tmp_path):
    """Turning it OFF is never gated (e.g. clearing a stale flag after a backend switch)."""
    state = _state(tmp_path, backend="local", enabled=True)
    out = state.update_live_browser_config(LiveBrowserConfigDTO(enabled=False))
    assert out.enabled is False
