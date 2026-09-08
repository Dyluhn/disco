# Self-hosting Disco

This is the supported one-command path for a local Linux or WSL2 host. Sandboxes
use the current user's rootless Podman service by default: no root-owned container
socket is inherited by a fresh install.

## Quickstart

```bash
sudo apt-get update && sudo apt-get install -y git podman docker-compose   # Debian 13
# Ubuntu 24.04 instead: ... git podman podman-compose  (its docker-compose is the
# retired Python v1). See the README Prerequisites note on compose providers —
# podman-compose 1.3.x drops ${VAR:-default} and mangles ports and environment.
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

Then open **http://localhost:8088** in a browser.

On a Docker host, skip the Podman lines above — `podman.socket` does not exist
there and the enable step fails — and substitute:

```bash
export PATH="$HOME/bin:$PATH"
export DOCKER_HOST="unix://$XDG_RUNTIME_DIR/docker.sock"
DISCO_LOCAL_ENGINE=docker DISCO_SANDBOX_SOCKET=$XDG_RUNTIME_DIR/docker.sock \
  docker compose up -d --build
```

Rootless Docker is not a distribution package on Ubuntu, and Ubuntu 23.10+ needs
an AppArmor exception for `rootlesskit`. The README's **Rootless Docker** block
has the whole install, including the error you get without that exception.

See [Sandbox Image](#sandbox-image) for the rootful alternative and why the
rootless socket is preferred. Compose prints `pull access denied for
disco-server` before it builds; that is expected, not a failure.

The app-server logs print the working UI URL and a one-time first-run pairing
token. On localhost the browser normally pairs automatically; if it asks for a
token, paste the token from `podman compose logs app-server`.

No driver model is bundled and no dead local endpoint is seeded. After the UI
loads, open **Settings -> Models & Providers**, add an OpenAI-compatible local,
LAN, or paid endpoint, set it as **Default primary**, then prove it:

```bash
podman compose exec agent-server disco-verify --quick
```

## Services

| Service | Default port | Notes |
|---|---:|---|
| `frontend` | 8088 | Nginx-served web UI with runtime `/env.js`. |
| `app-server` | 8800 | Settings, auth pairing, library API. |
| `agent-server` | 8000 | Conversation runtime, tools, retrieval, TTS. |
| `sandbox-image` | none | Build-only; builds by default so Build/Agent surfaces work out of the box. |

All durable application state lives in the `disco-data` volume. The default
server image already contains the fastembed ONNX models and Kokoro TTS weights
under `/opt/disco-cache`, so the `/data` volume does not hide them.

## Backup and restore

Use the repository lifecycle command rather than copying a live `disco.db` file.
Backup briefly stops the front door and the two database writers, uses SQLite's
backup API, archives the complete `disco-data` volume with per-entry SHA-256
checksums, then restores the prior service state. The writers start first and the
front door is brought back through its healthy-backend dependencies, so a
Docker or rootless-Podman backend reattachment cannot leave nginx on a stale
network path:

```bash
.venv/bin/python development/scripts/self_host_data.py backup \
  --output "$HOME/disco-backup-$(date +%Y%m%d).tar.gz"
```

The archive includes the consistent SQLite database, its sidecar settings files,
the generated `.secret_key`, encrypted `secrets.json`, origin approvals, projects,
skills, generated audio/assets, and every other regular entry under `/data`.
Transient SQLite `-wal`/`-shm` files are replaced by the consistent backup
database. Rebuildable image assets under `/opt/disco-cache` are deliberately not
included because they are baked into `disco-server`. Unsafe escaping symlinks and
special device/socket files make backup fail instead of being silently omitted.

Restore accepts only a checksummed v1 archive and only an empty `disco-data`
volume. It rejects traversal, unsafe links, duplicate members, checksum drift,
and a failed SQLite integrity check before promoting any restored entry:

```bash
.venv/bin/python development/scripts/self_host_data.py restore \
  --archive "$HOME/disco-backup-20260721.tar.gz"
