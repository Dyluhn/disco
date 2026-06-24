# Disco

A self-hosted **Research + Agent** platform — a Perplexity-class grounded answer
engine and a Manus-class autonomous agent — built as **one agent core over an
append-only event log**, and engineered to be **reliable on local / open-weight
models**, not just frontier APIs.

> Design authority: [`basis-of-design.md`](./basis-of-design.md) (the cornerstone)
> and [`event-state-contract.md`](./event-state-contract.md) (the spine's binding
> contract). When code and prose disagree, those documents win. The road from here
> to a public release is in [`docs/release-execution-plan.md`](./docs/release-execution-plan.md).

## Status

**The product works end-to-end, and the release engineering has landed.** A real
question gets a real, grounded, cited answer; the agent plans, runs tools in a sandbox,
and produces artifacts — all driveable from local open-weight models. It now also
*packages* for strangers: a one-command `docker compose` self-host
([`docs/self-host.md`](./docs/self-host.md)), a published threat model
([`SECURITY.md`](./SECURITY.md)), a contributor guide
([`CONTRIBUTING.md`](./CONTRIBUTING.md)) + provider/VRAM matrix
([`docs/provider-matrix.md`](./docs/provider-matrix.md)), and **`disco verify`** — a
command that checks *your* model actually drives the loop. What's left before a public
v0.1 is small: a LICENSE + CI, and a mobile polish pass.

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

What's left: a **LICENSE + CI**, and a **mobile polish pass** (the research + history
surfaces are mostly responsive; the Build/Agent inspector is desktop-first). **No auth
yet** — every conversation is owner `"local"`, so don't expose it without your own TLS +
auth in front. Windows is via WSL2 / Docker, not native (POSIX deps: tmux sessions,
gVisor/podman). The sequenced plan is in
[`docs/release-execution-plan.md`](./docs/release-execution-plan.md).

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
to the servers over HTTP/WS. `disco` is a PEP 420 namespace package, so every
workspace member shares the `disco.*` namespace.

## Quickstart (self-host)

A clean Linux box (or WSL2) with a container runtime. Disco's servers + the ONNX
search encoders are light (the encoders are ~0.15 GB on the `lite` tier, ~4 GB on
`full`). You **wire in the driver model** yourself — there's no bundled one (a
keyless CPU 4B is a demo, not a real driver). Point `DISCO_DRIVER_BASE_URL` at a
self-hosted endpoint (Ollama / llama.cpp / vLLM / LM Studio) or a paid
OpenAI-compatible API.

```bash
cp .env.example .env            # then skim it — set DISCO_DRIVER_BASE_URL + security notes
docker compose up -d --build    # podman compose works too
open http://localhost:8088
# confirm your model can actually drive the loop:
docker compose exec agent-server python -m disco.agent_server.verify
```

Keyless for everything except the driver (DuckDuckGo search + local extraction +
bundled ONNX encoders + in-process TTS) — a grounded answer needs no API key beyond
your model endpoint. For real agent work, point it at a 24–32B-class endpoint (see
[`docs/provider-matrix.md`](./docs/provider-matrix.md)). **No auth in v1** — defaults
bind `127.0.0.1`; read [`SECURITY.md`](./SECURITY.md) before exposing it. The full story
is in [`docs/self-host.md`](./docs/self-host.md).

## Develop

Requires [uv](https://docs.astral.sh/uv/) and Python 3.12+ (and Node for the frontend).
See [`CONTRIBUTING.md`](./CONTRIBUTING.md) for the full contributor guide.

```bash
uv sync --all-packages     # provision the venv + install every workspace member
uv run pytest              # the headless test suite (no LLM, no network)
uv run ruff check .        # lint
uv run ruff format .       # format

cd frontend && npm install && npm run dev   # the web UI (fixtures unless VITE_API_BASE is set)
```

Audio overviews (RP-09) use the **bundled** in-process Kokoro TTS — `kokoro-onnx` +
`lameenc`. This is a default product tier, so it ships in the agent-server's core
dependencies: a bare `uv sync` (or `uv sync --all-packages`) installs it out of the
box, no `--extra` flag needed. Weights (~0.3 GB) download to `~/.cache/disco-tts` on
first use. Bring-your-own / self-host / paid TTS tiers stay opt-in.

Other optional extras live on the member packages (not the workspace root), so install
them with `--package` (e.g. the tools `browser` extra for Playwright).

## License

disco is licensed under the **Apache License 2.0** — see [`LICENSE`](./LICENSE).
Permissive: use it, modify it, run it commercially, fold it into your own product; just
keep the notices.

**Our promise: this license will never change.** disco will not be relicensed
to a source-available, "fair-source", BSL/SSPL, or commercial license — not as it grows,
not after adoption, not on acquisition. Every past and future release stays Apache-2.0.
The whole point of this project is to be the *trustworthy* one you can self-host; a
license rug-pull would break that promise, so we don't reserve the right to make one.

## The load-bearing idea

The append-only event log is the single source of truth. `State` (what the loop
knows) and `View` (what the LLM sees) are **pure functions** of the ordered log;
forgetting is done with **condensation tombstones**, never deletion. That one
decision buys replay, resume-after-disconnect, audit, and bounded memory — see
event-state-contract §1 for the invariants this rests on.
