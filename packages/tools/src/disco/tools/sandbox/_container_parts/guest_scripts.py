"""Embedded, stdlib-only guest-side Python helpers for exec/read/delete/list.

S-W5 D3: the sandbox-to-host boundary is bounded before docker-py/podman-py
materializes a response. Each helper script runs INSIDE the guest and streams
arbitrary output into fixed-size head/tail buffers (and, for exec, a bounded
workspace spill); only that bounded projection crosses the container API. The
argv builders wrap each script with its arguments; the result parsers map the
guest's typed `DISCO_*` failure markers back to host-side exceptions.

Kept as its own module (not inline in `.._container`) purely because embedded
script text counts as logical source under the module-size budget — every
constant here is re-imported into `.._container` unchanged, so
`_container._BOUNDED_EXEC_HELPER` (etc.) keeps resolving for callers/tests
that read it off that module.
"""

from __future__ import annotations

import errno
import json

from ..base import SandboxFileNotFoundError, SandboxPermissionError

# Exec-capture bounds: the in-guest helper streams arbitrary command output
# into fixed-size head/tail buffers and a bounded workspace spill; only that
# bounded projection crosses the container API.
EXEC_CAPTURE_HEAD_BYTES = 64 * 1024
EXEC_CAPTURE_TAIL_BYTES = 64 * 1024
EXEC_CAPTURE_RETURN_BYTES = EXEC_CAPTURE_HEAD_BYTES + EXEC_CAPTURE_TAIL_BYTES
EXEC_CAPTURE_SPILL_BYTES = 1024 * 1024
MAX_SANDBOX_READ_BYTES = 32 * 1024 * 1024
MAX_SANDBOX_LIST_ENTRIES = 4096

_BOUNDED_EXEC_HELPER = r"""
import os
import signal
import subprocess
import sys
import threading

timeout_s = float(sys.argv[1])
command = sys.argv[2]
shell_executable = sys.argv[3]
token = sys.argv[4]
head_cap = int(sys.argv[5])
tail_cap = int(sys.argv[6])
return_cap = int(sys.argv[7])
spill_cap = int(sys.argv[8])
spill_dir = os.path.join(os.getcwd(), ".disco", "spills")
os.makedirs(spill_dir, mode=0o700, exist_ok=True)


class Capture:
    def __init__(self, kind):
        self.kind = kind
        self.total = 0
        self.small = bytearray()
        self.head = bytearray()
        self.tail = bytearray()
        self.relpath = os.path.join(".disco", "spills", token + "." + kind + ".log")
        self.path = os.path.join(os.getcwd(), self.relpath)
        self.spilled = 0
        self.file = open(self.path, "wb")

    def feed(self, chunk):
        self.total += len(chunk)
        if len(self.small) <= return_cap:
            room = return_cap + 1 - len(self.small)
            self.small.extend(chunk[:room])
        if len(self.head) < head_cap:
            self.head.extend(chunk[: head_cap - len(self.head)])
        self.tail.extend(chunk)
        if len(self.tail) > tail_cap:
            del self.tail[:-tail_cap]
        if self.spilled < spill_cap:
            part = chunk[: spill_cap - self.spilled]
            self.file.write(part)
            self.spilled += len(part)

    def finish(self):
        self.file.close()
        if self.total <= return_cap:
            try:
                os.unlink(self.path)
            except OSError:
                pass
            return bytes(self.small[:self.total])
        dropped = max(0, self.total - len(self.head) - len(self.tail))
        marker = (
            "\n[disco: output truncated at sandbox boundary; "
            + str(self.total)
            + " bytes total, "
            + str(dropped)
            + " omitted; bounded prefix saved at "
            + self.relpath
            + " (max "
            + str(spill_cap)
            + " bytes)]\n"
        ).encode()
        return bytes(self.head) + marker + bytes(self.tail)


def drain(stream, capture):
    while True:
        chunk = stream.read(65536)
        if not chunk:
            return
        capture.feed(chunk)


out = Capture("stdout")
err = Capture("stderr")
proc = subprocess.Popen(
    command,
    shell=True,
    # H342: the sandbox image deliberately provisions Bash as the agent user's
    # shell and container command.  Leaving ``shell=True`` ambient selected
    # Debian's dash via /bin/sh, so one-shot shell/DoD commands used a different
    # language than persistent shell sessions.  Pin the shared command contract.
    executable=shell_executable,
    stdin=subprocess.DEVNULL,
    stdout=subprocess.PIPE,
    stderr=subprocess.PIPE,
    start_new_session=True,
)


def forward_termination(signum, _frame):
    # The process-backend wrapper can be cancelled while this helper is waiting.
    # Forward its TERM/INT to the command's separate process group so neither the
    # shell nor its descendants outlive the sandbox task.
    try:
        os.killpg(proc.pid, signum)
    except OSError:
        pass


signal.signal(signal.SIGTERM, forward_termination)
signal.signal(signal.SIGINT, forward_termination)
t_out = threading.Thread(target=drain, args=(proc.stdout, out), daemon=True)
t_err = threading.Thread(target=drain, args=(proc.stderr, err), daemon=True)
t_out.start()
t_err.start()
timed_out = False
try:
    rc = proc.wait(timeout=timeout_s)
except subprocess.TimeoutExpired:
    timed_out = True
    try:
        os.killpg(proc.pid, signal.SIGTERM)
    except OSError:
        pass
    try:
        rc = proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except OSError:
            pass
        proc.wait()
        rc = 137
t_out.join(timeout=5)
t_err.join(timeout=5)
out_bytes = out.finish()
err_bytes = err.finish()
try:
    os.rmdir(spill_dir)
    os.rmdir(os.path.dirname(spill_dir))
except OSError:
    pass
sys.stdout.buffer.write(out_bytes)
sys.stderr.buffer.write(err_bytes)
raise SystemExit(124 if timed_out else rc)
"""


