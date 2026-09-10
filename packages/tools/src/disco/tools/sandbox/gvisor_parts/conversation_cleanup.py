"""`GvisorSandboxService.destroy_by_conversation` — the dual-prefix, dual-label sweep.

Destroys all `disco-sbx-*`/`pmx-sbx-*` containers, `disco-egr-*` internal
networks, and named workspace volumes labelled with one conversation_id.
Dual-read on BOTH the current and legacy label keys so a container/network
started under the pre-rename label scheme is still torn down.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any

from .._container import _remove_container, _remove_volume

if TYPE_CHECKING:
    from ..gvisor import GvisorSandboxService


def _sweep_containers(svc: GvisorSandboxService, client: Any, conversation_id: str) -> set[str]:
    from .. import gvisor

    # Dual-read: match BOTH the current `disco.conversation_id` and
    # legacy `pmx.conversation_id` label keys (union, dedup by id) so
    # a container/network started under the old label scheme is still
    # torn down after the rename.
    seen_ids: set[str] = set()
    workspace_volume_names: set[str] = set()
    for key in gvisor.LABEL_CONV_KEYS:
        for c in client.containers.list(
            all=True,
            filters={"label": f"{key}={conversation_id}"},
        ):
            if c.id in seen_ids:
                continue
            seen_ids.add(c.id)
            name = str(getattr(c, "name", "") or "").lstrip("/")
            for prefix in gvisor.SBX_NAME_PREFIXES:
                if name.startswith(prefix):
                    workspace_volume_names.add(
                        f"{svc._cfg.workspace_volume_prefix}-{name[len(prefix) :]}"
                    )
                    break
            try:
                c.stop(timeout=2)
            except Exception:  # noqa: BLE001 — already stopped is fine
                pass
            try:
                _remove_container(c)
            except Exception:  # noqa: BLE001 — already gone is fine
                pass
    return workspace_volume_names


def _sweep_networks(client: Any, conversation_id: str) -> None:
    from .. import gvisor

    # Clean up the egress internal network(s) by LABEL — networks are
    # named disco-egr-{instance_id}, which has NO relation to the
    # conversation_id, so a name match can never work. Labels are set
    # at create time (same conversation label as the containers);
    # containers were removed above, so the network is detachable.
    seen_nets: set[str] = set()
    for key in gvisor.LABEL_CONV_KEYS:
        for net in client.networks.list(filters={"label": f"{key}={conversation_id}"}):
            if net.id in seen_nets:
                continue
            seen_nets.add(net.id)
            try:
                net.remove()
            except Exception:  # noqa: BLE001 — in-use or gone
                pass


def _sweep_volumes(
    client: Any, conversation_id: str, workspace_volume_names: set[str]
) -> None:
    from .. import gvisor

    # LocalSandboxService inherits this release path and uses per-run
    # named workspace volumes. Remove labeled volumes after containers
    # have gone so no Podman/Docker volume lock survives release.
    volumes = getattr(client, "volumes", None)
    if volumes is None:
        return
    seen_vols: set[str] = set()
    for key in gvisor.LABEL_CONV_KEYS:
        try:
            candidates = volumes.list(filters={"label": f"{key}={conversation_id}"})
        except Exception:  # noqa: BLE001 — client lacks volume listing
            continue
        for vol in candidates:
            name = getattr(vol, "name", "") or getattr(vol, "id", "")
            if name in seen_vols:
                continue
            seen_vols.add(name)
            try:
                _remove_volume(vol)
            except Exception:  # noqa: BLE001 — already gone / in use
                pass
    for name in workspace_volume_names:
        if name in seen_vols:
            continue
        try:
            vol = volumes.get(name)
        except Exception:  # noqa: BLE001 — missing or unsupported
            continue
        seen_vols.add(name)
        try:
            _remove_volume(vol)
        except Exception:  # noqa: BLE001 — already gone / in use
            pass


def _destroy_conversation_resources(svc: GvisorSandboxService, conversation_id: str) -> None:
    try:
        client = svc._client()
        workspace_volume_names = _sweep_containers(svc, client, conversation_id)
        _sweep_networks(client, conversation_id)
        _sweep_volumes(client, conversation_id, workspace_volume_names)
    except Exception:  # noqa: BLE001 — docker unreachable: best-effort
        pass


async def destroy_by_conversation(svc: GvisorSandboxService, conversation_id: str) -> None:
    """Destroy all pmx-sbx-* and pmx-egr-* containers labelled with this conversation_id."""
    await asyncio.to_thread(_destroy_conversation_resources, svc, conversation_id)
