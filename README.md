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

### Prerequisites

`git`, rootless **Podman**, and a compose provider. Nothing else — no Python, no
Node, no `uv` on the host; every build happens inside the containers.

```bash
sudo apt-get update && sudo apt-get install -y git podman docker-compose   # Debian 13
sudo apt-get update && sudo apt-get install -y git podman podman-compose   # Ubuntu 24.04
```

Those two are the combinations this project installs and tests on. On any other
distribution install `git`, `podman`, and a compose provider — prefer **Compose
v2** (usually packaged as `docker-compose`), and fall back to `podman-compose`
where Compose v2 is not packaged or is still the retired Python v1, which is the
case on Ubuntu 24.04.

`podman compose` is a thin wrapper that hands the file to whichever provider it
finds, and the providers are not equivalent. Installing `docker-compose` does
not install a Docker daemon and does not change which engine runs the
containers.

If your provider is `podman-compose` 1.3.x, `podman compose up` fails with

```
Error: invalid port format - format is [[hostIP:]hostPort:]containerPort
```

and, more quietly, hands the containers environment values that still read
`${DISCO_ENCODER_TIER:-full}`. That version does not apply a `${VAR:-default}`
when `VAR` is unset and is also one of the service's own environment keys.
Install `docker-compose` and run `podman compose up -d --build` again.

The repository is private, so `git clone` needs your own GitHub access. Either
put an SSH key on your account and clone `git@github.com:Dyluhn/disco.git`, or
create a personal access token with `repo` scope and give it as the password the
`https://` clone asks for; `gh auth login` sets up either. Without access the
clone stops at `Repository not found` — GitHub does not distinguish "private"
from "missing" for a client it does not recognise.