def bounded_exec_argv(command: str, timeout_s: int, *, python: str = "python3") -> list[str]:
    """Return an argv whose stdout/stderr crossing the runtime API is bounded."""
    import secrets

    return [
        python,
        "-c",
        _BOUNDED_EXEC_HELPER,
        str(timeout_s),
        command,
        "/bin/bash",
        secrets.token_hex(8),
        str(EXEC_CAPTURE_HEAD_BYTES),
        str(EXEC_CAPTURE_TAIL_BYTES),
        str(EXEC_CAPTURE_RETURN_BYTES),
        str(EXEC_CAPTURE_SPILL_BYTES),
    ]


_BOUNDED_READ_HELPER = r"""
import errno
import os
import stat
import sys

root, relpath, cap_raw = sys.argv[1:4]
cap = int(cap_raw)
parts = [part for part in relpath.split("/") if part not in ("", ".")]


def fail(kind, detail, code):
    sys.stderr.write("DISCO_READ_" + kind + ":" + detail[:512])
    raise SystemExit(code)


if not parts:
    fail("DENIED", "invalid relative path", 45)

dir_flags = os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_NOFOLLOW", 0)
file_flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
opened = []
try:
    current = os.open(root, dir_flags)
    opened.append(current)
    for part in parts[:-1]:
        if part == "..":
            if len(opened) == 1:
                fail("DENIED", "path escapes workspace", 45)
            os.close(opened.pop())
            current = opened[-1]
            continue
        current = os.open(part, dir_flags, dir_fd=current)
        opened.append(current)
    if parts[-1] == "..":
        fail("DENIED", "directory paths are not readable files", 45)
    fd = os.open(parts[-1], file_flags, dir_fd=current)
    opened.append(fd)
    info = os.fstat(fd)
    if not stat.S_ISREG(info.st_mode):
        fail("DENIED", "only regular, non-symlink files may be read", 45)
    if info.st_size > cap:
        fail("TOO_LARGE", str(info.st_size), 46)
    remaining = cap + 1
    chunks = []
    while remaining:
        chunk = os.read(fd, min(1024 * 1024, remaining))
        if not chunk:
            break
        chunks.append(chunk)
        remaining -= len(chunk)
    data = b"".join(chunks)
    if len(data) > cap:
        fail("TOO_LARGE", "file grew while being read", 46)
    sys.stdout.buffer.write(data)
except FileNotFoundError as exc:
    fail("MISSING", str(exc), 44)
except OSError as exc:
    if exc.errno in {errno.ELOOP, errno.ENOTDIR, errno.EACCES, errno.EPERM}:
        fail("DENIED", str(exc), 45)
    fail("ERROR", str(exc), 47)
finally:
    for descriptor in reversed(opened):
        try:
            os.close(descriptor)
        except OSError:
            pass
"""


