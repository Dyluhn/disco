"""Preview-target detection, validation, and HTTP probing for ``verify_web_app``.

Finds the live preview with the SAME backend-aware resolver the finish gate uses,
validates an agent-supplied explicit URL against the backend-aware rule, and
probes HTTP reachability. This is EVIDENCE collection, not a verdict: it never
manufactures or upgrades a typed ``HostVerificationResult``.
"""

from __future__ import annotations

import shlex

from ...anatomy import ToolContext, ToolOutcome
from ._render import _render


async def _explicit_url_allowed(url: str, ctx: ToolContext) -> bool:
    """Validate an agent-supplied explicit verify url against the backend-aware
    rule. ISOLATED backend → honored as-is (today's behavior). SHARED host →
    honored ONLY if the url's port is conversation-owned + non-reserved (probed
    live, including the explicit port even if outside the canonical preview set)."""
    assert ctx.sandbox is not None
    from disco.core.loop.finish import _PREVIEW_PORTS
    from disco.core.loop.preview_target import backend_shares_host_network

    host_shared = backend_shares_host_network(ctx.sandbox)
    if not host_shared:
        return True
    from disco.core.loop.preview_target import (
        PortOwnership,
        explicit_target_allowed,
        target_url_port,
    )

    from ...sandbox.port_owner import port_owners

    port = target_url_port(url)
    if port is None:
        return False  # no explicit port → cannot confirm it is a served preview
    ports = sorted(set(_PREVIEW_PORTS) | {port})
    try:
        owners = await port_owners(ctx.sandbox, ports)
    except Exception:  # noqa: BLE001 — probe failure → cannot confirm ownership → reject
        owners = {}
    owned = {
        p: PortOwnership(pid=o.pid, session=o.session)
        for p, o in owners.items()
        if o is not None
    }
    return explicit_target_allowed(
        port=port,
        host_shared=True,
        owned=owned,
        conversation_id=str(getattr(ctx.sandbox, "conversation_id", "") or ""),
    )


def _rejected_explicit_outcome(url: str) -> ToolOutcome:
    """A not-serving verdict for an explicit url that is NOT this build's preview
    on the shared host (a reserved control/UI port, or a foreign/unowned port).
    Verdict-shaped exactly like a real not-serving result so the finish gate routes
    it to the honest-unverifiable path — never a false PASS — with a CLEAR reason."""
    from disco.core.loop.preview_target import reserved_control_ports, target_url_port
    from disco.tools.verify.web_app_probe import compute_verdict

    port = target_url_port(url)
    if port is not None and port in reserved_control_ports():
        reason = (
            f"{url} is a RESERVED control/UI port ({port}: the agent-server, "
            "app-server, or frontend) — not this build's preview, so it was NOT "
            "verified."
        )
    else:
        reason = (
            f"{url} is not a port this build serves on (not owned by this "
            "conversation) — it was NOT verified as your app."
        )
    verdict = compute_verdict(
        url=url, reachable=False, http_status=0, structured=None, meaningful=False
    )
    verdict["summary"] = reason
    verdict["next_action"] = (
        "Serve your build on its own port and verify that, or leave url empty to "
        "auto-detect your served preview."
    )
    return ToolOutcome(success=True, content=_render(verdict), structured=verdict)


async def _detect_preview_url(ctx: ToolContext) -> str:
    """Find the live preview with the SAME backend-aware resolver the finish gate
    uses (`preview_target.resolve_preview_port`).

    On a SHARED-host backend (process/local — `sandbox.workspace_path` is set)
    the agent-server's `:8000`, the app-server's `:8800`, and the UI's Vite
    `:5173` are NOT the build's app (Bug 7): the resolver returns ONLY a
    CONVERSATION-OWNED non-reserved port, else None. Here None ⇒ UNDETECTABLE — we
    return "" and do NOT blind-guess a port: probing an arbitrary port (5173 = the
    Vite UI, or a sibling conversation's server) would be a FALSE PASS against the
    wrong app. An empty url makes `_probe_http` report "not serving", which routes
    the gate to the honest-unverifiable path. On an ISOLATED backend (gVisor/
    Podman) `:8000` IS the app, so the legacy "first owned, else 8000" holds."""
    assert ctx.sandbox is not None
    from disco.core.loop.finish import _PREVIEW_PORTS
    from disco.core.loop.preview_target import (
        PortOwnership,
        active_managed_preview_ports,
        resolve_preview_port,
    )

    from ...sandbox.port_owner import port_owners

    preview_ports = tuple(
        dict.fromkeys((*_PREVIEW_PORTS, *active_managed_preview_ports(ctx.sandbox)))
    )
    try:
        owners = await port_owners(ctx.sandbox, list(preview_ports))
    except Exception:  # noqa: BLE001 — detection failure → resolver default
        owners = {}

    from disco.core.loop.preview_target import backend_shares_host_network

    host_shared = backend_shares_host_network(ctx.sandbox)
    owned = {
        p: PortOwnership(pid=o.pid, session=o.session)
        for p, o in owners.items()
        if o is not None
    }
    chosen = resolve_preview_port(
        host_shared=host_shared,
        owned=owned,
        conversation_id=str(getattr(ctx.sandbox, "conversation_id", "") or ""),
        preview_ports=preview_ports,
    )
    if chosen is None:
        # Shared-host backend with no conversation-owned preview → UNDETECTABLE.
        # Return "" rather than guess a port: a guess could verify the UI / a
        # sibling app (false pass). "" → probe reports not-serving → honest path.
        return ""
    return f"http://127.0.0.1:{chosen}/"


async def _probe_http(ctx: ToolContext, url: str) -> tuple[bool, int]:
    """GET the URL inside the sandbox; return (reachable, http_status). A
    connection failure → (False, 0). Server-free + portable (urllib)."""
    assert ctx.sandbox is not None
    script = (
        "import urllib.request as U\n"
        "try:\n"
        f"    r=U.urlopen({url!r},timeout=10)\n"
        "    print(getattr(r,'status',None) or r.getcode())\n"
        "except Exception:\n"
        "    print(0)\n"
    )
    try:
        res = await ctx.sandbox.exec_shell(f"python3 -c {shlex.quote(script)}", timeout_s=15)
    except Exception:  # noqa: BLE001 — sandbox/transport error → unreachable
        return False, 0
    out = (res.stdout or "").strip().splitlines()
    code = 0
    for line in reversed(out):
        try:
            code = int(line.strip())
            break
        except ValueError:
            continue
    return (code > 0, code)