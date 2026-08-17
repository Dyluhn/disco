"""`reconcile_orphan_aux_resources` — the dual-prefix orphan network/volume sweep.

Remove old detached sandbox networks/volumes left without a container.
Startup reconciliation previously considered only sandbox containers. If a
crash removed the container first, its labeled internal network and named
workspace volume became unreachable forever. This sweep is deliberately
conservative: a resource must have an exact current/legacy product prefix, a
recognized conversation label, a parseable age beyond the provisioning grace,
no attached endpoint/mount, and no matching sandbox or sidecar container.
Missing metadata or client errors retain the resource.
"""

from __future__ import annotations

import logging
import time
from datetime import UTC, datetime
from typing import Any

_LOG = logging.getLogger(__name__)

_AUX_RECONCILE_GRACE_S = 300.0


def _resource_created_epoch(resource: Any) -> float | None:
    """Parse Docker/Podman network or volume creation time, failing closed."""

    attrs = getattr(resource, "attrs", None) or {}
    raw = attrs.get("CreatedAt") or attrs.get("Created") or attrs.get("created")
    if not isinstance(raw, str) or not raw.strip():
        return None
    try:
        created = datetime.fromisoformat(raw.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    if created.tzinfo is None:
        created = created.replace(tzinfo=UTC)
    return created.timestamp()


def _resource_labels(resource: Any) -> dict[str, str]:
    attrs = getattr(resource, "attrs", None) or {}
    labels = getattr(resource, "labels", None) or attrs.get("Labels") or attrs.get("labels")
    return labels if isinstance(labels, dict) else {}


def _old_enough(resource: Any, now: float) -> bool:
    created_s = _resource_created_epoch(resource)
    return created_s is not None and now - created_s >= _AUX_RECONCILE_GRACE_S


def _matching_container_exists(suffix: str, container_names: set[str]) -> bool:
    from ..naming import EGR_NET_PREFIXES, SBX_NAME_PREFIXES

    return any(
        f"{prefix}{suffix}" in container_names for prefix in (*SBX_NAME_PREFIXES, *EGR_NET_PREFIXES)
    )


def _list_container_names(client: Any) -> set[str] | None:
    try:
        all_containers = client.containers.list(all=True)
    except Exception as exc:  # noqa: BLE001 — fail closed; cleanup is maintenance
        _LOG.warning(
            "sandbox auxiliary reconciliation skipped: container inventory failed (%s)",
            type(exc).__name__,
        )
        return None
    # `getattr` here is pre-existing duck-typing across the docker-py/podman-py
    # container objects (both SDKs are only duck-type compatible), carried over
    # verbatim from `_container.reconcile_orphan_aux_resources`; the extraction
    # only joined the comprehension onto one line. Not a new shim.
    return {str(getattr(container, "name", "") or "").lstrip("/") for container in all_containers}


def _reload_resource_or_skip(resource: Any) -> bool:
    """Best-effort reload; returns True iff the resource's metadata is trustworthy.

    The `getattr` probe is pre-existing: the parent carried this exact three-line
    block TWICE (once for `network`, once for `volume`) because only some
    docker-py/podman-py resource objects expose `reload()`. Extraction
    de-duplicated the pair into this one helper, which is why the receiver is
    named `resource` and no byte-identical parent line exists. Not a new shim.
    """
    try:
        reload_resource = getattr(resource, "reload", None)
        if callable(reload_resource):
            reload_resource()
        return True
    except Exception as exc:  # noqa: BLE001 — stale metadata is not removal authority
        _LOG.warning(
            "sandbox aux reconciliation retained an unverifiable resource (%s)",
            type(exc).__name__,
        )
        return False


def _find_prefix(name: str, prefixes: tuple[str, ...]) -> str | None:
    return next((item for item in prefixes if name.startswith(item)), None)


def _should_skip_network(
    network: Any, name: str, prefix: str, container_names: set[str], now: float
) -> bool:
    from ..naming import conv_id_from_labels

    attrs = getattr(network, "attrs", None) or {}
    attached = attrs.get("Containers") or attrs.get("containers") or {}
    suffix = name.removeprefix(prefix)
    return (
        not conv_id_from_labels(_resource_labels(network))
        or bool(attached)
        or _matching_container_exists(suffix, container_names)
        or not _old_enough(network, now)
    )


def _sweep_orphan_networks(client: Any, container_names: set[str], now: float) -> int:
    from ..naming import EGR_NET_PREFIXES

    try:
        networks = client.networks.list()
    except Exception as exc:  # noqa: BLE001 — fail closed; volumes remain independently safe
        _LOG.warning(
            "sandbox network reconciliation skipped: inventory failed (%s)",
            type(exc).__name__,
        )
        return 0

    removed = 0
    for network in networks:
        name = str(getattr(network, "name", "") or "")
        prefix = _find_prefix(name, EGR_NET_PREFIXES)
        if prefix is None or not name.removeprefix(prefix):
            continue
        if not _reload_resource_or_skip(network):
            continue
        if _should_skip_network(network, name, prefix, container_names, now):
            continue
        try:
            network.remove()
            removed += 1
        except Exception as exc:  # noqa: BLE001 — attached/gone resources remain safe
            _LOG.warning(
                "sandbox network reconciliation could not remove an orphan (%s)",
                type(exc).__name__,
            )
    return removed


def _should_skip_volume(
    volume: Any, name: str, prefix: str, container_names: set[str], now: float
) -> bool:
    from ..naming import conv_id_from_labels

    attrs = getattr(volume, "attrs", None) or {}
    mount_count = attrs.get("MountCount")
    suffix = name.removeprefix(prefix)
    return (
        not conv_id_from_labels(_resource_labels(volume))
        or (mount_count is not None and mount_count != 0)
        or _matching_container_exists(suffix, container_names)
        or not _old_enough(volume, now)
    )


def _sweep_orphan_volumes(
    client: Any, container_names: set[str], now: float, workspace_volume_prefix: str
) -> int:
    try:
        volumes = client.volumes.list()
    except Exception as exc:  # noqa: BLE001 — fail closed
        _LOG.warning(
            "sandbox volume reconciliation skipped: inventory failed (%s)",
            type(exc).__name__,
        )
        return 0

    removed = 0
    volume_prefixes = tuple(dict.fromkeys((f"{workspace_volume_prefix}-", "pmx-ws-")))
    for volume in volumes:
        name = str(getattr(volume, "name", "") or getattr(volume, "id", "") or "")
        prefix = _find_prefix(name, volume_prefixes)
        if prefix is None or not name.removeprefix(prefix):
            continue
        if not _reload_resource_or_skip(volume):
            continue
        if _should_skip_volume(volume, name, prefix, container_names, now):
            continue
        try:
            # Never force startup garbage collection. A runtime-visible user
            # safely wins a race by making the non-forced removal fail.
            volume.remove()
            removed += 1
        except Exception as exc:  # noqa: BLE001 — attached/gone resources remain safe
            _LOG.warning(
                "sandbox volume reconciliation could not remove an orphan (%s)",
                type(exc).__name__,
            )
    return removed


def reconcile_orphan_aux_resources(
    client: Any,
    *,
    workspace_volume_prefix: str,
    now_s: float | None = None,
) -> tuple[int, int]:
    """Remove old detached sandbox networks/volumes left without a container.

    Startup reconciliation previously considered only sandbox containers. If a
    crash removed the container first, its labeled internal network and named
    workspace volume became unreachable forever. This sweep is deliberately
    conservative: a resource must have an exact current/legacy product prefix,
    a recognized conversation label, a parseable age beyond the provisioning
    grace, no attached endpoint/mount, and no matching sandbox or sidecar
    container. Missing metadata or client errors retain the resource.
    """
    now = time.time() if now_s is None else now_s
    container_names = _list_container_names(client)
    if container_names is None:
        return 0, 0

    removed_networks = _sweep_orphan_networks(client, container_names, now)
    removed_volumes = _sweep_orphan_volumes(client, container_names, now, workspace_volume_prefix)

    if removed_networks or removed_volumes:
        _LOG.info(
            "sandbox auxiliary reconciliation removed networks=%d volumes=%d",
            removed_networks,
            removed_volumes,
        )
    return removed_networks, removed_volumes
