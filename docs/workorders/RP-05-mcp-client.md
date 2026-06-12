# RP-05 — MCP client (L — the big lever)

Parent plan: `docs/next-fix-set-plan.md` §2 RP-05 (locked) and §2 RP-00
(egress allowlist proxy as RP-05 rung 0 dependency).

This is an **L-size** order — it is dispatched as **two sequential rounds
(rung A → rung B)**, each with its own manifest subset. Rung A establishes
the client core, stdio transport, registry, and per-server config. Rung B
adds the Streamable-HTTP transport over the egress allowlist proxy, the
retrieval-tier MCP registry, and the UI surface that actually takes the
NotWired banner off `McpSection.tsx`. Rungs are **not** parallel-dispatchable:
B consumes the public API A establishes.

Recon: the tree currently has only a scaffold (`packages/app-server/.../config_state.py`
`_mcp` fixture list, `frontend/src/components/settings/McpSection.tsx` with
`NotWired` + `PendingBadge`, `frontend/src/api/config.ts` `listMcpConnections`
hitting `GET /api/mcp`). There is no `mcp` Python package in `uv.lock` yet
and no MCP code anywhere in `packages/`. The `egress_proxy.py` sidecar
(RP-00 rung 0) **is already in-tree** at
`packages/tools/src/disco/tools/sandbox/egress_proxy.py` — see
"Rung 0" below; no separate design-doc gate is needed.

---

## Verified anchors (line drift noted; use symbols, not line numbers)

- `frontend/src/components/settings/McpSection.tsx:8` — `NotWired` banner
  present; `PendingBadge` present; `Add connection` button is `disabled`.
  Plan says line 8-72; current file is shorter (65 lines) but the
  NotWired + disabled button + fixture rows shape is intact.
- `frontend/src/types/config.ts:30-34` — `McpConnection` shape
  (`id, name, url, status: connected|disconnected|error`).
- `frontend/src/api/config.ts:96-98` — `listMcpConnections()` → `GET /api/mcp`
  live, fixture in offline mode.
- `frontend/src/hooks/useConfig.ts:55-57` — `useMcpConnections()` query
  (`queryKey: ["mcp-connections"]`).
- `packages/app-server/src/disco/app_server/app.py:213-216` —
  `GET /api/mcp` returns `state.mcp_connections()` (scaffold).
- `packages/app-server/src/disco/app_server/config_state.py:411-418`
  — `_mcp` is a hard-coded list of two `McpConnectionDTO` (Filesystem +
  GitHub fixtures) with `status="connected"` / `status="disconnected"`.
  This is the scaffold; rung A replaces the **source of truth** (persisted
  config + live pool) but preserves the DTO shape so the frontend keeps
  rendering.
- `packages/core/src/disco/core/llm/config.py:138` — `RouterConfig`
  is a Pydantic model. RP-05 adds `mcp: McpSettings` to it; the plan's
  line citation (139-160) covers the field block it will live next to.
- `packages/agent-server/src/disco/agent_server/runtime.py:625` —
  `_compose_build_loop` (plan said 517-520; the build-loop **definition**
  is at 625, the call site that consumes the pool is at 548 — the plan's
  reference is the call site). Rung A's pool is built once at agent-server
  start and snapshotted into the per-conversation registry here.
- `packages/tools/src/disco/tools/sandbox/egress_proxy.py:1-50` —
  the stdlib-only allowlisting sidecar. **The design IS in-tree; rung 0
  is not a sequencing gate** for the operator's docs. Rung B routes MCP
  HTTP through this proxy.
- `packages/tools/src/disco/tools/sandbox/_container.py` —
  `EGRESS_PROXY_PORT=8888`, `proxy_env(host, port)`, `proxy_run_argv(allow, port)`
  (the helpers rung B calls to set the env block the MCP HTTP client
  inherits).
- `packages/tools/src/disco/tools/secrets.py:21-36` — `SecretsStore`
  Protocol + `InMemorySecretsStore` (the seam for per-server `env` secret
  references). Plan said line 68-90 — that range is the `CapabilityBroker`
  block. The right anchor for secrets is the `SecretsStore.get` Protocol
  method at line 27; rung A's MCP server config uses this.
- `packages/core/src/disco/core/security/analyzers.py:180` —
  `_score_other` (the keyword-based fallback the plan warns against;
  `analyzers.py:178-208` in the plan covers this entire function). Rung A
  makes the explicit `base_risk` per-tool the **only** path for MCP tools.
- `packages/tools/src/disco/tools/builtin/browser.py:131` —
  `_fence(view)` (plan said 131 was the docstring line; the function
  definition is at 131, body at 132). Rung B extends the same fence idiom
  to MCP tool results.
- `packages/retrieval/src/disco/retrieval/providers.py:21-42` —
  `SearchProvider` + `ExtractionProvider` `@runtime_checkable` Protocols
  (the contract the retrieval-tier MCP registry tier implements).
- `packages/retrieval/src/disco/retrieval/wiring.py:29-52` —
  `retrieval_capability_handlers(search, extraction)` returns
  `{"search": handler, "extract": handler}` (the shape the MCP retrieval
  tier must satisfy to plug into the Build broker at
  `runtime.py:_build_broker`).
