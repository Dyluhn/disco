# Contributing to perpleximanus

A self-hosted Research + Agent platform built as one agent core over an append-only
event log. Before you start, skim the two design authorities — when code and prose
disagree, they win:

- [`basis-of-design.md`](./basis-of-design.md) — the cornerstone design document.
- [`event-state-contract.md`](./event-state-contract.md) — the spine's binding contract.

There is no hosted CI yet; the `Makefile` *is* the runner. Run it locally before you
push.

## Prerequisites

- **[uv](https://docs.astral.sh/uv/)** — the workspace/dependency manager (this is a
  `uv` workspace; do not use bare `pip`).
- **Python 3.12+** — every package declares `requires-python = ">=3.12"`.
- **Node** for the frontend — the Vite/React/TS app; the frontend pins
  `@types/node ^22`, so Node 22 LTS is the safe target.
- **A container runtime** (Docker or rootless Podman) for the sandbox-backed tests and
  for running the Build/Agent surface for real. The fully hermetic `make test` path
  does not need one (it uses the `process` backend on the host); the integration tests
  and live runs do.

First run also fetches a few one-time artifacts that the dev deps drive: Playwright's
Chromium (`playwright install chromium`) for the browser-daemon integration test, and
the fastembed ONNX encoders on the first grounded answer.

## Getting set up

```bash
uv sync --all-packages     # provision the venv + install every workspace member
```

Optional extras live on the **member** packages, not the workspace root, so install
them with `--package`. A bare `uv sync --extra tts` errors (the root defines no `tts`
extra):

```bash
# Audio overviews — bundled in-process Kokoro TTS (weights download on first use):
uv sync --package perpleximanus-agent-server --extra tts
```

### Running the servers

Two FastAPI/uvicorn servers share one SQLite event store (`PMX_DB`, default
`./perpleximanus.db`). Run each as a module:

```bash
# Agent-server — per-conversation runtime (WebSocket + REST).
# Env: PMX_HOST (default 127.0.0.1), PMX_PORT (default 8000), PMX_DB.
#      PMX_SANDBOX (process|local|gvisor|podman) optionally forces a sandbox backend;
#      unset → the persisted Settings selector decides per request.
uv run python -m perpleximanus.agent_server

# App-server — settings + library gateway the frontend calls.
# Env: PMX_HOST (default 127.0.0.1), PMX_PORT (default 8800), PMX_DB.
uv run python -m perpleximanus.app_server
```

> Note: `.env.example` / the compose deploy use different default ports
> (`PMX_AGENT_PORT=8000`, `PMX_APP_PORT=8800`, UI on `8088`). The module-level
> defaults above are the bare-process dev defaults; see
> [`docs/self-host.md`](./docs/self-host.md) for the compose story.

### Running the frontend

```bash
cd frontend
npm install
npm run dev      # the web UI — fixtures unless VITE_API_BASE points at a live app-server
```

## Running tests

The suite splits into a **hermetic** half (fast, offline, no LLM, no network) and a
**live** half (drives real services/models). Keep the hermetic path fast and offline.

```bash
make test          # unit + harness — the fast hermetic gate (run this before pushing)
make unit          # per-package unit suites only (packages/*/tests) — `uv run pytest`
make harness       # all harness/ tests
make contract      # TS <-> Python wire/event contract drift
make fuzz          # property-based parser fuzzing (hypothesis)
make fault         # fault/chaos injection
```

Or invoke pytest directly: `uv run pytest`. Async tests run without per-test
decorators (`asyncio_mode = "auto"`); `testpaths = ["packages"]`.

Live/heavy targets (need real config, secrets, or a running instance — kept out of
`make test` on purpose):

```bash
make eval          # research eval, REPLAY mode (needs a cassette — run `make capture` first)
make eval-real     # research eval against REAL services (slow)
make verify        # `pmx verify`: does your configured model drive the loop? (ARGS=--quick)
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
make lint          # uv run ruff check packages harness
make fmt           # uv run ruff format packages harness
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

`perpleximanus` is a PEP 420 namespace package — every member shares the
`perpleximanus.*` namespace. **Do not create up-edges.** `core` must never import from
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
standing promise back to you: **the license will never change.** perpleximanus will not
be relicensed to a source-available or commercial license, so your contribution stays
Apache-2.0 forever (see the README's License section).
