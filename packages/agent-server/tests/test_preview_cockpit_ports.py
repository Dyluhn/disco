"""UI-8 — the preview status payload always reports the sandbox's bound USER_PORTS.

The Build Cockpit's Servers/Processes panels render exactly what
``GET /conversations/{id}/preview`` reports in ``ports`` — the one in-sandbox
``/proc`` + tmux ownership walk. On the isolated-sandbox path (the preview runs
in the manager's OWN sandbox) that list was hard-coded empty, so a server the
agent started in its own sandbox by any other means — ``python3 -m http.server
8000`` from tmux, say — was invisible and the Cockpit reported the port free.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from disco.agent_server.preview_runtime_projection import PreviewRuntimeProjection
from disco.tools.sandbox.port_owner import PortOwner


def _projection(session: object) -> PreviewRuntimeProjection:
    """A projection whose only live dependency is the session directory —
    ``preview()`` reads nothing else off the runtime."""
    return PreviewRuntimeProjection(
        live_sessions=SimpleNamespace(live_session=lambda _cid: session),  # type: ignore[arg-type]
        store=None,  # type: ignore[arg-type]
        config_store=None,  # type: ignore[arg-type]
        settings=None,  # type: ignore[arg-type]
        connections=None,  # type: ignore[arg-type]
    )


def _isolated_session(*, instance: object | None) -> SimpleNamespace:
    lifecycle = SimpleNamespace(
        to_dict=lambda: {
            "port": 19120,
            "status": "running",
            "generation": "pv_isolated",
            "launch_kind": "static",
            "reload_strategy": "reload",
            "detail": "",
        }
    )
    manager = SimpleNamespace(
        owns_sandbox=lambda: True,
        canonical_url=lambda: "http://127.0.0.2:19120",
        canonical_lifecycle_session=lambda: lifecycle,
    )
    return SimpleNamespace(
        _preview_manager=manager,
        _instance=instance,
        sessions=SimpleNamespace(namespace="cockpit"),
    )


async def _owners_with_http_server(
    _instance: object, ports: list[int]
) -> dict[int, PortOwner | None]:
    return {
        port: (
            PortOwner(
                port=port,
                pid=1934,
                cmdline="python3 -m http.server 8000",
                session="pmx-cockpit-preview",
            )
            if port == 8000
            else None
        )
        for port in ports
    }


@pytest.mark.asyncio
async def test_isolated_preview_still_reports_a_bound_user_port() -> None:
    projection = _projection(_isolated_session(instance=object()))

    payload = await projection.preview("conv_cockpit", port_owners_fn=_owners_with_http_server)

    assert payload["available"] is True
    bound = {entry["port"]: entry["owner"] for entry in payload["ports"]}
    assert 8000 in bound, "a server the preview did not start must still be visible"
    assert bound[8000]["pid"] == 1934
    assert "http.server" in bound[8000]["cmdline"]
    # The namespace prefix is stripped for display, exactly as on the other path.
    assert bound[8000]["session"] == "-preview"


@pytest.mark.asyncio
async def test_isolated_preview_reports_no_ports_when_the_sandbox_is_gone() -> None:
    """No sandbox to probe ⇒ an empty list, never an invented row."""
    projection = _projection(_isolated_session(instance=None))

    payload = await projection.preview("conv_cockpit", port_owners_fn=_owners_with_http_server)

    assert payload["ports"] == []


@pytest.mark.asyncio
async def test_isolated_preview_survives_a_failing_probe() -> None:
    """A probe that raises must not 500 the endpoint the Cockpit polls."""

    async def boom(_instance: object, _ports: list[int]) -> dict[int, PortOwner | None]:
        raise RuntimeError("exec failed")

    projection = _projection(_isolated_session(instance=object()))

    payload = await projection.preview("conv_cockpit", port_owners_fn=boom)

    assert payload["available"] is True
    assert payload["ports"] == []