**Rootless Podman** (the path this project's install testing actually covers):

```bash
git clone https://github.com/Dyluhn/disco.git
cd disco
systemctl --user daemon-reload
systemctl --user start dbus.socket
loginctl enable-linger "$USER"
systemctl --user enable --now podman.socket
export DISCO_SANDBOX_SOCKET=$XDG_RUNTIME_DIR/podman/podman.sock
podman compose up -d --build
podman compose logs app-server
```

The two `systemctl --user` lines before `enable-linger` set up the per-user
services the Podman install just added. Logging out and back in does the same
thing. Skipping them is the usual cause of

```
error running container: from /usr/bin/crun creating container for [...]:
  sd-bus call: Interactive authentication required.: Permission denied
```

part-way through the image build — rootless `crun` needs your user's D-Bus, and
a shell that was already open when Podman was installed does not have it. (On
Debian 13 a *fresh* login does not have it either until the socket is started
once.) `enable-linger` then keeps the stack running after you disconnect.

The `export` points the Build sandbox at *your* rootless Podman socket. The
compose file is written for the compose providers distributions actually ship,
which substitute variables in a single pass — so its own fallback can only spell
the literal uid-1000 path, not `$XDG_RUNTIME_DIR`. Exporting it is correct at any
uid. Keep it exported for every later `podman compose` command in the same shell
(`logs`, `exec`, `down`).

**Rootless Docker** — do NOT run the Podman lines above; `podman.socket` does not
exist on a Docker host and the enable step fails. Rootless Docker is not a
distribution package on Ubuntu; it comes from Docker's own installer, and the
`apt` line below is only the CLI, the compose plugin and the user-namespace
tools it needs:

```bash
sudo apt-get update && sudo apt-get install -y \
  git curl uidmap dbus-user-session docker-compose-v2
sudo systemctl disable --now docker.service docker.socket   # the rootful daemon apt pulled in
systemctl --user daemon-reload && systemctl --user start dbus.socket
loginctl enable-linger "$USER"
curl -fsSL https://get.docker.com/rootless | sh
```

On Ubuntu 23.10 and later that last command stops with

```
[rootlesskit:parent] error: failed to start the child: fork/exec /proc/self/exe: permission denied
[ERROR] RootlessKit failed, see the error messages and https://rootlesscontaine.rs/getting-started/common/
```

because Ubuntu restricts unprivileged user namespaces. The binaries are already
in place at that point; grant `rootlesskit` the exception and finish:

```bash
sudo tee /etc/apparmor.d/home."$USER".bin.rootlesskit >/dev/null <<EOF
abi <abi/4.0>,
include <tunables/global>
$HOME/bin/rootlesskit flags=(unconfined) {
  userns,
}
EOF
sudo systemctl restart apparmor.service
PATH="$HOME/bin:$PATH" dockerd-rootless-setuptool.sh install
```

Then point this shell at the rootless daemon and bring the stack up:

```bash
export PATH="$HOME/bin:$PATH"
export DOCKER_HOST="unix://$XDG_RUNTIME_DIR/docker.sock"
git clone https://github.com/Dyluhn/disco.git
cd disco
DISCO_LOCAL_ENGINE=docker DISCO_SANDBOX_SOCKET=$XDG_RUNTIME_DIR/docker.sock \
  docker compose up -d --build
docker compose logs app-server
```

Keep both exports set for every later `docker compose` command in this shell, or
add them to `~/.bashrc` as the installer suggests.

Confirm the services before going further — a compose provider can report
success while one image failed to build. Check with `podman ps`, not `compose
ps`: the two compose providers print different things, and only one of them
prints health at all.

```bash
podman ps --format '{{.Names}} {{.Status}}'     # or: docker ps --format ...
```

Expect three running containers — `app-server`, `agent-server` and `frontend` —
each **Up ... (healthy)**. The names are `disco_app-server_1` under
podman-compose and `disco-app-server-1` under Compose v2.

A fourth container, `sandbox-image`, is expected to be **Exited (0)**; it exists
only to deposit the `disco-sandbox:base` image into your daemon and then stops.
Add `-a` to `podman ps` to see it.

What `podman compose ps` shows instead, and why it is not the check: under
podman-compose it lists all four containers with health, but under the
Compose v2 (`docker-compose`) provider — the one Debian 13 uses — it lists three
rows, hides the exited `sandbox-image`, and never prints `(healthy)` even when
every healthcheck is passing.

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

The app-server logs print the working UI URL and the admin pairing token. A
browser on the same machine pairs itself and never asks for it. If it does ask —
you are opening the UI from another machine, or you cleared the cookie — print
the token on demand:

```bash
podman compose exec app-server disco-pairing-token
```

It is derived from this install's secret rather than minted per boot, so that
command prints the same token every time. To find it in the boot log instead,
grep for the banner — `logs | tail` will not do, healthcheck lines push the
banner out of the last twenty lines within minutes:

```bash
podman compose logs app-server | grep -A 8 "Disco self-host boot"
```

Configure a driver model after boot in **Settings -> Models & Providers**, then
prove the configuration:

```bash
podman compose exec agent-server disco-verify --quick
```

### Configuring the driver model without a browser

A headless server has no Settings page. The app-server API on port 8800 does the
same three things the Settings screen does. Adding a *provider* is the whole
job: it stores the key encrypted and approves the endpoint's origin for egress
in one call. (Storing a bare secret and hand-writing a model entry does not
work — a model whose `api_key_env` names a plain environment variable has that
reference quarantined, and the request then goes out unauthenticated.)

```bash
# Pair once. On a loopback-bound install (the default) no token is needed.
CSRF=$(curl -s -c /tmp/disco.jar -H 'Origin: http://127.0.0.1:8800' \
        -H 'Content-Type: application/json' -d '{}' \
        http://127.0.0.1:8800/api/auth/mint | python3 -c 'import json,sys;print(json.load(sys.stdin)["csrf_token"])')
AUTH=(-b /tmp/disco.jar -H "Origin: http://127.0.0.1:8800" -H "X-Disco-CSRF: $CSRF" -H 'Content-Type: application/json')

# 1. the provider — label, OpenAI-compatible base URL, and the key.
curl -s "${AUTH[@]}" -X POST http://127.0.0.1:8800/api/providers -d "{
  \"label\": \"My Provider\", \"base_url\": \"https://example.com/v1\",
  \"kind\": \"openai-compat\", \"api_key\": \"$YOUR_KEY\", \"requires_api_key\": true}"
# -> {"provider":{"id":"my-provider",...},"catalogue_ok":true}

# 2. see what it serves, and enable the one you want to drive the loop.
curl -s "${AUTH[@]}" http://127.0.0.1:8800/api/providers/my-provider/models
curl -s "${AUTH[@]}" -X POST http://127.0.0.1:8800/api/providers/my-provider/enable \
  -d '{"model_id": "<served-model-id>", "context_window": 131072}'
# -> the catalogue, including "prov-my-provider-<served-model-id>"

# 3. make it the default driver.
curl -s "${AUTH[@]}" -X PUT http://127.0.0.1:8800/api/models/assignments \
  -d '{"default_model": "prov-my-provider-<served-model-id>"}'
```

`GET /api/providers/presets` lists ready-made base URLs (OpenRouter, OpenAI,
Anthropic, Groq, DeepSeek, Together, Fireworks, Mistral, xAI). Pass the key in
the request body only — never on a command line that lands in shell history.

### Updating

```bash
cd disco
git pull
podman compose down             # or: docker compose down
podman compose up -d --build
```

The `down` is not optional. On `podman-compose` 1.0.6 (the version Ubuntu 24.04
ships) `podman compose up -d --build` builds the new images and then cannot
replace the containers that are already running:

```
Error: creating container storage: the container name "disco_frontend_1" is
already in use by ... You have to remove that container to be able to reuse
that name: that name is already in use
exit code: 125
```

It then restarts the **old** containers. The command looks like it worked, the
new images exist, and the stack keeps serving the old code. Compose v2 replaces
containers in place, so `down` costs it only the restart it was going to do.

`down` removes containers, not data: the `disco-data` volume — database,
settings, encrypted secrets, projects, skills — survives. Never use `down -v`
(or `down --volumes`) to update; that deletes it. To take a backup first, use
the lifecycle command, which now does the whole update itself:

```bash
.venv/bin/python development/scripts/self_host_data.py upgrade \
  --backup "$HOME/disco-pre-upgrade-$(date +%Y%m%d).tar.gz"
```

After either route, re-run the confirmation (`podman ps`) and the proof step
(`disco-verify --quick`).

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