def bounded_read_argv(workspace: str, relpath: str, *, python: str = "python3") -> list[str]:
    """Open and read a workspace file through no-follow directory descriptors."""
    return [python, "-c", _BOUNDED_READ_HELPER, workspace, relpath, str(MAX_SANDBOX_READ_BYTES)]


def bounded_read_result(path: str, rc: int, out: bytes, err: bytes) -> bytes:
    """Map the guest helper's bounded result to the sandbox read contract."""
    detail = err.decode("utf-8", "replace")
    if rc == 0:
        return out
    if detail.startswith("DISCO_READ_MISSING:"):
        # This parser runs inside ContainerInstance._guarded().  A native
        # FileNotFoundError would be mistaken for a raw container-client
        # failure and reclassified as a generic SandboxError.  Preserve the
        # expected filesystem result with the dual-typed sandbox exception so
        # callers can both recognize absence and distinguish it from transport
        # failure.
        raise SandboxFileNotFoundError(f"read_file {path!r}: file does not exist")
    if detail.startswith("DISCO_READ_DENIED:"):
        raise SandboxPermissionError(
            f"read_file {path!r}: only regular, non-symlink workspace files may be read"
        )
    if detail.startswith("DISCO_READ_TOO_LARGE:"):
        raise OSError(
            errno.EFBIG,
            f"read_file {path!r} exceeds the {MAX_SANDBOX_READ_BYTES}-byte transfer cap",
        )
    raise OSError(f"read_file {path!r} failed safely: {detail or f'exit {rc}'}")


_BOUNDED_DELETE_HELPER = r"""
import errno
import os
import stat
import sys

root, relpath = sys.argv[1:3]
parts = [part for part in relpath.split("/") if part not in ("", ".")]


def fail(kind, detail, code):
    sys.stderr.write("DISCO_DELETE_" + kind + ":" + detail[:512])
    raise SystemExit(code)


if not parts or any(part == ".." for part in parts):
    fail("DENIED", "invalid relative path", 45)

dir_flags = os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_NOFOLLOW", 0)
opened = []
try:
    current = os.open(root, dir_flags)
    opened.append(current)
    for part in parts[:-1]:
        current = os.open(part, dir_flags, dir_fd=current)
        opened.append(current)
    info = os.stat(parts[-1], dir_fd=current, follow_symlinks=False)
    if not stat.S_ISREG(info.st_mode):
        fail("DENIED", "only regular, non-symlink files may be deleted", 45)
    os.unlink(parts[-1], dir_fd=current)
except FileNotFoundError as exc:
    fail("MISSING", str(exc), 44)
except OSError as exc:
    if exc.errno in {errno.ELOOP, errno.ENOTDIR, errno.EACCES, errno.EPERM}:
        fail("DENIED", str(exc), 45)
    fail("ERROR", str(exc), 47)
finally:
    for descriptor in reversed(opened):
        try:
            os.close(descriptor)
        except OSError:
            pass
"""