```

If the named volume does not exist, restore creates it; if it exists and contains
anything, restore stops with no overwrite. After validation it starts the app,
agent, and frontend services. Use `--engine docker` when Docker Compose owns the
installation, and `--project-name NAME` if the original Compose project was not
the default `disco`.

## Rotate the encrypted Settings key

`DISCO_SECRET_KEY_ID` gives the active encryption key a non-secret identity.
`DISCO_SECRET_READ_KEYS` is a JSON object containing at most four prior key IDs
and their key material. Both server services receive the same bounded keyring;
never commit populated values or paste them into logs.

Take a backup first. Then stop the stack, put the new key and ID in `.env`, and
put the old key under its old ID in `DISCO_SECRET_READ_KEYS`. Existing unnamed
keys remain readable when you assign their material a name. Run the migration
through the image so it operates on the shared `/data/secrets.json` volume:

```bash
.venv/bin/python development/scripts/self_host_data.py backup \
  --output "$HOME/disco-before-key-rotation-$(date +%Y%m%d).tar.gz"
podman compose down
# Edit .env: DISCO_SECRET_KEY, DISCO_SECRET_KEY_ID, DISCO_SECRET_READ_KEYS
podman compose run --rm --no-deps app-server \
  python /app/scripts/rotate_secret_store.py --path /data/secrets.json migrate
podman compose run --rm --no-deps app-server \
  python /app/scripts/rotate_secret_store.py --path /data/secrets.json verify
podman compose up -d
podman compose exec agent-server disco-verify --quick
```

Migration is one atomic file replacement and retains encrypted rollback records
in that same file. If the process is interrupted, rerun `migrate` or use the
equivalent `resume` action; both verify an already-written pending migration and
continue safely. Once both services and stored provider keys work, finalize the
window, remove `DISCO_SECRET_READ_KEYS` from `.env`, and recreate both servers so
the retired material leaves their environments:

```bash
podman compose exec app-server \
  python /app/scripts/rotate_secret_store.py --path /data/secrets.json finalize
podman compose up -d --force-recreate app-server agent-server
```

Before finalization, rollback is available. Stop the stack, restore the old key
as `DISCO_SECRET_KEY`/`DISCO_SECRET_KEY_ID`, place the new key in the bounded read
map, then run the script's `rollback` action and restart. A missing named key,
malformed state, failed ciphertext authentication, or incomplete rollback window
fails loudly; the command never prints key material, ciphertext, or secret names.

## Upgrade and uninstall

Upgrade is backup-first, then rebuilds with fresh base images and starts the
stack:

```bash
.venv/bin/python development/scripts/self_host_data.py upgrade \
  --backup "$HOME/disco-pre-upgrade-$(date +%Y%m%d).tar.gz"
podman compose logs app-server agent-server
podman compose exec agent-server disco-verify --quick
```

Application rollback is a source/image operation. Once a newer version has
written a schema an older version may not understand, in-place rollback is not
supported; restore the pre-upgrade archive into a new empty volume with the old
source/image instead.

Ordinary uninstall removes containers and the project network but retains and
reports the exact named `disco-data` volume:

```bash
.venv/bin/python development/scripts/self_host_data.py uninstall
```

Deleting durable data is a separate, explicit operation. It reports the volume
it removed and refuses any confirmation other than the exact phrase below:

```bash
.venv/bin/python development/scripts/self_host_data.py uninstall \
  --destroy-data --confirm DELETE_DISCO_DATA
