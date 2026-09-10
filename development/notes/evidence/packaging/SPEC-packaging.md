# SPEC: one-command self-host packaging (Epic P, non-security subset)

Mission: any Linux user who finds this repo on GitHub is ONE command away from a
working Disco (`git clone …` then `docker compose up -d`), with **ALL runtime
dependencies baked into the images** — no first-run downloads, no source edits.
Only their model API key / endpoint configuration is theirs to provide, through
the UI, after boot.

Work ONLY in this worktree (branch wt-packaging). HARD FENCES:
- Do NOT touch anything under `current/sec-work-remaining/` — do not read those docs.
- Do NOT change sandbox/execution security posture: the agent-server's
  docker.sock mount and the sandbox backend default are OUT OF SCOPE (a
  security-classed task owns flipping that default). Where docs mention it,
  leave a loud `TODO(security-track)` fence instead of changing behavior.
- Do NOT print, copy, or bake any real secret. `DISCO_SECRET_KEY` is generated
  at first boot by the entrypoint (keep that mechanism).

## Ground truth (verified 2026-07-07)

- `compose.yaml` exists: app + agent (one `disco-server` image, two commands),
  frontend (node→nginx, runtime `/env.js`), `sandbox-image` build-only, one
  `disco-data` volume. NO `profiles:`. It references `current/docs/self-host.md`, which
  currently lives at `archive/docs/self-host.md` (broken pointer).
- `current/deploy/compose/Dockerfile.server`: multi-stage, `uv sync --frozen --no-dev
  --all-packages` (keep `--all-packages` — dropping it PRUNES workspace members),
  python-slim + WeasyPrint/pango/cairo/libreoffice-impress/jemalloc, no torch.
- `current/deploy/compose/entrypoint.sh`: mkdirs under /data, generates
  `DISCO_SECRET_KEY` → `/data/.secret_key`, runs `development/scripts/seed_config.py`.
- `development/scripts/seed_config.py:44`: driver default `http://host.docker.internal:11434/v1`
  (Ollama) — dead on most boxes; the #1 out-of-box failure.
- Encoders: in-process ONNX via `fastembed` (mandatory core dep of retrieval);
  weights lazy-download to `/data/cache/fastembed`. Default model
  BAAI/bge-small-en-v1.5 → runtime artifact Qdrant/bge-small-en-v1.5-onnx-Q
  (Apache-2.0/MIT — pre-baking is license-OK, ship attribution).
- TTS: `kokoro-onnx` is a CORE agent-server dep (rides onnxruntime, no torch);
  weights download on first use — must be pre-baked too.
- Search: `ddgs` (keyless) is a core retrieval dep — nothing to configure.
- `disco-verify` console script exists for self-hosters to prove their model
  config drives the loop.
- `.env.example`: stale CC-BY-NC jina reranker name (real default is MIT
  `bge-reranker-base`) + encoder-tier default mismatch (code `full`, example
  `lite`).
- Local container runtime for your verification: **podman 5.8.2** (no docker on
  this host). `podman build` / `podman compose` are the docker-compatible
  stand-ins; the shipped UX must still say `docker compose up -d`.

## Deliverables (ordered)

P1 — one command, loud first-run:
- Add compose `profiles:` so bare `docker compose up -d` brings app + agent +
  frontend + data with sane defaults; `--profile sandbox` adds the sandbox image
  build. Nothing in the default profile may require a GPU, a key, or the host
  network.
- On boot, the server MUST print (docker logs, and ideally also an obvious
  stdout banner): the working UI URL and the first-run admin pairing path/token
  instructions (the entrypoint already generates the pairing token — surface it).
- Fix the `current/docs/self-host.md` pointer: restore a CURRENT self-host doc at
  `current/docs/self-host.md` (rewrite from `archive/docs/self-host.md`, updated to the
  real P1 commands; the archived copy stays put).

P2 — bake ALL weights into the default image:
- Extend `Dockerfile.server` with a build step that pre-fetches into the image:
  (a) fastembed's default ONNX embedding weights, (b) the default reranker
  weights if the default config uses one locally, (c) kokoro-onnx TTS weights.
  Locate each library's cache-dir convention and bake to the path the runtime
  actually reads (fastembed's is `/data/cache/fastembed` via env/config — either
  bake there and make the volume mount not shadow it, or bake to an image-local
  cache dir the code checks first; PROVE at runtime which path wins with the
  volume mounted, don't assume).
- Acceptance: with networking DISABLED in the container (compose
  `network_mode: none` smoke or equivalent), a first RAG embed call and a first
  TTS synth call both succeed. Write the exact repro commands into the doc.
- A `lite` build-arg variant may exist but the DEFAULT tag is full/batteries-in.

P4 — first-run model config that cannot dead-end:
- Replace the dead-Ollama seed: seed NO driver endpoint, and make the state
  honest — the UI's first-run path must lead the user to configure a model
  (endpoint+key or local), and `disco-verify` documented as the proof step.
  If a "model not configured" state renders anywhere, it must be an explicit,
  actionable message, never a silent hang. (NO false affordances.)
- Trim `.env.example` to required-vs-optional, fix the stale reranker name and
  the encoder-tier default mismatch, document `DISCO_ENCODERS`, the sandbox
  profile, and `AUTH_DEV_AUTO_PAIR`.

P5 — release hygiene:
- README quickstart: the REAL P1 commands, copy-pasteable, tested.
- `current/docs/licenses-bundled.md`: every baked weight + heavyweight dep with its
  license (fastembed/bge weights, kokoro voices, Chromium/LibreOffice in the
  sandbox image) + required attributions.
- Record built image sizes in the doc; flag anything absurd.

## Verification you must run (exit code is truth)

1. `podman build` of server (with weight bake) and frontend images SUCCEED.
2. `podman compose config` (or `podman-compose config`) validates.
3. Boot the stack locally with podman, hit the frontend URL, capture the boot
   banner showing URL + pairing instructions into FINDINGS.md (text is fine —
   a UI screenshot pass happens later in the campaign).
4. The network-disabled encoder/TTS smoke from P2.
5. If you changed any Python (seed_config etc.): from the MAIN repo venv run
   `uv run basedpyright` equivalent via
   /var/home/dylan/projects/disclaude/.venv — do not introduce type errors —
   plus the relevant unit tests with:
   PYTHONPATH="$WT/packages/core/src:$WT/packages/retrieval/src:$WT/packages/tools/src:$WT/packages/agent-server/src:$WT/packages/app-server/src" \
     /var/home/dylan/projects/disclaude/.venv/bin/python3 -m pytest ...

## Constraints

- Keep image layering cache-friendly (weights layer AFTER deps, BEFORE code, so
  code iteration doesn't re-download weights).
- Match the house comment style (docs-heavy, WHY-first).
- Commit on wt-packaging with `command git commit --no-verify`; write
  FINDINGS.md at worktree root: what you built, what you proved, exact commands,
  image sizes, and anything you could NOT prove (be loud about gaps — no
  papering over).
