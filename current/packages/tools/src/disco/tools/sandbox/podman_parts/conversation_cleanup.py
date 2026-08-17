"""`PodmanSandboxService.destroy_by_conversation` — the dual-prefix, dual-label sweep.

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
    from ..podman import PodmanSandboxService


def _sweep_containers(svc: PodmanSandboxService, client: Any, conversation_id: str) -> set[str]:
    from .. import podman

    # Dual-read: match BOTH the current and legacy conversation label
    # keys (union, dedup) so an old-scheme container is still removed.
    seen_ids: set[str] = set()
    workspace_volume_names: set[str] = set()
    for key in podman.LABEL_CONV_KEYS:
        for c in client.containers.list(
            all=True,
            filters={"label": f"{key}={conversation_id}"},
        ):
            if c.id in seen_ids:
                continue
            seen_ids.add(c.id)
            name = str(getattr(c, "name", "") or "").lstrip("/")
            for prefix in podman.SBX_NAME_PREFIXES:
                if name.startswith(prefix):
                    workspace_volume_names.add(
                        f"{svc._cfg.workspace_volume_prefix}-{name[len(prefix) :]}"
                    )
                    break
            try:
                c.stop(timeout=2)
            except Exception:  # noqa: BLE001
                pass
            try:
                _remove_container(c)
            except Exception:  # noqa: BLE001
                pass
    return workspace_volume_names


def _sweep_networks(client: Any, conversation_id: str) -> None:
    from .. import podman

    # [P2] Clean up the filtered-egress internal network(s) by LABEL too —
    # mirrors the gVisor path. The network NAME is disco-egr-{instance_id}
    # (instance-keyed, NOT conversation-keyed), so only the conversation
    # label finds it; a crash/restart with filtered podman boxes would
    # otherwise strand the labeled `disco-egr-*` networks forever. The
    # containers above are already removed, so the network is detachable.
    seen_nets: set[str] = set()
    for key in podman.LABEL_CONV_KEYS:
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
    from .. import podman

    # Remove the per-sandbox named workspace volume(s) labeled with
    # this conversation. This is the release path used by soak kill;
    # container.remove(v=True) covers attached/anonymous volumes, but
    # named volumes may need explicit removal by the SDK.
    volumes = getattr(client, "volumes", None)
    if volumes is None:
        return
    seen_vols: set[str] = set()
    for key in podman.LABEL_CONV_KEYS:
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


def _destroy_conversation_resources(svc: PodmanSandboxService, conversation_id: str) -> None:
    try:
        client = svc._client()
        workspace_volume_names = _sweep_containers(svc, client, conversation_id)
        _sweep_networks(client, conversation_id)
        _sweep_volumes(client, conversation_id, workspace_volume_names)
    except Exception:  # noqa: BLE001 — best-effort
        pass


async def destroy_by_conversation(svc: PodmanSandboxService, conversation_id: str) -> None:
    """Destroy all disco-sbx-*/pmx-sbx-* containers labelled with this conversation_id."""
    await asyncio.to_thread(_destroy_conversation_resources, svc, conversation_id)
