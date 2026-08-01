"""Fix 2 (B-E) — a LIVENESS proxy to a server bound INSIDE the sandbox.

`SandboxSession.fetch_inside` delegates here. On sealed/filtered network modes
the backend publishes NO host port, so `expose_port` resolves to None even
while a dev server is up. The only host-reachable channel is then the run-end
snapshot — stale/empty mid-run. This bridges that gap: it `curl`s
`http://127.0.0.1:{port}/{path}` from INSIDE the box (the one place the
server IS reachable) over the existing exec path, base64-framing the body so
binary assets (PNG/wasm/…) survive the text-only exec channel byte-identical.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ..session import SandboxSession

# Cap on the body we'll pull back through the exec/base64 channel (Fix 2 B-E).
# Mirrors the 25 MB per-file upload cap; base64 inflates ~33% over the wire but
# we truncate the SOURCE at this many bytes so a runaway response can't blow up
# the exec stdout buffer.
FETCH_INSIDE_CAP_BYTES = 25 * 1024 * 1024


async def fetch_inside(
    session: SandboxSession, port: int, path: str, *, timeout_s: int
) -> tuple[int, bytes, str] | None:
    """Fix 2 (B-E) — a LIVENESS proxy to a server bound INSIDE the sandbox.

    On sealed/filtered network modes the backend publishes NO host port, so
    `expose_port` resolves to None even while a dev server is up. The only
    host-reachable channel is then the run-end snapshot — stale/empty mid-run.
    This bridges that gap: it `curl`s `http://127.0.0.1:{port}/{path}` from
    INSIDE the box (the one place the server IS reachable) over the existing
    exec path, base64-framing the body so binary assets (PNG/wasm/…) survive
    the text-only exec channel byte-identical.

    Returns `(status, body, content_type)` when the in-sandbox server answers,
    or `None` when it isn't up (connection refused → curl http_code 000),
    `curl` is missing, the port is not a curated USER_PORT, or the box died.
    GET only; body truncated at `FETCH_INSIDE_CAP_BYTES`. A liveness probe
    must never raise into the preview route — every failure path yields None.
    """
    import base64
    import urllib.parse

    # Container/generic service containment stays on USER_PORTS. A process
    # sandbox may additionally fetch one of the platform's managed host
    # runtime ports; signed Preview authority, not this liveness primitive,
    # decides whether any such response is user-visible.
    from disco.core.loop.preview_target import is_managed_host_preview_port

    from .._container import USER_PORTS

    managed_host_port = session.shares_host_network and is_managed_host_preview_port(port)
    if port not in USER_PORTS and not managed_host_port:
        return None

    # URL-encode the path so it can't break out of the single-quoted shell arg
    # (a `'` becomes %27); preserve the URL-structural characters.
    safe_path = urllib.parse.quote(path or "", safe="/?=&%#-._~+,:@!$()*;")
    url = f"http://127.0.0.1:{int(port)}/{safe_path}"
    # curl writes the body to a temp file and prints `status\tcontent_type`
    # (no trailing newline) to stdout; we then add a newline and stream the
    # (truncated) body back base64-encoded. `;` not `&&` so we always reach the
    # base64 step — a curl failure leaves an empty/`000` header we map to None.
    cmd = (
        f"curl -s -o /tmp/.pv -w '%{{http_code}}\\t%{{content_type}}' "
        f"--max-time {int(timeout_s)} '{url}' 2>/dev/null; "
        f"printf '\\n'; "
        f"head -c {FETCH_INSIDE_CAP_BYTES} /tmp/.pv 2>/dev/null | base64 -w0 2>/dev/null"
    )
    try:
        res = await session.exec_shell(cmd, timeout_s=int(timeout_s) + 2)
    except Exception:  # noqa: BLE001 — a liveness probe must never raise into the route
        return None
    out = res.stdout
    nl = out.find("\n")
    if nl < 0:
        return None  # curl missing / no header line → not reachable
    header = out[:nl]
    b64 = out[nl + 1 :].strip()
    parts = header.split("\t")
    status_str = parts[0].strip()
    ctype = parts[1].strip() if len(parts) > 1 else ""
    if not status_str.isdigit():
        return None
    status = int(status_str)
    if status == 0:
        return None  # curl http_code 000 → connection refused / server not up
    try:
        body = base64.b64decode(b64) if b64 else b""
    except Exception:  # noqa: BLE001 — corrupt frame → treat as not reachable
        return None
    return (status, body, ctype or "application/octet-stream")
