"""Settings-level gate: enabling noVNC is rejected unless the sandbox backend can run
the live stack (only gVisor — LIVE_VIEW_BACKENDS). Defense in depth so a stale flag or a
direct API call can't persist enabled=true for a backend that would only ever fail at
runtime with "live_start_failed". Disabling is always allowed."""

import pytest
from disco.app_server.config.dtos import LiveBrowserConfigDTO
from disco.app_server.config_state import ConfigState, ConfigValidationError
from disco.core.llm import ModelEntry
from disco.core.llm.config import LiveBrowserSettings, RouterConfig, SandboxSettings


def _state(*, backend: str, enabled: bool = False) -> ConfigState:
    entry = ModelEntry(model_id="m", provider="local", context_window=8192)
    cfg = RouterConfig(
        models={"m": entry},
        default_model="m",
        sandbox=SandboxSettings(backend=backend),
        live_browser=LiveBrowserSettings(enabled=enabled),
    )
    return ConfigState(config=cfg)


def test_enable_rejected_on_local_backend():
    """Turning noVNC ON while on the local (shared-kernel) backend is rejected with a
    typed reason — and NOT persisted."""
    state = _state(backend="local")
    with pytest.raises(ConfigValidationError) as exc:
        state.update_live_browser_config(LiveBrowserConfigDTO(enabled=True))
    assert exc.value.reason == "unsupported_backend"
    # not persisted: it still reads disabled.
    assert state.live_browser_config().enabled is False


def test_enable_rejected_on_process_and_podman_backends():
    for backend in ("process", "podman"):
        state = _state(backend=backend)
        with pytest.raises(ConfigValidationError):
            state.update_live_browser_config(LiveBrowserConfigDTO(enabled=True))


def test_enable_allowed_on_gvisor_backend():
    """gVisor ships the stack + the accepted live-jail model → enabling persists."""
    state = _state(backend="gvisor")
    out = state.update_live_browser_config(LiveBrowserConfigDTO(enabled=True))
    assert out.enabled is True
    assert state.live_browser_config().enabled is True


def test_disable_always_allowed_even_on_local():
    """Turning it OFF is never gated (e.g. clearing a stale flag after a backend switch)."""
    state = _state(backend="local", enabled=True)
    out = state.update_live_browser_config(LiveBrowserConfigDTO(enabled=False))
    assert out.enabled is False
