"""`ContainerInstance.destroy` + the shared policy/control-aux teardown.

Every container backend uses an ephemeral, quota-capped workspace volume
which is explicitly removed during teardown, and every mode (sealed/filtered/
public) owns a policy/control sidecar + internal network torn down here too —
the ONE shared teardown path every backend's `create()` composes against.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .._container import ContainerInstance


def teardown_egress_aux(instance: ContainerInstance) -> None:
    """Best-effort teardown of the policy/control sidecar + internal network.
    Refs may be None only for partial construction or a backend without this
    topology. The order MATTERS: the sidecar is on the internal net, so we
    remove the sidecar first, then the net (else the net remove errors out on
    an attached endpoint)."""
    from .. import _container

    sidecar, network = instance._egress_sidecar, instance._egress_network
    if sidecar is None and network is None:
        return
    if sidecar is not None:
        try:
            sidecar.stop(timeout=2)
        except Exception:  # noqa: BLE001 — best-effort; force-remove next
            pass
        try:
            _container._remove_container(sidecar)
        except Exception:  # noqa: BLE001 — already gone is fine
            pass
    if network is not None:
        try:
            network.remove()
        except Exception:  # noqa: BLE001 — still-attached (we removed the sidecar
            # above, but the client may not see it yet) or already gone
            pass


async def destroy(instance: ContainerInstance) -> None:
    """Stop + remove the container, then tear down the policy/control aux (if
    any). Every container backend uses an ephemeral, quota-capped workspace
    volume which is explicitly removed during teardown. The aux
    teardown is shared across every container backend — see
    `_teardown_egress_aux`."""
    if instance._destroyed:
        return

    def _teardown() -> None:
        from .. import _container

        try:
            instance._container.stop(timeout=instance._stop_timeout_s)
        except Exception:  # noqa: BLE001 — best-effort stop; force-remove next
            pass
        if instance._loopback_tunnel is not None:
            instance._loopback_tunnel.close()
        # Successful force removal is the process-termination proof consumed
        # by SandboxSession and Preview. Never turn a failed removal into a
        # successful destroy or discard the retryable instance handle.
        _container._remove_container(instance._container)
        try:
            _container._remove_volume(instance._workspace_volume)
        except Exception:  # noqa: BLE001 — already gone is fine
            pass

    await asyncio.to_thread(_teardown)
    instance._destroyed = True
    # Aux AFTER the sandbox: the sandbox is on the internal net; removing the
    # net while the sandbox still references it would error out.
    await asyncio.to_thread(instance._teardown_egress_aux)
