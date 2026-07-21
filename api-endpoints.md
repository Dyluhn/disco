# API Endpoints — disco

The HTTP + WebSocket surface, and the frontend data-layer wiring against it. This
is the integration contract between `frontend/src/api/*` and the backend.

There are **two servers**, split by responsibility (BoD §5):

| Server | Owns | Default | Run |
|--------|------|---------|-----|
| **app-server** | settings (model-assignment matrix, skills, MCP) + library (conversation list/delete) | `:8800` | `python -m disco.app_server` |
| **agent-server** | per-conversation runtime: create/message + the live WebSocket event stream | `:8000` | (TestClient / uvicorn wrapper) |

Both are thin adapters over the **same** core `EventStore` (a SQLite file, shared
via `PMX_DB`). The frontend points at one base URL (`VITE_API_BASE`); for a single
origin in dev, run the two behind one reverse proxy, or set the app-server as the
primary and the agent-server WS URL via `VITE_WS_BASE`. Since S-W1, CORS on **both**
servers is pinned to an explicit frontend-origin allowlist (`DISCO_FRONTEND_ORIGINS`;
localhost dev origins by default), and **ownership comes from the HttpOnly session
cookie only** — client-supplied `owner_id` body/query params are ignored (see
"Auth (S-W1)" below).

> **Status legend:** ✅ live · 🟡 scaffold (wiring-pending, fixture-backed data) ·
> ⛔ planned (Phase-1, not implemented — the frontend uses a local fixture).

---

## Auth (S-W1) — sessions, CSRF, admin routes

Both servers run the same auth middleware (`app_server/auth.py`, `agent_server/auth.py`;
shared primitives in `disco.core.auth`). Non-public routes require an HttpOnly
`SameSite=Strict` session cookie (`disco_session`); state-changing methods
(POST/PUT/PATCH/DELETE) additionally require the CSRF header **`X-Disco-CSRF`** (the
token is returned by the mint/session endpoints). WebSocket handshakes are
Origin-checked against the frontend allowlist and authenticated by the same cookie.
Ownership is derived from the session; any client `owner_id` param is ignored.

Auth endpoints (present on **both** :8800 and :8000):

- ✅ **POST `/api/auth/mint`** — loopback-only. Body `{ pairing_token? }`: exchanges the
  **one-time pairing token** (logged at server startup; dev auto-pair via
  `DISCO_AUTH_DEV_AUTO_PAIR=1`) for a `Set-Cookie` session →
  `{ ok, owner_id, csrf_token, admin }`.
- ✅ **GET `/api/auth/pairing-token`** — loopback-only bootstrap channel; `401` once the
  token is consumed.
- ✅ **GET `/api/auth/session`** → `{ authenticated, owner_id?, csrf_token?, admin? }`.
- ✅ **GET `/api/auth/origins`** → `{ origins: string[] }` (the pinned frontend-origin
  allowlist).

**Admin route class** — install-wide state is admin-only (`403` for non-admin sessions).
App-server prefixes: `/api/models`, `/api/openrouter`, `/api/secrets`, `/api/security`,
`/api/skills`, `/api/mcp`, `/api/sandbox`, `/api/encoders`, `/api/tts`, `/api/image-gen`,
`/api/data-sources`, `/api/role-fallback`, `/api/projects/storage`, `/api/live-browser`,
`/api/build-kernel`. Agent-server admin prefixes: `/api/mcp`, `/api/tts`,
`/api/image-gen`, `/api/sandbox`, `/api/encoders`, `/api/data-sources`,
`/api/role-fallback`, `/api/projects/storage`, `/api/live-browser`, `/api/build-kernel`.

- ✅ **POST `/api/security/approve-origin`** (app-server, admin-only) — body
  `{ origin, purpose, secret_ref }` → approves an outbound origin in the S-W2 egress
  origin-approval ledger (`disco.core.origin_approvals`; consumed by the
  `disco.core.host_egress` chokepoint).

---

## app-server (`/api/*`) — settings + library

