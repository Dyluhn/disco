# Self-hosting perpleximanus

A one-command `docker compose` self-host. A stranger on a clean Linux box (or WSL2)
copies one file and runs one command, and gets a working instance: the UI loads, a
grounded answer works keyless, and the Build/Agent surface can run a task.

> **No authentication in v1.** The defaults bind every port to `127.0.0.1`. Do not
> expose this to a network without putting your own TLS + auth (a reverse proxy) in
> front. Every conversation is owner `"local"`.

## Quickstart

```bash
git clone <repo> && cd "perpleximanus build"
cp .env.example .env          # then open it — at minimum skim the model + security notes
docker compose up -d --build  # podman compose works too (see below)
# wait for the model to download on first run, then:
open http://localhost:8088
```

`docker compose ps` shows five things: `sandbox-image` (a build-only step that exits
0), `app-server`, `agent-server`, `frontend`, and — with the bundled model profile on
— `llm`. The first `up` builds the images and (with `bundled-llm`) downloads a small
model, so it takes a while; subsequent starts are fast.

## What runs

| Service | Port (default) | What it is |
|---|---|---|
| `frontend` | `8088` | the web UI (nginx-served SPA; reads its backend URLs at runtime) |
| `app-server` | `8800` | settings + library gateway |
| `agent-server` | `8000` | the per-conversation runtime (WebSocket + REST) |
| `llm` | (internal) | bundled llama.cpp model — the keyless default; in-network only |
| `sandbox-image` | — | build-only: deposits `pmx-sandbox:base` into your container daemon |

All state lives in one named volume (`pmx-data`): the SQLite DB, the model config,
the encrypted secrets, your skills, your Build project workspaces, and the
encoder/TTS caches. Downloaded models live in `pmx-models`.

## Models

The strategic point of perpleximanus is **reliability on local / open-weight models**.
Three configurations, switched in `.env`:

1. **Bundled (default).** `COMPOSE_PROFILES=bundled-llm` runs a llama.cpp server with
   a small model (Qwen3-4B Q4, ~2.6 GB). It proves the whole loop keyless on a CPU-only
   host. **Honest caveat:** a 4B is a *working demo* — the reliability work was proven
   on 24–32B-class models. For real use, bring your own bigger endpoint.
2. **Bring your own endpoint.** Delete `bundled-llm` from `COMPOSE_PROFILES`, set
   `PMX_DRIVER_BASE_URL=http://host.docker.internal:11434/v1` (or your llama.cpp
   `/v1`). Any OpenAI-compatible endpoint that does tool-calling works. GPU serving
   (Vulkan/ROCm/CUDA) stays *native on your host* — pointing the container at it is far
   simpler than passing a GPU into compose.
3. **OpenRouter (keyed fallback).** Leave the bundled model off, then paste an
   OpenRouter key in **Settings → Models → OpenRouter** (it's encrypted at rest, never
   in `.env`) and assign the roles.

The first-run seed (`scripts/seed_config.py`) writes the model catalogue from
`PMX_DRIVER_*` **only if no config exists yet** — after that the Settings UI owns it.

Search (DuckDuckGo via `ddgs`), extraction (bundled), and the embedding/rerank/NLI
encoders (bundled ONNX/CPU) are all **keyless by default** — a grounded answer needs
no API key, only outbound internet for the search + the first encoder download.

## Verify your setup

The whole point of perpleximanus is reliability on *your* model — so check it before
you trust it. `pmx verify` runs a small battery against your configured driver and
prints a pass/fail table: config resolves a driver, the endpoint completes, the model
emits a structured **tool call** (the capability the whole agent loop rests on), and the
full research pipeline returns a cited answer.

```bash
# in a compose deployment
docker compose exec agent-server python -m perpleximanus.agent_server.verify
# from a checkout
make verify              # or: uv run python -m perpleximanus.agent_server.verify
make verify ARGS=--quick # skip the live grounding step (config + LLM only)
```

It exits non-zero if any check fails, with the provider's **verbatim** error (so a wrong
`base_url`, an unserved model, or a model that can't tool-call is obvious). A check it
can't run here — e.g. grounding with no internet — is reported `SKIP`, not a failure.
This is a capability check; the faithfulness *score* is the eval harness's job
(`make eval`).

## Security

### The sandbox-socket tradeoff (read this)