- `packages/tools/src/disco/tools/anatomy.py:80-90` — `ToolDef`
  fields (`base_risk`, `runs_in`, `capabilities`, etc.) — the shape rung A
  builds for every registered MCP tool.
- `packages/tools/src/disco/tools/executor.py:60-150` — DefaultToolExecutor
  dispatch on `runs_in` ("sandbox" vs "in_process"). MCP stdio MUST be
  `runs_in="sandbox"` (inside gVisor); MCP HTTP MUST be `runs_in="in_process"`
  with `capabilities=Capability.NETWORK` so the executor routes it through
  the egress proxy env block.

---

## Rung 0 — egress allowlist proxy (already in-tree)

The plan marks the egress allowlist proxy as RP-05's rung 0 dependency. The
sidecar **design exists** in this repo at
`packages/tools/src/disco/tools/sandbox/egress_proxy.py` and has
been validated end-to-end by BP-09 (sandbox-image rebuild + prewarm), with
the `egress_mode`, `proxy_env`, `proxy_run_argv`, and `format_allow`
helpers exposed in `packages/tools/src/disco/tools/sandbox/_container.py`.
The 3 gotchas the plan cites (DNS, sidecar internal-net IP, NO_PROXY
loopback) are already handled.

Rung B's HTTP transport reuses `proxy_env(host, port)` to set `HTTP_PROXY`
/ `https_proxy` on the MCP HTTP client's outbound httpx client and feeds
the per-server host allowlist (`mcpServers.<name>.allowed_hosts` ∪ the
allowed-tool host derived from the tool URL) into `format_allow(...)` and
onward into `proxy_run_argv(allow, port)` so the **container's** egress is
constrained to exactly those hosts. Rung B's per-server allowlist is the
ONLY list the sidecar enforces — there is no path that bypasses it.

**No sequencing gate.** Rung A can start immediately; rung B inherits the
sidecar wiring without further operator action.

---

## Locked design — rung A (client core + stdio + registry/config)

### 1. Pin and install dependencies

Add to `packages/tools/pyproject.toml` (the home of the MCP client — it is
a tool-tier concern that agent-server composes):

- `mcp>=1.27.2,<2` — 1.27.2 = 2026-05-29; v2 is unreleased + breaking; the
  2026-07-28 RC moves to a stateless core. **Do NOT couple to
  session-statefulness.** Use the v1 client APIs; design all internal state
  as per-conversation-pool, not per-connection-session.
- `anyio>=4.4` (already present in `uv.lock` at 4.13.0 — confirm ≥4.4 and
  pin a floor in the `[dependency-groups].dev` block only if it is not
  declared in a runtime `[project.dependencies]`).
- `httpx` / `websockets` / `pydantic` already in tree.

`uv lock && uv sync` once; commit `uv.lock` deltas with the rung A diff.

### 2. Module layout (new files under `packages/tools/src/disco/tools/mcp/`)

- `__init__.py` — public surface: `McpPool`, `McpServerSpec`, `McpServerConfig`,
  `McpToolDescriptor`, `ApprovalRecord`.
