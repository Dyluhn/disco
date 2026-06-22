"""Shared container-backend logic — the part identical across the gVisor (docker-py)
and Podman (podman-py) backends, factored out so each service is just its create
path (tool-sandbox §5.1; the interface is unchanged).

Both SDKs expose a duck-type-compatible Container: `exec_run(cmd, demux, workdir)`,
`put_archive(path, data)`, `stop(timeout)`, `remove(force)`. So one `ContainerInstance`
drives both. File ops go through the container (exec/cp), never a host path, so they
work over any transport (local socket or Docker/Podman-over-SSH).
"""

from __future__ import annotations

import asyncio
import io
import posixpath
import tarfile
import threading
import time
from typing import Any

from ..anatomy import Capability
from .base import (
    ExecResult,
    SandboxError,
    SandboxPermissionError,
    SandboxSpec,
    SandboxUnavailableError,
    raise_read_error,
    strip_redundant_workspace_prefix,
)

# Exit codes the `timeout` coreutil reports when it fires (SIGTERM / then SIGKILL).
TIMEOUT_EXIT_CODES = frozenset({124, 137})

# Default upper bound on a docker-py / podman-py `reload()` call (Dispo #25 wedge-guard).
# A healthy reload is sub-ms; this is ~500x that — large enough to absorb a brief
# scheduler hiccup, small enough that a dead daemon can't wedge a session. The
# value is overridable per-instance (constructor kwarg, typically sourced from
# `SandboxConfig.reload_timeout_s` for hot-apply via the Settings layer). Never
# `None` (a missing config must still keep the loop bounded).
_DEFAULT_RELOAD_TIMEOUT_S: float = 0.5

# The user-facing dev-server port (the primary preview) plus a curated set of
# framework-default ports a build may legitimately bind (API on 3000, Vite's
# native 5173, …). Docker cannot add mappings to a RUNNING container, so the
# publishable set is declared at create time. Containment: nothing outside
# PUBLISHED_PORTS is ever reachable, and INTERNAL_PORTS (agent-server plumbing,
# e.g. the BP-08 kernel gateway) are never handed out as user URLs.
PREVIEW_PORT = 8000
# noVNC websockify port — the WebSocket-to-VNC bridge that the frontend's iframe
# connects to. VNC itself (port 5901) is loopback-bound inside the sandbox and is
# NOT in USER_PORTS; only the websockify bridge (NOVNC_PORT) is exposed via the
# existing per-conversation preview proxy (same auth/jail as the dev-server preview).
NOVNC_PORT = 6080
USER_PORTS: frozenset[int] = frozenset({8000, 3000, 5173, 8080, 5000, 4321, NOVNC_PORT})
INTERNAL_PORTS: frozenset[int] = frozenset({8899})
PUBLISHED_PORTS: frozenset[int] = USER_PORTS | INTERNAL_PORTS


def sealed(spec: SandboxSpec) -> bool:
    """Network is SEALED unless the capability set grants it (§7 deny-by-default):
    a non-empty egress allowlist, or NETWORK in `permitted`. Default => sealed."""
    return not spec.egress_allow and Capability.NETWORK not in spec.permitted


# The single egress proxy port a filtered box reaches its allowlisting sidecar on.
EGRESS_PROXY_PORT = 8888


def egress_mode(spec: SandboxSpec) -> str:
    """Resolve a spec's egress posture to one of THREE modes — the fix for the old
    binary (none|bridge) that silently gave an allowlisted box full network:

      • "sealed"   — no allowlist, no NETWORK cap → no network at all.
      • "filtered" — a non-empty `egress_allow` → an allowlisting PROXY sidecar
                     enforces the list per-connection; the box's only route out is
                     the proxy (internal, no-NAT network). THIS is what makes the
                     allowlist real instead of a false guarantee.
      • "open"     — NETWORK capability granted with NO allowlist → deliberate raw
                     egress (e.g. the browser tool needs the whole web). An explicit
                     capability grant, not an unenforced allowlist.

    `filtered` takes precedence: an allowlist always means "only these", even if the
    NETWORK capability is also present."""
    if spec.egress_allow:
        return "filtered"
    if Capability.NETWORK in spec.permitted:
        return "open"
    return "sealed"


def format_allow(entries: frozenset[str]) -> str:
    """Render an allowlist as the comma string the proxy CLI (`--allow`) consumes.
    Sorted for determinism (stable container args → reproducible runs)."""
    return ",".join(sorted(e.strip() for e in entries if e.strip()))


