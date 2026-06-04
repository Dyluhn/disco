# perpleximanus — project status

_A snapshot of what's built, what's wired live, and what's still ahead._

## TL;DR

The platform is **architected end-to-end and exhaustively tested headlessly**, with
a **real, navigable web UI now wired to a live config/library backend**. What's
*not* connected yet is the **intelligence path**: real model calls, the agent loop
running inside a server, and real (non-fixture) research answers. Those are the
documented "build/capability" remainder — live model adapters, the agent surface,
and research streaming — not new architecture.

Think of it as: **the whole skeleton + nervous system + the settings/library
organs are live; the brain (a real model producing answers) isn't plugged in.**

---

## The shape

A `uv` Python monorepo (`packages/*`) + a Vite/React/TS `frontend/`.

```
packages/
  core/          the foundation — events, state, SQLite event store, view/condenser,
                 wire frames, llm/ (router), loop/ (agent loop), security/ (risk gate)
  tools/         tool system + sandbox boundary (builtin tools, executor, process sandbox)
  retrieval/     retrieval engine + citation/grounding (NLI verify), eval hooks
  agent-server/  per-conversation runtime: REST + WebSocket over the event log (Phase 0)
  app-server/    NEW — settings + library gateway the frontend calls (config + conversations)
frontend/        the full web UI (design system, shell, research surface, settings, history)
```

Dependency direction: `core → tools → retrieval → agent-server → app-server`; the
frontend talks to the servers over HTTP/WS.

---

## What actually runs today (the live path)

Two FastAPI servers, both thin adapters over the **same** SQLite event store:

| Server | Run | Serves |
|--------|-----|--------|
| **app-server** | `PMX_PORT=8800 python -m perpleximanus.app_server` | `/api/models`, `/api/models/assignments`, `/api/skills`, `/api/mcp`, `/api/conversations` (+ delete), `/api/health` — with CORS |
| **agent-server** | (TestClient today; needs a uvicorn wrapper) | `/conversations`, `/conversations/{id}/{messages,events,state}`, `WS /ws/conversations/{id}` |

