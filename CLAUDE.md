# CLAUDE.md — operating manual for AI agents working in this repo

This file is a **bootstrap**, not an authority. It captures verified repository
navigation, package layering, commands, durable gotchas, and event-sourcing
invariants — the things that aren't obvious from the code and cost real time when
missed. Humans: see [`CONTRIBUTING.md`](./CONTRIBUTING.md).

## Where authority actually lives

**Read [`docs/governance/README.md`](./docs/governance/README.md) first.** It is
the finite authority surface. The order, highest first:

1. **Current code + passing tests + live evidence** — beats every document.
2. [`docs/governance/ENGINEERING-STANDARDS.md`](./docs/governance/ENGINEERING-STANDARDS.md) — sealed.
3. [`docs/governance/ARCHITECTURE-BOUNDARIES.md`](./docs/governance/ARCHITECTURE-BOUNDARIES.md) — sealed.
4. [`docs/governance/CAMPAIGN-PLAN.md`](./docs/governance/CAMPAIGN-PLAN.md) — the current work order.
5. [`docs/governance/CURRENT-STATE.md`](./docs/governance/CURRENT-STATE.md) and
   [`CAMPAIGN-STATUS.md`](./docs/governance/CAMPAIGN-STATUS.md) — what is true right now.

The two sealed files are hash-gated by
[`scripts/check_governance_seal.py`](./scripts/check_governance_seal.py); an agent
cannot silently edit them.

Subject-matter design authority (below governance, above prose):
[`basis-of-design.md`](./basis-of-design.md),
[`event-state-contract.md`](./event-state-contract.md),
[`agent-loop-contract.md`](./agent-loop-contract.md),
[`llm-router-contract.md`](./llm-router-contract.md),
[`retrieval-grounding-contract.md`](./retrieval-grounding-contract.md),
[`security-analyzer-contract.md`](./security-analyzer-contract.md),
[`tool-sandbox-contract.md`](./tool-sandbox-contract.md).

> **Historical campaign prose is non-authoritative.** `archive/`,
> `docs/archive/`, and any dated handoff, "current status", findings, or
> work-order file are **history only**. Read them for a specific code-archaeology
> question — never as status, policy, or open-work authority. This file
> deliberately carries **no** campaign status and no second copy of the campaign.

## What this is

Disco is a self-hosted **Research + Agent** platform built as *one agent core
over an append-only event log*, engineered to stay reliable on local/open-weight
models. Monorepo: five Python packages under `packages/*` (uv workspace, Python
3.13, namespace `disco.*`) + a React/Vite/TS `frontend/` + a `harness/`.

## Package layout & the layering rule (enforced)

```
core  ←  retrieval  ←  tools  ←  { agent_server | app_server }
```

