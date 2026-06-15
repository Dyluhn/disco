import json
import shlex
from dataclasses import dataclass

from .base import SandboxInstance


@dataclass
class PortOwner:
    port: int
    pid: int | None
    cmdline: str | None = None
    session: str | None = None


_PROBE_SRC = """\
import json
import os
import subprocess
import sys

def get_tmux_panes():
    try:
        out = subprocess.check_output(
            ['tmux', 'list-panes', '-a', '-F', '#{pane_pid} #{session_name}']
        ).decode('utf-8')
        res = {}
        for line in out.splitlines():
            parts = line.strip().split()
            if len(parts) >= 2:
                res[int(parts[0])] = parts[1]
        return res
    except Exception:
        return {}

def read_tcp_listen(port_hex):
    inodes = set()
    for net_file in ['/proc/net/tcp', '/proc/net/tcp6']:
        try:
            with open(net_file) as f:
                for line in f.readlines()[1:]:
                    parts = line.strip().split()
                    if len(parts) >= 10:
                        local_addr = parts[1]
                        state = parts[3]
                        inode = parts[9]
                        if state == '0A' and local_addr.endswith(':' + port_hex):
                            inodes.add(inode)
        except Exception:
            pass
    return inodes

def main():
    ports = [int(a) for a in sys.argv[1:]]
    inode_to_port = {}
    for target_port in ports:
        for ino in read_tcp_listen(f"{target_port:04X}"):
            inode_to_port.setdefault(ino, target_port)

    port_to_pid = {}
    if inode_to_port:
        for pid_str in os.listdir('/proc'):
            if not pid_str.isdigit():
                continue
            fd_dir = f'/proc/{pid_str}/fd'
            if not os.path.isdir(fd_dir):
                continue
            try:
                for fd in os.listdir(fd_dir):
                    try:
                        link = os.readlink(f'{fd_dir}/{fd}')
                        if link.startswith('socket:['):
                            ino = link[8:-1]
                            if ino in inode_to_port:
                                port_to_pid.setdefault(inode_to_port[ino], int(pid_str))
                    except Exception:
                        pass
            except Exception:
                pass

    panes = get_tmux_panes()
    results = []
    for target_port in ports:
        found_pid = port_to_pid.get(target_port)
        if not found_pid:
            results.append({"port": target_port, "pid": None})
            continue

        cmdline = ""
        try:
            with open(f'/proc/{found_pid}/cmdline', 'rb') as f:
                cmdline = f.read().replace(b'\\x00', b' ').decode('utf-8').strip()
        except Exception:
            pass

        current_pid = found_pid
        session = None
        for _ in range(6):
            if current_pid in panes:
                session = panes[current_pid]
                break
            try:
                with open(f'/proc/{current_pid}/stat') as f:
                    stat_data = f.read().split()
                    if len(stat_data) >= 4:
                        current_pid = int(stat_data[3])
                    else:
                        break
            except Exception:
                break

        results.append(
            {"port": target_port, "pid": found_pid, "cmdline": cmdline, "session": session}
        )

    print(json.dumps(results))

if __name__ == "__main__":
    main()
"""


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
