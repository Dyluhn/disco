# Basis of Design — Self-Hosted Research & Agent Platform

**Codename:** (TBD — referred to here as "the Platform")
**Document status:** v1.0 — Basis of Design (authoritative)
**Author:** Synthesized from the project research corpus, the v3 distilled findings, and two source-grounded OpenHands teardowns (app/server layer + SDK loop internals).
**Purpose of this document:** This is the cornerstone reference the entire build proceeds from. It captures *what* we are building, *why* each decision was made, and *how* the subsystems fit together — at a level of detail sufficient to begin implementation without re-litigating settled questions. It is deliberately opinionated. Where something is genuinely undecided it is marked **[OPEN]**; where a decision is locked it is marked **[LOCKED]**; where a claim should be re-verified at build time (because it moves fast) it is marked **[VERIFY]**.

---

## How to read this document

The document moves from intent → constraints → architecture → subsystems → cross-cutting concerns → execution plan. The early sections are stable ground; the middle sections are the engineering substance; the final sections turn it into work.

- **Part I — Foundations** (§1–§4): vision, scope, principles, locked decisions.
- **Part II — System Architecture** (§5–§7): the layered topology, the data and event model, the deployment shape.
- **Part III — Subsystem Designs** (§8–§16): one section per subsystem, each self-contained.
- **Part IV — Cross-Cutting** (§17–§21): security, observability, configuration, testing, performance.
- **Part V — Execution** (§22–§24): phased build plan, risk register, open decisions.

A note on provenance: three subsystems (the agent loop, memory/condensation, and security gating) are specified from *direct reading of the OpenHands SDK source* (v1.23.1-era), not from secondary description. Those carry the most implementation detail because we have the most ground truth. Where the design adopts an OpenHands pattern, it is cited inline as `[OH: …]`.

---

# PART I — FOUNDATIONS

## 1. Vision & Product Definition

The Platform is a **single self-hosted application with two capability families that share one infrastructure spine**:

1. **The Research Engine** — a Perplexity-class answer system. A user submits a question (vague or precise); the system plans, retrieves across the live web and local corpora, grounds every claim in sources, and returns an authoritative, cited, document-grade answer. A heavier **Deep Research** mode runs a longer multi-step investigation and produces a structured report.

2. **The Agent** — a Manus-class autonomous worker. A user states a goal; the system produces a visible plan, executes it as a tool-using loop inside a secure sandbox (browsing, running code, writing files, building and previewing deliverables), shows the work live, and yields a finished artifact the user owns.

These are not two apps. They are **two scoped surfaces over one agent core** `[OH: Perplexity's "Computer" engine and OpenHands both converge on one core, many surfaces]`. The Research Engine is the agent core with a read-only/retrieval tool scope and a synthesis-oriented prompt; the Agent is the same core with the full sandbox tool scope and a long-horizon prompt. This unification is the central architectural bet of the project: it means one loop, one event model, one memory system, one routing layer, and one UI language serve both.

### 1.1 What "good" means here

The project exists because the existing self-hosted alternatives are functional but feel amateurish — what the owner calls "vibe-coded." The quality bar is therefore explicit and non-negotiable:

- **Premium UI is a first-class requirement, not a finish.** The interface must feel like Linear/Perplexity/Raycast, not a hobby dashboard. (See §13.)
- **Retrieval quality must approach Perplexity's**, because retrieval — not model choice — is the dominant determinant of answer quality. (See §9.)
- **The system must be legible and controllable** when the agent runs autonomously. Trust through legibility is the core UX principle. (See §13.)
- **It must be deployable and operable** without the brittle, multi-service sprawl that makes the OSS clones painful to run.

The working thesis, carried from the research: **~80% of the capability is reachable** through mature open-source building blocks, careful orchestration, and good UX. The last ~20% is model capability and reliability, which is where local models degrade and where OpenRouter overflow is justified.

### 1.2 Explicit non-goals (v1)

- **Not multi-tenant SaaS.** Single primary user, with the architecture left open to add multi-user later (§4.1). No billing, no organizations, no public sign-up.
- **Not a mobile-first product.** Responsive web is in scope; native mobile is not.
- **Not a model-training project.** We consume models (local and via OpenRouter); we do not fine-tune in v1.
- **Not Windows-guest execution.** The sandbox runs Linux workloads (§11.4). Deliverables are consumed on any OS; the sandbox itself is Linux-only by design.
- **Not a general plugin marketplace** in v1, though the tool architecture (§10) is built to make one possible later.

---

## 2. Stakeholders & Operating Context

- **Primary user / owner / operator:** a single technically sophisticated individual with strong UI taste, security awareness, and homelab infrastructure. This person is also the builder. This matters for the design: there is no team to absorb operational complexity, so **operational simplicity and a clean single-builder codebase are themselves requirements**.
- **Hosting environment:**
  - **Production target:** a **Proxmox** homelab node (always-on; ideal for 24/7 agent tasks; KVM/Firecracker available). `[LOCKED]`
  - **Development environment:** a **Bazzite** (Fedora Atomic) workstation; immutable, container-friendly (Podman/Docker). `[LOCKED]`
- **Compute model:** local open-weight models via **Ollama / llama.cpp** as primary; **OpenRouter** as overflow for hard tasks. (See §12.)
- **Existing owned infrastructure to reuse:** a self-hosted **Firecrawl** instance already wired to a self-hosted **SearXNG** instance — i.e., a fully self-hosted retrieval pipeline already exists and is a building block, not a thing to build. `[LOCKED]`

---

## 3. Design Principles

These principles are the tie-breakers. When a detailed-design decision is ambiguous, resolve it toward these.

1. **One core, many scoped surfaces.** Never build a second engine where a differently-scoped instance of the first will do. Research and Agent are scopes, not codebases.

2. **Everything swappable is behind an interface.** Storage, sandbox, model providers, and search providers each sit behind an abstract base with dependency-injected implementations `[OH: the pervasive `*_service_base.py` + injector pattern]`. This is the structural form of "build to scope but never wall yourself off." Start with the simplest implementation (filesystem, SQLite, local process); leave the door open (S3, Postgres, Firecracker, remote) without rework.

3. **The event log is the source of truth, and it is append-only.** All conversation and agent state is reconstructed from an append-only event stream. We never mutate history. This gives us replay, resume-after-disconnect, debuggability, and audit for free. Forgetting (for context-window management) is done with tombstones and read-time views, never deletion `[OH: the View + Condensation-tombstone pattern]`.

4. **The sandbox interior is hostile.** No secret, credential, or piece of internal configuration ever lives anywhere the agent can read it. The orchestrator holds secrets and injects only narrow, short-lived, scoped capabilities. Egress is deny-by-default. This is the load-bearing lesson from the Manus leaks and is confirmed by OpenHands keeping secrets in a separate store outside the sandbox.

5. **Treat the model as untrusted, and separate reading from acting.** Content fetched from the web is hostile until proven otherwise. Reading (low privilege) is separated from acting (high privilege). State-changing actions are risk-scored and gated. (See §17.)

6. **One action per iteration, observed before the next.** The agent loop takes exactly one tool action per step and observes its result before choosing the next. This is the single most important reliability decision; it is what separates a controllable agent from a runaway one.

7. **Legibility over spectacle.** Show the plan, hide the loop. Surface what the agent is doing and why, with intervention always one click away; keep raw machinery behind progressive disclosure. The UI absorbs complexity internally to project authority externally.

8. **Capability-based routing, not model-name routing.** Route work to models by *task type and difficulty* through an abstraction, so swapping or upgrading a model is a config change, not a code change. Keep routine work local; overflow only the genuinely hard steps.

9. **Operational simplicity is a feature.** Prefer a small number of well-understood services. Every additional always-on dependency must earn its place. A clean `docker compose up` (or equivalent) is the target operator experience.

10. **The prompt is part of the model abstraction.** System prompts are versioned per model family *and* per operating mode (interactive / planning / long-horizon), swapped alongside routing `[OH: per-model and per-mode prompt variants]`.

---

## 4. Locked Decisions (Decision Record)

This section is the authoritative record of decisions already made in the design process. Each has a rationale and the consequences that flow from it. Treat these as settled.