```

### Scaling and preview redemption

The supported Compose topology runs one `agent-server` against the single
`/data/disco.db` SQLite database in `disco-data`. Do not horizontally scale
preview-serving processes onto independent database files while they share a
`DISCO_SECRET_KEY`: preview launch intents are one-time credentials, and their
atomic redemption fence is the transaction in that shared database.

The preview credential's redemption transaction is cross-process-safe when every
redeemer uses that same SQLite database on a filesystem with correct SQLite
locking semantics. This narrow guarantee does **not** make the complete Agent
runtime multi-worker-safe: loop ownership, schedules, and several runtime caches
are process-local. Run exactly one `agent-server` worker per database. A future
multi-worker Agent runtime would additionally need durable conversation-run and
schedule leases; copying or replicating the database is unsupported.

### Remote preview cookie boundary

For an Internet-facing deployment, route wildcard DNS and TLS for a separately
registrable preview site to the same current/frontend/agent-server ingress, then set:

```bash
DISCO_PUBLIC_UI_URL=https://app.example.com
DISCO_PREVIEW_ORIGIN_BASE=https://preview.example.net
```

The preview origin must not be a child, parent, or sibling site of the UI. For
example, `preview.example.com` is **not** isolated from `app.example.com`: code
on the preview child can still set `Domain=example.com` cookies and exhaust the
parent cookie jar or request-header budget. Disco conservatively rejects a
configured preview base that shares its final two DNS labels with the request
host or `DISCO_PUBLIC_UI_URL`; the public UI URL is required whenever this
override is set. Unicode and punycode spellings are canonicalized before this
comparison. The configured preview base must use HTTPS, a valid nonzero port if
one is explicit, and a DNS name that leaves room for the generated preview
label. The wildcard (`*.preview.example.net` in this example) must terminate TLS
and forward the original `Host` header to the standard front door.

Signature validation prevents a tossed cookie from becoming authenticated, but
it cannot prevent browser cookie eviction or oversized-header denial of service.
The separate registrable site is therefore the supported remote availability
boundary. Localhost deployments can leave this setting blank.

## Sandbox Image

The default boot builds the sandbox image, so the Build and Agent surfaces work
out of the box. For a lean stack without build capability, comment out the
`sandbox-image` service and the agent-server `depends_on` entry in
`compose.yaml`.

The agent-server mounts the current user's rootless Podman socket by default. On
Linux and WSL2 with systemd, enable it once before bringing up the stack:

```bash
systemctl --user daemon-reload
systemctl --user start dbus.socket
loginctl enable-linger "$USER"
systemctl --user enable --now podman.socket
export DISCO_SANDBOX_SOCKET=$XDG_RUNTIME_DIR/podman/podman.sock
podman compose up -d --build
```

The first two lines start the per-user services the Podman install added;
without the user D-Bus, rootless `crun` fails the image build with `sd-bus call:
Interactive authentication required`. Logging out and back in is equivalent.
`enable-linger` keeps the containers running after the operator disconnects.

Export the socket rather than relying on the compose default. `compose.yaml` is
written for the compose providers distributions ship, which substitute variables
in a single pass and so cannot expand a nested `${A:-${B}}` — the fallback baked
into the file can therefore only spell the literal
`/run/user/1000/podman/podman.sock`, which is wrong for any UID but 1000. Keep
`DISCO_SANDBOX_SOCKET` exported for every later `podman compose` command in the
same shell. `DISCO_LOCAL_ENGINE=podman` is the default and
keeps native Podman lifecycle/exec semantics even though the socket has the
engine-neutral in-container name `/var/run/docker.sock`.

Docker is also supported, and rootless Docker is the preferred way to run it: it
keeps the same non-root-owned boundary as the Podman default. Its socket lives
under `$XDG_RUNTIME_DIR`, not `/var/run`:

```bash
DISCO_LOCAL_ENGINE=docker DISCO_SANDBOX_SOCKET=$XDG_RUNTIME_DIR/docker.sock \
  docker compose up -d --build
```

Rootful Docker's socket at `/var/run/docker.sock` grants root-equivalent control
of the host. It is not selected automatically. A trusted single-user operator can
explicitly accept that weaker host boundary (and ensure the sandbox image is
built in that daemon):

```bash
DISCO_LOCAL_ENGINE=docker DISCO_SANDBOX_SOCKET=/var/run/docker.sock \
  docker compose up -d --build
```

gVisor is an optional stronger tier, but **not on the rootless-Podman default
path.** Podman's Docker-compatible API silently drops the requested runtime when
creating a container, so asking for `runsc` there produced an ordinary `crun`
container with no isolation upgrade and no error. The sandbox now inspects the
runtime it actually received and refuses to run when it does not match the one
requested — you get a typed failure instead of a boundary you only believed in.
That check runs on **every** container backend, not just Podman: pointing
`DISCO_LOCAL_ENGINE=docker` at what is in fact Podman's Docker-compatible socket
reaches the same daemon, and that socket's `/info` advertises `runsc` from a
static candidate path whether or not the binary is installed — so a pre-flight
"is the runtime registered?" check cannot tell the truth there. The same
after-the-fact inspection also confirms the memory/CPU/pids caps were recorded,
and refuses to start a container that came back without them.

For a real gVisor boundary, install `runsc` per
[gVisor's instructions](https://gvisor.dev/docs/user_guide/install/) and use
**rootful Docker**, which honours the runtime:

```bash
DISCO_LOCAL_ENGINE=docker DISCO_SANDBOX_SOCKET=/var/run/docker.sock \
  DISCO_LOCAL_RUNTIME=runsc docker compose up -d --build
