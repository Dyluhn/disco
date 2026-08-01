"""`ContainerInstance._resolve_mapping` — the Docker/Podman port-binding read.

The published mapping lives on the SIDECAR, not the sandbox: every guest is
on an internal no-NAT net and publishes nothing directly. Sealed sidecars
publish only INTERNAL_PORTS; filtered/public sidecars publish the full
curated set. The sandbox fallback supports only older/minimal
`ContainerInstance` implementations without a sidecar.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .._container import ContainerInstance


def resolve_mapping(instance: ContainerInstance, port: int) -> tuple[str, int] | None:
    """Internal helper to read the Docker/Podman port binding. Goes through
    `_safe_reload` so a hung/raising client returns None (no URL) instead of
    wedging the loop. (Dispo #25 wedge-guard.)

    The published mapping lives on the SIDECAR, not the sandbox: every guest
    is on an internal no-NAT net and publishes nothing directly. Sealed
    sidecars publish only INTERNAL_PORTS; filtered/public sidecars publish the
    full curated set. The sandbox fallback supports only older/minimal
    ContainerInstance implementations without a sidecar."""
    from .. import _container

    if port not in _container.PUBLISHED_PORTS:
        return None
    source = instance._container if instance._egress_sidecar is None else instance._egress_sidecar
    try:
        if not instance._safe_reload(source):
            return None
        ports = source.attrs.get("NetworkSettings", {}).get("Ports") or {}
        binding = ports.get(f"{port}/tcp")
        if not isinstance(binding, list) or len(binding) != 1:
            return None
        only_binding = binding[0]
        if not isinstance(only_binding, dict):
            return None
        host_ip = str(only_binding.get("HostIp") or "")
        if host_ip != "127.0.0.1":
            # Bindings are requested on IPv4 loopback. Accepting IPv6 here
            # would require bracketed URL construction and would advertise a
            # transport shape this stack does not create.
            return None
        host_port = int(only_binding["HostPort"])
        if not 1 <= host_port <= 65_535:
            return None
        if instance._loopback_tunnel is not None:
            return instance._loopback_tunnel.forward(host_port)
        return host_ip, host_port
    except Exception:  # noqa: BLE001 — no mapping yet / box gone / wedged client
        return None