Base: `http://<host>:8800`. CORS pinned to the frontend-origin allowlist; session cookie
required on non-public routes (settings/secrets/MCP/skills are admin-class — see "Auth
(S-W1)" above). Source: `packages/app-server/src/disco/app_server/app.py`.

### Health
- ✅ **GET `/api/health`** → `{ "status": "ok", "service": "app-server" }`

### Models — the absolute, manual model story

- ✅ **GET `/api/models`** → `ModelDTO[]` (the assignable catalogue, derived from
  the core `RouterConfig`, llm-router §7)
  ```jsonc
  // ModelDTO
  { "id": "driver-local", "label": "Driver Local",
    "provider": "local" | "openrouter",      // local = free, openrouter = paid overflow
    "price_in_per_m": 0, "price_out_per_m": 0,
    "capabilities": ["tool_calling","json_mode","long_context"],  // advisory metadata
    "note": "Q4_K_M" }
  ```
- ✅ **GET `/api/models/assignments`** → `AssignmentsDTO`
  ```jsonc
  { "default_model": "driver-local",          // AGENT_DRIVER + fallback
    "roles": { "rag_answerer": "rag-local", "query_rewriter": "rewriter-local",
               "summarizer": "summarizer-local", "nli_verifier": "nli-local" } }
  ```
- ✅ **PUT `/api/models/assignments`** — body `AssignmentsPatch`
  `{ "default_model"?: string, "roles"?: { <role>: <model_id> } }` → `AssignmentsDTO`.
  **Absolute**: the system uses exactly what is set; no validation/prediction here
  (capabilities are advisory + fail-loud at runtime, not blocked in the UI).

### Skills 🟡 (scaffold — the skills subsystem lands later)

- 🟡 **GET `/api/skills`** → `SkillDTO[]` — `{ id, name, description, enabled }`
- 🟡 **PUT `/api/skills/{skill_id}`** — body `{ "enabled": boolean }` → `SkillDTO[]`

### MCP connections 🟡 (scaffold — the MCP client lands later)

- 🟡 **GET `/api/mcp`** → `McpConnectionDTO[]` —
  `{ id, name, url, status: "connected"|"disconnected"|"error" }`

### Library — owner-scoped conversations (§6.1)

- ✅ **GET `/api/conversations?cursor=&limit=`** → `ConversationSummaryDTO[]`
  (newest first; **owner-scoped to the session owner** — a client `owner_id` param is
  ignored; never returns another owner's conversations)
  ```jsonc
  { "id": "conv_…", "owner_id": "local", "title": "…", "created_at": "2026-06-03T…" }
  ```
- ✅ **DELETE `/api/conversations/{conversation_id}`** →
  `{ "id": "conv_…", "deleted": boolean }`. **Owner-scoped from the session**: a caller
  can only delete its own; deleting another owner's is a no-op (`deleted: false`).

---

## agent-server — per-conversation runtime

Base: `http://<host>:8000`. Source: `packages/agent-server/src/disco/agent_server/app.py`.
Phase 0: a thin wire/REST adapter over the event log — **no agent loop or model
yet**, so control frames and token streaming are accepted but produce nothing.

### REST

- ✅ **POST `/conversations`** — body `{ space_id?, title? }` (a client `owner_id` is
  ignored — the owner comes from the authenticated session) →
  `{ "conversation_id": "conv_…", "conversation_url": "/ws/conversations/conv_…" }`
- ✅ **POST `/conversations/{cid}/messages`** — body `{ "content": string }` →
  `{ "event_id": string, "seq": int|null }` (appends a USER `MessageEvent`)
- ✅ **GET `/conversations/{cid}/events?after_seq=&limit=`** → `Page`
  `{ "events": Event[], "next_cursor": int|null }`
- ✅ **GET `/conversations/{cid}/state`** → `ConversationState`
  `{ conversation_id, execution_status, iteration, max_iterations, last_seq, pending_action_id, extras }`
- ✅ **GET `/conversations?cursor=&limit=`** → `{ "conversation_ids": string[] }`
  (scoped to the session owner; the agent-server's id-only list — the **library** list
  with titles is the app-server's `/api/conversations`)

### Projects (Build persistence) — owner-scoped

Source: `packages/agent-server/src/disco/agent_server/routes/{projects,release}.py`. All
are owner-scoped from the session; `403 project_forbidden` (another owner), `404
project_not_found` (no manifest), `404 storage_unavailable` (storage unconfigured/invalid),
`404 files_missing` (workspace tree gone).

- ✅ **GET `/api/projects`** → `{ projects: […], status, root }` — Build projects joined
  with conversation metadata (title/surface/created_at).
- ✅ **POST `/api/projects/import`** — seed a new Build project from a zip upload, local
  directory (admin), or shallow git clone. Runtime-secret paths (`.env`, `.dev.vars`) are
  rejected; `.env.example`/`.dev.vars.example` templates are kept.
- ✅ **GET `/api/projects/{id}/download`** — stream a zip of the workspace (runtime-secret
  files always excluded). **WO-7:** when `/api/projects/{id}/release` assesses `candidate`
  and validation passes, the zip ADDITIONALLY carries the generated self-host overlay
  (`compose.yaml`, `Dockerfile`(s), `.dockerignore`, `.env.example`, `SELFHOST.md`,
  `release.json`); a workspace file wins any path collision. Every other project's zip is
  byte-for-byte the plain filtered workspace zip (no overlay).
- ✅ **GET `/api/projects/{id}/release`** — assess whether the committed workspace can be
  self-hosted. Reads the workspace + the host-owned release-intent sidecar, runs the pure
  detector + validator (NO subprocess, nothing runs at finish time), and returns a stable
  shape (identical key set for every assessment):
  ```jsonc
  { "assessment": "candidate" | "needs_review" | "not_web",
    "reasons": string[],
    "blockers": [ { "code": string, "message": string,
                    "field"?: string|null, "path"?: string|null } ],
    "required_env": [ { "name": string, "scope": "runtime"|"build",
                        "required": boolean, "secret": boolean } ],   // NAMES only, never values
    "command": "docker compose up -d --build",
    "ingress": { "service": string, "port": string, "health_path": string|null } | null,
    // ingress.port is the env-var NAME the ingress binds (the `$PORT` contract), never a literal number
    "self_host": boolean,          // true iff candidate AND validation passes
    "spec_digest": string|null,    // stable content id of the release spec (null when no topology)
    "version_seq": number, "tree_digest": string }   // the committed source binding
  ```
- ✅ **GET `/api/projects/{id}/manifest`** → project metadata + file tree + last deliverable.
- ✅ **DELETE `/api/projects/{id}`** → `{ id, deleted }` (removes manifest + workspace).

### WebSocket

- ✅ **WS `/ws/conversations/{cid}?last_seq=`** — the handshake is Origin-checked and
  authenticated by the session cookie (S-W1; unauthenticated connects close `1008`).
  On connect: one `state` frame, then replay of every event after `last_seq`, then live
  events. (Phase 0 streams the event log; it does not yet stream model tokens.)

**Client → server** (`WSClientFrame`, core `wire.py`):
`{ type: "send_message"|"confirm"|"reject"|"steer"|"pause"|"resume"|"cancel"|"ping",
   content?, action_id?, steer_text?, last_seq? }`
(Phase 0: `ping`→`pong`, `send_message`/`steer` append a message; confirm/reject/
pause/resume/cancel are accepted but no-op — no loop to drive yet.)

**Server → client** (`WSServerFrame`):
`{ type: "event"|"token"|"state"|"error"|"pong",
   event?, token?, token_for_event_id?, state?, error? }`

### Event union (what `event` / `Page.events` carry)

`Event` is discriminated by `kind`: `message` | `action` | `observation` |
`agent_error` | `condensation` | `status` | `error`. Common fields: `id, kind,
source, timestamp, schema_version, seq, meta`. Notable payloads: `ActionEvent`
carries `tool_call`, `self_assessed_risk`, and (when scored) `meta.risk_assessment`
(security §7); `StatusEvent.status` drives the lifecycle; `ErrorEvent{code,detail}`
surfaces a model/provider error reactively (`code:"model_error"`).

---

## Research answer streaming ⛔ (Phase-1, not implemented)

The frontend's research surface consumes a **grounded-answer** frame stream
(`token` → `block` → `final` with a `GroundedAnswer`, see `frontend/src/types/grounded.ts`)
— a different, higher-level contract than the agent-server's generic event WS.
Producing it requires the **Phase-1 agent loop + a model + retrieval/grounding**,
which are not wired. Until then `frontend/src/api/research.ts` replays a fixture.
The eventual endpoint will be the agent-server WS, with the loop emitting
research-shaped frames (or a `/research` composition translating events → answer
blocks). Documented here as the planned target; **the frontend stays fixture-backed
for research until it lands.**

---

## Frontend wiring map (`frontend/src/api/*` → endpoint)

When `VITE_API_BASE` is set, the data layer calls the live endpoint; otherwise it
uses the in-repo fixture (the "build against fixtures first, then wire live"
discipline). A component never calls these directly — only hooks do.

| `src/api` function | Method + path | Status |
|--------------------|---------------|--------|
| `models.listModels` | GET `/api/models` | ✅ live / fixture fallback |
| `models.getAssignments` | GET `/api/models/assignments` | ✅ |
| `models.updateAssignments` | PUT `/api/models/assignments` | ✅ |
| `config.listSkills` | GET `/api/skills` | 🟡 |
| `config.setSkillEnabled` | PUT `/api/skills/{id}` | 🟡 |
| `config.listMcpConnections` | GET `/api/mcp` | 🟡 |
| `conversations.listConversations` | GET `/api/conversations` (session-owner-scoped) | ✅ |
| `conversations.deleteConversation` | DELETE `/api/conversations/{id}` (session-owner-scoped) | ✅ |
| `research.subscribeResearch` | (planned WS) | ⛔ fixture only |
| `research.requestResearch` | POST `/conversations` (planned) | ⛔ fixture only |

**Config:** `VITE_API_BASE` (e.g. `http://localhost:8800`) turns on live mode;
unset → fixtures. Ownership is **not** frontend config: in live mode the backend scopes
the owner from the session cookie (S-W1). `VITE_OWNER_ID` is legacy and unused by
`frontend/src` (fixture mode filters by a fixture-internal owner constant); the stale
entry in `frontend/.env.example` has no effect.
