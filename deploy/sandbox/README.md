# Sandbox base image — `pmx-sandbox:base`

The image the gVisor / Podman / local container backends run agent code in
(`packages/tools/.../sandbox/`). It is the **source-of-truth copy** of the
Dockerfile that previously lived only on the sandbox host (`/opt/sandbox/Dockerfile`
on VM 201) — keep them in sync.

## What's in it

- **Debian 13 (trixie) slim** base.
- **Python 3.13** + venv + pip.
- **Node.js 22 LTS** + npm + npx + corepack (so `code_exec(language="node")` and
  JS/TS builds — vite/next/etc. — work in the box). Added 2026-06-07.
- CLI utils: `bash`, `curl`, `git`, `jq`, `ripgrep`, `procps`, `less`, `nano`.
- Non-root user `agent` (uid 1000); workdir `/workspace`.
- **No secrets baked in — ever.**

## Build + load

The backend **never pulls** (a missing image is a typed error, by design). Build
the image where Docker runs (the daemon host — VM 201 for gVisor) and tag it
`pmx-sandbox:base`:

```sh
# on the daemon host (e.g. VM 201), from this directory:
docker build -t pmx-sandbox:base .
```

To build elsewhere and load onto the host:

```sh
docker build -t pmx-sandbox:base .
docker save pmx-sandbox:base | ssh sandbox@<host> 'sudo docker load'
```

## Where it must be built

Each container backend has its OWN image store — build the image in every one you
use (the backend never pulls):

- **gVisor** (`backend="gvisor"`) → VM 201's Docker: `cd /opt/sandbox && docker build -t pmx-sandbox:base .`
- **local** (`backend="local"`) → the workstation's rootless Podman: `podman build -t pmx-sandbox:base -f deploy/sandbox/Dockerfile deploy/sandbox/`
- **process** (`backend="process"`, the default) → no image; runs on the HOST, so it uses the host's own `node`/`python3`.
- **podman** (remote) → build on that host's Podman when provisioned.

Both gVisor (VM 201) and local (workstation Podman) were rebuilt with Node 22 on
2026-06-07.

## Verify under gVisor

```sh
docker run --rm --runtime=runsc pmx-sandbox:base sh -c \
  'node --version; npm --version; python3 --version'
```

See `gvisor-sandbox-host` / VM 201's `/opt/sandbox/CONTRACT.md` for the host
contract (runtime name `runsc`, workspace bind convention, network posture).
Filtered egress (allowlisting proxy sidecar) is set up by the backend at
container-create — see `sandbox/gvisor.py::_setup_filtered_egress`.
