# Packaging Findings

Date: 2026-07-07
Branch: `wt-packaging`
Runtime used for verification: Podman 5.8.2

## Built

- `podman build -t disco-server -f current/deploy/compose/Dockerfile.server .`
- `podman build -t disco-frontend -f current/frontend/Dockerfile .`
- `podman build -t disco-sandbox:base -f current/deploy/sandbox/Dockerfile .`

Image sizes:

| Image | Size |
|---|---:|
| `localhost/disco-server:latest` | 4.69 GB |
| `localhost/disco-frontend:latest` | 65.4 MB |
| `localhost/disco-sandbox:base` | 2.95 GB |

The server image is large but expected: it includes LibreOffice plus the full
fastembed embedding/reranker cache and Kokoro ONNX voice assets. The sandbox
image size is also expected because it includes Chromium/Playwright, noVNC,
Pandoc, LibreOffice, Node, Python, and build tooling.

## Proved

Compose validation:

```bash
uvx podman-compose config
```

`podman compose` had no compose provider on this host, so `uvx podman-compose`
1.6.0 was used for the compose validation and stack smoke.

Offline baked-asset smoke, no network:

```bash
podman run --rm --network none disco-server \
  python /app/scripts/offline_asset_smoke.py
```

Output:

```text
offline assets ok: embedding_dim=1024 rerank_top=p1 tts_samples=19968
```

Offline baked-asset smoke with `/data` mounted, no network:

```bash
podman volume rm -f disco-offline-smoke >/dev/null 2>&1 || true
podman run --rm --network none -v disco-offline-smoke:/data disco-server \
  python /app/scripts/offline_asset_smoke.py
podman volume rm -f disco-offline-smoke >/dev/null
```

Output:

```text
offline assets ok: embedding_dim=1024 rerank_top=p1 tts_samples=19968
```

That proves the runtime reads the baked `/opt/disco-cache` paths; the `/data`
volume does not shadow the fastembed or Kokoro assets.

Stack smoke:

```bash
DISCO_SANDBOX_SOCKET=/run/user/$(id -u)/podman/podman.sock \
DISCO_UI_PORT=18088 \
DISCO_APP_PORT=18800 \
DISCO_AGENT_PORT=18000 \
DISCO_PUBLIC_UI_URL=http://localhost:18088 \
uvx podman-compose up -d

curl -fsS http://127.0.0.1:18088/
curl -fsS http://127.0.0.1:18088/env.js
```

Result:

```text
disco_app-server_1    Up (healthy)    127.0.0.1:18800->8800/tcp
disco_agent-server_1  Up (healthy)    127.0.0.1:18000->8000/tcp
disco_frontend_1      Up (healthy)    127.0.0.1:18088->80/tcp
```

Boot banner captured from `podman logs disco_app-server_1`:

```text
[entrypoint] generated a new DISCO_SECRET_KEY into /data/.secret_key
[seed] wrote /data/disco-config.json: model not configured yet; open Settings to add a driver endpoint; projects -> /data/projects

================ Disco self-host boot ================
UI: http://localhost:18088
First-run admin pairing:
  Open http://localhost:18088
  If the browser asks for a pairing token, use this one-time token:
  [redacted one-time token]
After pairing, configure a driver model in Settings -> Models & Providers.
Proof step: docker compose exec agent-server disco-verify --quick
======================================================
```

Persisted first-run config sanity check:

```bash
podman exec disco_app-server_1 python -c \
  "from disco.core.llm import ConfigStore; cfg=ConfigStore().load(); print(cfg.default_model); print(cfg.models[cfg.default_model].model_id); print(cfg.models[cfg.default_model].base_url); print(cfg.assignments)"
```

Output:

```text
driver-unconfigured
not-configured
None
{}
```

First-run verifier behavior before model configuration:

```bash
podman exec disco_agent-server_1 disco-verify --quick
```

Expected exit code: `1`.

Output:

```text
FAIL config driver model is not configured: add an OpenAI-compatible endpoint
in Settings -> Models & Providers, set it as the Default primary, then re-run
disco-verify
```

Python checks:

