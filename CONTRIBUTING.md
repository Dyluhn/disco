# Contributing to Disco

A self-hosted Research + Agent + Build platform built as one agent core over an
append-only event log. Before you start, skim the two design authorities — when
code and prose disagree, they win:

- [`basis-of-design.md`](./docs/contracts/basis-of-design.md) — the cornerstone design document.
- [`event-state-contract.md`](./docs/contracts/event-state-contract.md) — the spine's binding contract.

Hosted CI runs the deterministic release gates. Before opening a PR, run the
local equivalents for the area you touched; the `Makefile` remains the fastest
way to exercise the Python and harness suites.

## Prerequisites

- **[uv](https://docs.astral.sh/uv/)** — the workspace/dependency manager (this is a
  `uv` workspace; do not use bare `pip`).
- **Python 3.12+** — every package declares `requires-python = ">=3.12"`.
- **Node 22 LTS** for the frontend — the Vite/React/TS app pins `@types/node ^22`.
- **A container runtime** (Docker or rootless Podman) for the sandbox-backed tests and
  for running the Build/Agent surface for real. The fully hermetic `make test` path
  does not need one (it uses the `process` backend on the host); the integration tests
  and live runs do.
- **LibreOffice (`soffice`)** for deck→PDF export on the **`process` (host) backend** —
  the default the dev server runs. PPTX + HTML deck export need nothing extra, but
  converting a deck to PDF shells out to headless LibreOffice. Install the minimal
  Impress-only package on your host (the container/sandbox image already ships it):
  - Debian/Ubuntu: `sudo apt-get install -y libreoffice-impress`
  - Fedora: `sudo dnf install -y libreoffice-impress`
  - macOS: `brew install --cask libreoffice`

  Without it, deck→PDF fails soft with an honest message naming the missing binary; the
  rest of the app is unaffected.

First run also fetches a few one-time artifacts: Playwright's Chromium
(`uv run playwright install chromium`) for browser-daemon integration tests, and
the fastembed ONNX encoders on the first grounded answer.

## Getting set up

```bash
uv sync --all-packages     # provision the venv + install every workspace member
```

The bundled in-process Kokoro TTS (audio overviews) is a default product tier, so it
ships in the agent-server's **core** dependencies — a bare `uv sync` installs
`kokoro-onnx` + `lameenc` out of the box (weights download on first use). No `--extra`
flag is needed. Bring-your-own / self-host / paid TTS tiers stay opt-in.

Other optional extras still live on the **member** packages, not the workspace root,
so install them with `--package` (e.g. the tools `browser` extra for Playwright).

### Running the servers

Two FastAPI/uvicorn servers share one SQLite event store (`DISCO_DB`, default
`./disco.db`; legacy `PMX_DB` is still honored). For local dev, use a
repo-local data dir and a generated app secret:

```bash
cp .env.example .env
python - <<'PY'
from pathlib import Path
import secrets

p = Path(".env")
text = p.read_text()
text = text.replace(
    "DISCO_SECRET_KEY=\n",
    f"DISCO_SECRET_KEY={secrets.token_urlsafe(32)}\n",
)
p.write_text(text)
PY
mkdir -p .data
```

Export the shared dev environment in each server terminal:

```bash
set -a; source .env; set +a
export DISCO_DB="$PWD/.data/disco.db"
export DISCO_CONFIG="$PWD/.data/disco-config.json"
export DISCO_SECRETS="$PWD/.data/secrets.json"
```

Seed first-run model/project config once:

```bash
export DISCO_PROJECTS_ROOT="$PWD/.data/projects"
uv run python development/scripts/seed_config.py
```

Then run the servers in separate terminals with the shared environment above.
Agent-server:

```bash
# Agent-server — per-conversation runtime (WebSocket + REST).
# Env: DISCO_HOST (default 127.0.0.1), DISCO_PORT (default 8000), DISCO_DB.
#      DISCO_SANDBOX (process|local|gvisor|podman) optionally forces a sandbox backend;
#      unset → the persisted Settings selector decides per request.
export DISCO_SANDBOX=process
export DISCO_ALLOW_PROCESS_SANDBOX_FOR_DEV=1
uv run python -m disco.agent_server
```

App-server:

```bash
# App-server — settings + library gateway the frontend calls.
# Env: DISCO_HOST (default 127.0.0.1), DISCO_PORT (default 8800), DISCO_DB.
uv run python -m disco.app_server
```

The `process` sandbox is intentionally dev-only and shares the host process and
network namespaces. Container backends are the default for real Build runs.
Compose publishes agent `8000`, app `8800`, and the built frontend on `8088`.

### Running the frontend

```bash
cd frontend
npm ci
VITE_API_BASE=http://localhost:8800 \
VITE_AGENT_BASE=http://localhost:8000 \
npm run dev
```

