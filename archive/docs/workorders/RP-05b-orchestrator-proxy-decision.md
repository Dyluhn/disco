# RP-05b orchestrator-side MCP egress proxy — design decision (2026-06-11)

**Status:** RATIFICATION-PENDING (decided under the AFK autonomy grant; flagged
for Dylan). Resolves Fable's rp-05b-py REJECT defect #2 ("egress proxy never
wired — every HTTP MCP request bypasses the sidecar").

## The gap Fable found, and why it was non-trivial
`_connect_http` (`runtime.py`) built `McpHttpClient(...)` and called
`client.connect()` with **no `proxy_env`** — so the agent-server's own outbound
MCP HTTP traffic never routed through any allowlisting proxy. The client already
*accepts* `proxy_env` at both construction and `connect()` (`mcp/http.py`); the
arg was simply dropped.

It was dropped because the design was genuinely ambiguous:
- The workorder says (lines 104–105, 206–211) the **MCP HTTP client's outbound
  httpx** must route through `proxy_env(host, port)`, "so there is no path that
  bypasses [the allowlist]."
- But its anchor for *where* (line 206 → `executor.py:143-150`) is **stale** —
  that range is `_build_context`/the kill switch, not egress routing.
- The only real `proxy_env` caller is `gvisor.py:257`, using the **per-conversation
  sidecar's container IP** (`proxy_ip`). The MCP pool, by contrast, is
  orchestrator-side and long-lived. There is **no orchestrator-reachable proxy
  host** anywhere in the tree.

So "wire the proxy" required *deciding* the proxy source, not just passing an arg.

## Decision
The MCP HTTP client is proxied through a **single runtime accessor
`_mcp_proxy_env()`**, governed by the same `PMX_BUILD_EGRESS` posture that already
gates the sandbox spec:

- **filtered** (`PMX_BUILD_EGRESS=filtered`): return
  `proxy_env(host, EGRESS_PROXY_PORT)` where `host =
  os.environ.get("PMX_MCP_EGRESS_PROXY_HOST", "127.0.0.1")`. The MCP client's
  outbound httpx then routes through the allowlisting egress proxy; a host not in
  the unioned allowlist is denied **403** by the proxy, exactly like sandboxed
  traffic. `_connect_http` passes this into `client.connect(proxy_env=...)`.
- **open** (default): return `None` — direct, matching the open sandbox posture
  (no proxy, no allowlist).

`proxy_env` keeps `NO_PROXY=localhost,127.0.0.1`, so a loopback-bound proxy
doesn't accidentally proxy loopback *destinations*; remote MCP hosts still route
through it.

### Why this shape
- It reuses the existing, tested `proxy_env()` contract — no new proxy protocol.
- The posture decision (filtered vs open) is already the single source of truth
  for egress (`_build_sandbox_spec`); the MCP client now obeys the **same** switch,
  so there is no second, divergent policy.
- `PMX_MCP_EGRESS_PROXY_HOST` lets a deployment point the orchestrator-side client
  at wherever its allowlisting proxy actually lives (loopback in dev; the sidecar's
  published address in the §6 gVisor acceptance), without code change.

### Anti-gaming proof obligation
The unit test stands up a **real** stub allowlisting proxy (parses the absolute-URI
request line, 403s a non-union host, 200s an allowed one), points
`_mcp_proxy_env()` at it, and asserts: (a) allowed host → request *arrives at the
proxy* and is forwarded/200; (b) non-union host → **403 from the proxy**; (c)
`proxy_env=None` → the client does **not** reach the proxy (bypass differs). The
origin is addressed by a **non-loopback hostname** so `NO_PROXY` does not silently
bypass the proxy and void the test. A `len(_mounts) > 0` config-shape assertion is
explicitly NOT acceptable (Fable rejected exactly that).

### Deviation flagged for ratification
- New env vars `PMX_MCP_EGRESS_PROXY_HOST` (the workorder names no such knob; it
  assumed the per-conversation sidecar, which the orchestrator-side pool cannot
  reach). Surfaced here so it is not silent.
- The workorder's `executor.py:143-150` anchor for this wiring is wrong; the real
  site is `_connect_http` → `McpHttpClient.connect(proxy_env=...)`. Recorded so the
  spec can be corrected on ratification.

## §6 acceptance note
Drill 2 (gVisor backend, sidecar denies a non-union host with 403) exercises the
**sandbox-side** egress (path A: `build_egress_union` → `_build_sandbox_spec`,
already wired). This decision adds the **orchestrator-side** path (path B) so the
agent-server's own MCP fetches are equally constrained — closing the "no path
bypasses it" requirement end-to-end.