The frontend (`npm run dev`) points at `VITE_API_BASE`; with it set, the config +
library surfaces fetch **live** from the app-server. (Verified: Settings renders the
backend's model catalogue; History renders owner-scoped conversations from the DB.)

Full endpoint reference: **`api-endpoints.md`**.

---

## Frontend ↔ backend wiring

Every server call lives in `frontend/src/api/*` behind hooks (a component never
calls `fetch`). Each function calls the live endpoint when `VITE_API_BASE` is set,
else replays an in-repo fixture (the "fixtures-first, then wire live" discipline).

| Surface | `src/api` | Endpoint | Status |
|---------|-----------|----------|--------|
| Settings — model matrix | `models.listModels` / `getAssignments` / `updateAssignments` | `GET/PUT /api/models[/assignments]` | ✅ **live** |
| Settings — skills | `config.listSkills` / `setSkillEnabled` | `GET/PUT /api/skills` | ✅ live (scaffold data) |
| Settings — MCP | `config.listMcpConnections` | `GET /api/mcp` | ✅ live (scaffold data) |
| Main-screen model pill | `models.listModels` | `GET /api/models` | ✅ **live** |
| History — list + delete | `conversations.listConversations` / `deleteConversation` | `GET/DELETE /api/conversations` | ✅ **live**, owner-scoped |
| **Research answer (search)** | `research.subscribeResearch` | _(planned WS)_ | ⛔ **fixture only** |

So: **Settings, History, and the model pill are genuinely talking to the backend.**
A **search renders an answer, but it's the canned fixture** — the answer surface
streams/cites/structures correctly, it just isn't a model's output.

---

## Subsystem status — built/tested vs live capability

The distinction that matters: most subsystem **logic** is built and tested headless
(with fakes); the **live I/O** (real models, real isolation, real answers) is the gap.

| Subsystem | Logic built + tested | Live capability |
|-----------|----------------------|-----------------|
| Event & state spine (store, log, reconstruct, wire) | ✅ | ✅ (SQLite, real) |
| LLM router (deterministic config selection, reactive errors, prompt injection, cost tracking) | ✅ | ⛔ **no real provider adapters** — `ModelProvider` is a protocol + fakes; no Ollama/OpenRouter HTTP client |
| Agent loop (step machine, condensation, stuck detection, confirmation gate) | ✅ | ⛔ **not running in a server** — exists in `core`, not wired into the agent-server request path |
| Security analyzer + confirmation (rule/ensemble, policies, audit) | ✅ | ✅ (pure functions; gate works wherever the loop runs) |
| Retrieval + grounding (search/extract tools, rerank, NLI verify, drop-unsupported) | ✅ | ⛔ **not live-wired** — needs real search/extract providers + a model |
| Tools + sandbox (registry, executor, capability broker) | ✅ | 🟡 **process sandbox only** (local subprocess, weak isolation); the hard microVM isolation is not built |
| Config + library API (app-server) | ✅ | ✅ **live** |
| Web UI (design system, shell, research surface, settings, history) | ✅ (30 tests) | ✅ runs; config/library live, research fixture |

---

## The critical path to a working product

To go from "fixture answers" to "a real question gets a real, grounded answer," in
rough order:

1. **A live `ModelProvider` adapter** (Ollama and/or OpenRouter HTTP) — the single
   biggest unlock; everything model-driven is blocked on this.
2. **Run the agent loop inside the agent-server** — wire `AgentLoop` into the
   conversation runtime so messages drive real steps (today the server only appends
   events; no loop, no model).
3. **Live retrieval providers** (SearXNG/Firecrawl/etc. + a reranker/embedder) so
   grounding has real passages.
4. **Research-answer streaming** — compose loop + retrieval + grounding to emit the
   `token → block → final` grounded-answer frames the frontend already consumes, and
   point `research.subscribeResearch` at that WS.
5. **Production sandbox** (microVM) to replace the weak process sandbox before the
   Agent (Build) surface runs untrusted tool calls.

Deferred-by-design (present-but-dormant in the UI): the **Build/agent surface**,
**Deep Research**, and the live wiring of **skills/MCP**.

No auth yet — every conversation is owner `"local"`.

---

## Tests & verification

- **Backend:** 269 passed / 1 skipped (`uv run pytest`); `ruff` + `ty` clean. Load-
  bearing guarantees mutation-checked (router determinism, security no-lowering /
  UNKNOWN handling, loop confirmation gate).
- **Frontend:** 30 vitest tests (streaming/empty/loading/error/failure paths);
  `npm run build` + `npm run lint` clean.
- **Live wire:** smoke-tested — app-server endpoints serve, CORS passes, the frontend
  renders live Settings + History with zero console errors.

(Screenshots are captured via **Firefox**; the sandbox's headless **chromium can't
rasterize text**, so chromium-based shots come out blank — not an app bug.)

---

## Git state

Committed on `master` (trunk-based): Phase 0 spine → agent-server wire → Phase 1
router + loop → Phase 2 retrieval → Phase 3 tools/sandbox → router lobotomy +
reactive errors (`6c968fc`) → security (`0d788ea`) → the web UI (`1c47f91`) →
retrieval doc (`fc8b1a8`).

**Uncommitted** (this session's endpoint work): the **app-server** (config/library
API + tests), the **core store** additions (`ConversationSummary`,
`list_conversation_summaries`, `delete_conversation`), the **frontend api wiring**
(`client.ts` + live branches + `.env.example`), and the docs **`api-endpoints.md`**
+ this file.

---

## How to run it (dev)

```bash
# 1. backend config/library API
cd "perpleximanus build"
PMX_PORT=8800 PMX_DB=./perpleximanus.db uv run python -m perpleximanus.app_server

# 2. frontend, pointed at it (copy frontend/.env.example -> frontend/.env.local first)
cd frontend && VITE_API_BASE=http://localhost:8800 npm run dev
```

Settings + History + the model pill are live; asking a question renders the fixture
answer until the model/loop path (above) is wired.

---

## Contracts (the authoritative design docs in this directory)

`basis-of-design.md` · `event-state-contract.md` · `llm-router-contract.md` (v1.3) ·
`agent-loop-contract.md` (v1.2) · `tool-sandbox-contract.md` (v1.1) ·
`retrieval-grounding-contract.md` (v1.1) · `security-analyzer-contract.md` (v1.0) ·
`ui-implementation.md` · `api-endpoints.md` · this `project-status.md`.