## Running tests

The suite splits into a **hermetic** half (fast, offline, no LLM, no network) and a
**live** half (drives real services/models). Keep the hermetic path fast and offline.

```bash
make test          # unit + harness — the fast hermetic gate (run this before pushing)
make unit          # per-package unit suites only (packages/*/tests) — `uv run pytest`
make harness       # all development/harness/ tests
make contract      # TS <-> Python wire/event contract drift
make fuzz          # property-based parser fuzzing (hypothesis)
make fault         # fault/chaos injection
```

Or invoke pytest directly: `uv run pytest`. Async tests run without per-test
decorators (`asyncio_mode = "auto"`); `testpaths = ["packages", "development/tests/architecture", "development/tests/unbiased_gate"]`.

Live/heavy targets (need real config, secrets, or a running instance — kept out of
`make test` on purpose):

```bash
make eval          # research eval, REPLAY mode (needs a cassette — run `make capture` first)
make eval-real     # research eval against REAL services (slow)
make verify        # `disco verify`: does your configured model drive the loop? (ARGS=--quick)
make capture       # record the real research cassette (HEAVY: cold fastembed + LLM)
make capture-loop  # record a real deep-research conversation (event-log + cassette)
make canary        # live probe of a running agent-server: /health + a real grounded query
make canary-health # live /health probe only (no model call)
make replay        # event-log deterministic replay (needs the loop_demo fixture)
make e2e           # frontend Playwright E2E + visual regression (fixture mode)
make e2e-update    # refresh E2E visual snapshots
```

Frontend tests:

```bash
cd frontend
npm test           # vitest run
npm run test:e2e   # Playwright E2E
```

## Linting & formatting

`ruff` is the one tool (config in the root `pyproject.toml`):

- **line-length 100**, `target-version = py312`.
- selected rule sets: **E, F, I, B, UP** (pycodestyle, pyflakes, isort, bugbear,
  pyupgrade). `UP042` is intentionally disabled — the event-state contract's normative
  code defines enums as `(str, Enum)` and the source mirrors it verbatim.

```bash
make lint          # uv run ruff check packages development/harness
make fmt           # uv run ruff format packages development/harness
```

The frontend lints with `npm run lint` (eslint).

## The monorepo layering rule

`packages/` is a five-member `uv` workspace with a strict, one-directional dependency
topology (basis-of-design §5). Dependencies only point **down**:

```
core  →  tools  →  agent-server  →  app-server
              ↘  retrieval  ↗
```

- **core** — the brain: events, state, store, LLM router, agent loop, security. Depends
  on nothing in the workspace.
- **tools** — the action space: executor, the four sandbox backends, builtin + MCP
  tools. Depends on `core`.
- **retrieval** — discovery/extraction/rerank + the grounding/citation pipeline.
  Depends on `core`.
- **agent-server** — per-conversation runtime (WS + REST). Depends on `core`,
  `retrieval`, `tools`.
- **app-server** — user-facing settings/library gateway. Depends on `core` (+ httpx to
  proxy the OpenRouter catalogue).

`disco` is a PEP 420 namespace package — every member shares the
`disco.*` namespace. **Do not create up-edges.** `core` must never import from
`tools`/`agent_server`/`app_server`; a lower layer reaching up into a higher one is a
review-blocking defect, not a style nit.

## Verification discipline (the house rules)

This project verifies against reality, not against green checkmarks:

- **Every feature lands with a test or harness built from real, captured samples.**
  Pull a verbatim sample from the real service first, inject it through the common
  endpoint, and assert against that — not against a hand-invented fixture.
- **UI changes need visual evidence.** End a frontend change with a screenshot of it
  working in the real app (Playwright/Firefox), not just a passing `vitest` run.
- **A green test count is not, by itself, evidence.** It is necessary, not sufficient.
  The eval/canary targets exist because the wedge — reliability on local models — can
  only be shown by driving the real loop.
- **No cheap workarounds.** Never hardcode state to make a test pass; if a path isn't
  wired, flag the gap explicitly rather than papering over it. Don't ship UI that looks
  usable but does nothing.

## Landing on main (maintainers)

`main` only ever moves to a **landing head**, and the shape is fixed by the
sealed authorities (`development/architecture/test-inventory.json`,
`public-api.json`, `development/governance/PROTECTED.sha256`):

1. **Candidate chain P** — the reviewed commits, pushed on their branch. Every
   gate except the two lineage-checked ones (public API, test inventory) must be
   green on P; GitHub's required job shows exactly that on a branch.