The agent's value is running tools, code, and a browser in a **sandbox**. In compose,
the agent-server gets one by mounting your host's container socket
(`/var/run/docker.sock`) and spawning *sibling* sandbox containers through it.

> Mounting the socket makes the **agent-server process root-equivalent on your host**:
> anything that fully compromises the agent-server could start a privileged container
> and own the machine. The **agent's own code never touches the socket** — it runs in
> sibling sandboxes with a clean environment and real CPU/memory limits. The socket is
> a blast-radius concern for an *agent-server RCE*, not a lane the agent drives in.

> **Network, honestly:** the sandbox *primitive* is network-sealed, but the Build/Agent
> surface **grants the sandbox full outbound internet by default** (an agent usually
> needs to `pip install`, `npm i`, call APIs). Set `PMX_BUILD_EGRESS=filtered` for a
> deny-by-default allowlist (package registries + your configured MCP hosts only),
> enforced by an egress-proxy sidecar. See **Network egress** below.

Mitigations, in order of value:

1. **Use a rootless Podman socket.** An escape then lands as your unprivileged user,
   not root:
   ```bash
   systemctl --user enable --now podman.socket
   podman build -t pmx-sandbox:base deploy/sandbox     # build into your rootless daemon
   # in .env:
   PMX_SANDBOX_SOCKET=/run/user/$(id -u)/podman/podman.sock
   ```
2. **Keep ports on `127.0.0.1`** (the default — there is no auth in v1).
3. **Upgrade to the `gvisor` backend on a separate VM** for adversarial workloads —
   strong (user-space-kernel) isolation. Configure it in Settings → Sandbox.

A read-only socket mount is theater (the Docker API is read-write regardless), and a
socket-proxy filters accidents, not attackers — we don't pretend otherwise.

### No-runtime fallback

On a host with no container runtime at all, set `PMX_SANDBOX=process`. This runs the
agent's tools **in the agent-server's own container with no isolation** — fine for
trying it out, unsafe for untrusted/agentic workloads. It is labeled that way in the UI.

### Network egress

By default the Build/Agent sandbox has **full outbound internet** — agents routinely
need to install packages and reach APIs, so an open default is the usable one. The cost
is that prompt-injected or buggy agent code can also exfiltrate or call out freely.

For untrusted/agentic workloads, set `PMX_BUILD_EGRESS=filtered` (uncomment it in
`.env`). The sandbox then goes **deny-by-default**: an egress-proxy sidecar permits only
package registries plus the hosts of any MCP servers you've configured, and denies
everything else with a 403 the agent cannot bypass. The full threat model — isolation
tiers, prompt-injection handling, action guards — is in [`SECURITY.md`](../SECURITY.md).

## Podman

Everything works with `podman compose` (Podman 4+). Use the rootless socket as above.
Build the sandbox image into your rootless daemon yourself (`podman build -t
pmx-sandbox:base deploy/sandbox`) — the compose `sandbox-image` build step does this
automatically for the default Docker path, but rootless daemons are per-user.

## WSL2 / Windows

Run it under WSL2 with Docker Desktop (the Docker socket and `host.docker.internal`
are present). There is no native-Windows path — the sandbox stack is POSIX (tmux
sessions, container runtimes).

## Previews on a LAN

When you open the UI as `http://<lan-ip>:8088`, the Build live-preview's
`<cid>-<port>.<host>` subdomains won't resolve off-box (they rely on `*.localhost`).
The path-based preview (`/conversations/<cid>/preview-app/`) still works; full
subdomain previews are a localhost convenience.

## Footprint

Rough steady-state: the server image is ~1.3–1.8 GB, the sandbox image ~2.5–3 GB,
nginx ~60 MB. RAM: ~1 GB for the servers + ~1–2 GB once the encoders load on the first
grounded answer + ~5–6 GB for the bundled 4B model. An 8 GB machine works with BYO
model; 12–16 GB is comfortable with `bundled-llm`. Disk ~7–8 GB after first run.

## Persisting / backing up

Everything durable is in the `pmx-data` volume. Back it up with
`docker run --rm -v perpleximanus_pmx-data:/data -v "$PWD":/out alpine tar czf
/out/pmx-backup.tgz /data`. Pin `PMX_SECRET_KEY` in `.env` so your encrypted API keys
survive a volume rebuild.
