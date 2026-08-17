"""`GatewayKernel._ensure_gateway` — lazily reach or launch the gateway.

Reuses a surviving gateway across suspend/resume (a bare reachability probe),
and otherwise launches it in the reserved `__kernel` tmux session and polls
`/api` until ready or the wall-clock startup budget (inclusive of the tmux
launch wait) is exhausted.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

from .text import _bounded_gateway_diagnostic, _gateway_transport_state

if TYPE_CHECKING:
    from ..kernel import GatewayKernel


def _gateway_start_budget_s() -> int:
    import os

    budget_s = int(
        os.environ.get("DISCO_KERNEL_GATEWAY_START_S")
        or os.environ.get("PMX_KERNEL_GATEWAY_START_S")
        or "120"
    )
    return max(1, budget_s)


def _resolve_gateway_url(gk: GatewayKernel, port: int) -> str:
    from .. import kernel

    # Never probe ambient host localhost as a hidden fallback: another
    # process could answer there, and a sealed container would still have
    # no transport to its own gateway. Container provisioning must provide
    # the explicit loopback mapping or code_exec fails closed.
    mapping = None
    if hasattr(gk._sandbox, "internal_port_mapping"):
        mapping = gk._sandbox.internal_port_mapping(port)

    if not mapping:
        raise kernel.SandboxError(
            "kernel gateway internal transport is unavailable; "
            "code_exec cannot start for this sandbox session"
        )

    # Return the resolved URL as well as storing it. `GatewayKernel._url` is
    # declared `str | None`, and before the extraction this assignment and the
    # `return self._url` below it sat in one method, so the narrowing to `str`
    # carried. Across a function boundary it does not — returning the value
    # keeps the caller's `str` return type honest without a cast or an ignore.
    url = f"http://{mapping[0]}:{mapping[1]}"
    gk._url = url
    return url


async def _probe_gateway_once(gk: GatewayKernel) -> bool:
    import httpx

    try:
        async with httpx.AsyncClient() as client:
            res = await client.get(f"{gk._url}/api", timeout=1.0, headers=gk._auth_headers)
            return res.status_code == 200
    except Exception:
        # Unreachable is not fatal here, just "not yet" — the caller falls
        # through to starting the gateway. Mirrors the parent's pre-extraction
        # `except Exception: pass` at this exact point; `return False` is the
        # same control flow now that the probe is its own predicate.
        return False


async def _start_gateway_process(gk: GatewayKernel, port: int) -> str:
    """Launch the gateway in tmux; return bounded startup diagnostic evidence.

    The gateway requires every caller (REST + WS) to present the token;
    without it the gateway is an unauthenticated RCE endpoint on whatever
    interface the port is published to. W3 C-5: pass the token via the
    KG_AUTH_TOKEN *environment variable* (Kernel Gateway reads it natively)
    as an inline assignment, NOT as `--auth_token=<hex>` in argv — argv is
    world-readable via `ps`/`/proc/<pid>/cmdline` to any process sharing the
    sandbox's PID namespace, so an argv token is a self-exfiltrating secret.
    The token is hex, but quote defensively regardless.
    """
    import shlex

    from .. import kernel

    cmd = (
        f"KG_AUTH_TOKEN={shlex.quote(gk._token)} "
        f"jupyter kernelgateway --KernelGatewayApp.api=kernel_gateway.jupyter_websocket "
        f"--ip 0.0.0.0 --port {port}"
    )
    started = await gk._launch_gateway(cmd)
    started_running = getattr(started, "running", None)
    started_exit = getattr(started, "exit_code", None)
    started_diagnostic = _bounded_gateway_diagnostic(
        getattr(started, "output", ""),
        exit_code=started_exit,
        auth_token=gk._token,
    )
    if started_running is False:
        diagnostic = started_diagnostic or "gateway process exited without startup output"
        await gk._normalize_gateway_session()
        raise kernel.SandboxError(
            "jupyter kernel gateway exited during startup; "
            f"diagnostic={diagnostic}. code_exec is unavailable for this sandbox session; "
            "continuing with shell is a degraded fallback and does not prove kernel state"
        )
    return started_diagnostic


async def _poll_gateway_ready(gk: GatewayKernel, deadline: float) -> tuple[str | None, str]:
    """Poll /api until ready. The gateway start is CPU-bound; on a saturated
    box (local-LLM inference + the build agent competing for cores) it can
    take well over 30s, so an autonomous build would forfeit on a
    slow-but-fine start. The env-tunable budget is a REAL wall-clock bound,
    inclusive of the tmux launch wait: poll count × request timeout × sleep
    must never silently stretch a claimed 120 seconds toward four minutes.
    (DISCO_KERNEL_GATEWAY_START_S, default 120s; PMX_ legacy honored)."""
    import httpx

    last_readiness = "not_reachable"
    loop = asyncio.get_running_loop()
    async with httpx.AsyncClient() as client:
        while (remaining := deadline - loop.time()) > 0:
            try:
                res = await client.get(
                    f"{gk._url}/api",
                    timeout=max(0.05, min(1.0, remaining)),
                    headers=gk._auth_headers,
                )
                if res.status_code == 200:
                    return gk._url, last_readiness
                last_readiness = f"http_status={res.status_code}"
            except Exception as exc:  # noqa: BLE001 — class-only readiness evidence below
                last_readiness = _gateway_transport_state(exc)
            remaining = deadline - loop.time()
            if remaining > 0:
                await asyncio.sleep(min(1.0, remaining))
    return None, last_readiness


async def _build_final_diagnostic(gk: GatewayKernel, started_diagnostic: str) -> str:
    """The process may have exited after ShellSessionManager's initial 15-second
    observation. Capture one final, separately bounded pane view without
    letting diagnostics materially extend the advertised startup budget."""
    from .. import kernel

    final_diagnostic = started_diagnostic
    try:
        view = await asyncio.wait_for(gk._sessions.view(kernel._GATEWAY_SESSION), timeout=1.0)
        # Pre-existing defensive read carried over verbatim from
        # `GatewayKernel._ensure_gateway`; the only edits are joining the parent's
        # two-line call and the receiver (`self._token` → `gk._token`) now that
        # this is a free function. Not a new shim.
        viewed = _bounded_gateway_diagnostic(getattr(view, "output", ""), auth_token=gk._token)
        if viewed:
            final_diagnostic = viewed
    except Exception:  # noqa: BLE001 — keep the launch-time bounded fallback
        pass
    return final_diagnostic


async def ensure_gateway(gk: GatewayKernel) -> str:
    """Start the gateway lazily in tmux and wait for ready."""
    from .. import kernel
    from .._container import INTERNAL_PORTS

    port = next(iter(INTERNAL_PORTS))  # 8899
    gateway_url = _resolve_gateway_url(gk, port)

    if await _probe_gateway_once(gk):
        return gateway_url

    # Not running -> start in tmux session '__kernel'. exec_dir=None means the
    # container tmux default dir (WORKDIR /workspace), so kernels spawned by the
    # gateway inherit the workspace as cwd — user code's relative paths resolve
    # against the same tree the file API serves.
    budget_s = _gateway_start_budget_s()
    loop = asyncio.get_running_loop()
    deadline = loop.time() + budget_s
    started_diagnostic = await _start_gateway_process(gk, port)

    url, last_readiness = await _poll_gateway_ready(gk, deadline)
    if url is not None:
        return url

    final_diagnostic = await _build_final_diagnostic(gk, started_diagnostic)
    diagnostic_suffix = f"; diagnostic={final_diagnostic}" if final_diagnostic else ""

    await gk._normalize_gateway_session()
    raise kernel.SandboxError(
        f"jupyter kernel gateway failed to start within {budget_s}s; "
        f"final_readiness={last_readiness}{diagnostic_suffix}. "
        "code_exec is unavailable for this sandbox session; continuing with shell is a "
        "degraded fallback and does not prove kernel state"
    )