### 4.1 Tenancy: single-user now, multi-user-ready `[LOCKED]`
**Decision:** Build single-tenant. Do **not** build auth/sessions/permissions now. **Do** carry an `owner_id` on every persistent record from day one, keep per-task sandbox isolation (which is per-user isolation with a different key), and keep secrets scoped in the orchestrator.
**Rationale:** The expensive part of multi-user (real auth, session isolation, a permission layer) is exactly the part most likely to change shape depending on who the eventual users are — and it's deferrable cheaply *if* the data and isolation layers are clean from the start. The cheap 80% (ownership hygiene, per-task isolation, scoped secrets) is gotten almost for free by designing those layers cleanly.
**Consequence / mechanism:** Implement the multi-user "door" the way OpenHands does — keep auth, sessions, and integrations as **separately-loaded modules** (dynamic import / optional package), so the single-user path carries zero auth overhead and the upgrade is "enable a module," not "refactor the core." `[OH: the `enterprise/` dynamic-import split]`

### 4.2 Retrieval: self-hosted-first, provider-extensible `[LOCKED]`
**Decision:** Discovery via self-hosted **SearXNG**; extraction via the already-owned self-hosted **Firecrawl** (pointed at that SearXNG). Put a **provider abstraction** in front so third-party search/extraction APIs (Serper, Brave, Exa, Tavily, Jina) can be added for quality without dependency.
**Rationale:** The owner already runs a fully self-hosted retrieval pipeline with zero third-party query egress — privacy and cost are already solved. The abstraction preserves the option to buy quality later.
**Consequence:** Two retrieval slots in the architecture — **discovery** (find URLs) and **extraction** (URL → clean content) — both filled by owned infrastructure, both behind a `SearchProvider` / `ExtractionProvider` interface. (See §9.)

### 4.3 Sandbox: Firecracker via E2B on Proxmox `[LOCKED, pending a trivial host check]`
**Decision:** Use **hardware-isolated microVMs** for code execution — self-hosted **E2B** (Firecracker-backed) as the production sandbox backend, behind a pluggable `SandboxService` interface. A **local process backend** serves development; **gVisor** is the fallback if KVM is ever unavailable.
**Rationale:** Agent-generated code is untrusted; container isolation is insufficient. Firecracker gives microVM-grade isolation at ~150ms startup and is the exact technology Manus runs. Proxmox provides KVM natively, so Firecracker is available; the only gate is confirming `/dev/kvm` access (trivial on Proxmox, and nested-virt is a host toggle if the sandbox runs inside a VM).
**Consequence:** E2B/Firecracker is *one backend* behind the sandbox interface (§11). Develop against the cheap local backend; deploy on Firecracker without touching the orchestrator. Windows guests are out of scope (Firecracker is Linux-only).

### 4.4 Orchestration: own the loop, OpenHands as teacher `[LOCKED]`
**Decision:** Build our own agent loop on **LangGraph** primitives (durable state, checkpointing, human-in-the-loop interrupts) + **E2B** sandbox + a **browser-automation library** (browser-use / Playwright-based) as a tool. **Study OpenHands as a reference corpus; do not fork it.**
**Rationale:** Owning the loop gives full architectural control — critical because UI quality and bespoke control surfaces are exactly where forking someone else's agent becomes painful, and the owner has high taste and is not under multi-user time pressure. OpenHands has already solved the expensive sub-problems (stuck detection, condensation, risk gating); we harvest those lessons (we have read the source) without inheriting their abstractions or upstream-tracking burden.
**Consequence:** The loop, memory, and security designs in §10/§12/§17 adopt specific OpenHands patterns by *re-implementation*, not dependency.

### 4.5 Models: local-first, OpenRouter overflow `[LOCKED]`
**Decision:** Local open-weight models via Ollama/llama.cpp are primary. OpenRouter is the overflow path for hard planning/synthesis and high-precision verification. Routing is **capability-based** behind an `LLMRouter` abstraction.
**Rationale:** Cost, privacy, and control favor local; quality ceilings on the hardest steps favor occasional frontier overflow. The research established a realistic capability floor (a robust agentic loop wants the Llama-3.1/3.3-70B or Qwen3-30–35B class; sub-20B is viable only for short, simple loops). (See §12.)
**Consequence:** Four distinct model roles, different model classes, same-family where possible: agentic loop driver (70B/35B class), RAG answerer (8–14B), query rewriter (7–8B), and an NLI/citation verifier (a ~300M cross-encoder, *not* an LLM).

### 4.6 Streaming transport: WebSocket-primary `[LOCKED — corrected from an earlier SSE-leaning assumption]`
**Decision:** **WebSocket** is the primary real-time channel between client and agent (bidirectional). REST endpoints provide convenience and history/replay. A **pending-message buffer** holds messages that arrive before the socket connects. Side effects hang off the event stream as **callbacks**, not in request handlers.
**Rationale:** Our UX includes mid-task steering (redirect a running agent without losing context, §13.4), which is inherently bidirectional. OpenHands chose WebSocket-primary for the same reason; an earlier SSE-primary assumption in the distilled findings is hereby corrected.
**Consequence:** See §7 (event/streaming model) and §13 (UI).

### 4.7 Hosting: Proxmox production, Bazzite dev `[LOCKED]`
Covered in §2 and §6; recorded here for completeness.

---

# PART II — SYSTEM ARCHITECTURE

## 5. Layered Architecture & Package Topology

The system is organized into four layers, each a separately-versioned package with a clean dependency direction (downward only). This mirrors the structure OpenHands arrived at after refactoring away from a monolith — and we adopt it deliberately so we *start* where they ended up, skipping their V0→V1 rewrite.

```
┌─────────────────────────────────────────────────────────────────┐
│  app  (the product)                                               │
│  • Web frontend (React)                                           │
│  • App-server: multi-conversation orchestration, library,         │
│    projects/spaces, settings, optional auth module                │
│  • Talks to one-or-more agent-servers via conversation URLs       │
└───────────────┬───────────────────────────────────────────────────┘
                │ depends on
┌───────────────▼───────────────────────────────────────────────────┐
│  agent-server  (per-conversation runtime)                          │
│  • Hosts a single running agent/conversation                       │
│  • Exposes WebSocket (real-time) + REST (history/replay)           │
│  • Owns the sandbox lifecycle for its conversation                 │
│  • Streams events; buffers pending messages                        │
└───────────────┬───────────────────────────────────────────────────┘
                │ depends on
┌───────────────▼───────────────────────────────────────────────────┐
│  tools  (the action space)                                         │
│  • Browser, shell, file ops, code-exec, search/extract, deploy     │
│  • Each tool: typed schema + validator + executor                  │
│  • MCP client for external tool servers                            │
└───────────────┬───────────────────────────────────────────────────┘
                │ depends on
┌───────────────▼───────────────────────────────────────────────────┐
│  core  (the brain — no server, no UI, importable library)          │
│  • The agent loop (status state machine)                           │
│  • Event model + event store interface                             │
│  • Memory: View + tombstone condensation                           │
│  • LLM router + per-model/per-mode prompts                         │
│  • Security: risk analyzer + confirmation policy                   │
│  • Stuck detector, critic (optional), subagents                    │
└─────────────────────────────────────────────────────────────────┘
```

### 5.1 Why this split (and why even a solo builder should honor it)

The temptation as one person is to collapse this into a single app. Resist it. The boundaries are what make the loop **testable in isolation** (core has no server/UI to stand up), what let the **UI evolve independently** of the loop, and what make the eventual multi-user / multi-node story tractable. The `core` package in particular must be usable as a plain library — you can drive an agent from a script with no web server at all. OpenHands proves this pays off; the loop logic we care most about lives in exactly such a core SDK.

### 5.2 The two-server topology

There are two server processes, not one `[OH: app-server + agent-server]`:

- **App-server** is the user-facing orchestrator. It manages *many* conversations, the library/history, projects/spaces, settings, and (later) auth. It does **not** run agent loops itself.
- **Agent-server** runs *one* conversation: it hosts the loop, owns that conversation's sandbox, and exposes a WebSocket + REST endpoint addressed by a `conversation_url`.