```bash
PYTHONPATH="$PWD/packages/core/src:$PWD/packages/retrieval/src:$PWD/packages/tools/src:$PWD/packages/agent-server/src:$PWD/packages/app-server/src" \
  /var/home/dylan/projects/disclaude/.venv/bin/python3 -m py_compile \
  development/scripts/seed_config.py development/scripts/prefetch_packaged_assets.py development/scripts/offline_asset_smoke.py

PYTHONPATH="$PWD/packages/core/src:$PWD/packages/retrieval/src:$PWD/packages/tools/src:$PWD/packages/agent-server/src:$PWD/packages/app-server/src" \
  /var/home/dylan/projects/disclaude/.venv/bin/python3 -m pytest \
  current/packages/retrieval/tests/test_env_knobs_B1a.py \
  current/packages/retrieval/tests/test_env_knobs_B1b.py \
  current/packages/retrieval/tests/test_encoder_oom_guard_B3.py \
  current/packages/agent-server/tests/test_disco_verify_runner.py \
  current/packages/app-server/tests/test_app.py
```

Result: `102 passed, 1 warning`.

Type check:

```bash
ln -s /var/home/dylan/projects/disclaude/.venv .venv
PYTHONPATH="$PWD/packages/core/src:$PWD/packages/retrieval/src:$PWD/packages/tools/src:$PWD/packages/agent-server/src:$PWD/packages/app-server/src" \
  /var/home/dylan/projects/disclaude/.venv/bin/basedpyright
rm .venv
```

Result: `0 errors, 0 warnings, 0 notes`.

The temporary symlink was needed because `pyproject.toml` configures
`venvPath = "."` and `venv = ".venv"`, while the spec requires using the main
repo venv.

## Deviations And Code Reality

- The spec ground truth says the fastembed default is `BAAI/bge-small-en-v1.5`.
  Code reality on this branch defaults to the full tier:
  `intfloat/multilingual-e5-large` for embeddings and `BAAI/bge-reranker-base`
  for rerank/NLI. I followed code reality and baked the full tier by default.
- The frontend image could not build from `current/frontend/` as its only context,
  because `vite.config.ts` imports raw shared assets from
  `current/packages/agent-server/src/disco/agent_server/*.js`. I changed the frontend
  image build to use the repo root context and copy only the frontend tree plus
  those shared assets.
- Default host ports `8000` and `8800` were already occupied by local Python
  servers on this workstation. The stack smoke used alternate host ports
  `18000`, `18800`, and `18088`; container-internal ports were unchanged.
- The real generated pairing token was redacted above to avoid committing a live
  first-run admin token. The banner does print the token at runtime as required.

## Gaps

No required packaging verification remains unproved.

## Security default close-out (2026-07-11)

The deploy-path security follow-up changed the inherited sandbox daemon from
`/var/run/docker.sock` to the current user's rootless Podman socket. The
Settings-facing backend remains `local`; the existing service dispatcher uses
Podman's native client for a Podman socket. gVisor/runsc remains an opt-in runtime
tier, and the process backend retains its separate dev-only opt-in.

Default Compose resolution:

```bash
XDG_RUNTIME_DIR=/run/user/$(id -u) uvx podman-compose config
```

Exit `0`; the resolved mount was:

```text
/run/user/1000/podman/podman.sock:/var/run/docker.sock
```

The documented `DISCO_SANDBOX_SOCKET=/var/run/docker.sock` root-Docker opt-in
was also config-validated. Exit `0`; it resolved to
`/var/run/docker.sock:/var/run/docker.sock` and never affects the default.

The live default socket was proved to be rootless:

```bash
podman --url unix:///run/user/1000/podman/podman.sock \
  info --format '{{.Host.Security.Rootless}}'
```

Output `true`, exit `0`.

Focused verification:

- Python sandbox/config/persistence tests: 74 passed, exit `0`; the additional
  agent-server sandbox-route file passed 7 tests, exit `0`.
- Frontend Sandbox Settings tests: 10 passed, exit `0`.
- Frontend shipping TypeScript check: exit `0`.
- Changed-file Ruff: exit `0`.
- Architecture budget, import contracts, generated-diagram freshness, and
  basedpyright: all exit `0`; basedpyright reported 0 errors/warnings/notes.
