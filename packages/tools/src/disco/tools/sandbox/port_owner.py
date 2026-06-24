import json
import shlex
from dataclasses import dataclass

from disco.core.loop.preview_target import PORT_OWNER_PROBE_SRC

from .base import SandboxInstance


@dataclass
class PortOwner:
    port: int
    pid: int | None
    cmdline: str | None = None
    session: str | None = None


# SINGLE source of truth for the /proc + tmux ownership walk lives in core
# (`disco.core.loop.preview_target`) so the finish gate and this tool can never
# disagree about who owns a port. `PortOwner` here just adds `cmdline` to the
# gate's pid+session view.
_PROBE_SRC = PORT_OWNER_PROBE_SRC


async def port_owners(
    instance: SandboxInstance, ports: list[int]
) -> dict[int, PortOwner | None]:
    """Probe many ports in ONE in-container exec (single /proc pass) — the UI
    polls this; per-port execs would multiply SSH round-trips."""
    if not ports:
        return {}
    arg = " ".join(str(p) for p in ports)
    res = await instance.exec_shell(
        f"python3 -c {shlex.quote(_PROBE_SRC)} {arg}", timeout_s=15
    )
    out: dict[int, PortOwner | None] = {p: None for p in ports}
    if res.exit_code != 0:
        return out
    try:
        for data in json.loads(res.stdout):
            out[data["port"]] = PortOwner(
                port=data["port"],
                pid=data.get("pid"),
                cmdline=data.get("cmdline"),
                session=data.get("session"),
            )
    except Exception:  # noqa: BLE001 — malformed probe output → no owners
        return {p: None for p in ports}
    return out


async def port_owner(instance: SandboxInstance, port: int) -> PortOwner | None:
    return (await port_owners(instance, [port])).get(port)