The app-server's message endpoints are **thin proxies** to the agent-server — no business logic smuggled into the request path. Any custom processing (title generation, notifications, indexing) is implemented as an **event callback**, so that direct agent-server invocation and the proxied path stay functionally equivalent `[OH: explicit design note in their conversation router]`.

For v1 single-node deployment, both servers run on the same Proxmox host (as separate processes/containers). The topology is what makes a future split — agent-servers on separate nodes, app-server as a control plane — a deployment change rather than a rewrite.

### 5.3 Technology choices per layer `[VERIFY versions at build]`

- **core / agent-server / app-server:** Python 3.12+, FastAPI for HTTP/WS surfaces, Pydantic for typed models, LangGraph for the durable orchestration primitives the loop is built on.
- **tools:** Python; Playwright-based browser automation (browser-use or equivalent); the sandbox SDK (E2B) for code execution.
- **frontend:** React + TypeScript, Vite build, TanStack Query for all data access, Tailwind + a Radix/shadcn-class component layer. (Design system in §13.)
- **persistence:** SQLite + filesystem for v1 (single node), behind interfaces that allow Postgres + object storage later. Vector store: a self-hostable engine (e.g., a pgvector or a dedicated local vector DB) behind a `VectorStore` interface. `[OPEN: §24-D1]`
- **queue / background work:** an in-process task model for v1 backed by the durable event log; a dedicated broker (e.g., Redis-backed queue) only if/when multi-node demands it. (§16.)

---

## 6. Deployment & Runtime Topology

### 6.1 v1 deployment (single node, Proxmox)

```
Proxmox host
├── Platform stack (docker compose or Podman pods)
│   ├── app-server        (FastAPI; HTTP + WS; serves frontend build)
│   ├── agent-server(s)   (FastAPI; one per active conversation OR a
│   │                      pool that hosts conversations)
│   ├── postgres/sqlite   (state + vector store)
│   ├── searxng           (already owned; discovery)
│   ├── firecrawl         (already owned; extraction)
│   └── egress-proxy      (per-task allowlist enforcement; §17)
├── E2B / Firecracker     (sandbox microVMs; needs /dev/kvm)
└── Ollama / llama.cpp    (local model serving; GPU)
```

- **Local models** run via Ollama/llama.cpp with GPU access on the host (the owner runs dual-GPU-class hardware).
- **Sandboxes** are Firecracker microVMs launched on demand, network-isolated behind the egress proxy.
- **OpenRouter** is reached over the network for overflow calls only.

### 6.2 The "door open" deployment seams

Three seams are designed now (cheaply) so that later upgrades are configuration, not surgery:

1. **State backend:** SQLite/filesystem → Postgres/object storage (interfaces only; §7).
2. **Sandbox backend:** local process (dev) → Firecracker (prod) → remote (future multi-node) (interface; §11).
3. **Multi-node:** agent-servers co-located now → distributed later (the two-server topology already supports it).

### 6.3 Operator experience target

A single declarative bring-up (`docker compose up` / a Makefile target) starts the full stack. Configuration is one file plus secrets (§19). The owner should never need to hand-wire five services — operational simplicity is Principle 9.

---

## 7. The Event & State Model (the spine)

This is the most important architectural section, because nearly everything else derives from it.

### 7.1 Events are the atomic unit, and the log is append-only

All conversation and agent activity is represented as a stream of typed **Events**. The principal event types `[OH: ActionEvent/ObservationEvent/MessageEvent/AgentErrorEvent/Condensation]`:

- **MessageEvent** — a message from user, agent, or environment.
- **ActionEvent** — the agent decided to take a tool action (carries thought, the action, tool name).
- **ObservationEvent** — the result of an action.
- **AgentErrorEvent** — a tool/execution error observation.
- **CondensationEvent** — a memory tombstone (see §7.3).
- **ConversationErrorEvent / status events** — lifecycle signals (e.g., max-iterations reached, stuck).

The log is **append-only and never mutated.** Consequences, all of them desirable:

- **Replay & resume:** the full state is reconstructed by replaying events. Close the tab, reopen, rehydrate — for free.
- **Debuggability:** even if the sandbox is gone, there is a perfect record of what happened.
- **Audit:** the security audit trail (§17) is just a view over the event log.

### 7.2 Event storage is pluggable

An `EventStore` interface supports filtering by kind/timestamp, sorting, and pagination, with a filesystem/SQLite implementation for v1 and the door open to object storage / Postgres `[OH: filesystem/aws/gcloud implementations behind one interface]`. History endpoints are paginated queries against this store; that is also the replay mechanism.

### 7.3 Memory: the View + tombstone pattern (how we forget from an append-only log)

The tension is obvious: an append-only log grows without bound, but the LLM context window is finite. The resolution is elegant and we adopt it directly `[OH: the condenser README's design]`:

- We never delete events. To "forget," we append a **Condensation tombstone** that records *what to forget and what summary to insert in its place* (conceptually identical to Cassandra/Kafka tombstones).
- A **`View`** object computes, at read time, "what the LLM currently sees" by applying tombstones to the raw event stream. The agent talks to the model through the View, never the raw log.
- The default condensation strategy: **replace the first half of the view with a single summary event, leave the back half untouched.** Summaries are themselves re-summarized in later condensations, so early context survives in compressed form while recent context stays verbatim for task continuity.

Condensation parameters and behavior (§12.4 covers the model used):
- `keep_first` (≈2): never condense the system prompt and first user message.
- `minimum_progress` (≈0.1): if a condensation would forget less than ~10% of the view, treat it as a no-op error (avoids pointless cache-destroying churn).
- **Soft vs hard triggers:** soft = size/token limit reached → if condensation isn't structurally possible this step, return the view uncondensed and retry next step. Hard = context-window-exceeded (no next step possible) → forget-and-summarize everything (`hard_context_reset`) with retries and per-retry size scaling.
- **Cache-aware cadence:** condense *regularly* in moderate amounts rather than rarely in huge amounts, because each condensation invalidates the prompt cache and frequent-small keeps the rebuild cost bounded.
- **Provider-specific detection:** "context window exceeded" manifests differently per model/provider; a classifier enumerates the known shapes. Budget for this — it is unglamorous and necessary.

### 7.4 Two-tier memory

- **Working memory** = the View over the event log (short-term, condensed as above).
- **Persistent memory** = a durable, human-readable store of project/space-specific knowledge that auto-loads into context each session, which the agent can also write to ("self-documentation") `[OH: AGENTS.md as persistent repo memory]`. This is the mechanism behind **Spaces** (§8.3) — bounded, persistent context per project.

### 7.5 Streaming model (transport)

- **WebSocket** carries the live event stream and bidirectional control (messages, steering, confirmations) between client and agent-server. `[LOCKED §4.6]`
- **REST** provides convenience message-send (a thin proxy) and history/replay (paginated `EventStore` queries).
- **Pending-message buffer:** messages that arrive before a socket is established are persisted server-side and processed on connect, so nothing is lost during connection setup `[OH: pending_messages]`.
- **Side-effects-as-callbacks:** title generation, indexing, notifications, and audit hooks subscribe to the event stream rather than living in the request path `[OH: event_callback]`.

---

# PART III — SUBSYSTEM DESIGNS

## 8. Surfaces: Research, Agent, and Spaces

The Platform exposes the one agent core through differently-scoped **surfaces**. A surface is a bundle of (a) tool scope, (b) system prompt/mode, (c) UI layout, and (d) default confirmation policy.

### 8.1 The Research surface
- **Tool scope:** discovery + extraction + (optional) code-exec for data analysis. **No** state-changing/world-affecting tools.
- **Prompt/mode:** synthesis-oriented; for Deep Research, the long-horizon planning prompt.
- **Loop shape:** plan → iterative (search → read → refine) → synthesize → ground/cite. Standard answers run a short loop; Deep Research runs many iterations across many sources and emits a structured report.
- **UI:** single-column document-grade answer view with the source panel (§13.3).
- **Confirmation policy:** effectively `NeverConfirm` (read-only scope means low risk by construction).