```

Note that `/var/run/docker.sock` is root-equivalent, so this trades one boundary
for another; the remote `gvisor` backend avoids that trade. Also be aware that
`runsc` under a rootless engine currently fails on cgroup delegation
(`/sys/fs/cgroup/cgroup.subtree_control: permission denied`), and the
`--runtime-flag ignore-cgroups` workaround disables the memory/CPU/pids limits
this project treats as load-bearing. That posture is invisible to the container
inspection above — the caps are recorded and simply never enforced — so the
sandbox instead reads the runtime's registered arguments and logs a loud
`sandbox.cgroup_enforcement_disabled` error when it finds the flag. It is a
warning rather than a refusal because this guide documents the workaround; if you
are running it, agent code can exhaust host memory, CPU and PIDs.

The unisolated `process` backend is development-only and fails closed unless both
`DISCO_SANDBOX=process` and `DISCO_ALLOW_PROCESS_SANDBOX_FOR_DEV=1` are explicit.

## Models

Disco needs a driver LLM that can speak the OpenAI chat-completions API and emit
tool calls. Good options are Ollama, llama.cpp server, vLLM, LM Studio, or a paid
OpenAI-compatible vendor. Configure the endpoint and key in Settings, not by
editing source files.

The non-generative defaults are keyless:

| Capability | Default |
|---|---|
| Search | `ddgs` |
| Extraction | local fetch/readability |
| Embeddings | fastembed ONNX, `intfloat/multilingual-e5-large` |
| Rerank/NLI | fastembed ONNX, `BAAI/bge-reranker-base` |
| TTS | bundled Kokoro ONNX, voices `af_heart` and `af_bella` |

### Reaching a service running on the host machine

An endpoint on the host — Ollama, llama.cpp, LM Studio, a local MCP server —
is **not** at `localhost` from inside the containers, and the address that does
work depends on which engine you run. Measured on this project's own hosts,
2026-08-21:

| Engine | `host.docker.internal` | Host's LAN IP |
|---|---|---|
| rootless Podman (the documented default) | **works** — resolves to `169.254.1.2`, host ports reachable | **fails** — pasta gives the container the host's own address, so this loops back to the container |
| rootless Docker | **fails** — resolves to `172.17.0.1`, every port refused | **works** — use `192.168.x.y` etc. |
| rootful Docker / Docker Desktop | works (`host-gateway`) | works |

Evidence for the rootless-Docker row: with `sshd` listening on `0.0.0.0:22` on
the host, a socket from inside `agent-server` to `172.17.0.1:22` (and to the
compose network gateway `172.21.0.1:22`) is refused, while `<host-LAN-IP>:22`
connects. Rootless Docker's network namespace has no route back to host
listeners, so the `host-gateway` alias Compose sets is a dead address there.

The `Ollama (local)` provider preset ships `http://host.docker.internal:11434/v1`
because rootless Podman is the documented default. **On rootless Docker, replace
that host with your machine's LAN IP** (`hostname -I | awk '{print $1}'`) and
make sure the service binds `0.0.0.0`, not `127.0.0.1`. The same substitution
applies to any MCP server URL pointing at the host.

The default image bakes the full encoder tier. A smaller build can be made with:

```bash
docker build \
  --build-arg DISCO_ENCODER_TIER=lite \
  -t disco-server:lite \
  -f current/deploy/compose/Dockerfile.server .
```

## MCP servers

Both MCP transports are served by the `agent-server` container. Add servers in
**Settings -> MCP**; each one passes two approvals (the config/origin approval,
then a tool-schema approval) before any tool becomes callable.