`agent_server` and `app_server` are independent top siblings (neither imports the
other — there's an import-linter contract proving it). Dependencies point
**downward only**. The live, auto-derived map is
[`docs/architecture.generated.md`](./docs/architecture.generated.md) (never hand-edit
it — regenerate). **Three** tracked upward-debt edges exist, all `tools →
agent_server` and all whitelisted in `.importlinter`'s `ignore_imports`; don't
add more:

- `disco.tools.builtin.audio_overview → disco.agent_server.audio_config`
- `disco.tools.builtin.audio_overview → disco.agent_server.tts_local`
- `disco.tools.builtin.preview → disco.agent_server.preview_manager`

- `core` — events, the loop engine (`AgentLoop`), LLM router, store.
- `retrieval` — deep research (search/extract/synthesis).
- `tools` — sandbox + builtin tools.
- `agent-server` — per-conversation runtime + `AgentLoop` driver, WS+REST on :8000.
- `app-server` — config/admin/library gateway the frontend calls, REST on :8800.

## Navigating the code (use the symbol graph, not grep)

This repo is wired for **Serena** — an LSP-backed code-navigation MCP server
(config in `.mcp.json`; it indexes all Python under `packages/*/src` into
`.serena/cache/`). When its tools are available (any Claude Code session started
in this directory, and the subagents it spawns), **prefer them over `grep`/`rg`
for symbol work**:

- `find_symbol` (go-to-definition by name path, e.g. `DefaultLLMRouter/complete`)
  — returns the one exact span. `grep "complete"` returns ~120 lines
  (`stream_complete`, `CompletionRequest`, `completed`, comments) you then have to
  Read and disambiguate.
- `find_referencing_symbols` — the true reference/call graph, each hit tagged with
  its *enclosing* function/class. `grep` gives flat line numbers with no structure
  and import/`__all__`/annotation noise mixed in.
- `get_symbols_overview` (a file's symbol tree), `find_implementations`,
  `find_declaration`, `rename_symbol`.

Why it matters here: names recur across the 5 packages (`run`, `complete`,
`record`, `execute`), so the win is fewer tool-calls + tokens + no wrong-symbol
disambiguation — exactly what bounds a subagent. Reach for `rg` for non-symbol
text (log strings, config keys, comments) and when Serena isn't loaded.

Note: `.mcp.json` is read at Claude Code **startup**, so adding/changing it only
takes effect next launch. First index build is slow (minutes); it's cached after,
warm symbol calls are sub-second.

## Running things

Use the venv interpreter directly — **`.venv/bin/python3 -m pytest`, not
`uv run pytest`**. (`uv run`'s cached entrypoint shebang can be stale and silently
runs the wrong interpreter.) Type-check and fitness gates go through `uv run`,
which is fine for those.

```bash
# Unit tests (what CI's required job runs). Integration tests need a real
# browser/shell/docker and are EXCLUDED by the marker — they are not hidden,
# they run in CI's advisory job.
.venv/bin/python3 -m pytest -m "not integration"
.venv/bin/python3 -m pytest packages/core packages/agent-server -m "not integration"

# The four architecture fitness gates (all required in CI + pre-commit):
uv run python scripts/check_arch_budget.py     # size caps — no god-objects
uv run lint-imports                            # layering + no cycles
uv run python scripts/gen_arch_diagram.py --check   # generated diagram is fresh
uv run basedpyright                            # strict: ZERO type errors tree-wide

# Frontend (from frontend/):
npm run test            # vitest
npm run typecheck:build # shipping-file tsc (excludes test files) — the honest gate
npx vite build          # production build
```

Before declaring any Python change done, run the four gates above **plus** the
relevant unit suites. A green vitest/pytest count is necessary but not sufficient
for UI work — see "Visual evidence" below.

## Gotchas that cost time

- **Exit code is truth, not the summary line.** pytest's final `N passed` line is
  buffered and can scroll off or be swallowed under `-q`/pipes. Check
  `EXIT=$?` / `${PIPESTATUS[0]}`, don't eyeball the tail.
- **basedpyright is strict with zero baseline.** The tree is at 0 errors and the
  baseline file was retired, so *any* new type error fails the gate. Fix types for
  real — do not add `# type: ignore`. Config-driven `uv run basedpyright` (no path
  args) reads `[tool.pyright]` in `pyproject.toml`; passing explicit paths changes
  what's included, so prefer the bare invocation.
- **Firefox, not Chromium, for screenshots.** Headless Chromium can't rasterize
  text/oklch on this host. Playwright projects are Firefox-only by design.
- **Env vars: `DISCO_<NAME>` with a `PMX_<NAME>` fallback.** Always read through
  `disco.core.env.disco_env("NAME")` (handles the legacy prefix + a one-time
  deprecation log). Ciphertext encrypted under the old `PMX_SECRET_KEY` still
  decrypts via the fallback — don't drop it.
- **Secrets never go on the wire or to disk in plaintext.** The MCP stdio launcher
  passes only an allowlisted base env to child servers (`tools/mcp/stdio.py`); the
  decrypted OpenRouter key is overlaid into the provider env per-request, never
  persisted. Never echo secret values.
- **No false affordances.** Don't ship UI that looks usable but isn't wired. Flag
  non-functional paths explicitly rather than leaving a dead button.
- **Something may `git stash` your uncommitted tracked-file edits.** Commit working
  changes promptly; new *untracked* files survive a stash, tracked edits don't.

## The fitness functions (what they enforce)

| Gate | File | Enforces |
|------|------|----------|
| Size budget | `scripts/check_arch_budget.py` | No class > 800 / function > 200 LOC, except a small whitelist of capped coordinators + dispatchers (`ALLOW_CLASSES`/`ALLOW_FUNCS`). Adding LOC to a capped item past its cap fails. |
| Tool schemas | `scripts/check_tool_schemas.py` | Built-in tool args must not advertise blind object params or arrays with untyped items; genuinely free-form JSON requires a justified allow entry. |
| Layering | `.importlinter` (`uv run lint-imports`) | The downward-only layering above + no package cycles + app/agent independence. |
| Diagram freshness | `scripts/gen_arch_diagram.py --check` | `docs/architecture.generated.md` matches the code's real (AST-parsed) imports. Re-run without `--check` to refresh after changing cross-package imports. |
| Types | `uv run basedpyright` | Zero type errors tree-wide. |

If you split or move code across package boundaries, you will likely need to
regenerate the diagram and re-check layering.

## Event-sourcing invariants

State is a **projection of an append-only event log**, never mutated in place.

- Events are appended; `View.of(...)` (and the store's `get_state`) folds them into
  the current view. Don't reach around the projection to mutate state.
- `EventKind` is a discriminated union — add a new kind by extending the union and
  its projection, and (if it crosses to the frontend) its TS mirror. The harness
  has a contract suite that flags Python↔TS event drift.
- The loop runs its turn body under `self._lock`; extracted helper methods must not
  re-acquire it. Decomposition preserved public APIs via thin delegators — keep that
  shape when refactoring further.

## Surfaces → loop mapping

Four surfaces exist: **research**, **build**, **agent**, **deep_research**.
Crucially, **the Agent surface IS the Build machinery** — `_BUILD_LIKE_SURFACES =
{"build", "agent"}` in `runtime.py`, so both use `BuildAgent` + the build loop
(plan→approve→execute, affirmative `finish` to terminate). Research/deep_research
use `ResearchAgent` (prose answer = completion, never gated). The *surface* picks
the agent class, so completion semantics are owned by type, not a shared flag.

## Proving the chain end-to-end (DISCO_INSPECT)

To prove a request travelled UI → loop → router → provider → back without grepping
logs, set `DISCO_INSPECT=1`. The runtime then captures, per conversation, every
routing decision (model/path/retries) and `agent.step` span (model + token counts),
readable over REST:

```
GET /api/debug/trace/{conversation_id}   # interleaved routing + span trace
GET /api/debug/inspect                   # enabled? + live conversation ids
```

Off → zero overhead (no handler, `NullRoutingSink`). Implementation:
`packages/core/src/disco/core/inspect.py`; the canonical test exercising the real
loop → trace → REST chain is `packages/agent-server/tests/test_inspect_trace.py`.

## Visual evidence for UI changes

End every UI-affecting change with a real screenshot of it working in the running
app (Firefox/Playwright) — a passing vitest count is not evidence that the feature
reached the screen. Build harnesses from VERBATIM captured real samples (pull from
the real service, inject through the common endpoint); use only real results for
verification, never hardcoded stand-ins.

## CI topology

- `.github/workflows/ci.yml` — `required` job: the four fitness gates + unit tests +
  frontend typecheck/build/vitest (deterministic, blocks merge). `advisory` job:
  full pytest incl. integration, eslint, full tsc (informs, never blocks).
- `.github/workflows/e2e-live.yml` — nightly + manual-dispatch live e2e on a
  self-hosted `disco-live` runner (real stack + real model + Firefox). Never on PR,
  never a gate, never faked on a hosted runner.
