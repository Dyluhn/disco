# API Endpoints — perpleximanus

The HTTP + WebSocket surface, and the frontend data-layer wiring against it. This
is the integration contract between `frontend/src/api/*` and the backend.

There are **two servers**, split by responsibility (BoD §5):

| Server | Owns | Default | Run |
|--------|------|---------|-----|
| **app-server** | settings (model-assignment matrix, skills, MCP) + library (conversation list/delete) | `:8800` | `python -m perpleximanus.app_server` |
| **agent-server** | per-conversation runtime: create/message + the live WebSocket event stream | `:8000` | (TestClient / uvicorn wrapper) |

Both are thin adapters over the **same** core `EventStore` (a SQLite file, shared
via `PMX_DB`). The frontend points at one base URL (`VITE_API_BASE`); for a single
origin in dev, run the two behind one reverse proxy, or set the app-server as the
primary and the agent-server WS URL via `VITE_WS_BASE`. CORS on the app-server is
open (ownership is an explicit query param, never a cookie).

> **Status legend:** ✅ live · 🟡 scaffold (wiring-pending, fixture-backed data) ·
> ⛔ planned (Phase-1, not implemented — the frontend uses a local fixture).

---

## app-server (`/api/*`) — settings + library

Base: `http://<host>:8800`. CORS open. Source: `packages/app-server/src/perpleximanus/app_server/app.py`.

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

- ✅ **GET `/api/conversations?owner_id=&cursor=&limit=`** → `ConversationSummaryDTO[]`
  (newest first; **owner-scoped** — never returns another owner's conversations)
  ```jsonc
  { "id": "conv_…", "owner_id": "local", "title": "…", "created_at": "2026-06-03T…" }
  ```
- ✅ **DELETE `/api/conversations/{conversation_id}?owner_id=`** →
  `{ "id": "conv_…", "deleted": boolean }`. **Owner-scoped**: a caller can only
  delete its own; deleting another owner's is a no-op (`deleted: false`).

---

## agent-server — per-conversation runtime

Base: `http://<host>:8000`. Source: `packages/agent-server/src/perpleximanus/agent_server/app.py`.
Phase 0: a thin wire/REST adapter over the event log — **no agent loop or model
yet**, so control frames and token streaming are accepted but produce nothing.

### REST

- ✅ **POST `/conversations`** — body `{ owner_id?, space_id?, title? }` →
  `{ "conversation_id": "conv_…", "conversation_url": "/ws/conversations/conv_…" }`
- ✅ **POST `/conversations/{cid}/messages`** — body `{ "content": string }` →
  `{ "event_id": string, "seq": int|null }` (appends a USER `MessageEvent`)
- ✅ **GET `/conversations/{cid}/events?after_seq=&limit=`** → `Page`
  `{ "events": Event[], "next_cursor": int|null }`
- ✅ **GET `/conversations/{cid}/state`** → `ConversationState`
  `{ conversation_id, execution_status, iteration, max_iterations, last_seq, pending_action_id, extras }`
- ✅ **GET `/conversations?owner_id=&cursor=&limit=`** → `{ "conversation_ids": string[] }`
  (the agent-server's id-only list; the **library** list with titles is the
  app-server's `/api/conversations`)

### WebSocket

- ✅ **WS `/ws/conversations/{cid}?last_seq=`** — on connect: one `state` frame, then
  replay of every event after `last_seq`, then live events. (Phase 0 streams the
  event log; it does not yet stream model tokens.)

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
| `conversations.listConversations` | GET `/api/conversations?owner_id=` | ✅ |
| `conversations.deleteConversation` | DELETE `/api/conversations/{id}?owner_id=` | ✅ |
| `research.subscribeResearch` | (planned WS) | ⛔ fixture only |
| `research.requestResearch` | POST `/conversations` (planned) | ⛔ fixture only |

**Config:** `VITE_API_BASE` (e.g. `http://localhost:8800`) turns on live mode;
unset → fixtures. `VITE_OWNER_ID` (default `local`) scopes the owner. See
`frontend/.env.example`.