### 8.2 The Agent surface
- **Tool scope:** the full sandbox — browser, shell, file ops, code-exec, deploy — plus discovery/extraction.
- **Prompt/mode:** long-horizon or interactive depending on invocation.
- **Loop shape:** plan (visible) → one-action-per-iteration execution with observation → reflect/replan → deliver.
- **UI:** split-pane / "Sidecar" with the live agent-working view and inspector (§13.4).
- **Confirmation policy:** `ConfirmRisky` (gate on risk threshold; confirm-on-UNKNOWN), with the plan-preview gate up front (§13.2).

### 8.3 Spaces (bounded context)
A **Space** is a persistent context wrapper around a body of work `[Perplexity Spaces pattern]`. It bundles:
1. **Persistent instructions** silently prepended to every interaction in the Space.
2. A **bounded retrieval corpus** — files/URLs/repos uploaded to the Space form an *exclusive* RAG silo for that Space, preventing cross-contamination (a coding query never retrieves an unrelated document).
3. (Future, multi-user) **access control.**

Spaces are implemented on the §7.4 persistent-memory tier plus a scoped vector-store namespace (§9.5). They are the primary unit of organization in the library and the mechanism that keeps per-project context clean.

---

## 9. Retrieval Engine

Retrieval is the dominant determinant of answer quality (Principle/§1.1). This subsystem gets disproportionate engineering care. It has two slots — **discovery** and **extraction** — and a quality pipeline on top.

### 9.1 Discovery (find URLs)
- **Primary:** self-hosted **SearXNG** behind a `SearchProvider` interface. `[LOCKED §4.2]`
- **Optional upgrades:** Serper (cheap Google), Brave, Exa (semantic/neural), Tavily (structured + crawl), Jina — each a `SearchProvider` implementation, selectable per query or per Space, off by default.

### 9.2 Extraction (URL → clean content)
- **Primary:** the already-owned self-hosted **Firecrawl** behind an `ExtractionProvider` interface (it converts pages to clean, LLM-ready content). `[LOCKED §4.2]`
- **Fallbacks/alternatives:** Trafilatura/Crawl4AI-class local extractors, or Jina Reader, behind the same interface.

### 9.3 The quality pipeline (do this regardless of provider)
Raw search results are not good enough for Perplexity-class answers. The pipeline:

1. **Query transformation:** rewrite-retrieve-read; for hard questions, multi-query / RAG-Fusion (generate 3–5 paraphrases, retrieve each, fuse with Reciprocal Rank Fusion). HyDE / step-back as options.
2. **Retrieve broadly:** pull 20–50 candidate passages across providers/corpora.
3. **Rerank:** a **cross-encoder reranker** (self-hosted `bge-reranker-v2-m3` with `bge-m3` embeddings; Cohere/Jina rerankers as API options) reduces to the top 5–10. Reranking is the single highest-leverage quality lever and is far cheaper than enlarging context.
4. **Extract & ground:** fetch the winning URLs via the extraction provider; carry **span-level provenance** into generation (§10/§14).
5. **Cache aggressively** and weight domains (allow/deny) per Space.

### 9.4 The "never trust the snippet" rule
Information priority is **authoritative source/API > extracted page content > model internal knowledge**. The agent must **open the original URL** rather than answering from a search snippet `[OH: info_rules]`. This is a grounding-quality rule enforced in the prompt and the tool design.

### 9.5 Vector store & corpora
A `VectorStore` interface backs both ad-hoc RAG and Space corpora, with **namespaced collections** so each Space is an isolated silo (§8.3). Embeddings via a local model (`bge-m3`-class). `[OPEN §24-D1: which vector engine]`

---

## 10. The Tool System (the action space)

Tools are the agent's hands. The reliability of the whole system depends as much on tool design as on model choice — the research showed a tool-calling success rate going from ~7% to ~100% purely by tightening the tool schema and harness, with no model change. Tool design is therefore treated as a reliability discipline.

### 10.1 Tool anatomy
Every tool is three things:
1. A **typed schema** (strict, validated — Pydantic) describing inputs/outputs.
2. A **validator + auto-repair** wrapper: validate the model's tool call against the schema; on malformed output, return a structured error and the prior call to the model with "fix this," rather than failing the step.
3. An **executor** that performs the action (often inside the sandbox).

### 10.2 The core toolset (v1)
- **Browser** (Playwright/browser-use): navigate, click, type, scroll, extract, screenshot. Perceives pages via DOM + accessibility tree, with screenshot/vision for grounding where needed.
- **Shell** (in-sandbox): run commands. Risk-scored via a shell parser (§17).
- **File ops** (in-sandbox): read/write/edit via **dedicated file tools, not shell redirection** `[OH: file_rules]` — this sidesteps the string-escaping failures of piping model output through bash.
- **Code execution** (in-sandbox): run Python/Node; the preferred action representation is **CodeAct (emit code)** for complex/data work, which benchmarks materially higher than JSON tool calls for multi-step work.
- **Search / Extract:** thin tool wrappers over the §9 providers.
- **Deploy/preview:** start a dev server / produce a preview URL for built artifacts (§8.2, Labs-style).

### 10.3 Tool-calling format & harness
- Prefer **CodeAct** for code-heavy workflows; use **strict typed JSON tool calls** elsewhere.
- **Deterministic decoding** (temperature ≈ 0) for tool-call generation.
- **External validation + auto-repair** on every call (10.1).
- This harness discipline is non-optional on local models, where it is the difference between a usable and an unusable loop.

### 10.4 MCP (external tools)
An **MCP client** lets the agent use external Model-Context-Protocol tool servers `[OH: mcp module]`. This is how the toolset extends without bloating core, and the seed of a future tool marketplace (a v1 non-goal but an explicit forward path).

---

## 11. Sandbox & Code Execution

### 11.1 Purpose & threat model
The sandbox runs **untrusted, agent-generated code**. Its interior is hostile (Principle 4). Isolation must be hardware-grade, secrets must never be present inside it, and egress must be controlled.

### 11.2 Spec vs instance
The sandbox subsystem separates `[OH: sandbox spec vs instance]`:
- **Sandbox spec** — the template: base image, installed tools, resource limits, egress allowlist.
- **Sandbox instance** — a running microVM created from a spec, with lifecycle (create / start / stop / destroy) and user-scoping.

### 11.3 Backends (pluggable)
A `SandboxService` interface with:
- **Local process** backend — for development (fast, no isolation; dev only).
- **Firecracker via E2B** — production; microVM isolation, ~150ms start, runs on the Proxmox host's KVM. `[LOCKED §4.3]`
- **gVisor** — fallback if KVM is unavailable (user-space kernel, no hardware-virt requirement).
- **Remote** — future multi-node.

### 11.4 Linux-only, by design
Firecracker runs Linux guests only. The sandbox executes Linux workloads (Python, Node, web builds, shell) — exactly the Manus model. Deliverables (web apps, reports, analyses) are consumed on any OS. Native Windows execution is out of scope (§1.2); if ever needed, it would be a separate full-VM tool outside the fast-sandbox path.

### 11.5 Secrets & egress (the hard constraints)
- **No secrets inside the sandbox — ever.** The orchestrator holds credentials in a separate secrets store (§17, §19) and injects only narrow, short-lived, scoped capabilities per task `[OH: separate secrets store outside sandbox]`.
- **Deny-by-default egress** via a per-task allowlist enforced by the egress proxy (§17). A task that needs to reach specific domains gets exactly those.

### 11.6 The live sandbox view
For browser/GUI work, the sandbox exposes a live view via **VNC (xvfb + x11vnc) → websockify → noVNC** in the browser, streamed over WebSocket alongside the event stream `[OH/Manus pattern]`. This powers the Inspector's picture-in-picture (§13.4) so the user can verify the agent isn't stuck on a CAPTCHA.

---

## 12. The Agent Loop & Orchestration (the core)

This is the heart of the system. It is specified in detail because we read the reference implementation's source and because the loop's correctness *is* the product's reliability. We build it ourselves (Principle/§4.4) on LangGraph primitives, adopting the OpenHands loop shape by re-implementation.

### 12.1 The loop is an explicit status state machine
The loop is driven by an explicit `ExecutionStatus`, not an implicit "while not done" `[OH: ConversationExecutionStatus]`:

