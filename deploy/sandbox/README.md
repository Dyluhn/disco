# Sandbox base image — `disco-sandbox:base`

The image the gVisor / Podman / local container backends run agent code in
(`packages/tools/.../sandbox/`). Published with each release as
`ghcr.io/dyluhn/disco-sandbox:<tag>` (amd64 + arm64); the compose stack's
`sandbox-image` service pulls it and tags it `disco-sandbox:base` in the host
daemon (`deploy/compose/ensure_sandbox_image.py`).

> **Tag name:** the canonical tag is **`disco-sandbox:base`** — it is what the
> config default (`SandboxConfig.image`) expects. `pmx-sandbox:base` is the
> legacy pre-rename tag; existing hosts may carry both, but new builds should
> use `disco-sandbox:base` only.

## What's in it

- **Debian 13 (trixie) slim** base.
- **Python 3.13** + venv + pip + `python-is-python3` (agents write `python`, not
  `python3`), `uv`, and pre-installed `pytest` + `ruff` + `ipykernel` +
  `jupyter_kernel_gateway` (the persistent `code_exec` kernel).
- **Node.js 22 LTS** + npm + npx + corepack + pnpm (so `code_exec(language="node")`
  and JS/TS builds — vite/next/etc. — work in the box).
- **Playwright + Chromium** (`--with-deps`) in the shared `/ms-playwright` bundle,
  readable/executable by the non-root runtime user and shared with marp through
  `CHROME_PATH=/usr/local/bin/playwright-chromium`. `CHROME_NO_SANDBOX=true` —
  Chromium's own sandbox can't nest; gVisor/runc is the boundary.
- **Renderers, jailed by design**: `@marp-team/marp-cli` (slide decks),
  `pandoc` (DOCX export), `libreoffice-impress` + fonts-liberation (PPTX→PDF).
  Agent-generated content renders inside the sandbox, never on the host.
- **noVNC live-browser stack**: `xvfb`, `x11vnc`, `novnc`, `websockify`
  (packages only — the backend starts the daemons lazily; gVisor backend only).
- **Build toolchain + CLI utils**: `build-essential`, `bash`, `curl`, `git`,
  `jq`, `ripgrep`, `procps`, `less`, `nano`, `tmux`, `unzip`, `sqlite3`, `lsof`.
- Non-root user `agent` (uid 1000); workdir `/workspace`.
- **No secrets baked in — ever.**

## Build + load

The backend **never pulls** (a missing image is a typed error, by design). On a
daemon the compose stack does not manage, pull the published image and tag it,
or build it there:

```sh
docker pull ghcr.io/dyluhn/disco-sandbox:v0.2.0
docker tag ghcr.io/dyluhn/disco-sandbox:v0.2.0 disco-sandbox:base
# or, on the daemon host, from this directory:
docker build -t disco-sandbox:base .
```

To build elsewhere and load onto the host:

```sh
docker build -t disco-sandbox:base .
docker save disco-sandbox:base | ssh sandbox@<host> 'sudo docker load'
```

## Where it must be built

Each container backend has its OWN image store — build the image in every one you
use (the backend never pulls):

- **gVisor** (`backend="gvisor"`) → VM 201's Docker: `cd /opt/sandbox && docker build -t disco-sandbox:base .`
- **local** (`backend="local"`) → the workstation's rootless Podman: `podman build -t disco-sandbox:base -f deploy/sandbox/Dockerfile deploy/sandbox/`
- **process** (`backend="process"`) → no image; runs on the HOST, so it uses the host's own `node`/`python3`.
- **podman** (remote) → build on that host's Podman when provisioned.

## Verify under gVisor

```sh
docker run --rm --runtime=runsc disco-sandbox:base sh -c \
  'node --version; npm --version; python3 --version'
```

See `gvisor-sandbox-host` / VM 201's `/opt/sandbox/CONTRACT.md` for the host
contract (runtime name `runsc`, workspace bind convention, network posture).
Filtered egress (allowlisting proxy sidecar) is set up by the backend at
container-create — see `sandbox/gvisor.py::_setup_filtered_egress`.
