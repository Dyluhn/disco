# Disco

A self-hosted **Research + Agent + Build** platform. Disco combines a grounded
answer engine, a deep-research report writer, and a sandboxed build agent on one
append-only event log. It is designed to run with local or open-weight models,
not only frontier hosted APIs.

The architecture authority is [`basis-of-design.md`](./current/docs/contracts/basis-of-design.md);
the event/state contract is [`event-state-contract.md`](./current/docs/contracts/event-state-contract.md).
When implementation and prose disagree, those documents define the intended
shape of the system.

## Status

Disco is single-tenant software with a built-in auth layer: cookie sessions
with CSRF protection, a single-use pairing-token mint, and owner-scoping of
every conversation (see [`current/sec-work-remaining/disco-security-state.md`](./current/sec-work-remaining/disco-security-state.md)
for the full security state). It is still local-first — keep the default
loopback binding unless you put your own TLS in front of it.

### Surfaces

Four surfaces, one shared event log + agent core:

| Surface | What it does |
|---|---|
| **Search** | A single grounded answer — claims tethered to extracted passages, with a citation-verification (NLI) pass that marks unsupported claims rather than hiding them. |
| **Deep Research** | A multi-source report: broad investigation plan → adaptive search/read/gap loop → evidence-led report outline → cited synthesis and claim verification, with depth tiers, replay, and export (Markdown / PDF / DOCX). |
| **Build** | A single-threaded coding agent in a sandbox: shell sessions, dev-server preview, browser automation, a persistent IPython kernel, file/code artifacts, plan + risk-gated execution, workspace versions, preview, and rollback. |
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

### Known limits

The Build/Agent inspector is desktop-first. Windows is via WSL2 or Docker, not
native, because the sandbox/tooling path uses POSIX process tools such as tmux.

## Layout

```
.
├── basis-of-design.md          # cornerstone design document
├── event-state-contract.md     # the spine's binding contract
├── pyproject.toml              # uv workspace root (virtual; dev toolchain)
├── current/frontend/                   # the Vite/React/TS web UI (four surfaces + settings)
└── current/packages/                   # uv workspace (BoD §5, four-layer topology)
    ├── core/                   # "the brain": events, state, store, llm router, agent loop, security
    ├── tools/                  # the action space: executor, 4 sandbox backends, builtin + MCP tools
    ├── agent-server/           # per-conversation runtime: WebSocket + REST over the event log
    └── app-server/             # user-facing gateway: config + library the frontend calls
```

Dependency direction: `core → tools → agent-server → app-server`; the frontend talks
to the servers over HTTP/WS. `disco` is a PEP 420 namespace package, so every
workspace member shares the `disco.*` namespace.

## Quickstart

This is the supported self-host path from a clean checkout. It boots the app,
agent, frontend, data volume, bundled encoders, and bundled TTS without source
edits or a required `.env` file:

