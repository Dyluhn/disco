"""Sandbox spec / instance / service — tool-sandbox-contract.md §5, §7.

Spec-vs-instance split [OH]: a `SandboxSpec` (template: image, limits, permitted
capabilities, egress allowlist) is distinct from a `SandboxInstance` (a running,
lifecycle-managed environment). Tools execute against an instance. Backends are
pluggable behind `SandboxService` (process/e2b/gvisor/remote) — the executor and
tools depend only on these protocols (§5.1, the "door open" seam).
"""

from __future__ import annotations

from typing import NoReturn, Protocol, runtime_checkable

from disco.core.workspace_paths import (
    strip_redundant_workspace_prefix as strip_redundant_workspace_prefix,
)
from pydantic import BaseModel, ConfigDict

from ..anatomy import Capability


def clean_sandbox_env(workspace: object) -> dict[str, str]:
    """The allowlisted child environment for host-visible sandbox subprocesses on
    the `process` (dev) backend — the shell path AND the jupyter kernel path.

    W3 C-4: nothing from `os.environ` is inherited, so the master `DISCO_SECRET_KEY`,
    the OpenRouter key, and every other host secret stay out of reach of the
    untrusted model code that runs here. Factored to ONE place so the shell and
    kernel launchers cannot drift (the kernel path used to inherit `os.environ`
    wholesale and leaked the master key to `code_exec`)."""
    ws = str(workspace)
    return {
        "PATH": "/usr/local/bin:/usr/bin:/bin",
        "HOME": ws,
        "TMPDIR": ws,
        # Browser/helper subprocesses use the same guest contract as container
        # backends, but their real process-backend workspace is a host path.
        # This is capability metadata, not inherited host configuration.
        "DISCO_WORKSPACE": ws,
    }



class SandboxError(Exception):
    """Raised by an instance on a backend/lifecycle failure (e.g. use after
    destroy). The executor maps it to a `sandbox_error` ToolResult."""


class SandboxUnavailableError(SandboxError):
    """The backend infrastructure itself is unusable — Docker unreachable, the
    `runsc` runtime missing, the base image absent, the container failed to start.
    Carries the real underlying cause (reactive-error rule); distinct from a
    per-command failure so the caller can tell "the box is broken" from "the
    command failed"."""


class ProductionValidityError(SandboxError):
    """EPIC H (§1.4/§9.3): a NON-production-valid backend was selected for a path that
    requires real isolation (Build-Soak / production build). The `process` backend
    shares the host PID + network namespace — it is dev-only, and is exactly the
    source of the isolation incidents (a build `kill <pid>` took down the
    agent-server). Raised by `require_production_valid_backend(...)` BEFORE any work
    runs, naming the offending backend and the valid alternatives so the operator can
    re-select. Distinct from SandboxUnavailableError: the box isn't broken, it is the
    WRONG KIND of box for this path."""


class SandboxFileNotFoundError(SandboxError, FileNotFoundError):
    """W1: a workspace file/dir was MISSING on a read. Subclasses BOTH SandboxError (so the
    executor's `sandbox_error` mapping still applies) AND FileNotFoundError (so the loop's
    ~8 ``except (FileNotFoundError, OSError)`` bookkeeping handlers — file_state,
    recitation, observe, view_render, … — catch it instead of letting it ESCAPE and crash
    the loop task into a silent forever-RUNNING hang). The container/podman backends used to
    wrap a missing file in a plain SandboxError, defeating those handlers; the local
    (process) backend already raised native FileNotFoundError. This makes them agree."""


class SandboxPermissionError(SandboxError, PermissionError):
    """W1: a read was denied (EACCES). Typed so `except (PermissionError, OSError)` loop
    handlers catch it instead of a bare SandboxError crashing the task."""


class SandboxNotADirectoryError(SandboxError, NotADirectoryError):
    """W1: a path component was a file, not a directory (ENOTDIR). Typed for the same reason
    — `except (NotADirectoryError, OSError)` handlers must catch it."""


class SandboxIsADirectoryError(SandboxError, IsADirectoryError):
    """W1: a `cat`/read was pointed at a DIRECTORY (EISDIR). `cat <dir>` emits "Is a
    directory"; typed so `except (IsADirectoryError, OSError)` handlers catch it rather than a
    bare SandboxError escaping the loop (codex round-3)."""


# stderr signatures from a failed `cat`/read, by errno class. Substring match on the
# lowercased message — `cat`/coreutils phrase these consistently across the backends.
_NOT_FOUND_MARKER = "no such file"
_IS_A_DIR_MARKER = "is a directory"
_PERMISSION_MARKERS = ("permission denied", "operation not permitted")
_NOT_A_DIR_MARKER = "not a directory"