2. **Source commit S** — one commit with parent P carrying the mutable status
   docs (`development/governance/CURRENT-STATE.md`, `LAUNCH-CHECKLIST.md`) and
   nothing under the derived paths. Push it on its own branch and keep it: the
   authorities cite S by hash and a clone must resolve it.
3. **Regenerate against S** (HEAD must be S): the test inventory with the
   package label (`regenerate_inventory(root, S, package=..., null_advance=True)`
   when the package adds no test id; renames and module splits need their
   transition records), then the public API (`regenerate_public_api` with any
   additive, member, declaration or relocation records the package needs), then
   `gen_arch_diagram.py --check`. A delta the records do not explain is a stop.
4. **Rebaseline the seal** on that single command only, owner-authorized:
   `DISCO_GOVERNANCE_REBASELINE=1 uv run python development/scripts/check_governance_seal.py --rebaseline`,
   then `check_governance_seal.py` clean. Quote the owner's written authorization
   in the landing commit.
5. **Landing head L** — S's tree plus the regenerated derived files, committed
   with **parent P** (`git commit-tree`), so S and L are siblings. Run the twelve
   gates and the receipts (backend, full unit, lint, types, architecture,
   imports, diagram, architecture tests, frontend) on L and keep the job/log/
   result files in the ledger.
6. **Move main**: fast-forward `main` to L, push `main` and S's branch. The
   required job on `main` must be green; that run is the record.

Commits cited by the authorities that are not on any branch are anchored by the
tag `governance/authority-commit-anchors` (an empty-tree commit whose parents
are those commits). If a landing cites a commit that will not stay on a pushed
branch, add it to that anchor.

## Cutting a release (maintainers)

A release is a landing head with a tag on it. In the candidate: promote the
CHANGELOG section to `## vX.Y.Z - date` and move the default image tag in
`compose.yaml`, `compose.build.yaml` and `.env.example`. The version lives in
the tag and the CHANGELOG only: `packages/*/pyproject.toml` and
`frontend/package.json` are pinned contract files whose bytes the public-API
authority does not let a landing change. Land it, then tag the landing head:

```bash
git tag -a vX.Y.Z <landing head> -m "vX.Y.Z"
git push origin vX.Y.Z
```

`.github/workflows/release.yml` runs the twelve gates and the suites on the
tag, drafts the GitHub release from the CHANGELOG section (a missing section
fails the run), and only then builds the three images on native amd64 and
arm64 runners and pushes the multi-arch tags to GHCR. Publish the draft once
the images are up. `check_ci_contract` allows extra jobs in that workflow only
when they depend on `draft-release`, so nothing publishes unless every gate
passed.

## Commit & PR conventions

Conventional commits with a scope: `type(scope): summary`. Observed types in the
history: `feat`, `fix`, `docs`, `chore`, `sec`. Scopes name the area touched, e.g.:

```
feat(deploy): one-command docker/podman compose self-host
fix(loop): ...
docs(security): SECURITY.md threat model + correct the egress overclaim
feat(sheets): wire a real sheet download — declared-artifact jail
```

Keep the subject imperative and honest about scope. When a change spans the wire
contract (TS ↔ Python), run `make contract`. When it touches a surface the user sees,
attach the screenshot. Reference the design authorities when a change reinterprets
them.

## License

By contributing, you agree your contributions are licensed under the project's
**Apache License 2.0** ([`LICENSE`](./LICENSE)) — no CLA, no copyright assignment. And a
standing promise back to you: **the license will never change.** disco will not
be relicensed to a source-available or commercial license, so your contribution stays
Apache-2.0 forever (see the README's License section).

## Benchmarks against the field: sota-scan

`/sota-scan` (a project skill under `.claude/skills/`, vendored from
[MerlijnW70/sota-scan](https://github.com/MerlijnW70/sota-scan)) benchmarks this
repository against real projects in the same domain and writes a cited capability
matrix and a ranked gap list to `.sota/`. The exported scorecard is
`.sota/report.<domain>.md`; regenerate it with
`node development/sota-scan/scripts/sota-report.mjs --report` and verify it is
current with `--check`. See `development/sota-scan/VENDORED.md`.

## Repository research: DeepGit

`.mcp.json` registers [DeepGit](https://github.com/zamalali/DeepGit) (MIT), an
agentic GitHub research engine, as an MCP server for Claude Code sessions in this
repository, pinned to a commit and launched with `uvx`. It reads `GITHUB_API_KEY`
(a token with public-repo read access), `LLM_PROVIDER` (default `groq`) and the
matching provider key (`GROQ_API_KEY`) from your environment; nothing is stored in
the repository. Tools: `find_repositories`, `find_repositories_json`,
`deep_research`, `check_setup`. To give Disco's own Agent surface the same
capability, add it in **Settings → MCP** as a stdio server — see
`docs/self-host.md` § MCP servers.