**Rootless Podman** (the path this project's install testing actually covers):

```bash
git clone https://github.com/Dyluhn/disco.git
cd disco
systemctl --user enable --now podman.socket
podman compose up -d --build
podman compose logs app-server
```

**Rootless Docker** — do NOT run the Podman lines above; `podman.socket` does not
exist on a Docker host and the enable step fails:

```bash
git clone https://github.com/Dyluhn/disco.git
cd disco
DISCO_LOCAL_ENGINE=docker DISCO_SANDBOX_SOCKET=$XDG_RUNTIME_DIR/docker.sock \
  docker compose up -d --build
docker compose logs app-server
```

Then open **http://localhost:8088** in a browser.

Two things Docker prints that look like failures and are not. Compose attempts a
registry pull before it builds, so `Error: pull access denied for disco-server`
scrolls past on every `up --build` — the build then runs normally. And if the
build dies within about 20 seconds on a `dns error` reaching a package index,
that is rootless Docker's network namespace failing to reach systemd-resolved's
loopback stub, not a problem with disco; give the daemon explicit resolvers:

```bash
mkdir -p ~/.config/docker
echo '{"dns":["1.1.1.1","8.8.8.8"]}' > ~/.config/docker/daemon.json
systemctl --user restart docker
```

The app-server logs print the working UI URL and a one-time admin pairing token.
Configure a driver model after boot in **Settings -> Models & Providers**, then
prove the configuration:

```bash
podman compose exec agent-server disco-verify --quick
```

Copy `.env.example` to `.env` only when you need to override ports, bind
addresses, provider keys, or the sandbox socket. The default is local rootless
Podman. Docker also works and is an explicit override, not automatic; prefer
rootless Docker's socket (`$XDG_RUNTIME_DIR/docker.sock`) over the root-equivalent
`/var/run/docker.sock` — see [`current/docs/self-host.md`](./current/docs/self-host.md#sandbox-image)
for both invocations.

See [`current/docs/self-host.md`](./current/docs/self-host.md) for Podman notes, offline asset
smoke commands, and the bundled-weight license inventory.

## Local Development

This path starts the stack from source. It uses the `process` sandbox so a first
build works without a container socket; do not use that sandbox for exposed
deployments.

```bash
uv sync --all-packages
mkdir -p .data
export DISCO_DB="$PWD/.data/disco.db"
export DISCO_CONFIG="$PWD/.data/disco-config.json"
export DISCO_SECRETS="$PWD/.data/secrets.json"
export DISCO_PROJECTS_ROOT="$PWD/.data/projects"
export DISCO_SANDBOX=process
export DISCO_ALLOW_PROCESS_SANDBOX_FOR_DEV=1
uv run python development/scripts/seed_config.py
```

Run the agent server, app server, and frontend in separate terminals:

```bash
uv run python -m disco.agent_server
uv run python -m disco.app_server
cd frontend && npm ci && VITE_API_BASE=http://localhost:8800 VITE_AGENT_BASE=http://localhost:8000 npm run dev
```

Open `http://localhost:5173`, configure a model in Settings, then run
`uv run disco-verify --quick`.

## Providers

Disco has keyless defaults for retrieval and local artifact services. The driver
LLM is intentionally not bundled; point it at a model endpoint you run or a paid
OpenAI-compatible API.

| Capability | Keyless default | Self-host option | Paid/BYO-key option |
|---|---|---|---|
| Driver LLM | OpenAI-compatible local endpoint, if you run one | Ollama, llama.cpp, vLLM, LM Studio | Any OpenAI-compatible endpoint configured in Settings |
| Search | DuckDuckGo via `ddgs` | SearXNG | Tavily or Brave |
| Extraction | Local HTTP fetch/readability | Crawl4AI | Firecrawl |
| Embeddings/rerank/NLI | Bundled ONNX CPU encoders | Remote encoder/NLI endpoints | OpenAI-compatible embedding endpoints where configured |
| TTS | Bundled Kokoro ONNX | Speaches/OpenAI-compatible TTS | OpenAI-compatible TTS |
| Image generation | None bundled | ComfyUI | OpenAI-compatible image API or OpenRouter |
| App deploy | Local export and dry-run plan | Cloudflare account connected by owner | Cloudflare API token, owner-gated |

Provider keys are read from encrypted Settings secrets first, then matching
environment variables such as `DISCO_OPENROUTER_API_KEY`, `TAVILY_API_KEY`,
`BRAVE_API_KEY`, `FIRECRAWL_API_KEY`, or `OPENAI_API_KEY`.

## Hardware

- **Basic research/dev:** 8 GB RAM is workable with `DISCO_ENCODER_TIER=lite`.
  The lite encoder tier downloads about 0.15 GB of ONNX models.
- **Full local retrieval quality:** use `DISCO_ENCODER_TIER=full` on a box with
  at least 16 GB RAM; the full encoder tier is roughly 4 GB of model weights.
- **Useful local agent driver:** use a 24-32B-class instruction model with a
  32k+ context window, served by Ollama, llama.cpp, vLLM, LM Studio, or a LAN
  endpoint. Smaller CPU-only models are suitable only for smoke tests.
- **Build isolation:** Docker or rootless Podman is recommended for real Build
  runs. The `process` sandbox is a convenience for local development only.
- **Windows:** use WSL2 or Docker.

## Develop

Requires [uv](https://docs.astral.sh/uv/), Python 3.12+, and Node 22 LTS for the
frontend. See [`CONTRIBUTING.md`](./CONTRIBUTING.md) for the full contributor guide.

```bash
uv sync --all-packages
uv run pytest -m "not integration"
uv run ruff check packages harness
uv run basedpyright

cd frontend
npm ci
npm run typecheck:build
npm run build
npm run test
```

Deep Research development runs can be injected, replayed, and inspected with
the [outside-observer harness](development/harness/DEEP_RESEARCH.md).

Audio overviews (RP-09) use the **bundled** in-process Kokoro TTS — `kokoro-onnx` +
`lameenc`. This is a default product tier, so it ships in the agent-server's core
dependencies: a bare `uv sync` (or `uv sync --all-packages`) installs it out of the
box, no `--extra` flag needed. Weights (~0.3 GB) download to `~/.cache/disco-tts` on
first use. Bring-your-own / self-host / paid TTS tiers stay opt-in.

Other optional extras live on the member packages (not the workspace root), so install
them with `--package` (e.g. the tools `browser` extra for Playwright).

Deck→PDF export shells out to headless **LibreOffice** (`soffice`). On the `process`
(host) backend the dev server runs by default, install the Impress-only package on
your host so it works out of the box — the container/sandbox image already ships it:
`sudo apt-get install -y libreoffice-impress` (Debian/Ubuntu), `sudo dnf install -y
libreoffice-impress` (Fedora), or `brew install --cask libreoffice` (macOS). PPTX +
HTML deck export need nothing extra; only PDF conversion needs it, and it fails soft
with an honest message when absent.

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