`IDLE · RUNNING · PAUSED · STUCK · WAITING_FOR_CONFIRMATION · FINISHED · ERROR`

The run loop is a bounded `while` over `agent.step()`, where each step takes **exactly one tool action and observes its result before the next** (Principle 6). Bound: a hard `max_iterations` ceiling (≈500) that forces termination if all other exits fail — the backstop when stuck detection misses.

### 12.2 Step lifecycle (one iteration)
1. **Check status under lock** (see 12.3) — handle PAUSED/STUCK/FINISHED/confirmation transitions before doing work.
2. **Stuck check** (§12.5) — if stuck, set STUCK and break.
3. **Build the View** (§7.3) — condense if triggered; this is what the model sees.
4. **Select model & prompt** (§12.6) — route by task/difficulty; choose per-model, per-mode prompt.
5. **Agent proposes one action** (with thought + self-assessed risk, §17).
6. **Risk gate** (§17) — analyzer scores the action; the confirmation policy decides. If confirmation needed → set WAITING_FOR_CONFIRMATION, stop; resume executes on approval (two-phase, 12.4).
7. **Execute** the action (in sandbox if applicable); validate/auto-repair tool output (§10.1).
8. **Append** ActionEvent + ObservationEvent (or AgentErrorEvent) to the log.
9. **Loop**; increment iteration; enforce ceiling.

### 12.3 Concurrency & control (pause / steer / not-drop)
- **Pause-under-lock:** pausing acquires the same state lock a step needs, so a pause can never land mid-action. Clean, race-free interruption — the mechanism behind the UI "Steering Wheel" (§13.4). `[OH]`
- **Don't drop concurrent messages:** the loop deliberately does **not** terminate on FINISHED inside the lock; a user message arriving concurrently (via the pending buffer / send path) flips status back to IDLE so the next iteration processes it instead of losing it. `[OH]`
- **FIFO lock** on state mutation serializes concurrent inputs deterministically.

### 12.4 Confirmation as a two-phase step (HIL at the loop level)
Human-in-the-loop is implemented *in the loop*, not bolted onto the UI `[OH: confirmation mode]`:
- **First pass:** the agent *creates* the action but does not execute it; status → WAITING_FOR_CONFIRMATION; the loop stops and surfaces the proposed action (and, up front for a whole plan, the plan-preview gate, §13.2).
- **Second pass (on approval):** the pending action executes.
This is the same machinery for (a) the upfront plan-preview gate and (b) per-action gating of risky steps (§17).

### 12.5 Stuck detection (first-class, ported from source)
Checked **every iteration**; on detection, set STUCK and stop. Scans only the **last ~20 events** (never materializes huge histories) and **resets after the last user message** (a new instruction = not stuck). Five patterns `[OH: stuck_detector.py, re-implemented]`:
1. Repeating identical **action→observation** cycles.
2. Repeating identical **action→error** cycles (the classic "retry the same failing call" failure).
3. **Agent monologue** (N consecutive agent messages, no user input).
4. **Alternating A-B-A-B** action/observation loops.
5. **Context-window-error loops** — *known-hard; even OpenHands stubs this.* We treat it with extra care (couple it to hard-reset in §7.3).

Equality compares **content** (thought, action, tool name, observation, error) and **ignores volatile IDs** (tool_call_id, response_id, action_id), so it catches *semantic* repetition. Thresholds are configurable.

### 12.6 Stop-hooks (programmable completion gate)
On FINISHED, a hook may **veto** stopping and inject feedback ("you have not actually satisfied the goal; continue"), resuming the loop `[OH: stop hooks]`. This is where a **critic** (§12.8) or an acceptance check plugs in for quality enforcement.

### 12.7 Planning & externalized state
- The plan is **externalized** (a visible task list the user sees and the agent maintains), not held only in context — this doubles as the §13 progress view and survives condensation.
- Working memory is the View (§7.3); persistent/cross-session memory is the Space's memory tier (§7.4, §8.3).

### 12.8 Subagents & the critic (forward-compatible, partly v1)
- **Subagents:** the loop supports delegating a bounded sub-task to a child agent `[OH: subagent module]`. v1 keeps this minimal (single main agent); the seam exists.
- **Critic:** a self-evaluation module that scores intermediate/final output against intent `[OH: critic module — present, flagged not-yet-deep-read]`. This is the natural home for the **visual-iteration** loop (render → screenshot → vision-critique → revise) and for answer-quality acceptance. **[OPEN §24-D3: read the critic source before finalizing this design.]**

### 12.9 Efficiency directive
Because each step is comparatively expensive, the prompt actively encourages **compound actions within a single step** (e.g., chained shell with grep/sed) rather than taking more steps `[OH: EFFICIENCY]`. This pairs with one-action-per-iteration to balance reliability against cost/latency.

---

## 13. UI / UX

The UI is a first-class requirement (§1.1), not a finish. The governing thesis: **absorb complexity internally to project authority externally** — the interface recedes and acts as a calm, structured vessel for facts. Four principles drive it: radical source transparency, visual modularity over text walls, process visibility, and aesthetic restraint.

### 13.1 Two display paradigms (the system needs both)
- **Replayable timeline** (research / API-style work): logically grouped actions, milestones, smart diffs — not a raw character stream.
- **Live visual desktop** (computer-use): a live noVNC feed (§11.6).
- Default surface = timeline; the live desktop is **one click away** in the Inspector. We explicitly avoid the "black box with no live preview" failure that drew criticism of Perplexity's Personal Computer — an intermediate/live view is always available.

