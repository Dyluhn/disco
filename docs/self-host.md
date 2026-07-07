# Self-hosting Disco

This is the supported one-command path for a local Linux or WSL2 host with
Docker Compose. Podman works too, but Docker remains the user-facing command.

## Quickstart

```bash
git clone <repo-url>
cd disclaude
docker compose up -d --build
docker compose logs app-server
open http://localhost:8088
```

The app-server logs print the working UI URL and a one-time first-run pairing
token. On localhost the browser normally pairs automatically; if it asks for a
token, paste the token from `docker compose logs app-server`.

No driver model is bundled and no dead local endpoint is seeded. After the UI
loads, open **Settings -> Models & Providers**, add an OpenAI-compatible local,
LAN, or paid endpoint, set it as **Default primary**, then prove it:

```bash
docker compose exec agent-server disco-verify --quick
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

## Sandbox Image

The default boot builds the sandbox image, so the Build and Agent surfaces work
out of the box. For a lean stack without build capability, comment out the
`sandbox-image` service and the agent-server `depends_on` entry in
`compose.yaml`.

TODO(security-track): the agent-server still mounts the host container socket by
default for the local sandbox backend. That is root-equivalent on a Docker host.
For Podman, prefer a rootless socket:

```bash
systemctl --user enable --now podman.socket
DISCO_SANDBOX_SOCKET=/run/user/$(id -u)/podman/podman.sock \
  docker compose up -d --build
```

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