def looks_like_not_found(stderr: bytes | str) -> bool:
    """True iff a failed read's stderr indicates a MISSING FILE (ENOENT), vs a missing
    TOOL ("cat: not found"), permission error, etc. Lets the boundary type the error so a
    renamed/deleted file becomes a FileNotFoundError, not a fatal untyped escape."""
    s = stderr.decode("utf-8", "replace") if isinstance(stderr, bytes) else stderr
    return _NOT_FOUND_MARKER in s.lower()


def raise_read_error(path: str, stderr: bytes | str, *, op: str = "read_file") -> NoReturn:
    """The SINGLE boundary every backend's filesystem op (`read_file`, `list_dir`, …) uses to
    raise a failure. Maps the stderr onto the matching NATIVE OSError subtype — missing file →
    SandboxFileNotFoundError (FileNotFoundError), denied → SandboxPermissionError
    (PermissionError), ENOTDIR → SandboxNotADirectoryError (NotADirectoryError) — so the loop's
    `except (FileNotFoundError, OSError)` bookkeeping handlers catch ALL benign fs failures
    (read OR list), not just missing-file reads. Anything unrecognised stays a plain
    SandboxError (still never a bare/untyped escape)."""
    s = (stderr.decode("utf-8", "replace") if isinstance(stderr, bytes) else stderr).strip()
    low = s.lower()
    if _NOT_FOUND_MARKER in low:
        raise SandboxFileNotFoundError(f"{op} {path!r}: {s}")
    if _IS_A_DIR_MARKER in low:  # `cat <dir>` (EISDIR) — checked before ENOTDIR (both contain
        raise SandboxIsADirectoryError(f"{op} {path!r}: {s}")  # "a directory")
    if _NOT_A_DIR_MARKER in low:
        raise SandboxNotADirectoryError(f"{op} {path!r}: {s}")
    if any(m in low for m in _PERMISSION_MARKERS):
        raise SandboxPermissionError(f"{op} {path!r}: {s}")
    raise SandboxError(f"{op} {path!r}: {s}")


class ExecResult(BaseModel):
    model_config = ConfigDict(frozen=True)
    exit_code: int
    stdout: str
    stderr: str
    # [v1.1 adjustment, flagged] A command killed for exceeding its timeout is
    # reported with this flag set (not raised), so partial stdout/stderr + the exit
    # code survive and the caller can distinguish a timeout from a normal non-zero
    # exit. Additive + defaulted, so existing callers are unaffected.
    timed_out: bool = False


class SandboxSpec(BaseModel):
    """[CONTRACT] The template an instance is created from. Pure config."""

    model_config = ConfigDict(frozen=True)
    image: str = "python-node-base"  # base image id [VERIFY contents]
    # capabilities the instance is permitted — the union of the scope's tools'
    # `needs` (§2). Anything not listed is denied.
    permitted: frozenset[Capability] = frozenset()
    cpu: float = 1.0
    memory_mb: int = 2048
    disk_mb: int = 4096
    # EPIC H host-protection: cap the container's process count (cgroup pids.max) so a
    # runaway build (fork bomb / npm-install storm) can't exhaust host PIDs and freeze
    # the box. 0 => unset on the spec, so the backend falls back to its configured
    # default (`SandboxConfig.default_pids_limit`, 512). A spec MAY tighten/loosen it.
    pids: int = 0
    timeout_s: int = 300  # default per-call ceiling
    # egress allowlist (§7); empty => deny ALL network (deny-by-default).
    egress_allow: frozenset[str] = frozenset()
    # S-W5 D1: browser/agent surfaces may reach arbitrary PUBLIC web origins,
    # but still traverse the isolated proxy boundary which rejects non-global,
    # host-owned, metadata, sibling, and private addresses. Legacy NETWORK
    # grants resolve to this same boundary; no model-shaped spec gets a raw bridge.
    public_web: bool = False
    # A narrow sandbox -> host-service capability. This does NOT carry a bearer or
    # upstream address. Backends provision a lifecycle-owned relay and expose only
    # its non-secret internal URL; the runtime injector owns credentials separately.
    host_services: bool = False

    def egress_allowed(self, host: str) -> bool:
        """[CONTRACT §7] Deny-by-default: a host is reachable only if explicitly
        allow-listed. Enforced outside the guest by the backend (§7 rule 3).

        Matching: an exact host (`api.example.com`) OR a leading-dot suffix entry
        (`.example.com`) which matches the apex and any subdomain. This is the
        predicate an egress-interception proxy consults per connection (Cluster 3).
        """
        h = host.lower().strip()
        for entry in self.egress_allow:
            e = entry.lower().strip()
            if e.startswith("."):
                # `.example.com` → matches `example.com` and `*.example.com`
                if h == e[1:] or h.endswith(e):
                    return True
            elif h == e:
                return True
        return False


