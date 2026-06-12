# perpleximanus

A self-hosted **Research + Agent** platform — a Perplexity-class grounded answer
engine and a Manus-class autonomous agent — built as **one agent core over an
append-only event log**, and engineered to be **reliable on local / open-weight
models**, not just frontier APIs.

> Design authority: [`basis-of-design.md`](./basis-of-design.md) (the cornerstone)
> and [`event-state-contract.md`](./event-state-contract.md) (the spine's binding
> contract). When code and prose disagree, those documents win. The road from here
> to a public release is in [`docs/release-execution-plan.md`](./docs/release-execution-plan.md).

## Status

**The product works end-to-end; the release engineering doesn't exist yet.** A real
question gets a real, grounded, cited answer; the agent plans, runs tools in a
sandbox, and produces artifacts — all driveable from local open-weight models. What's
*not* done is packaging it for strangers: a one-command deploy, a published security
doc, LICENSE/CI, and an eval users can run against their own model (see the execution
plan). This is a working system without a release, not the reverse.

### Surfaces

Four surfaces, one shared event log + agent core:

| Surface | What it does |
|---|---|
| **Search** | A single grounded answer — claims tethered to extracted passages, with a citation-verification (NLI) pass that marks unsupported claims rather than hiding them. |
| **Deep Research** | A multi-source report: decompose → parallel retrieval → adversarial claim verification → cited synthesis, with depth tiers, replay, and export (Markdown / PDF / DOCX). |
| **Build** | A single-threaded coding agent in a sandbox — shell sessions, dev-server preview, a browser daemon, a persistent IPython kernel, file/code artifacts, plan + risk-gated execution, workspace snapshot/resume. |
| **Agent** | The *same* agent machinery as Build, framed as a general task agent — point it at any multi-step task, extend its reach with MCP servers. |

### What's live

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
  retrieval-tier that turns MCP search/fetch servers into cited providers.
- **Replay & share** — scrubbed, versioned, revocable share bundles with a static
  viewer; scheduled (cron) re-runs; rich report blocks (charts, tables, citations).
- **Web UI** — the full Vite/React/TS frontend: the four surfaces, settings
  (model matrix, encoders, data providers, sandbox, audio, MCP), history, projects.

### Honest gaps (the road to v0.1)

Release engineering (one-command compose deploy, published sandbox image, a
`SECURITY.md` threat model, LICENSE / CI / CONTRIBUTING); an eval users can run
against their own local model; some artifact polish (an in-app sheet viewer,
share-bundle *import*). No auth yet — every conversation is owner `"local"`. Windows
is via WSL2 / Docker, not native (POSIX deps: tmux sessions, gVisor/podman). The
sequenced plan is in [`docs/release-execution-plan.md`](./docs/release-execution-plan.md).

## Layout

```
.
├── basis-of-design.md          # cornerstone design document
├── event-state-contract.md     # the spine's binding contract
├── pyproject.toml              # uv workspace root (virtual; dev toolchain)
├── frontend/                   # the Vite/React/TS web UI (four surfaces + settings)
└── packages/                   # uv workspace (BoD §5, four-layer topology)
    ├── core/                   # "the brain": events, state, store, llm router, agent loop, security
    ├── tools/                  # the action space: executor, 4 sandbox backends, builtin + MCP tools
    ├── agent-server/           # per-conversation runtime: WebSocket + REST over the event log
    └── app-server/             # user-facing gateway: config + library the frontend calls
```

Dependency direction: `core → tools → agent-server → app-server`; the frontend talks
to the servers over HTTP/WS. `perpleximanus` is a PEP 420 namespace package, so every
workspace member shares the `perpleximanus.*` namespace.

## Develop

Requires [uv](https://docs.astral.sh/uv/) and Python 3.12+ (and Node for the frontend).

```bash
uv sync --all-packages     # provision the venv + install every workspace member
uv run pytest              # the headless test suite (no LLM, no network)
uv run ruff check .        # lint
uv run ruff format .       # format

cd frontend && npm install && npm run dev   # the web UI (fixtures unless VITE_API_BASE is set)
```

Optional extras live on the member packages (not the workspace root), so install
them with `--package`:

```bash
# Audio overviews (RP-09): bundled in-process Kokoro TTS — kokoro-onnx + lameenc.
# Weights (~0.3 GB) download to ~/.cache/perpleximanus-tts on first use.
uv sync --package perpleximanus-agent-server --extra tts
```

A bare `uv sync --extra tts` errors — the root defines no `tts` extra. Without this
extra installed, the audio-overview tool stays importable but fails soft when run.

## The load-bearing idea

The append-only event log is the single source of truth. `State` (what the loop
knows) and `View` (what the LLM sees) are **pure functions** of the ordered log;
forgetting is done with **condensation tombstones**, never deletion. That one
decision buys replay, resume-after-disconnect, audit, and bounded memory — see
event-state-contract §1 for the invariants this rests on.
