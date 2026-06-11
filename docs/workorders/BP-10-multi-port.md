# BP-10 — Multi-port exposure (backend + frontend builds)

**Read `README.md` first. Requires BP-02 (server_status exists). BP-08 depends on this.**

## Why

`expose_port()` (`sandbox/_container.py`) rejects everything except `PREVIEW_PORT` 8000,
and containers publish only that port at create. A backend+frontend build (API on :3000,
Vite on :8000) cannot expose both. Manus's `deploy_expose_port(port)` takes any port.

**Hard constraint that shapes the design:** Docker cannot add port mappings to a running
container (long-standing, documented Docker limitation). So the publishable set is
declared at create. The production-honest fix on Docker is a curated port set covering
framework defaults — not a proxy-rewrite hack (URL rewriting breaks absolute-path apps).

## The decided design

```python
# sandbox/_container.py
PREVIEW_PORT = 8000                                   # stays the user-facing primary
USER_PORTS: frozenset[int] = frozenset({8000, 3000, 5173, 8080, 5000, 4321})
INTERNAL_PORTS: frozenset[int] = frozenset({8899})    # kernel gateway (BP-08) — never user-exposed
PUBLISHED_PORTS = USER_PORTS | INTERNAL_PORTS
```

- Container create (`gvisor.py` `_start_container` and the podman/local equivalents):
  publish every port in `PUBLISHED_PORTS` with host-assigned mappings
  (`ports={f"{p}/tcp": None for p in PUBLISHED_PORTS}` in the docker-py call).
- `expose_port(port)`: allow any `port in USER_PORTS` (same reload/attrs lookup as
  today, generalized); return `None` for unpublished AND for `INTERNAL_PORTS` (internal
  plumbing must never be handed out as a user URL).
- Process backend `expose_port(port)`: return `f"http://127.0.0.1:{port}"` when
  `port in USER_PORTS` and the port is bound (reuse BP-02's `port_owner`); else None.
  (Dev-mode usability; there is no isolation boundary to defend on this backend.)
- `server_status` (BP-02) probes all of `USER_PORTS`, listing owner per bound port.

### Agent-server proxy per port (`agent_server/app.py`)

Alongside the existing `/conversations/{cid}/preview-app/{path:path}` (which stays =
port 8000), add:

```
GET /conversations/{cid}/port/{port}/{path:path}
```
Validate `port in USER_PORTS` (404 otherwise — never proxy arbitrary ints); resolve
upstream exactly like `preview_upstream` but per port (`runtime.port_upstream(cid,
port)`); stream the response. The agent-server stays loopback-only; nothing new is
exposed beyond the existing origin.

### Prompt bullet (prompts.py, BP-03 section — append)

```
"  • Port 8000 is what the user SEES. Extra services (APIs, websockets) may use "
"ports 3000, 5173, 8080, 5000, or 4321 — these are reachable for your own testing "
"via the browser tool and curl, and proxied for the user on request. Anything else "
"is unreachable from outside the sandbox.\n"
```

### Frontend

`PreviewPane` (`frontend/src/components/build/ExecutionCanvas.tsx`): when the preview
availability response (extend the `GET …/preview` payload with
`ports: [{port, owner}]` from `server_status`'s probe) lists >1 bound USER port, render
a small port-selector pill row above the iframe; selecting a port points the iframe at
`/conversations/{cid}/port/{port}/`. Default stays 8000. No bound extra ports → no pills
(zero UI change for the common case).

## Acceptance

1. **Unit**: expose_port allows 3000/5173, refuses 8899 and 9999; process-backend URL
   logic; proxy route 404s on non-USER ports.
2. **Integration (gvisor, VM-201)**: one sandbox; sessions serve
   `python3 -m http.server` on 8000 AND 3000; `expose_port(3000)` returns a reachable
   URL (curl from the test host); `expose_port(8899)` → None; agent-server
   `/port/3000/` proxies content; `server_status` lists both owners.
3. **Behavioral (live driver)**: prompt: "API on port 3000 returning JSON, static
   frontend on 8000 that fetches it." Event log shows both sessions + a browser/curl
   check of :3000. Save → `test-record/bp-10/`.
4. **UI surface (live, Firefox)** — `bp-10-ports.spec.ts`: the step-3 build; assert the
   port pills appear (8000 + 3000), clicking 3000 swaps the iframe to the JSON.
   Screenshots: `ports-pills.png`, `port-3000.png` →
   `test-record/screenshots/bp-10/`, sent to user.

## Prohibitions

- No path-rewriting reverse proxy inside the sandbox; no socat relays; no dynamic
  container re-creation to add ports.
- 8899 (and any future INTERNAL port) must never appear in expose_port results,
  server_status output shown to the model (mark it `internal` or omit), or the UI.
- Do not widen USER_PORTS beyond the six listed.