- `config.py` — `McpServerConfig` (Pydantic) with fields:
  - `name: str` — must match `^[a-z0-9_]+$`; rejected at parse time with a
    typed error (LibreChat's naming bugs).
  - `transport: Literal["stdio", "streamable_http"]` (the only two values
    accepted; `sse` is rejected at parse with a clear error).
  - For `stdio`: `command: list[str]`, `args: list[str]`, `env: dict[str, SecretRef]`
    (where `SecretRef = str` naming a key in the `SecretsStore` — never a
    raw value).
  - For `streamable_http`: `url: str` (https only; http rejected in
    non-dev profiles), `headers: dict[str, SecretRef]`, `allowed_hosts: list[str]`
    (the allowlist rung B will push to the sidecar).
  - `enabled: bool = True`, `allowed_tools: list[str] | None = None`
    (None = all), `risk_tier: SecurityRisk` (REQUIRED; not inferred),
    `description_hash: str | None = None` (filled by the approval flow).
- `pool.py` — `McpPool` class. Lifecycle:
  - `async def start(self) -> None` — connect + `initialize()` (with
    `asyncio.wait_for(initialize, timeout=...)` — see SDK #1452 hang
    defense) for every enabled server in the config. **One task = one
    client lifecycle.** Use `AsyncExitStack` per connection for cleanup.
  - `async def snapshot(self) -> list[ToolDef]` — returns the **frozen** list
    of `ToolDef` objects for one conversation; `list_changed` notifications
    (if the SDK ever surfaces them) **do not** mutate the live list — they
    enqueue a re-approval request; the pool replaces the snapshot only on
    explicit re-approval. (v1 may not see list_changed at all; the gate is
    there for the moment we do.)
  - `async def aclose(self) -> None` — drain AsyncExitStack; kill stdio
    subprocesses (SIGTERM, then SIGKILL after a 2s grace); close httpx
    transports; capture any pending stderr into the conversation log.
- `stdio.py` — stdio transport. Spawns the server subprocess via
  `asyncio.create_subprocess_exec`. **Tolerates non-JSON stdout lines**
  (the spec says servers MAY emit logging on stdout; we drop them with a
  warning) and **always captures stderr** into a buffer surfaced through
  the per-server status.
- `approval.py` — `ApprovalRecord` (server name, list of tool names,
  `description_hash: str` computed via `hashlib.sha256` over the
  canonicalized tool descriptions, `approved_at: str` ISO-8601,
  `approved_by: str` = operator username). Storage: a new SQLite-backed
  table `mcp_approvals` in the same DB the agent-server already uses
  (mirror the `share_tokens` pattern from RP-06 for the schema location
  — find it before writing the SQL). Verification path:
  `McpPool.start()` reads approval; if `description_hash` mismatches the
  current pool hash, the pool refuses to start the affected server and
  raises `ApprovalRequired` (which the agent-server maps to a typed WS
  event the UI uses to render the re-approval banner).
- `naming.py` — `qualified_name(server, tool) -> "mcp__<server>__<tool>"`
  with the `[a-z0-9_]` regex check; `split_qualified_name(qn)` for the
  reverse mapping. Both reject names that would not round-trip (no double
  underscores inside components, etc.).

### 3. ToolDef construction (per-server, per-tool)

For every `tools/list` response, build a `ToolDef` (matching
`tools/anatomy.py:80-90`):

- `name` = the **qualified** name (`mcp__<server>__<tool>`).
- `description` = the server's tool description, wrapped in the fence
  header so the LLM-side prompt treats it as **untrusted text** (the
  wrapper reads `<untrusted_tool_description name="...">...</untrusted_tool_description>`
  — keeps the LLM from blindly trusting a description that arrived over
  MCP).
- `parameters` = the JSON schema from the tool, passed through unchanged.
- `base_risk` = `risk_tier` from the per-server config (REQUIRED at
  registration time — `RuleBasedAnalyzer` MUST NOT be the source of truth
  for MCP tools per the plan).
- `runs_in` = **`"sandbox"`** for stdio (the stdio subprocess runs inside
  the gVisor sandbox; the MCP client just shuttles JSON over the
  subprocess's stdio) and **`"in_process"`** for streamable_http (the
  HTTP client itself runs orchestrator-side; the egress proxy
  constrains the network).
- `capabilities` = the tool's declared capabilities. For stdio tools
  this is whatever the tool name implies (no extra grants); for HTTP
  tools this includes `Capability.NETWORK` so the executor
  (`executor.py:143-150`) routes the call through the proxy env block.

### 4. McpSettings on RouterConfig

Add `mcp: McpSettings` to `RouterConfig` in
`packages/core/src/disco/core/llm/config.py:138`. Shape:

```python
class McpSettings(BaseModel):
    enabled: bool = False          # off by default — opt-in
    servers: dict[str, McpServerConfig] = Field(default_factory=dict)
    max_active_schemas: int = 20   # cap; beyond this, tool_search is exposed
    # Per-server `description_hash` is stored in the DB, not here.
```

Default `McpSettings(enabled=False)` so a fresh install is unchanged.
The settings surface (app-server) reads this block to render the MCP
config UI in rung B.

### 5. Agent-server wiring (the half that does NOT change surface)

`packages/agent-server/src/disco/agent_server/runtime.py`:

- New attribute: `self._mcp_pool: McpPool | None = None`.
- In `__aenter__` (or wherever the lifespan starts the singleton
  resources), read `RouterConfig.mcp`; if `enabled` and the server list
  is non-empty, call `pool = McpPool(config, secrets=self._secrets)`;
  `await pool.start()`; assign `self._mcp_pool = pool`. On startup
  failure (any server's `initialize()` times out or returns an error),
  log structured, mark the failing server `status="error"` in the live
  status projection, but DO NOT bring the agent-server down — the rest
  of the system runs MCP-less. The `ApprovalRequired` path is the
  exception: it MUST refuse startup if at least one enabled server has
  no fresh approval (otherwise the security guarantee — human
  re-approval on ANY change — is bypassable by restart).
- In `_compose_build_loop` (line 625), after the existing `executor =
  DefaultToolExecutor(build_default_registry(), ...)`, extend the
  registry with `pool.snapshot()` (the frozen list for THIS
  conversation). The per-conversation snapshot is built once at
  conversation start from the live pool — **not** from a re-query.
  In-flight list_changed notifications on the live pool do NOT mutate
  this snapshot.
- New per-conversation helper:
  `def _qualified_tool_call_name(self, name: str) -> tuple[str, str] | None` —
  returns `(server, tool)` if `name` is an MCP-qualified name, else
  `None`. Used by the executor to route the call back to the right
  per-server client.

`packages/agent-server/src/disco/agent_server/app.py`:

- New WebSocket frame (or extend an existing typed event): `mcp_approval_required`
  carrying `{server, tool, description_hash, old_description_hash}`. The
  UI surface (rung B) renders this as a re-approval banner.

### 6. `tool_search` meta-tool (the cap)

If `len(pool.snapshot()) > config.mcp.max_active_schemas` (default 20),
add a single `tool_search` meta-tool to the conversation registry:

- `name="tool_search"`, `description="Return up to N tools whose
  description matches the query. Use this when you need an MCP tool not
  already in your active schema set."`
- `parameters = {"query": str, "limit": int (default 5)}`
- Implementation: a thin orchestrator-side handler that does a cheap
  keyword/embedding match against the *full* tool set, returns the
  qualified names + descriptions of the top N, and does NOT mutate the
  active schema set (the LLM can call them by qualified name in the
  next turn).
- `base_risk = LOW`, `runs_in = "in_process"`, `capabilities = {}`
  (pure orchestrator-side retrieval; no sandbox involvement).

This is the lever that lets a 27B's context stay bounded regardless of
how many MCP servers are configured.

### 7. Stdlib-only stubs for tests

A new `tests/mcp_fakes.py` module under `packages/tools/tests/`:

- `FakeStdioServer`: an `asyncio` server that speaks the MCP JSON-RPC
  protocol over a pair of in-memory streams (a pipe, or two
  `asyncio.StreamReader`/`StreamWriter` pairs). Exposes 2-3 tools
  (`echo`, `add`, `read_file` against a temp dir). Used by rung A's
  unit + integration tests; no external process needed.
- The transport-under-test still spawns a real subprocess for stdio
  (that's the whole point of stdio transport — the subprocess is the
  process boundary), but the subprocess IS this fake server. Use
  `sys.executable -m tests.mcp_fakes` as the command; the fake's `__main__`
  picks the right tool set from argv.

---

## Locked design — rung B (Streamable HTTP + egress proxy + retrieval tier + UI)

### 1. Streamable-HTTP transport

`packages/tools/src/disco/tools/mcp/http.py` — new module:

- Uses the official `mcp` SDK's streamable-HTTP client (`streamablehttp_client`
  or whatever the 1.27.x API exposes — check the pinned version's docs in
  the new dep's site-packages before writing the call; do NOT hardcode an
  import path that may have changed across the 1.27.x range).
- Auth: read `headers: dict[str, SecretRef]` from the `McpServerConfig`;
  resolve each `SecretRef` via `SecretsStore.get(name)` (orchestrator-side
  closure — the secret never reaches the sandbox even though the HTTP
  call is `runs_in="in_process"`, the closure is what enforces it).
- Per-call timeout (10s default, configurable per-server).
- Surface init failure as `status="error"` with the captured error
  message; the agent-server status projection surfaces it to the UI.

### 2. Egress-proxy routing (the only path that egresses)

For each HTTP server in the pool:

- `allowed = format_allow(set(server.allowed_hosts) ∪ {url_host(url)})`
- Build the `HTTP_PROXY` / `https_proxy` / `NO_PROXY` env block via
  `proxy_env(proxy_host, EGRESS_PROXY_PORT)` from
  `packages/tools/src/disco/tools/sandbox/_container.py`.
- Pass both the env block AND the per-call `client=httpx.AsyncClient(
  proxies=..., timeout=...)` to the SDK call. **The httpx client MUST
  honor the env block** — if the SDK wraps the call in a way that
  bypasses it (e.g., a fresh `AsyncClient(proxies=...)` not set), the
  call is rejected and the unit test "bypass attempt" fails.
- The container-side egress is constrained by the same `allow` string
  via the existing `proxy_run_argv(allow, port)` — but rung B does NOT
  spawn the sidecar per-MCP-call. The sidecar runs once for the sandbox
  (BP-09 wiring); the per-server allowlist is folded into the sandbox's
  single egress allowlist at conversation start (union over enabled
  HTTP servers' allowed_hosts). Update the sandbox spec construction
  (find the spec builder in `runtime.py:_build_sandbox_spec` or its
  callers) to **union** the per-conversation MCP allowlist into the
  existing `egress_allow` set, NOT replace it. **Tool calls to a host
  NOT in the union are denied by the sidecar with 403**, exactly like
  pip/curl deny paths — symmetric threat model.

### 3. Retrieval-tier MCP registry

A separate registry tier in
`packages/tools/src/disco/tools/mcp/retrieval_tier.py`:

- Picks a subset of MCP tools whose names match the OpenAI
  `search(query)→{results:[{id,title,url}]}` and
  `fetch(id)→doc` shape.
- Wraps each picked tool as a `SearchProvider` and `ExtractionProvider`
  that satisfy the `@runtime_checkable` Protocols in
  `packages/retrieval/src/disco/retrieval/providers.py:21-42`.
- Registers them with the build broker at `_compose_build_loop` time
  (extend `runtime.py:_build_broker` — currently lines 510-526 — to
  accept the retrieval-tier MCP providers as additional handlers).
- Citations in the Research surface that go through the MCP retrieval
  tier use the existing `GroundingPipeline` exactly like bundled
  providers do. **No new grounding path** — the MCP tier plugs into
  the existing seam.

This is the lever that makes MCP research sources first-class (cited
the same way SearXNG/Firecrawl are).

### 4. Fenced MCP output (extends browser `_fence()`)

`packages/tools/src/disco/tools/mcp/fence.py` — new module:

- `fence_mcp_result(server: str, tool: str, result: Any) -> str` —
  produces a fenced string the LLM sees as untrusted. The wrapper
  reads:

  ```
  <untrusted_mcp_result server="..." tool="...">
  <schema-validated JSON of result>
  </untrusted_mcp_result>
  ```

- Reuses the design discipline of `builtin/browser.py:_fence` (line
  131): structured but untrusted, never parsed back into a tool call
  without an explicit human-confirmation step. **The fenced block is
  appended to the tool result text**; the executor does NOT feed raw
  MCP output back into the tool-call parser.
- This is the CyberArk / MCPTox answer to the "every output channel is
  injectable" rule.

### 5. UI surface — wire `McpSection.tsx` for real

`frontend/src/components/settings/McpSection.tsx`:

- Remove the `<NotWired ...>` block.
- Remove the `<PendingBadge />` next to the heading.
- Replace the disabled `<button>` "Add connection" with a live form
  (or modal — match the existing skills-create UX) that POSTs to
  `POST /api/mcp/servers` with the config payload. The new endpoint
  lives in `packages/app-server/src/disco/app_server/app.py`
  next to the existing `GET /api/mcp` (line 213-216) and persists via
  the new `mcp_approvals` table (config + approval are persisted
  together — re-approval flow mutates the row, never creates a new one).
- Per-row affordances:
  - **Enabled toggle** — PATCH `enabled` field; agent-server pool
    picks it up on the next conversation start (no live reload of an
    in-flight conversation — the plan is explicit: static-per-session
    discovery).
  - **Approve / Re-approve** — shown when the server's
    `description_hash` doesn't match the stored approval. The button
    opens a diff view (old tools list vs new tools list) and requires
    an explicit confirm. Until confirmed, the server's status is
    `error` and any build that tries to use it fails fast with a typed
    error.
  - **Remove** — DELETE the row + the approval row. Pool stops trying
    to connect to that server on the next conversation.
- New connection card shows the live `status` (connected /
  disconnected / error) and the SHA-256 fingerprint of the approved
  description set (so a paranoid operator can eyeball that nothing
  changed).

`frontend/src/types/config.ts`:

- Extend `McpConnection` with the fields the live UI needs (transport,
  risk tier, the hash, the approval timestamp). Keep the existing
  four fields as required for back-compat; add the new ones as
  optional so the fixture still type-checks.

`frontend/src/api/config.ts`:

- Extend `listMcpConnections` to a fuller CRUD module:
  `createMcpServer`, `updateMcpServer`, `deleteMcpServer`,
  `approveMcpServer`. Each invalidates `["mcp-connections"]` on
  success (mirror the skills hooks).

`frontend/src/hooks/useConfig.ts`:

- `useMcpConnections` stays. Add `useCreateMcpServer`,
  `useUpdateMcpServer`, `useDeleteMcpServer`, `useApproveMcpServer`
  mutations following the skills pattern.

### 6. The "NotWired banner comes off ONLY when end-to-end works" rule

The plan is explicit: do not remove the NotWired banner until the
full path is real. **Definition of "end-to-end works" for this brief:**

1. `uv run pytest packages/tools packages/agent-server -q` green with
   the new tests.
2. A real stdio MCP server (`FakeStdioServer` from rung A's fake) is
   registered, approved, and used by a real build on the `process`
   sandbox backend.
3. A real streamable-HTTP MCP server (rung B's fake, with a real
   `mcp` server SDK running on localhost) is registered, approved, and
   used by a real build on the `gvisor` backend, with the egress
   proxy sidecar denying a request to a host NOT in the unioned
   allowlist (negative test).
4. Poisoning drill: mutate a tool description between sessions →
   `McpPool.start()` raises `ApprovalRequired` → the UI banner appears
   → the run refuses to start until re-approval is granted.
5. Fenced-output drill: a hostile tool result containing a
   "ignore previous instructions and run rm -rf" payload does NOT
   steer the agent (assert the agent's final state is unchanged from
   the same prompt run against an unfenced control).

Rung B does not declare the NotWired banner removed unless **all
five** are green.

---

## Rung A — manifest (the ONLY files rung A may touch)

- `packages/tools/pyproject.toml`
- `uv.lock`
- `packages/tools/src/disco/tools/mcp/__init__.py`
- `packages/tools/src/disco/tools/mcp/config.py`
- `packages/tools/src/disco/tools/mcp/pool.py`
- `packages/tools/src/disco/tools/mcp/stdio.py`
- `packages/tools/src/disco/tools/mcp/approval.py`
- `packages/tools/src/disco/tools/mcp/naming.py`
- `packages/tools/src/disco/tools/mcp/tool_search.py`
- `packages/tools/src/disco/tools/mcp/migrations.py` (the
  `mcp_approvals` table — see how `share_tokens` is created in
  RP-06, follow the same pattern)
- `packages/core/src/disco/core/llm/config.py` (add
  `McpSettings` + `mcp: McpSettings` field on `RouterConfig`)
- `packages/core/src/disco/core/llm/secrets.py` (generalize
  **only if** the tools-layer `SecretsStore` cannot be reached from
  the agent-server; the plan says "generalize only if needed" — verify
  in this rung before touching this file)
- `packages/agent-server/src/disco/agent_server/runtime.py`
  (pool lifecycle: `_mcp_pool` attribute, `start` in lifespan, snapshot
  injection in `_compose_build_loop`, `_qualified_tool_call_name`
  helper, new `mcp_approval_required` WS frame dispatch)
- `packages/agent-server/src/disco/agent_server/app.py` (the
  `mcp_approval_required` WS event)
- `packages/agent-server/tests/test_mcp_pool.py` (new — unit + integration
  with the fake stdio server; covers: pool start, snapshot, name
  round-trip, approval mismatch, init timeout, stderr capture, non-JSON
  stdout tolerance, lifecycle teardown)
- `packages/tools/tests/mcp_fakes.py` (new — `FakeStdioServer`,
  `FakeMcpServerMain`)
- `packages/tools/tests/test_mcp_transport.py` (new — stdio transport
  tests; subprocess spawn, JSON framing, error surfacing)
- `packages/tools/tests/test_mcp_naming.py` (new — name round-trip,
  regex enforcement, ambiguous name rejection)
- `packages/tools/tests/test_mcp_approval.py` (new — hash mismatch,
  hash pin, re-approval required path)
- `packages/tools/tests/test_mcp_tool_search.py` (new — cap behavior,
  meta-tool invocation, no-schema-mutation guarantee)
- `test-record/rp-05a/units-tools.log`
- `test-record/rp-05a/units-server.log`
- `test-record/rp-05a/units-core.log`
- `agent-projects/gemini/rp-05a-report.md`

Rung A may NOT touch any frontend file. Rung A may NOT touch the
app-server (no UI changes yet; the existing `GET /api/mcp` returns
the scaffolded fixtures, which is fine — the live pool is
agent-server-side).

---

## Rung B — manifest (the ONLY files rung B may touch)

- `packages/tools/src/disco/tools/mcp/__init__.py` (re-export
  the new modules)
- `packages/tools/src/disco/tools/mcp/http.py` (new)
- `packages/tools/src/disco/tools/mcp/retrieval_tier.py` (new)
- `packages/tools/src/disco/tools/mcp/fence.py` (new)
- `packages/tools/src/disco/tools/mcp/http_egress.py` (new —
  the per-server allowlist union + `proxy_env`/`format_allow` glue)
- `packages/agent-server/src/disco/agent_server/runtime.py`
  (extend `_build_broker` with the retrieval-tier MCP providers;
  extend `_build_sandbox_spec` to union the per-conversation MCP
  allowlist into the sandbox `egress_allow` set; wire HTTP pool
  lifecycle)
- `packages/agent-server/src/disco/agent_server/app.py` (the
  HTTP-side `mcp_approval_required` path; per-server health-check
  route for the UI status projection)
- `packages/agent-server/tests/test_mcp_http.py` (new — proxy env
  honored, allowed-host enforcement, denied-host 403, init failure
  status surfacing, secret closure)
- `packages/agent-server/tests/test_mcp_egress.py` (new — unioned
  allowlist in the sandbox spec; the sandbox `egress_allow` is
  SUPERSET, not REPLACEMENT, of the existing registry egress)
- `packages/agent-server/tests/test_mcp_retrieval_tier.py` (new —
  `SearchProvider`/`ExtractionProvider` Protocol conformance;
  citations flow through `GroundingPipeline` like bundled sources)
- `packages/agent-server/tests/test_mcp_fence.py` (new — hostile
  payload in MCP result does not steer the agent; structural
  property of the wrapper is asserted)
- `packages/agent-server/tests/test_mcp_poisoning.py` (new — mutate
  description between sessions → `ApprovalRequired` raised → agent
  refuses)
- `packages/app-server/src/disco/app_server/app.py` (new
  `POST /api/mcp/servers`, `PATCH /api/mcp/servers/{name}`,
  `DELETE /api/mcp/servers/{name}`, `POST /api/mcp/servers/{name}/approve`;
  the `GET /api/mcp` route is REPLACED, not duplicated, to return the
  live pool status projection)
- `packages/app-server/src/disco/app_server/config_state.py`
  (drop the `_mcp` fixture list; replace with the live pool
  projection; the `mcp_connections()` accessor stays for back-compat
  with the rung A surface; `McpConnectionDTO` gains the new optional
  fields rung A's TypeScript type gained)
- `packages/app-server/tests/test_mcp_endpoints.py` (new — full CRUD
  + approval + hash diff endpoint tests)
- `frontend/src/types/config.ts` (extend `McpConnection` per rung B
  design)
- `frontend/src/api/config.ts` (full CRUD + approve module)
- `frontend/src/hooks/useConfig.ts` (mutation hooks for the above)
- `frontend/src/components/settings/McpSection.tsx` (full rewiring:
  drop NotWired, drop PendingBadge, live form, per-row controls,
  approval diff view, status colors stay — keep the existing
  `STATUS_META` map; show the description hash for paranoid mode)
- `frontend/src/components/settings/McpSection.live.test.tsx` (new —
  RQ + MSW: form posts, list invalidates, approval diff renders)
- `frontend/src/components/settings/McpSection.approval.test.tsx` (new —
  hash-mismatch server renders the re-approval banner with the diff)
- `test-record/rp-05b/units-tools.log`
- `test-record/rp-05b/units-server.log`
- `test-record/rp-05b/units-app-server.log`
- `test-record/rp-05b/units-frontend.log`
- `agent-projects/gemini/rp-05b-report.md`

Rung B may NOT touch: `engine.py` (RP-04 owns it), the `_tools_for_step`
/ valve code (DC-05 ratified), the `runtime.py` tracking dicts at
`runtime.py:210-259` (those are runtime-state plumbing; rung A/B do
not add new dicts), the sandbox `egress_proxy.py` itself (BP-09 owns
it — rung B calls its helpers, does not edit it).

---

## Standing rules (Wave-2 + Wave-3 — apply to BOTH rungs verbatim)

- Never modify an existing test to make new code pass; if a test
  contradicts your change, STOP and flag.
- DC-05 meta-tool withholding and valve semantics are ratified
  design — changes to `_tools_for_step` / valve behavior are out of
  scope for both rungs.
- Run the FULL suite for evidence; a green run of only your own test
  files hid 17 broken tests in wave 1.
- Flag every manifest deviation at the top of your report with
  justification.
- Do NOT commit, do NOT git add, do NOT start dev servers on
  :5173/:5174.
- Anchors are symbols, not line numbers. The verified-anchors list
  above uses the line numbers from the 2026-06-09 recon, but every
  reference resolves to a named function / class / field. If a
  quoted anchor does not exist, STOP and report.
- No workarounds, no hardcoded state, no synthetic green. The
  `McpSection.tsx` NotWired banner is a hard gate: rung B does NOT
  remove it until all five end-to-end conditions (above) are green.
- The `[BASE_RISK]` requirement for MCP tools is non-negotiable:
  every `McpServerConfig` MUST declare `risk_tier`; the loader
  rejects an MCP server without one. `RuleBasedAnalyzer` is NOT a
  source of truth for MCP tools — it is a fallback for unknown
  built-in tools, and the plan is explicit that MCP must not rely
  on it.

---

## Anti-scope (BOTH rungs)

- **No SSE transport.** `streamable_http` is the only HTTP option;
  the loader rejects `transport="sse"` with a clear error. v1
  has no path to SSE.
- **No `runs_in="in_process"` for stdio MCP tools.** Stdio is
  always `"sandbox"` (the subprocess is inside gVisor).
- **No v2 MCP API.** Pin is `<2`. The 2026-07-28 RC is out of
  scope; design stateless (no per-connection session state lives
  past the conversation).
- **No live `list_changed` mutations.** Static-per-session
  discovery; re-approval gates any change.
- **No removing the NotWired banner early.** The plan forbids
  false affordances — the banner comes off only when the path
  works end-to-end (see rung B design §6).
- **No changes to `engine.py`** (RP-04 owns it), **no changes to
  the `_tools_for_step` / valve code** (DC-05 ratified), **no
  changes to `runtime.py:210-259`** runtime tracking dicts
  (RP-01's anti-scope carries over).
- **No new module outside the `mcp/` subpackage** under
  `packages/tools/src/disco/tools/`. The MCP code is a
  single tree; do not scatter it.
- **No `mcp` client call that bypasses the proxy env block for
  HTTP servers.** Symmetric threat model: an HTTP server call
  without `proxies=` is rejected by the test suite.
- **No inference of `risk_tier` from tool name keywords.** The
  whole point of the lock-down is that MCP tools are explicit.
- **No silent in-process tool results** — every MCP tool result
  is fenced (`fence.py`) and the fence structure is asserted in
  the test suite.
- **No paid OpenRouter models** for any test scaffolding; tests
  run against the bundled llama.cpp on 192.168.1.231:18080 (or
  the in-process fakes; never both at once in a single test).

---

## Evidence (BOTH rungs)

Rung A:

- `timeout 600 uv run pytest packages/tools -v` >
  `test-record/rp-05a/units-tools.log`
- `timeout 600 uv run pytest packages/agent-server --ignore=packages/agent-server/tests/test_build_surface.py -v` >
  `test-record/rp-05a/units-server.log`
- `timeout 600 uv run pytest packages/core -v` >
  `test-record/rp-05a/units-core.log`
- `test_mcp_pool.py` covers: pool start, snapshot, name
  round-trip, approval mismatch, init timeout, stderr
  capture, non-JSON stdout tolerance, lifecycle teardown.
- Behavioral run with the real `FakeStdioServer` registered
  + approved + used by a build on the `process` backend is
  the REVIEWER's rung (do NOT start servers; a harness may
  be live on :8000).
- Report: `agent-projects/gemini/rp-05a-report.md`

Rung B:

- `timeout 600 uv run pytest packages/tools -v` >
  `test-record/rp-05b/units-tools.log`
- `timeout 600 uv run pytest packages/agent-server --ignore=packages/agent-server/tests/test_build_surface.py -v` >
  `test-record/rp-05b/units-server.log`
- `timeout 600 uv run pytest packages/app-server -v` >
  `test-record/rp-05b/units-app-server.log`
- `cd frontend && timeout 300 npx vitest run` >
  `test-record/rp-05b/units-frontend.log`
- `test_mcp_egress.py` covers: unioned allowlist in
  `SandboxSpec.egress_allow`; denied host returns 403 from
  the sidecar; the existing registry egress allowlist is
  preserved (NOT replaced) by the MCP union.
- `test_mcp_fence.py` covers: hostile payload in MCP
  result does not steer the agent; fence structural property
  asserted directly.
- `test_mcp_poisoning.py` covers: mutate a tool description
  between sessions → `ApprovalRequired` raised → agent
  refuses → re-approval restores the path.
- `test_mcp_retrieval_tier.py` covers: `SearchProvider` /
  `ExtractionProvider` Protocol conformance; citations
  flow through `GroundingPipeline` like bundled sources
  (no new grounding path).
- Playwright Firefox spec on the live backend, screenshots
  in `test-record/screenshots/rp-05/`, SendUserFile the
  screenshots. The spec walks: open Settings → MCP →
  add a stdio server (approving the hash) → add an HTTP
  server (approving the hash) → start a build that calls
  one tool from each → mutate the stdio server's tool
  description externally → the build's NEXT attempt
  surfaces the re-approval banner → re-approve → build
  proceeds. This is the REVIEWER's rung.
- Report: `agent-projects/gemini/rp-05b-report.md`

---

## Sequencing gates (per the plan §3 + this brief)

- Rung A has no upstream dependency. It can dispatch immediately
  on the wave-3 lane.
- Rung B depends on rung A's `McpPool` + `McpServerConfig` +
  `RouterConfig.mcp` shape being stable. Rung B's `http.py`,
  `retrieval_tier.py`, `fence.py`, and the UI rewiring land
  on the same wave-3 lane but in a second round.
- Rung 0 (egress allowlist proxy) is **not** a sequencing gate
  for either rung: the sidecar design is in-tree
  (`packages/tools/src/disco/tools/sandbox/egress_proxy.py`)
  and BP-09 has already validated it end-to-end on VM-201. Rung
  B's HTTP transport reuses it.
- The wave-3 lane is shared with RP-07 (sandbox image rebuild
  for pandoc + WeasyPrint) and RP-08 (scheduler). MCP itself
  does not require a sandbox image change — the `mcp` Python
  package lives in the agent-server's workspace, not in the
  sandbox image. Rung A's stdio MCP servers run inside gVisor
  the same way shell commands do; the sandbox image is
  unchanged.

---

## Self-check (the brief is implementable without re-reading the plan)

- Every "Locked:" decision in plan §2 RP-05 is transcribed:
  pin `mcp>=1.27.2,<2` + `anyio>=4.4`; stdio + Streamable HTTP
  only (SSE rejected at parse); no coupling to
  session-statefulness; static-per-session discovery;
  `mcp__<server>__<tool>` naming with `[a-z0-9_]` server name
  enforcement; 15-20 active schema cap with `tool_search`
  meta-tool; `McpSettings` on `RouterConfig` with
  `enabled / allowed_tools / risk_tier / description_hash`;
  secrets via `SecretsStore.get(name)`; explicit `base_risk`
  per tool; stdio runs in gVisor sandbox; HTTP routes through
  egress allowlist proxy; hash-pin + re-verify + re-approve
  on change; result fencing extending browser `_fence()`; one
  task = one client lifecycle with `AsyncExitStack`; timeout
  on `initialize()`; tolerate non-JSON stdout; capture
  stderr; retrieval-tier MCP with OpenAI `search/fetch`
  contract via `@runtime_checkable` Protocols; UI rewires
  `McpSection.tsx`; NotWired banner gated on end-to-end.
- RP-00 rung 0 is acknowledged and resolved: the sidecar
  design is in-tree, no operator design-doc sequencing gate
  is needed.
- Standing rules + anti-scope + Evidence all use the same
  shape as RP-04 / DC-07 (and the wave-2 global rules in
  `docs/workorders/README.md`).
- Reports go to `agent-projects/gemini/rp-05a-report.md` and
  `agent-projects/gemini/rp-05b-report.md`; evidence is teed
  to `test-record/rp-05a/units-*.log` and
  `test-record/rp-05b/units-*.log` with `timeout` guards.
- Rung A and rung B manifests are **disjoint**:
  rung A does not touch any frontend or app-server file;
  rung B does not touch any of the rung A's new files except
  `__init__.py` (re-export). The only shared file is
  `runtime.py`, and the edits are sequential (rung A adds
  pool lifecycle; rung B adds retrieval-tier provider
  registration + spec allowlist union + HTTP pool
  lifecycle).
- Anchors are symbol-based, not line-based, in the body
  text. The verified-anchors list at the top is the only
  place line numbers appear, and they are flagged with
  drift notes where the file has changed since the plan.