**Remote (`streamable_http`).** The URL must be reachable *from the agent-server
container* — see [Reaching a service running on the host
machine](#reaching-a-service-running-on-the-host-machine) if the server runs on
your own machine. Under the default `DISCO_BUILD_EGRESS=filtered` posture the
orchestrator's MCP HTTP client connects directly once the origin is approved.
It does **not** route through an egress proxy, because the shipped stack runs
none: the allowlisting proxy is created per-sandbox with a per-run allowlist by
the sandbox backends, and a static orchestrator-side one could not track an
allowlist that changes whenever Settings changes. An unapproved origin is
refused before a client is ever constructed, so the approval gate — not the
proxy — is what bounds where the orchestrator can connect. Operators who do run
their own outbound proxy can force approved MCP origins back through it with
`DISCO_MCP_EGRESS_PROXY_REQUIRED=1` (default `0`); it must then be listening on
`DISCO_MCP_EGRESS_PROXY_HOST:8888` from inside the container.

**Local (`stdio`).** The server image ships Node 22 plus `npm`/`npx`, so the
usual `npx -y @modelcontextprotocol/server-...` command lines work out of the
box. The subprocess runs **inside the agent-server container**, not in a
sandbox: it gets only `PATH`, `HOME`, locale, `TMPDIR` and the secret refs you
explicitly attach — never `DISCO_SECRET_KEY` or provider keys — but it does run
with the agent-server's filesystem access (including `/data`) and network.

> **Security tradeoff, stated plainly.** Approving a stdio MCP server lets `npx`
> download and execute an arbitrary npm package, with its transitive
> dependencies, inside the agent-server container. That is the cost of stdio MCP
> support at all; the approval gate is the control. Pin versions
> (`@scope/pkg@1.2.3`) rather than floating tags, and prefer a remote
> `streamable_http` server when one exists. If you do not want this, do not
> approve stdio servers — the runtime being present does not launch anything on
> its own.

## Compose environment overrides

Compose passes an override only to the service that consumes it. Blank remote
encoder URLs keep the bundled local encoder tier; setting them has an effect when
`DISCO_ENCODERS=remote` (or the equivalent Settings mode) is selected.

| Override | Effective service | Omitted/default behavior |
|---|---|---|
| `DISCO_BUILD_EGRESS` | `agent-server` | `filtered`; registry allowlist proxy |
| `DISCO_MCP_EGRESS_PROXY_REQUIRED` | `agent-server` | `0`; approved MCP origins connect directly |
| `DISCO_MCP_EGRESS_PROXY_HOST` | `agent-server` | `127.0.0.1`; only read when the above is `1` |
| `DISCO_DRIVER_VISION` | `agent-server` | `0`; no driver-local vision claim |
| `DISCO_EMBEDDER_URL` | `agent-server` | blank; bundled embedder |
| `DISCO_RERANKER_URL` | `agent-server` | blank; bundled reranker |
| `DISCO_NLI_URL` | `agent-server` | blank; bundled NLI verifier |
| `DISCO_INSPECT` | `agent-server` | `0`; debug trace routes inert |
| `DISCO_LOG_LEVEL` / `DISCO_LOG_JSON` | both Python servers | `INFO` / `0` |
| provider/search/image/TTS key variables from `.env.example` | both Python servers | blank; imported only after the matching origin is approved |

`DISCO_BUILD_EGRESS` accepts `filtered`, `public`, `sealed`, `open`, or `raw`;
unknown values fail closed. `open` and `raw` are explicit weaker postures. The
`DISCO_*` names take precedence, while the documented legacy `PMX_*` aliases
remain accepted by Compose where one exists.

## Offline Asset Smoke

This proves the baked encoder and TTS assets are used with networking disabled:

```bash
podman run --rm --network none disco-server \
  python /app/scripts/offline_asset_smoke.py
```

Expected output includes `offline assets ok` plus the embedding dimension,
reranked passage id, and TTS sample count.

## Podman Notes

This host used Podman 5.8.2 for packaging verification. If `podman compose` has
no compose provider installed, use `podman-compose` or Docker Compose for the
compose-specific checks. Plain image builds work with:

```bash
podman build -t disco-server -f current/deploy/compose/Dockerfile.server .
podman build -t disco-frontend -f current/frontend/Dockerfile .
```

## Image Sizes

Measured during the packaging verification on 2026-07-07, under Podman 5.8.2:

| Image | Podman | Docker (BuildKit) |
|---|---:|---:|
| `disco-server` | 4.69 GB | 7.88 GB |
| `disco-frontend` | 65.4 MB | — |
| `disco-sandbox:base` | 2.95 GB | 4.21 GB |

Docker's figures were measured on Ubuntu 24.04 with rootless Docker on
2026-08-17. BuildKit adds provenance/attestation layers that Podman's builder
does not, so size a Docker host off the right column — the gap is over 3 GB on
the server image alone.

The default `disco-server` is expected to be large because it includes
LibreOffice plus the full fastembed and Kokoro asset set. Anything materially
above the recorded values should be investigated.

Since 2026-08-21 the server image also carries Node 22 + npm for stdio MCP
servers, copied from `node:22-bookworm-slim` rather than apt-installed. Measured
in isolation on `python:3.12-slim-bookworm` that layer adds about **140 MB**
(a 127 MB base became 267 MB) — roughly 3% of the server image.
