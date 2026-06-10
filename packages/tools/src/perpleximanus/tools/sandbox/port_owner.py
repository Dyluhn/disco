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
        out = subprocess.check_output(['tmux', 'list-panes', '-a', '-F', '#{pane_pid} #{session_name}']).decode('utf-8')
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
    target_port = int(sys.argv[1])
    port_hex = f"{target_port:04X}"
    inodes = read_tcp_listen(port_hex)
    
    if not inodes:
        print(json.dumps({"port": target_port, "pid": None}))
        sys.exit(0)

    found_pid = None
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
                    if link.startswith('socket:[') and link[8:-1] in inodes:
                        found_pid = int(pid_str)
                        break
                except Exception:
                    pass
            if found_pid:
                break
        except Exception:
            pass

    if not found_pid:
        print(json.dumps({"port": target_port, "pid": None}))
        sys.exit(0)

    cmdline = ""
    try:
        with open(f'/proc/{found_pid}/cmdline', 'rb') as f:
            cmdline = f.read().replace(b'\\x00', b' ').decode('utf-8').strip()
    except Exception:
        pass

    panes = get_tmux_panes()
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

    print(json.dumps({"port": target_port, "pid": found_pid, "cmdline": cmdline, "session": session}))

if __name__ == "__main__":
    main()
"""


async def port_owner(instance: SandboxInstance, port: int) -> PortOwner | None:
    res = await instance.exec_shell(f"python3 -c {shlex.quote(_PROBE_SRC)} {port}", timeout_s=15)
    if res.exit_code != 0:
        return None
    try:
        data = json.loads(res.stdout)
        return PortOwner(
            port=data.get("port", port),
            pid=data.get("pid"),
            cmdline=data.get("cmdline"),
            session=data.get("session")
        )
    except Exception:
        return None
