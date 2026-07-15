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

### Scaling and preview redemption

The supported Compose topology runs one `agent-server` against the single
`/data/disco.db` SQLite database in `disco-data`. Do not horizontally scale
preview-serving processes onto independent database files while they share a
`DISCO_SECRET_KEY`: preview launch intents are one-time credentials, and their
atomic redemption fence is the transaction in that shared database.

Multiple workers or processes are safe only when every one of them uses the
same SQLite database on a filesystem with correct SQLite locking semantics.
Copying or replicating outstanding redemption rows into independently committing
databases can create more than one redemption winner. A multi-node deployment
therefore needs a shared transactional redemption store; independent-database
replicas are unsupported and may also reject valid launches when a request lands
on a replica that did not register the intent.

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
UID or socket location differs.

Docker's root-owned socket grants root-equivalent control of the host. It is not
selected automatically. A trusted single-user operator can explicitly accept
that weaker host boundary (and ensure the sandbox image is built in that daemon):

```bash
DISCO_SANDBOX_SOCKET=/var/run/docker.sock docker compose up -d --build
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