def bounded_delete_argv(workspace: str, relpath: str, *, python: str = "python3") -> list[str]:
    """Unlink a regular workspace entry through no-follow directory descriptors."""
    return [python, "-c", _BOUNDED_DELETE_HELPER, workspace, relpath]


def bounded_delete_result(path: str, rc: int, err: bytes) -> None:
    detail = err.decode("utf-8", "replace")
    if rc == 0:
        return
    if detail.startswith("DISCO_DELETE_MISSING:"):
        raise SandboxFileNotFoundError(f"delete_file {path!r}: file does not exist")
    if detail.startswith("DISCO_DELETE_DENIED:"):
        raise SandboxPermissionError(
            f"delete_file {path!r}: only regular, non-symlink workspace files may be deleted"
        )
    raise OSError(f"delete_file {path!r} failed safely: {detail or f'exit {rc}'}")


_BOUNDED_LIST_HELPER = r"""
import json
import os
import sys

target = sys.argv[1]
limit = int(sys.argv[2])
entries_out = []
try:
    with os.scandir(target) as entries:
        for entry in entries:
            if entry.is_symlink():
                kind = "other"
            elif entry.is_file(follow_symlinks=False):
                kind = "file"
            elif entry.is_dir(follow_symlinks=False):
                kind = "directory"
            else:
                kind = "other"
            entries_out.append((entry.name, kind))
            if len(entries_out) > limit:
                break
    payload = {
        "entries": sorted(entries_out[:limit]),
        "truncated": len(entries_out) > limit,
    }
    sys.stdout.write(json.dumps(payload, ensure_ascii=True, separators=(",", ":")))
except OSError as exc:
    errno_value = exc.errno if isinstance(exc.errno, int) else "unknown"
    sys.stderr.write(f"DISCO_LIST_ERROR:{type(exc).__name__}:{errno_value}")
    raise SystemExit(48)
"""


def bounded_list_argv(path: str, limit: int, *, python: str = "python3") -> list[str]:
    """Inspect at most ``limit`` entries in the guest without materializing the directory."""

    if limit <= 0 or limit > MAX_SANDBOX_LIST_ENTRIES:
        raise ValueError(f"list limit must be between 1 and {MAX_SANDBOX_LIST_ENTRIES}")
    return [python, "-c", _BOUNDED_LIST_HELPER, path, str(limit)]


def _parse_list_error(path: str, rc: int, err: bytes) -> None:
    detail = err.decode("utf-8", "replace")
    fields = detail.split(":")
    if (
        len(fields) == 3
        and fields[0] == "DISCO_LIST_ERROR"
        and fields[1].isidentifier()
        and fields[2].isdigit()
    ):
        raise OSError(int(fields[2]), f"list_dir_bounded {path!r} failed safely: {fields[1]}")
    raise OSError(f"list_dir_bounded {path!r} failed safely: {detail or f'exit {rc}'}")


def _is_valid_list_entry(entry: object) -> bool:
    return (
        isinstance(entry, list)
        and len(entry) == 2
        and isinstance(entry[0], str)
        and entry[1] in {"file", "directory", "other"}
    )


def _validate_list_payload(path: str, payload: object) -> None:
    if (
        not isinstance(payload, dict)
        or set(payload) != {"entries", "truncated"}
        or not isinstance(payload["entries"], list)
        or not all(_is_valid_list_entry(entry) for entry in payload["entries"])
        or not isinstance(payload["truncated"], bool)
    ):
        raise OSError(f"list_dir_bounded {path!r} returned invalid evidence")


def bounded_list_result(
    path: str,
    rc: int,
    out: bytes,
    err: bytes,
) -> tuple[list[tuple[str, str]], bool]:
    if rc != 0:
        _parse_list_error(path, rc, err)
    try:
        payload = json.loads(out.decode("utf-8", "strict"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise OSError(f"list_dir_bounded {path!r} returned malformed evidence") from exc
    _validate_list_payload(path, payload)
    return [tuple(entry) for entry in payload["entries"]], payload["truncated"]