def proxy_env(proxy_host: str, port: int = EGRESS_PROXY_PORT) -> dict[str, str]:
    """The HTTP(S)_PROXY environment a filtered sandbox gets so proxy-aware clients
    (curl, pip, npm, requests) route through the allowlisting sidecar. Defense in
    depth only — the no-NAT network is the real containment; a client that ignores
    these vars still has no route except the proxy. Both cases (lower/upper) are set
    because different tools read different ones."""
    url = f"http://{proxy_host}:{port}"
    return {
        "HTTP_PROXY": url,
        "HTTPS_PROXY": url,
        "http_proxy": url,
        "https_proxy": url,
        # never proxy loopback (a dev server talking to itself)
        "NO_PROXY": "localhost,127.0.0.1",
        "no_proxy": "localhost,127.0.0.1",
    }


def proxy_run_argv(allow: str, port: int = EGRESS_PROXY_PORT) -> list[str]:
    """The command that runs the allowlisting proxy inside the sidecar. The proxy
    script is `put_archive`'d to /egress_proxy.py first (it's stdlib-only, so the
    base image's python3 runs it with no install)."""
    return ["python3", "/egress_proxy.py", "--port", str(port), "--allow", allow]


class ContainerInstance:
    """A running container (gVisor or Podman). Tools execute against it; the
    workspace is reached only through the file methods (exec/cp), never a path."""

    # Set by the service for a "filtered" box (allowlist + proxy sidecar + internal
    # net); None otherwise. Class attrs (not __init__ params) so the destroy()
    # teardown is inherited uniformly across every backend that supports the
    # allowlisting aux (gVisor / Podman / local). The service mutates the INSTANCE
    # attr in create() — that shadows the class attr with a real object.
    _egress_sidecar: Any | None = None
    _egress_network: Any | None = None

    def __init__(
        self,
        *,
        id: str,
        owner_id: str,
        conversation_id: str,
        spec: SandboxSpec,
        container: Any,
        container_workspace: str,
        stop_timeout_s: int,
        workspace_uid: int = 1000,
        preview_host: str = "localhost",
        reload_timeout_s: float = _DEFAULT_RELOAD_TIMEOUT_S,
    ) -> None:
        self.id = id
        self.owner_id = owner_id
        self.conversation_id = conversation_id
        self.spec = spec
        self._container = container
        self._ws = container_workspace
        self._stop_timeout_s = stop_timeout_s
        # the host the published preview port is reachable at (localhost for a local
        # backend; the remote Docker host's tailnet IP for gVisor).
        self._preview_host = preview_host
        # The image's run-user uid (contract: `agent` = 1000). Written files are owned
        # by it so the sandbox user can EDIT them — put_archive defaults to uid 0 (root),
        # which a non-root container user can read but not modify.
        self._workspace_uid = workspace_uid
        # Wedge-guard bound for `_safe_reload()` (Dispo #25). A hung or failing
        # docker/podman client must never block the event loop. The service sources
        # this from `SandboxConfig.reload_timeout_s` at create time (hot-apply).
        self._reload_timeout_s = reload_timeout_s
        self._destroyed = False

    def _alive(self) -> None:
        if self._destroyed:
            raise SandboxError(f"sandbox instance {self.id} has been destroyed")

    def _safe_reload(self, obj: Any = None) -> bool:
        """Bounded wrapper around `reload()` (docker-py / podman-py) on `obj`, which
        defaults to `self._container`. [FIX6] passing the egress SIDECAR lets
        `_resolve_mapping` refresh the sidecar's published-port bindings under the
        SAME wedge-guard (the filtered-box preview is published on the sidecar).

        A hung or failing client must NEVER wedge the event loop. The original
        VM 201/202 incident that motivated this guard: a docker daemon stall
        caused the agent loop to block indefinitely inside `reload()`; the
        whole conversation hung waiting for an HTTP call that never returned.

        Mechanism: run the blocking call in a daemon thread, then wait on a
        `threading.Event` with `_reload_timeout_s`. The thread is `daemon=True`
        so even if it never returns, it cannot block process exit (the main
        thread bails out cleanly, the test fake's blocker is released by the
        test's `finally:` clause). Returning status: True if the reload
        succeeded AND the container reports 'running'; False on either a
        non-running state or a raised error.

        On ANY of:
          - the daemon thread didn't finish in `_reload_timeout_s` (hung client)
          - the daemon thread raised (dead daemon, connection refused, etc.)
        we raise a typed `SandboxUnavailableError`. The caller handles it
        uniformly — `_classify_failure` types the box as dead (session
        re-creates), `_resolve_mapping` returns None (no URL). The loop is
        NEVER blocked.

        Cost on a HEALTHY client: one thread spawn + one Event wait per call.
        A healthy reload is sub-ms, the guard's overhead is on the same order
        — measured: 1000 healthy reloads complete in <100ms on any CI runner
        (no measurable added latency).
        """
        target = self._container if obj is None else obj
        done = threading.Event()
        # Result slot: written by the worker thread, read by the main thread
        # AFTER `done.wait()` returns. A small dict keeps the assignment atomic
        # under the GIL (no lock needed for the simple `setattr` we do here).
        result: dict[str, Any] = {"exc": None, "status": "unknown"}

        def _runner() -> None:
            try:
                target.reload()
                result["status"] = getattr(target, "status", "unknown")
            except Exception as exc:  # noqa: BLE001 — surface as typed infra error
                result["exc"] = exc
            finally:
                done.set()

        thread = threading.Thread(target=_runner, daemon=True, name="disco-sbx-reload")
        thread.start()
        if not done.wait(timeout=self._reload_timeout_s):
            # Hung: the daemon thread is still running (we can't safely
            # interrupt a blocking C call in docker-py / podman-py), but
            # daemon=True means it can't block process exit, and we return
            # control to the event loop with a typed error. The caller
            # treats it as "box is dead / client is wedged" — uniformly.
            raise SandboxUnavailableError(
                f"sandbox client hung on reload() (>{self._reload_timeout_s}s); "
                f"the container state is unverifiable"
            )
        if result["exc"] is not None:
            raise SandboxUnavailableError(
                f"sandbox client failed on reload(): {result['exc']}"
            )
        return result["status"] == "running"

    def _classify_failure(self, exc: Exception) -> SandboxError:
        """A container op threw. If the container is no longer running (OOM-killed,
        exited, removed — the VM 202 'OOM kills the WHOLE box' finding), the box is
        gone → SandboxUnavailableError, which the session layer catches to RE-CREATE.
        Otherwise it's a generic per-op SandboxError. Typing death distinctly is what
        lets a backend-agnostic session tell a dead box from a normal op error.

        The `reload()` call goes through `_safe_reload` — a HUNG or RAISING client
        raises a typed `SandboxUnavailableError` within `_reload_timeout_s`. We
        catch it here and type the box as dead: the event loop NEVER blocks on
        the probe. (Dispo #25 wedge-guard.)"""
        alive = False
        try:
            alive = self._safe_reload()
        except SandboxUnavailableError:
            # Hung or raised reload — same typed signal as "the box is gone".
            # The session layer catches SandboxUnavailableError to RE-CREATE.
            alive = False
        if not alive:
            return SandboxUnavailableError(f"sandbox container died mid-session: {exc}")
        return SandboxError(f"sandbox op failed in {self.id}: {exc}")

    async def _guarded(self, fn: Any) -> Any:
        """Run a blocking container op in a thread; let already-typed SandboxErrors
        (file-not-found, path-escape) pass through, but classify a raw backend throw
        (which usually means the box died) as available/unavailable."""
        try:
            return await asyncio.to_thread(fn)
        except SandboxError:
            raise  # explicit, correctly-typed already
        except Exception as exc:  # noqa: BLE001
            raise self._classify_failure(exc) from exc

    @property
    def workspace_path(self) -> str | None:
        """W5 — container workspaces live INSIDE the container, not on the host FS.
        Returns None so C18 file_exists and C1c DoD degrade to no-op gracefully
        (they check `getattr(sbx, 'workspace_path', None)` and skip when None)."""
        return None

    def _container_path(self, path: str) -> str:
        """Resolve `path` to an absolute path INSIDE the workspace, rejecting escapes
        (../, absolute). File ops go through the container, so this is the only jail."""
        path = strip_redundant_workspace_prefix(path)  # ROOT-2: workspace/foo → foo
        target = posixpath.normpath(posixpath.join(self._ws, path))
        if target != self._ws and not target.startswith(self._ws + "/"):
            # W1/codex round-6: a path escaping the jail is an ACCESS denial → typed so the
            # loop's bookkeeping handlers (except PermissionError/OSError) catch it.
            raise SandboxPermissionError(f"path escapes workspace: {path!r}")
        return target

    async def exec_shell(self, cmd: str, *, timeout_s: int) -> ExecResult:
        """Run `cmd` in the live container, capturing stdout/stderr/exit code. The
        timeout is enforced INSIDE the container (the `timeout` coreutil), with an
        outer backstop in case the exec call hangs. A killed command is reported with
        `timed_out=True`, not raised — partial output is preserved."""
        self._alive()
        wrapped = ["timeout", "-k", "5", str(timeout_s), "sh", "-c", cmd]

        def _run() -> Any:
            return self._container.exec_run(wrapped, demux=True, workdir=self._ws)

        try:
            res = await asyncio.wait_for(asyncio.to_thread(_run), timeout=timeout_s + 15)
        except TimeoutError:
            return ExecResult(
                exit_code=124,
                stdout="",
                stderr=f"command exceeded its {timeout_s}s timeout",
                timed_out=True,
            )
        except Exception as exc:  # noqa: BLE001 — classify: dead box vs per-op failure
            raise self._classify_failure(exc) from exc

        exit_code = res[0] if res[0] is not None else -1
        out, err = res[1] if res[1] is not None else (None, None)
        return ExecResult(
            exit_code=exit_code,
            stdout=(out or b"").decode("utf-8", errors="replace"),
            stderr=(err or b"").decode("utf-8", errors="replace"),
            timed_out=exit_code in TIMEOUT_EXIT_CODES,
        )

    async def read_file(self, path: str) -> bytes:
        """Read a workspace file via `exec cat` — binary-safe, transport-agnostic."""
        self._alive()
        target = self._container_path(path)

        def _read() -> bytes:
            res = self._container.exec_run(["cat", "--", target], demux=True)
            if res[0] != 0:
                err = (res[1][1] if res[1] else b"") or b""
                raise_read_error(path, err)  # W1: missing file → typed FileNotFoundError
            return (res[1][0] if res[1] else b"") or b""

        return await self._guarded(_read)

    async def file_exists(self, path: str) -> bool:
        """[B4] Existence check INSIDE the container — `test -f` in the box, never
        a host `Path` check (the host has no view of the container FS; that host
        check is exactly the C18 false-positive this fixes). A missing file is
        False (the non-zero `test` exit, not a raise); a path that escapes the
        workspace jail is False. A genuinely dead box still surfaces through
        `_guarded` as a typed SandboxError, which C18 treats as unverifiable."""
        self._alive()
        try:
            target = self._container_path(path)
        except SandboxError:
            return False

        def _test() -> bool:
            res = self._container.exec_run(["test", "-f", target])
            return res[0] == 0

        return await self._guarded(_test)

    async def write_file(self, path: str, data: bytes) -> None:
        """Write a workspace file via `cp` (put_archive) — binary-safe."""
        self._alive()
        target = self._container_path(path)
        parent = posixpath.dirname(target) or self._ws
        name = posixpath.basename(target)

        def _write() -> None:
            self._container.exec_run(["mkdir", "-p", "--", parent])
            buf = io.BytesIO()
            with tarfile.open(fileobj=buf, mode="w") as tar:
                info = tarfile.TarInfo(name=name)
                info.size = len(data)
                # Own the file as the container's run-user (not root) so it's editable.
                info.uid = info.gid = self._workspace_uid
                # [BP-00 root-cause] TarInfo defaults mtime to 0 (epoch 1970), and
                # put_archive preserves it. Every agent write then lands with an
                # mtime that NEVER changes, so anything mtime-based lies: HTTP
                # If-Modified-Since answers 304 forever (the browser re-renders its
                # first cached copy of an edited file), make/vite see "unchanged".
                info.mtime = int(time.time())
                tar.addfile(info, io.BytesIO(data))
            if not self._container.put_archive(parent, buf.getvalue()):
                raise SandboxError(f"write_file {path!r} failed")

        await self._guarded(_write)

    async def list_dir(self, path: str) -> list[str]:
        """List a workspace dir via `exec ls`."""
        self._alive()
        target = self._container_path(path)

        def _list() -> list[str]:
            res = self._container.exec_run(["ls", "-1A", "--", target], demux=True)
            if res[0] != 0:
                err = (res[1][1] if res[1] else b"") or b""
                raise_read_error(path, err, op="list_dir")  # W1: typed missing-dir error
            out = (res[1][0] if res[1] else b"") or b""
            return sorted(n for n in out.decode("utf-8", "replace").splitlines() if n)

        return await self._guarded(_list)

    def display_url(self) -> str | None:
        return None  # no noVNC display on these backends (yet)

    def expose_port(self, port: int) -> str | None:
        """Return a browser-reachable URL for a published USER port (containment:
        only the curated USER_PORTS set is ever exposed; INTERNAL_PORTS are the
        agent-server's own plumbing and never become user URLs)."""
        if port not in USER_PORTS:
            return None
        mapping = self._resolve_mapping(port)
        if not mapping:
            return None
        return f"http://{mapping[0]}:{mapping[1]}"

    def internal_port_mapping(self, port: int) -> tuple[str, int] | None:
        """Resolve the published host mapping for an INTERNAL port only (BP-08).
        SECURITY: it must REFUSE USER_PORTS (the inverse gate) so it can never
        become a backdoor preview path."""
        if port not in INTERNAL_PORTS:
            return None
        return self._resolve_mapping(port)

    def _resolve_mapping(self, port: int) -> tuple[str, int] | None:
        """Internal helper to read the Docker/Podman port binding. Goes through
        `_safe_reload` so a hung/raising client returns None (no URL) instead of
        wedging the loop. (Dispo #25 wedge-guard.)

        [FIX6] For a FILTERED box (an egress sidecar is attached) the published
        mapping lives on the SIDECAR, not the sandbox: the sandbox is on an
        internal no-NAT net and publishes nothing, while the dual-homed sidecar
        publishes the preview ports and forwards them inward. Sealed / open boxes
        (no sidecar) keep the original sandbox-binding path."""
        if port not in PUBLISHED_PORTS:
            return None
        source = self._container if self._egress_sidecar is None else self._egress_sidecar
        try:
            self._safe_reload(source)  # bounded; raises SandboxUnavailableError on hang/raise
            ports = source.attrs.get("NetworkSettings", {}).get("Ports") or {}
            binding = ports.get(f"{port}/tcp")
            if not binding:
                return None
            return self._preview_host, int(binding[0]["HostPort"])
        except Exception:  # noqa: BLE001 — no mapping yet / box gone / wedged client
            return None

    def _teardown_egress_aux(self) -> None:
        """Best-effort teardown of the filtered-egress aux (proxy sidecar + internal
        network). Both refs default to None for sealed/open boxes, so this is a
        no-op on those modes — only the filtered path sets them in the service's
        create(). The order MATTERS: the sidecar is on the internal net, so we
        remove the sidecar first, then the net (else the net remove errors out on
        an attached endpoint)."""
        sidecar, network = self._egress_sidecar, self._egress_network
        if sidecar is None and network is None:
            return
        if sidecar is not None:
            try:
                sidecar.stop(timeout=2)
            except Exception:  # noqa: BLE001 — best-effort; force-remove next
                pass
            try:
                sidecar.remove(force=True)
            except Exception:  # noqa: BLE001 — already gone is fine
                pass
        if network is not None:
            try:
                network.remove()
            except Exception:  # noqa: BLE001 — still-attached (we removed the sidecar
                # above, but the client may not see it yet) or already gone
                pass

    async def destroy(self) -> None:
        """Stop + remove the container, then tear down the filtered-egress aux (if
        any). The workspace persists on the daemon host (bind dir for gVisor,
        named volume for Podman / local); only the container + the aux are
        ephemeral. The aux teardown is shared across every backend that supports
        the allowlisting proxy (E8) — see `_teardown_egress_aux`."""
        if self._destroyed:
            return
        self._destroyed = True

        def _teardown() -> None:
            try:
                self._container.stop(timeout=self._stop_timeout_s)
            except Exception:  # noqa: BLE001 — best-effort stop; force-remove next
                pass
            try:
                self._container.remove(force=True)
            except Exception:  # noqa: BLE001 — already gone is fine
                pass

        await asyncio.to_thread(_teardown)
        # Aux AFTER the sandbox: the sandbox is on the internal net; removing the
        # net while the sandbox still references it would error out.
        await asyncio.to_thread(self._teardown_egress_aux)