REGISTRY_EGRESS_ALLOW: frozenset[str] = frozenset(
    {
        "registry.npmjs.org",
        ".npmjs.org",
        "pypi.org",
        "files.pythonhosted.org",
        "github.com",
        "codeload.github.com",
        ".githubusercontent.com",
        "deb.debian.org",
        "security.debian.org",
        "cdn.jsdelivr.net",
        "unpkg.com",
        "esm.sh",
        "fonts.googleapis.com",
        "fonts.gstatic.com",
    }
)


@runtime_checkable
class SandboxInstance(Protocol):
    """[CONTRACT] A running, isolated environment. Tools execute against it.
    Lifecycle is owned by the agent-server for its conversation (BoD §5.2)."""

    id: str
    owner_id: str
    conversation_id: str
    spec: SandboxSpec

    async def exec_shell(self, cmd: str, *, timeout_s: int) -> ExecResult: ...
    async def read_file(self, path: str) -> bytes: ...
    async def write_file(self, path: str, data: bytes) -> None: ...
    async def list_dir(self, path: str) -> list[str]: ...
    # [B4] Existence check resolved in the BACKEND's own namespace — the only
    # honest answer for a container backend whose `workspace_path` is None (the
    # host has no view of the box FS). C18's done-condition `file_exists`
    # predicate calls this instead of a host `Path.exists()`. Returns False on a
    # missing file or a path escaping the workspace jail; never raises on absence.
    async def file_exists(self, path: str) -> bool: ...
    def display_url(self) -> str | None: ...  # live noVNC view, if supported
    # [ADDITIVE — flagged] Expose an inside-the-box dev-server port to a URL the user's
    # browser can reach, by the method appropriate to THIS backend's transport (the
    # backend owns "how to reach a port inside me"). Returns None if not supported /
    # not exposed. Like `timed_out` / `SandboxUnavailableError`, this is additive — it
    # does not change existing behavior; backends that don't implement it return None.
    def expose_port(self, port: int) -> str | None: ...
    async def destroy(self) -> None: ...
    # [W5 — workspace_path] Workspace root as an absolute host-FS path (process
    # backend) or None (container backends where the host has no direct view of
    # the box FS). C18 and C1c consume this to resolve done-condition predicates.
    @property
    def workspace_path(self) -> str | None: ...


@runtime_checkable
class HostServiceRelayInstance(Protocol):
    """Optional additive seam implemented only by relay-capable real backends."""

    @property
    def host_service_relay_url(self) -> str | None:
        """Non-secret URL reachable only inside this sandbox's network."""
        ...


@runtime_checkable
class SandboxService(Protocol):
    """[CONTRACT] Creates/destroys instances from specs. Pluggable backend."""

    name: str  # "process" | "e2b" | "gvisor" | "remote"

    # EPIC H (§1.4/§9.3): is this backend valid for a production / Build-Soak build?
    # FALSE only for the `process` dev backend (shared host PID + net namespace — the
    # source of the isolation incidents). TRUE for every container backend
    # (gvisor/local/podman), each of which has its OWN PID + network namespace. The
    # production build path consults this (`require_production_valid_backend`) and
    # refuses a non-valid backend up front.
    is_production_valid: bool

    async def create(
        self, spec: SandboxSpec, *, owner_id: str, conversation_id: str
    ) -> SandboxInstance: ...

    async def get(self, instance_id: str) -> SandboxInstance | None: ...

    async def healthcheck(self) -> None:
        """[W-48] Connectivity PREFLIGHT. Probe THIS backend's endpoint (the Docker
        socket / ssh:// host for the container backends; the Podman native-remote
        socket; the workspace root for the process backend) and return None when the
        backend is reachable + usable. On failure raise a typed
        ``SandboxUnavailableError`` whose message NAMES the endpoint and the real
        reason (e.g. "Docker unreachable at ssh://sandbox@<host>: <cause>") so the
        Settings save / first-use path can surface the truth instead of a silent
        failure or a generic 500 later. MUST be bounded (run the blocking client work
        in a thread; the client carries its own socket timeout) so an unreachable host
        fails fast rather than hanging."""
        return None

    async def list_live_instances(self) -> list[str]:
        """Return the conversation_ids of all live sandbox containers on this backend.
        Container backends return conversation_ids of running pmx-sbx-* containers;
        process backend returns [] (no containers to inspect after restart)."""
        return []

    async def destroy_by_conversation(self, conversation_id: str) -> None:
        """Destroy all recoverable backend resources owned by a conversation.

        Container backends remove labeled sandbox/egress containers. Process backends
        remove exact conversation-namespaced host process sessions. The default remains
        a no-op for services with no recoverable resource layer.
        """