### 13.2 The plan-preview gate (highest-leverage feature)
Before consuming compute, show a **written execution plan** the user can read, narrow, or correct. This is simultaneously (a) cost control (it empirically cut comparable systems' spend 30–50%), (b) intent alignment, and (c) the upstream HIL gate (§12.4). In-flight, the plan continues as the live, checked-off task list.

### 13.3 The research/answer surface — document, not chat
- **Input absorbs complexity:** accept vague/messy multi-part queries; do query expansion/intent classification silently (§9.3); shield the user from prompt-engineering mechanics.
- **Stream structured UI components, not just Markdown:** as the model emits schema-validated structured output, render interactive tables, syntax-highlighted code, and definition blocks inline mid-stream (React Server Components-style). This is the single biggest "premium vs. client-side-Markdown" differentiator and the thing cheap clones get wrong.
- **Kill layout shift (CLS):** pre-allocate space (fixed-height source panel), stream into rigid containers; jumpy reflow is the #1 tell of a cheap streaming UI.
- **Source panel + hover citations** (the highest-leverage trust pattern): inline bracketed numerals; hover/tap → a rich card (favicon, title, clean domain, the **corroborating snippet**) so the user verifies without leaving the page (mobile = bottom-sheet). Above the answer, a **dual-tab panel: "All Searched" vs "Cited."** Provide **re-scope controls** (filter domains, drop weak sources, force re-synthesis on the curated set) — the UI counterpart to the grounding pipeline (§14).
- **Make failures explicit:** broken/paywalled/blocked citations get visible warning states. Hidden failures destroy trust faster than visible ones.
- **Avoid:** end-of-document reference dumps with no inline anchors; cramming a long report into a narrow chat column instead of a full-bleed document layout with a sticky table of contents.

### 13.4 The live "agent working" view — show the plan, hide the loop (three-tier disclosure)
- **Default — Project-Manager view:** a tree/kanban of the plan with the current high-level step and progress. *Not* raw API calls/tokens. Alongside it, an **Agent Activity Feed** (filterable, audit-log-style: what was done, why, with the agent's **confidence**). A **confidence gradient** drives attention — routine high-confidence actions recede; low-confidence/novel actions are bolded and floated up. Stream **interim semantic checkpoints** ("Corroborating multiple sources," "Test suite 50%") — never go silent.
- **One click away — Inspector:** live noVNC PIP (verify it's not stuck), **stateful smart diffs** for code, real tool surfacing (the exact search queries; a terminal-style block for code-exec), and a visible **intermediate workspace/scratchpad** (staged CSVs, images, drafts). Raw JSON/logs live behind an explicit expander — unfiltered terminal dumps create anxiety.
- **Intervention — Steering Wheel:** not just Stop. A **Redirect input** ("stop researching X, focus on Y") lets the agent replan *without losing session context* (§12.3). Plus the kill switch (§13.6).

### 13.5 Spatial architecture (layout per surface)
- **Research surface:** clean single-column document stream.
- **Agent surface:** split-pane / "Sidecar" — a persistent control pane beside the execution canvas; SSE-style narration is carried on the WS event stream, with the bidirectional control channel for steering.
- **Deep Research report view:** sidebar-pinned table of contents, document navigation, export to PDF/Markdown/shareable HTML.
- **Labs/builder view (Agent deliverables):** multi-pane — **Apps** (live iframe preview of generated HTML/CSS/JS), **Tasks** (workflow visualizer), **Assets** (downloadable artifacts), **Sources**. Edits are **scoped** ("change the secondary chart to a scatter plot" modifies only that scope via version history + visual diffs, never regenerating the whole artifact and destroying manual tweaks).
- Reserve dense four-quadrant (chat/CLI/editor/browser) for a future "developer mode."

### 13.6 The kill switch (real definition)
Not a pause. A persistent, globally visible emergency halt that **revokes the agent's identity/tokens at the network level**, freezes state, writes immutable logs, and triggers **rollback-and-quarantine** of file changes `[Comet/JumpCloud pattern]`. This works *only because* secrets/capabilities live in the orchestrator (§11.5/§17) — the orchestrator is what revokes them. UI and security architecture reinforce each other.

### 13.7 Design language (concrete, committable)
Philosophy: the **"Scandinavian subway map"** — clean, ruthlessly functional, ultimately invisible. Anti-tells to avoid: unconstrained text streams, muddy dark modes, default system typefaces, everything-on-one-screen dashboards. The committable system `[VERIFY exact tokens at build]`:
- **Type — four roles:** a display face (wordmark only), a workhorse grotesk (UI/nav/headers), a reading grotesk (long-form answer body), and a mono (`JetBrains Mono`/`Fira Code`) for code. **rem-based scaling, never px.** Choose a grotesk with broad glyph coverage to avoid fallback-font layout shift.
- **Line-height:** UI/labels tight (1.3–1.4); long-form body relaxed (1.5–1.7).
- **Spacing scale (rem, base 16px), via flex/grid `gap`:** 0.25 / 0.5 / 1 / ~1.5 / ~1.9; border-radius ~0.5rem (soft, not bubbly). Use `:has()` for content-dependent padding rather than JS layout math.
- **Color — aggressively restrained:** stark light/dark, near-absolute backgrounds for contrast; desaturated muted blue for links (not reflex blue); chroma reserved strictly for **actionable/verifiable** elements (citations, approve/confirm, low-confidence flags) — never ambient decoration. Where a richer palette appears (dashboards), a strict 60-30-10.
- **Depth — a "surface ladder":** elevation via subtle lightness shifts + hairline borders; **never heavy shadows or glassmorphism.**
- **Motion — minimal, to mask latency:** a signature micro-interaction during time-to-first-token instead of a generic spinner.

### 13.8 Frontend data-flow discipline (the architectural side of "not vibe-coded")
Enforce a strict path `[OH: their documented rule]`: **UI components → TanStack Query hooks → a data-access layer → API.** Components never call the API directly. Query hooks `use[Resource]`, mutations `use[Action]`. This is exactly what makes the streaming/optimistic-update UX tractable rather than ad hoc, and it is the structural discipline that separates professional from amateur frontends.

---

## 14. Citation & Grounding

Citation faithfulness is a **pipeline problem, not a prompt** — strong models still leave a meaningful fraction of claims unsupported on their own. We build a verification stage; we do not rely on instructions.

### 14.1 The grounding pipeline
1. **Retrieve with span provenance** (§9): every passage carries its source identity and location.
2. **Constrained generation:** numbered passages in; each claim must end with `[source_ids]`. Optionally emit a JSON claim list first, then render prose from it.
3. **Automatic verification:** split the answer into atomic claims; run a per-claim **NLI/entailment check** (premise = retrieved passage; hypothesis = claim) using a compact cross-encoder (§12 model roles; e.g., a DeBERTa-v3-large-MNLI-class model, ~300M params — *not* an LLM).
4. **Self-correction:** drop or regenerate claims that fail entailment (regenerate from the supported subset).
5. **Surface honestly:** unsupported/weak claims and broken sources are shown as such in the UI (§13.3), never hidden.

### 14.2 Local-first, frontier-judge-sparingly
Generation + first-pass NLI verification run **fully local** (the cross-encoder is trivial next to the LLMs). A frontier "judge" via OpenRouter is reserved for **periodic eval sampling** (§20), not the live path — keeping per-answer latency and cost local while still measuring quality honestly.

---

## 15. Model Routing (local ⇄ OpenRouter)

### 15.1 Capability-based routing
Route by **task type and difficulty** through an `LLMRouter` abstraction, so model swaps/upgrades are config, not code (Principle 8). The router selects model *and* the matching per-model, per-mode prompt (§12.6).

### 15.2 The four roles (and realistic model classes) `[VERIFY model names/benchmarks at build]`
The research established a capability floor; the role assignments:

| Role | Runs where | Realistic class | Notes |
|---|---|---|---|
| **Agentic loop driver** | local primary; overflow on hard steps | **Llama-3.1/3.3-70B** or **Qwen3-30–35B-A3B** | The first sizes where tool-calling + planning + multi-turn stability are "good enough." Sub-20B only for short, simple loops. Quantize **Q4_K_M / Q5_K_M**; never below Q4 for the driver. |
| **RAG answerer** | local | **8–14B** (Llama-3.1-8B / Qwen2.5-14B) | Same family as the driver where possible. Low temperature; citations forced by schema. |
| **Query rewriter** | local | **7–8B** | Shallow semantic task; deterministic; small gains from going bigger. |
| **NLI / citation verifier** | local side-car | **~300M cross-encoder** (DeBERTa-v3-large-MNLI class) | Not an LLM. 3-way entailment. |
| **Summarizer/condenser** | local (cheap) | small/cheap model | Separate from the agent LLM (§7.3). |

### 15.3 Overflow policy
Overflow to OpenRouter **only** on: low local confidence, repeated tool errors / stuck recovery, the hardest planning/synthesis steps, or high-precision verification. Every overflow decision is logged (§20). The local/overflow threshold is a **tunable policy**, not hardcoded.

### 15.4 Cost envelope `[VERIFY pricing at build]`
A Deep Research run ≈ 20–40 model calls; route only the 5–10 hardest to a frontier model → on the order of low-single-digit to ~$18 per run at mid-frontier rates, near-zero marginal cost for the local majority. **The plan-preview gate (§13.2) cuts this 30–50%** by trimming exploration before it runs. Quantization guidance: Q4_K_M/Q5_K_M for the 30–70B driver; secondary models tolerate Q4_K_S/IQ4_XS.

---

## 16. Long-Running Tasks & Background Execution

Agent tasks run for minutes to hours. The "loading spinner" paradigm is dead; tasks are **background jobs keyed to a task/conversation ID**, decoupled from any UI connection.

- **Decoupling:** closing the tab ends only the *stream*, not the job. Reconnect rehydrates from the event log (§7.1). This is the Devin "sleep, don't end" model.
- **v1 mechanism:** an in-process async task model backed by the durable event log (the log *is* the job's state). A dedicated broker (Redis-backed queue, etc.) is introduced **only** when multi-node demands it — Principle 9.
- **Completion & notification:** completion is an event; the side-effects-as-callbacks mechanism (§7.5) handles notifications.
- **Resumption:** because state is event-sourced, resume-after-restart falls out of replay.

---

# PART IV — CROSS-CUTTING CONCERNS

## 17. Security Architecture

Security is not a layer bolted on; it is woven through the loop, the sandbox, and the network. Three threat surfaces dominate: untrusted **agent-generated code**, untrusted **web content** (prompt injection), and **secret exposure**.

### 17.1 Secrets (never in the sandbox)
A dedicated **secrets store** lives in the orchestrator, outside any sandbox `[OH: separate secrets store]`. The sandbox receives only narrow, short-lived, scoped capabilities per task. No credential, key, env var, or internal config is readable from inside the sandbox (Principle 4; the load-bearing Manus lesson). (§19 covers storage.)

### 17.2 Risk-scored confirmation (HIL, from source)
Adopt the OpenHands model directly `[OH: SecurityRisk / SecurityAnalyzer / ConfirmationPolicy]`:
- **`SecurityRisk` enum:** UNKNOWN / LOW / MEDIUM / HIGH (ordered; UNKNOWN non-comparable).
- **Pluggable `SecurityAnalyzer`** scores each action *before execution* — LLM-based, rule-based (a **shell-command parser** for sandbox commands), or an **ensemble** (defense in depth).
- **Pluggable `ConfirmationPolicy`** decides from the score: `AlwaysConfirm` / `NeverConfirm` / `ConfirmRisky(threshold=HIGH, confirm_unknown=True)`. **Confirm-on-UNKNOWN is the safe default.**
- The agent **self-assesses** risk in-prompt *and* an independent analyzer scores it — two signals, not one. This feeds the loop's WAITING_FOR_CONFIRMATION gate (§12.4) and the per-surface defaults (§8).

### 17.3 Prompt-injection defense (browsing agent)
Web content is hostile until proven otherwise. Defenses, layered (effective patterns, not theater):
- **Separate reading from acting (dual-LLM / quarantine):** a low-privilege model reads hostile page content; only structured, policy-checked data reaches the high-privilege planner.
- **Least privilege + domain allowlisting + per-domain tokens; no raw credentials in prompts.**
- **HIL confirmation for state-changing actions** (§17.2).
- **Schema-validated tool outputs** (§10.1).
- Detection filters (Rebuff/Guardrails-class) layered as heuristics, **never** as the sole defense.
- **Continuous red-teaming:** wire an injection benchmark (AgentDojo-class) into the eval harness (§20) from the start.

### 17.4 Egress control
**Deny-by-default** network egress from sandboxes, enforced by the **egress proxy** (§6.1) against a **per-task allowlist**. A task that needs specific domains gets exactly those and nothing else.

### 17.5 The kill switch (security view)
The §13.6 kill switch is a security control: network-level capability/token revocation + immutable logging + rollback-and-quarantine. It is effective precisely because capabilities are orchestrator-held (§17.1).

### 17.6 Audit
The append-only event log (§7.1) is the audit trail. Security-relevant events (risk scores, confirmations, overflow calls, egress grants, kill-switch activations) are first-class events, queryable as a view.

## 18. Observability

- **Tracing:** instrument the loop, tool calls, model calls, and retrieval with structured tracing (an OpenTelemetry-compatible approach; an LLM-observability layer is acceptable). Every step, every tool call, every model/route decision is a span. `[OH: they ship an observability module]`
- **The event log as observability:** replay is a debugging superpower — any run can be reconstructed exactly (§7.1).
- **Metrics:** per-run token/cost (local vs overflow), iteration counts, stuck-detection fires, condensation events, retrieval latency and source counts, citation-verification pass rates.
- **Logs:** structured; raw model I/O behind a verbosity flag and never surfaced raw in the UI (§13.4).

## 19. Configuration & Secrets

- **Single config surface:** one declarative config file plus a secrets mechanism; sensible defaults so the owner edits little (Principle 9).
- **Secrets:** stored in the orchestrator's secrets store (§17.1); injected as scoped capabilities; never committed, never in the sandbox, never in prompts. For v1, a file-based encrypted store is acceptable behind a `SecretsStore` interface (door open to a real secrets manager).
- **In-app settings:** model routing thresholds, provider selection (search/extract), confirmation policy per surface, and Space configuration are runtime settings, not redeploys.

## 20. Testing & Evaluation Strategy

Quality is measured, not asserted — this is what separates professional from "vibe-coded" (§1.1).

- **Unit/integration:** the `core` package is testable headless (no server) — loop transitions, stuck detection, condensation, risk gating, tool validation each get focused tests. (Stuck detection and condensation especially — they are subtle.)
- **RAG/answer evaluation:** a faithfulness/citation harness (RAGAS/DeepEval-class) measuring faithfulness, answer relevance, context precision/recall; run on a fixed question set; a frontier judge samples periodically (§14.2).
- **Agent-task evaluation:** trajectory-level evaluation on a held-out task set; track pass@1 and reliability across repeats (the research showed "can do" ≠ "does reliably").
- **Security evaluation:** an injection benchmark (AgentDojo-class) in CI (§17.3).
- **Model-capability pinning:** the §15 role assignments are validated empirically early (the owner has the hardware to just run candidate models through a tool-calling loop) and re-checked when models change `[VERIFY]`.
- **Frontend:** component tests (vitest-class); explicit attention to streaming states, empty states, and error/failure states (the things cheap clones neglect).

## 21. Performance & Cost Targets `[VERIFY/tune]`

- **Time-to-first-token** on standard answers: masked by the §13.7 latency micro-interaction; target low-single-digit seconds for local RAG answers.
- **Sandbox cold start:** ~150ms (Firecracker) — keep a small warm pool if needed.
- **Deep Research:** 2–4 min standard; longer for Labs-style builds — these are *background* (§16), so latency is a legibility problem (§13.4), not a blocking one.
- **Cost:** dominated by overflow; bounded by the plan-preview gate (§13.2, §15.4) and local-first routing.
- **Reranking** is cheaper than bigger context — prefer it (§9.3).

---

# PART V — EXECUTION

## 22. Phased Build Plan

The sequencing principle: **build the spine first, prove the loop on the cheapest possible substrate, then add capability and polish.** Each phase ends at a demonstrable, usable milestone. UI quality is woven through (not deferred to the end), because it is a primary requirement and because retrofitting polish is how clones end up looking vibe-coded.

### Phase 0 — Foundations & skeleton (the walking skeleton)
**Goal:** the four-package structure exists and a trivial agent loop runs end to end on the local-process sandbox, with events streaming to a minimal UI.
- Scaffold `core / tools / agent-server / app-server` with clean dependency direction (§5).
- Implement the **event model + filesystem/SQLite EventStore** (§7.1–7.2).
- Implement the **status-state-machine loop** with one-action-per-iteration and the `max_iterations` ceiling (§12.1–12.2) — no stuck detection, no condensation yet.
- One tool (shell or file-op) on the **local-process sandbox** backend (§11.3).
- **WebSocket event streaming** + pending-message buffer (§7.5); a bare React shell that renders the event timeline (§13.1) through the data-access discipline (§13.8).
- **LLMRouter** stub pointing at one local model (§15).
**Milestone:** "type a goal, watch one tool action execute, see it stream." The spine is alive.

### Phase 1 — A reliable loop
**Goal:** the loop is trustworthy on local models.
- **Stuck detection** (all five patterns, ID-ignoring equality) (§12.5).
- **Memory: View + tombstone condensation**, soft/hard triggers, separate cheap summarizer model (§7.3, §15).
- **Tool harness discipline:** strict typed schemas, deterministic decoding, validate-and-auto-repair (§10.1, §10.3); add file-ops + code-exec; introduce **CodeAct** for code work.
- **Risk-scored confirmation**: SecurityRisk + a rule-based shell analyzer + ConfirmationPolicy + the two-phase confirmation step (§17.2, §12.4).
- Empirically **pin the model roles** (§15.2, §20) on the owner's hardware.
**Milestone:** the agent completes a multi-step task on a local 70B/35B-class model without getting stuck, with risky actions gated.

### Phase 2 — The Research surface (vertical slice #1)
**Goal:** a Perplexity-class answer, end to end, on owned infrastructure.
- **Retrieval engine:** SearXNG discovery + Firecrawl extraction behind provider interfaces; the **quality pipeline** (multi-query, RRF, **cross-encoder rerank**) (§9).
- **Citation/grounding pipeline** with the **NLI cross-encoder verifier** and self-correction (§14).
- **Research UI:** the document-grade answer view — structured-component streaming, CLS control, the **source panel + hover citations + All/Cited tabs + re-scope controls** (§13.3).
- **Vector store + Spaces** (bounded corpora, namespaced) (§8.3, §9.5).
**Milestone:** ask a hard question, get a fast, grounded, beautifully-rendered, verifiably-cited answer; organize work into Spaces.

### Phase 3 — The Agent surface & secure sandbox (vertical slice #2)
**Goal:** autonomous tasks run in real isolation with a legible live view.
- **Firecracker/E2B sandbox** backend on Proxmox; **egress proxy** with per-task allowlists; secrets-out enforcement (§11, §17.1, §17.4).
- **Browser tool** (Playwright/browser-use) + **dual-LLM read/act separation** for injection defense (§10.2, §17.3).
- **Live agent-working UI:** plan-preview gate, three-tier disclosure, Activity Feed + confidence gradient, **Inspector** (noVNC PIP, smart diffs, scratchpad), **Steering Wheel** redirect, and the **kill switch** (§13.2, §13.4, §13.6).
- **Background-task model** + reconnect/rehydrate (§16).
**Milestone:** give the agent a real build/browse task; watch it work legibly; steer it; kill it safely; own the deliverable.

### Phase 4 — Deep Research, Labs/builder, and overflow
**Goal:** the heavier modes and the quality ceiling.
- **Deep Research mode** (long-horizon loop, ToC report view, export) (§8.1, §13.5).
- **Labs/builder** multi-pane (Apps/Tasks/Assets/Sources) with **scoped editing** + version history/visual diffs (§13.5).
- **OpenRouter overflow** wired with the tunable threshold policy and cost logging (§15.3).
- **Stop-hooks + critic** for acceptance/quality, including the **visual-iteration** loop — *after* reading the critic source (§12.8, §24-D3).
**Milestone:** long reports and built artifacts at a quality that clears the bar; frontier overflow only where it earns its keep.

### Phase 5 — Hardening, observability, and the eval harness
**Goal:** production-grade operation and measured quality.
- **Observability** (tracing, metrics, the event-log replay tooling) (§18).
- **Eval harnesses** in CI: RAG faithfulness, agent trajectories, injection red-teaming (§20).
- **Operator experience:** single-command bring-up, one config surface, encrypted secrets store (§6.3, §19).
- Performance tuning against §21 targets (warm sandbox pool, cache tuning, condensation cadence).
**Milestone:** "`up` and it runs," quality is on a dashboard, and the security posture is tested, not assumed.

### Forward (post-v1, doors already built)
Multi-user (enable the auth module, §4.1), multi-node (distribute agent-servers, §5.2), MCP tool marketplace (§10.4), richer subagent orchestration (§12.8). None require re-architecture — the seams exist.

---

## 23. Risk Register

| # | Risk | Likelihood | Impact | Mitigation |
|---|---|---|---|---|
| R1 | **Local models too weak** to drive the agentic loop reliably | Med | High | Pin roles empirically early (Phase 1); 70B/35B floor; strict harness + auto-repair (§10); overflow the hardest steps (§15.3). The whole design degrades gracefully to overflow. |
| R2 | **Retrieval quality** falls short of Perplexity feel | Med | High | Treat retrieval as the #1 quality lever (§9): rerank, multi-query, span provenance; provider abstraction to buy quality if needed. |
| R3 | **Context-window-error loop** (the one stuck pattern even OpenHands stubs) | Med | Med | Couple detection to hard-reset condensation (§7.3, §12.5); provider-specific classifier; cap with `max_iterations`. |
| R4 | **Sandbox escape / secret leak** | Low | Critical | microVM isolation (§11), secrets-never-inside (§17.1), deny-by-default egress (§17.4), kill switch (§13.6). The architecture's hardest constraint. |
| R5 | **Prompt injection** hijacks the browsing agent | Med | High | Dual-LLM read/act split, least privilege, HIL on state-change, red-teaming in CI (§17.3). |
| R6 | **UI slips toward "vibe-coded"** under build pressure | Med | High | UI is a primary requirement woven through every phase (§22), the committable design system (§13.7), and the data-flow discipline (§13.8) — not a finishing pass. |
| R7 | **Operational sprawl** makes it painful to run | Med | Med | Principle 9; minimize always-on services; single-command bring-up; introduce a broker only when multi-node forces it. |
| R8 | **Solo-builder scope/burnout** | Med | Med | Phased milestones each independently usable; own-the-loop chosen deliberately so effort goes to differentiators; OpenHands lessons remove the expensive rediscovery. |
| R9 | **Cost runaway** on overflow | Low | Med | Plan-preview gate (30–50% cut), local-first routing, per-run cost logging and budget alerts (§15, §18, §21). |
| R10 | **Model/pricing drift** invalidates `[VERIFY]` specifics | High | Low | Routing and prompts are config behind abstractions; re-verify names/prices/benchmarks at build; design unaffected. |

---

## 24. Open Decisions (to resolve during detailed design)

- **[D1] Vector store engine.** pgvector (one fewer service if Postgres is already in) vs. a dedicated local vector DB (better ANN ergonomics). Resolve in Phase 2. (§9.5)
- **[D2] Codename / branding.** Cosmetic but sets the design-language tone (§13.7). Owner's call.
- **[D3] Critic module.** Read the OpenHands `critic/` source before finalizing the §12.8 self-evaluation / visual-iteration design — flagged as not-yet-deep-read. Resolve before Phase 4. (Same approach that's worked: read the source.)
- **[D4] Agent-server process model.** One agent-server process per active conversation vs. a pool that hosts many. Affects resource use; defer until Phase 3 load is observed. (§5.2)
- **[D5] Browser-automation library.** browser-use vs. Stagehand vs. raw Playwright wrapper — pick in Phase 3 against the dual-LLM injection-defense requirement. (§10.2, §17.3)
- **[D6] Confirmation defaults per surface.** Exact risk thresholds (e.g., does the Agent surface confirm on MEDIUM or only HIGH by default?) — tune empirically once real tasks run. (§8, §17.2)
- **[D7] Frontend component foundation.** Confirm the Radix/shadcn-class base and lock the token set (§13.7) at the start of Phase 2's UI work.

---

## 25. Closing: What This Document Commits Us To

The architecture rests on a small number of load-bearing commitments. If they hold, everything else is detail; if any is abandoned, revisit this document.

1. **One core, many surfaces** — Research and Agent are scopes over a single loop, not two systems.
2. **An append-only event log as the spine** — replay, resume, audit, and (via tombstones + views) bounded memory all derive from it.
3. **One action per iteration, in an explicit state machine, with first-class stuck detection** — the reliability backbone.
4. **A hostile sandbox interior** — secrets in the orchestrator, capabilities scoped, egress denied by default.
5. **Read separated from act, actions risk-scored and gated** — the security posture for an agent that browses and executes.
6. **Retrieval as the #1 quality lever** — reranking and grounding over model size.
7. **Capability-based local-first routing with sparing overflow** — cost and control without sacrificing the hard cases.
8. **Premium UX as a requirement** — legibility over spectacle, structured rendering over text walls, and the design-language and data-flow disciplines that keep it from sliding into "vibe-coded."
9. **Everything swappable behind an interface** — so "build to scope" never means "walled in."

This is the cornerstone. The detailed design and the code proceed from here; when a question arises that this document does not answer, answer it in the direction of the §3 principles and record the decision back here.

---

### Appendix A — Provenance & companion documents
- **`distilled-findings-for-build-v3.md`** — the subsystem-organized synthesis of the full research corpus (the source for §9, §13, §15, and the competitive/UX grounding).
- **`openhands-lessons.md`** — source-grounded teardown of the OpenHands *app/server/sandbox* layer (the source for §5–§7, §11 patterns marked `[OH]`).
- **`openhands-sdk-loop-lessons.md`** — source-grounded teardown of the OpenHands *SDK loop internals* (the source for §7.3, §12, §17.2 patterns marked `[OH]`).
- The raw research corpus remains the citeable reference of last resort.

### Appendix B — Legend
- **[LOCKED]** — decided; do not re-litigate without cause.
- **[OPEN]** — undecided; tracked in §24.
- **[VERIFY]** — fast-moving fact (model names, prices, versions, exact tokens) to re-check at build; design is unaffected.
- **[OH: …]** — pattern adopted from the OpenHands source by re-implementation (not dependency).
