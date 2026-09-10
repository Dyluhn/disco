# What is live, and the known limits

Four surfaces share one event log and one agent core; the README lists them.
This page is the longer inventory.

## What's live

- **Event & state spine** — append-only SQLite event store; `State`/`View` as pure
  projections of the log; replay, resume-after-disconnect, and resume *across a
  recreated sandbox* (uploads re-materialized, workspace rehydrated).
- **LLM router** — provider-neutral, capability-based model selection with live
  adapters (local llama.cpp / OpenRouter / OpenAI-compatible); the loop and the
  condenser run on the per-conversation picked model.
- **Agent loop** — the research-grounded architecture for weak/local models:
  observation masking with restorable references, a persistent IPython kernel
  (no pickle-runner quadratic), KV-cache-stable prefixes with varied tails,
  action-space masking via assistant prefill, and verification-gated completion.
- **Tools & sandbox** — four backends (`process` dev, rootless-`podman` local,
  `gvisor` VM, remote) with a deny-by-default egress allowlist proxy; secrets stay
  orchestrator-side (tools get mediated capabilities, never credentials); shell
  sessions, browser automation, code execution, and artifact tools (spreadsheets,
  Marp slides, PDF/DOCX export, two-voice audio overviews) — all jailed.
- **Retrieval & grounding** — pluggable search (ddgs / SearXNG / Tavily / Brave) and
  extraction (local / Crawl4AI / Firecrawl) providers; bundled in-process ONNX
  encoders for embeddings / reranking / NLI (no torch, AMD-friendly), or remote
  endpoints; a citation pipeline that drops or flags unsupported claims.
- **MCP client** — connect external MCP servers (stdio + streamable-HTTP) to extend
  the Agent's action space, with hash-pinned approvals, output fencing,
  egress-routed HTTP, a `tool_search` meta-tool for large catalogs, and a
  retrieval tier that turns MCP search/fetch servers into cited providers.
- **Replay & share** — scrubbed, versioned, revocable share bundles with a static
  viewer; scheduled (cron) re-runs; rich report blocks (charts, tables, citations).
- **Web UI** — the full Vite/React/TS frontend: the four surfaces, settings
  (model matrix, encoders, data providers, sandbox, audio, MCP), history, projects.
- **AppKit builds** — generated React/Vite/TypeScript apps with Worker/D1 exports,
  Drizzle schema checks, a strict AppKit verifier, versioned previews, and
  owner-gated Cloudflare deploy routes.
- **App primitives** — a primitive framework for generated apps: each primitive
  is a registered definition (tier / host contract / spec schema / verify /
  apply-spec) that the agent adds via the `app_add_primitive` tool — validate
  spec → fold into the AppSpec → regenerate → provenance record — with a
  fail-closed finish gate so unverified security-critical scaffolds cannot
  ship. Shipped primitives: `form` (typed fields, server-side validation, D1
  submissions table, owner inbox), `seo` (meta/OG/JSON-LD, sitemap.xml,
  robots.txt), and `collection` (structured content collections).
- **Design directions** — a 22-direction design library, with a numeric
  design-constraint lint, behind the always-on art direction of generated sites.

## Known limits

The Build/Agent inspector is desktop-first. Windows is via WSL2 or Docker, not
native, because the sandbox/tooling path uses POSIX process tools such as tmux.
Disco is single-tenant software with a built-in auth layer (cookie sessions with
CSRF protection, a single-use pairing-token mint, owner-scoping of every
conversation; see
[`development/notes/security/disco-security-state.md`](../development/notes/security/disco-security-state.md)).
It is local-first: keep the default loopback binding unless you put your own TLS
in front of it.

## The load-bearing idea

The append-only event log is the single source of truth. `State` (what the loop
knows) and `View` (what the LLM sees) are **pure functions** of the ordered log;
forgetting is done with **condensation tombstones**, never deletion. That one
decision buys replay, resume-after-disconnect, audit, and bounded memory — see
[`event-state-contract.md`](./contracts/event-state-contract.md) §1 for the
invariants this rests on.
