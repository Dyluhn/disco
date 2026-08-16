# Self-hosting Disco

This is the supported one-command path for a local Linux or WSL2 host. Sandboxes
use the current user's rootless Podman service by default: no root-owned container
socket is inherited by a fresh install.

## Quickstart

```bash
git clone <repo-url>
cd disclaude
systemctl --user enable --now podman.socket
podman compose up -d --build
podman compose logs app-server
open http://localhost:8088
```

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
.venv/bin/python scripts/self_host_data.py backup \
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
.venv/bin/python scripts/self_host_data.py restore \
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
.venv/bin/python scripts/self_host_data.py backup \
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
.venv/bin/python scripts/self_host_data.py upgrade \
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
.venv/bin/python scripts/self_host_data.py uninstall
```

Deleting durable data is a separate, explicit operation. It reports the volume
it removed and refuses any confirmation other than the exact phrase below:

```bash
.venv/bin/python scripts/self_host_data.py uninstall \
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
registrable preview site to the same frontend/agent-server ingress, then set:

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
systemctl --user enable --now podman.socket
podman compose up -d --build
```

The compose default resolves the host socket from `$XDG_RUNTIME_DIR`, falling
back to `/run/user/1000/podman/podman.sock`. Set `DISCO_SANDBOX_SOCKET` when your
UID or socket location differs. `DISCO_LOCAL_ENGINE=podman` is the default and
keeps native Podman lifecycle/exec semantics even though the socket has the
engine-neutral in-container name `/var/run/docker.sock`.

Docker's root-owned socket grants root-equivalent control of the host. It is not
selected automatically. A trusted single-user operator can explicitly accept
that weaker host boundary (and ensure the sandbox image is built in that daemon):

```bash
DISCO_LOCAL_ENGINE=docker DISCO_SANDBOX_SOCKET=/var/run/docker.sock \
  docker compose up -d --build
```

gVisor remains an optional stronger tier for hosts where `runsc` is installed and
registered. Select it without changing the backend persistence model:

```bash
DISCO_LOCAL_RUNTIME=runsc podman compose up -d --build
```

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

The default image bakes the full encoder tier. A smaller build can be made with:

```bash
docker build \
  --build-arg DISCO_ENCODER_TIER=lite \
  -t disco-server:lite \
  -f deploy/compose/Dockerfile.server .
```

## Compose environment overrides

Compose passes an override only to the service that consumes it. Blank remote
encoder URLs keep the bundled local encoder tier; setting them has an effect when
`DISCO_ENCODERS=remote` (or the equivalent Settings mode) is selected.

| Override | Effective service | Omitted/default behavior |
|---|---|---|
| `DISCO_BUILD_EGRESS` | `agent-server` | `filtered`; registry allowlist proxy |
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
podman build -t disco-server -f deploy/compose/Dockerfile.server .
podman build -t disco-frontend -f frontend/Dockerfile .
```

## Image Sizes

Measured during the packaging verification on 2026-07-07:

| Image | Size |
|---|---:|
| `disco-server` | 4.69 GB |
| `disco-frontend` | 65.4 MB |
| `disco-sandbox:base` | 2.95 GB |

The default `disco-server` is expected to be large because it includes
LibreOffice plus the full fastembed and Kokoro asset set. Anything materially
above the recorded values should be investigated.
