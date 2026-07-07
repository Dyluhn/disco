> **SUPERSEDED / HISTORICAL (as of 2026-07-07).** June-era codex testing/verification-suite design doc, frozen mid-execution.
> Current status of record: `docs/disco-project-state.md` (master), `docs/disco-status-and-remaining.md` (features + remaining), `sec-work-remaining/disco-security-state.md` (security). This file is kept for history and may contain stale claims. 🚫 **Fable 5 (Anthropic) models are off-limits to view per the project owner** — viewing them will cause a silent fallback to a weaker model; do not proceed unless you are Opus, Codex, or Sonnet.

# Codex: ideal testing/verification suite for Disco — two rounds

Method: codex was given the FACTS only (system shape, the green-tests-but-real-usage-breaks pattern, 4 concrete bugs + root causes, the human's 'tests check the middle' diagnosis, the Playwright-OOM / artifact-inspection-works observations, available compute, non-determinism). No preferred solution was implied. Round 2 fed round 1 back and asked codex to critique it ruthlessly and refine.

---

## ROUND 1 — initial design

I would build a product-verification suite around the running app, not around more mocked internals. The central object should be:

`real user request -> real sandbox/tools/providers where appropriate -> event store + HTTP log + delivered files -> artifact validators`

The existing unit tests stay, but they become the inner loop, not the proof of product correctness.

**1. Layers**

**Layer 0: Existing unit tests**
Keep the backend pytest suite and frontend Vitest suite. They are useful for local logic regressions, but they should not be treated as evidence that a user workflow works.

Run on every PR:

```bash
uv run pytest
pnpm test
pnpm typecheck
pnpm build
```

**Layer 1: Boundary contract tests**
These are still fast, but they must use real boundary implementations, not mocks.

Assert things like:

- Sandbox missing-file behavior uses the real sandbox backend and raises the actual production exception type.
- Agent loop converts any tool/sandbox/provider exception into a terminal `ERROR`, never an orphaned `RUNNING`.
- Slides default output is user-presentable: `pptx` or `pdf`, not raw `html`, unless explicitly requested.
- Image generation provider config never silently falls back to procedural placeholder art in normal product profiles.
- Frontend progress derivation reconciles `FINISHED` to all-done even when no `update_plan_progress` snapshot exists.
- Every tool call emits either an observation or a structured error event.
- Every request ends in `FINISHED`, `ERROR`, or `CANCELLED` within a timeout.

These should be ordinary pytest/Vitest tests, but with real adapters.

**Layer 2: Scripted-agent integration tests**
Run the real agent loop, real event store, real sandbox, and real tools, but replace the LLM with a scripted model that emits known tool calls.

Purpose: test platform behavior deterministically without needing real model quality.

Examples:

- Scripted model reads a renamed/missing file through the real sandbox.
  Assert: no task death, request reaches terminal `ERROR` or tolerated recovery path, UI-visible error event exists.
- Scripted model invokes `slides_generate` without specifying format.
  Assert: produced deliverable is `pptx`/`pdf`, not `.html`.
- Scripted model requests image-backed slides with image provider disabled.
  Assert: tool fails clearly or omits images; it must not embed procedural placeholders as real deliverables.

This layer catches examples 2 and 4 without browser automation or real model nondeterminism.

**Layer 3: Product scenario tests**
This is the missing layer.

Create a scenario runner that talks to the same HTTP/API boundary as the frontend, using realistic input files and prompts. It should not call internal functions directly.

Example scenario file:

```yaml
id: slides_from_research_report
surface: agent
input_files:
  - fixtures/research_report.pdf
prompt: "Make slides from this research report"
provider_profile: real_or_local
timeout_seconds: 600
expect:
  terminal_status: FINISHED
  deliverables:
    - type: deck
      formats_any_of: [pptx, pdf]
validators:
  - event_store_integrity
  - no_orphan_running_request
  - deck_opens
  - deck_has_no_procedural_images
  - rendered_pages_nonblank
  - no_raw_html_unless_requested
```

The runner should archive a bundle per run:

```text
request_id/
  input/
  workspace/
  artifacts/
  event_store.sqlite
  http.log
  config.json
  stdout.log
  stderr.log
  verification.json
  rendered_pages/
```

On failure, the bundle is the debugging artifact.

**2. Real Input Boundary**

Use real fixtures, uploaded through the real request path.

Do not construct internal objects directly.

Representative fixtures should include:

- Small real PDF research report with images and headings.
- Markdown research report.
- Files with renamed/missing references.
- Nested project folder for Build.
- Small webpage/research fixture for Deep Research.
- Inputs with spaces, unicode, long filenames, and deleted files.

The point is not huge coverage. The point is that each user surface has at least one real end-to-end request that enters the product the way a user enters it.

**3. Real Output Boundary**

Every deliverable type gets a validator.

For decks:

- Locate produced files from event store/tool events.
- Assert default format is `pptx` or `pdf`.
- If `pptx`, unzip it and inspect `ppt/media/*`.
- Use `python-pptx` for slide count and relationship sanity.
- Use LibreOffice headless to convert to PDF on the off-box runner.
- Use `pdftoppm` to render pages to PNG.
- Use Pillow to assert images/pages are nonblank, have sufficient dimensions, reasonable entropy, and no known placeholder signature.
- Assert image provenance: no `provider=procedural`, no `placeholder=true`, no keyless fallback unless scenario explicitly allows it.
- Grep/parse HTML decks for `<img>` tags and base64 images; decode and validate them.

For PDFs:

- `pdfinfo`: page count > 0.
- `pdftotext`: expected topic words present.
- `pdftoppm`: pages render.
- Pillow: not blank, not all one color, no broken-image markers.

For audio:

- `ffprobe`: duration, codec, sample rate.
- Check non-silent waveform.

For sheets:

- `openpyxl`: opens cleanly.
- Expected sheet count/columns.
- No formula error cells.

For Build outputs:

- Run generated project’s install/build/test command where applicable.
- Assert expected entry files exist.
- Browser rendering can be off-box, not local.

**4. Non-Determinism**

Do not assert exact action sequences or golden outputs for real-agent runs.

Assert invariants:

- Request reaches terminal status.
- No uncaught loop task exception.
- No orphaned tool call.
- Every emitted deliverable exists and is readable through the sandbox.
- Delivered format matches user expectation.
- Artifacts open with standard tools.
- No placeholder/provider fallback leakage.
- Required semantic content appears somewhere in the output.
- Event store and HTTP log are internally consistent.

For model runs, use tiers:

- PR gate: scripted model plus deterministic/local provider profile.
- Nightly: real local model and real provider keys.
- Release gate: repeated real scenarios, including visual/artifact validation.
- Canary/soak: repeated runs, tracked as pass-rate metrics.

A real-model scenario should not depend on exact trajectory. It should pass if the user-visible outcome is acceptable.

**5. Visual / Subjective Outputs**

Avoid pixel-perfect goldens.

Use a combination of mechanical checks and rubric checks:

- Render final artifacts to PNG.
- Check blankness, entropy, dimensions, contrast, corrupted images, and placeholder signatures.
- Optionally run OCR to confirm requested headings/content are visible.
- Optionally run a VLM judge off-box with a strict rubric, for example:
  - “Is this a presentable slide deck?”
  - “Are any images abstract placeholder graphics?”
  - “Are there broken image icons?”
  - “Does the deck appear related to the research report?”

The VLM judge should be advisory or nightly/release-gating, not the only PR signal.

**6. Local vs Off-Box**

Run locally:

- Backend unit tests.
- Frontend Vitest/typecheck.
- Boundary contract tests.
- Scripted-agent integration tests.
- API-level product scenarios that do not need Chromium.
- Direct artifact inspection: unzip, parse, `pdftoppm`, Pillow, sqlite checks.

Run on the homelab/off-box runner:

- Playwright against the heavy SPA.
- LibreOffice conversions.
- OCR/VLM visual judging.
- Real model/provider scenarios.
- Repeated nondeterministic scenario packs.
- Longer Build/Deep Research workflows.

Given the workstation’s Chromium OOM behavior, browser automation should not be a local gate.

**7. CI Shape**

PR CI:

```text
1. lint/typecheck/unit
2. boundary contracts
3. scripted-agent scenarios
4. cheap artifact validators
5. upload verification bundles on failure
```

Nightly self-hosted CI:

```text
1. real local model scenario matrix
2. real provider-key scenario matrix
3. deck/pdf/audio/sheet artifact validation
4. Playwright smoke on homelab VM
5. pass-rate trend report
```

Release CI:

```text
1. full scenario pack
2. repeated runs for nondeterministic workflows
3. off-box browser tests
4. visual artifact validation
5. archived evidence bundle per scenario
```

Hard failures should include platform invariants: stuck `RUNNING`, uncaught task exception, missing deliverable, corrupt artifact, placeholder provider leakage.

Model-quality failures can be tracked as pass-rate/SLO unless the workflow is release-critical.

**8. Agent Pre-Done Verification**

Add a repo-level verification map, for example:

```yaml
slides:
  paths:
    - "packages/*/slides/**"
    - "packages/*/image_gen/**"
  commands:
    - "uv run pytest -m boundary"
    - "uv run disco-verify run slides_from_research_report --mode scripted"
    - "uv run disco-verify artifacts last"

agent_loop:
  paths:
    - "packages/*/agent*/**"
    - "packages/*/sandbox/**"
  commands:
    - "uv run pytest -m agent_loop"
    - "uv run disco-verify run missing_file_sandbox_error --mode scripted"
```

Before an AI agent says “done”, it should run the mapped checks for touched areas, inspect failures, fix, and rerun. If a required browser check only runs off-box, the agent should say that explicitly and provide the local evidence it did run.

**9. Highest-Leverage First Piece**

Build one tool first:

```bash
uv run disco-verify run slides_from_research_report
```

It should:

- Start the real app/backend test profile.
- Submit “Make slides from this research report” through the real API.
- Wait for terminal status with a hard timeout.
- Read the sqlite event store.
- Locate produced deliverables.
- Fail if the output is raw HTML by default.
- Fail if any image came from the procedural provider.
- Decode embedded images and reject known placeholder/low-quality signatures.
- Render final PDF/pages via `pdftoppm`.
- Save a verification bundle.

That single scenario would have caught failures 1, 2, and 4, and the same harness pattern can then be reused for Build, Agent, Deep Research, PDFs, audio, and sheets.

---

## ROUND 2 — codex's ruthless self-critique + refined final design

**Ruthless Critique**

The round-1 design is directionally right, but still too vague in the places where this system actually fails.

- API-only product scenarios would not have caught the progress bug. That was a frontend derivation failure, not a backend workflow failure. You need event-log replay tests against the real frontend state derivation.
- “Scripted model” can become another mock if it bypasses the exact LLM adapter/tool-call protocol. It must emit through the same message/streaming/tool-call boundary the real model uses.
- Entropy/nonblank image checks are weak. The procedural placeholder images may be colorful, nonblank, high-entropy, and valid PNGs. Provenance checks are mandatory.
- “Real provider profile” is underspecified. CI needs named profiles with explicit allowed fallbacks, key requirements, cost limits, and failure behavior.
- LibreOffice, OCR, VLMs, and Playwright are slow/flaky enough that they cannot be normal PR gates.
- Uploading full event stores and HTTP logs is risky. Those bundles may contain prompts, file contents, provider responses, secrets, or local paths. Redaction must be part of the harness.
- “Semantic content appears somewhere” is too weak. A deck with one correct word and broken visuals could pass.
- “Every request ends terminal within timeout” only works if the app has a crash supervisor that converts task exceptions into durable terminal state. Tests alone cannot infer a killed background task cleanly.
- The scenario YAML does not define isolation, app config, provider config, fixture seeding, time budget, retry policy, artifact retention, or validator versions.
- VLM judging can create false confidence. It is useful as a release/nightly signal, not as the only oracle.
- Browser automation is correctly deprioritized, but the design still leaves UI correctness undercovered unless captured event streams are replayed through frontend reducers/selectors.

**Refined Final Design**

Build `disco-verify` as the product verification harness. Its core contract is:

```text
real user/API request
-> real app process
-> real sandbox/tools/event store/http log
-> delivered artifacts
-> invariant + artifact validators
-> redacted evidence bundle
```

Do not treat this as “more tests.” Treat it as an executable product oracle.

**Layer 0: Existing Tests**

Keep current pytest/Vitest coverage.

Run on PR:

```bash
uv run pytest
pnpm test
pnpm typecheck
pnpm build
```

These remain inner-loop regression tests only.

**Layer 1: Startup And Boundary Contracts**

Fast PR-gated tests using production-shaped adapters.

Assert:

- Production/default slides output is `pptx` or `pdf`, never `html`.
- Normal product profiles cannot silently select `procedural` image generation.
- Sandbox missing-file behavior raises the actual production `SandboxError`.
- Agent loop catches `BaseException`-safe operational failures where appropriate and records terminal `ERROR`.
- Every tool call produces either an observation event or structured error event.
- Background task crashes are captured by a supervisor and persisted as request `ERROR`.
- Request status cannot remain `RUNNING` after loop task termination.

Add static checks too: flag sandbox call sites that only catch `OSError`/`FileNotFoundError` without also handling sandbox exceptions.

**Layer 2: Deterministic Agent Integration**

Use a scripted model only at the LLM adapter boundary. It should emit the same streamed/tool-call payload shape as the real model, not call internal functions.

PR scenarios:

```text
missing_file_sandbox_error
slides_default_format
slides_image_provider_disabled
tool_exception_terminalizes_request
```

Assertions:

- terminal status is `FINISHED`, `ERROR`, or `CANCELLED`
- no orphan `RUNNING`
- no uncaught task exception
- no orphan tool call
- user-visible error exists when the run fails
- deliverables, if emitted, are readable through the real sandbox

This catches platform failures without relying on model quality.

**Layer 3: Event-Store Replay For Frontend**

Create canonical event-store fixtures from real runs and feed them into frontend selectors/reducers/derivers in Vitest.

Include:

```text
finished_without_plan_snapshot
running_then_error
tool_error_visible
finished_with_partial_plan_updates
cancelled_request
```

Assert:

- finished request with no progress snapshot reconciles to all steps complete when appropriate
- UI-visible status matches backend terminal state
- errors are surfaced
- progress does not regress after terminal events

This directly covers failure #3 without Chromium.

**Layer 4: API Product Scenarios**

Run through the same HTTP/API boundary the frontend uses. No internal function calls.

Example scenarios:

```yaml
id: slides_from_research_report
mode: scripted_or_real_model
profile: product_test_no_placeholder
input_files:
  - fixtures/research_report.pdf
prompt: "Make slides from this research report"
timeout_seconds: 600
expect:
  terminal_status: FINISHED
  deliverable_type: deck
  formats_any_of: [pptx, pdf]
forbid:
  - raw_html_default
  - procedural_image_provider
  - placeholder_image_signature
```

Each run produces:

```text
bundle/
  inputs/
  artifacts/
  event_store.sqlite
  http.log.redacted
  config.json
  validator_versions.json
  stdout.log
  stderr.log
  verification.json
  rendered_pages/
```

Bundles are redacted before upload.

**Artifact Validators**

Decks:

- locate final user-visible deck from event/tool events
- reject `.html` unless explicitly requested
- open `pptx` as zip
- inspect `ppt/media/*`
- verify image provenance metadata: no `provider=procedural`, no `placeholder=true`
- decode embedded/base64 images with size limits
- reject known placeholder signatures
- validate slide count and relationships with `python-pptx`
- render to PDF/PNG off-box with pinned LibreOffice + Poppler
- assert pages are renderable, nonblank, correctly sized, and contain expected topic text via `pdftotext`/OCR where useful

PDFs:

- `pdfinfo` page count > 0
- `pdftotext` contains scenario-required terms
- `pdftoppm` renders pages
- Pillow checks blank/corrupt pages

Audio:

- `ffprobe` duration/codec/sample rate
- waveform is not silent

Sheets:

- `openpyxl` opens file
- expected sheets/columns exist
- no formula error cells

Build outputs:

- expected entry files exist
- install/build/test commands run in isolated temp workspace
- browser rendering only on off-box runner

**Non-Determinism Policy**

Do not assert exact model trajectories.

Assert product invariants:

- terminal request state
- durable error on failure
- readable deliverables
- expected output class/format
- no placeholder/provider leakage
- scenario-specific required content
- event store and HTTP log consistency

Use tiers:

```text
PR: unit + boundary + scripted agent + event replay + cheap artifact checks
Nightly: real local model + real provider keys + artifact rendering
Release: repeated real scenarios + visual/OCR/VLM review + Playwright smoke
Soak: pass-rate trends across repeated nondeterministic runs
```

Retries classify flakiness; they do not hide failures.

**Local Vs Off-Box**

Local workstation:

- pytest/Vitest/typecheck/build
- boundary contracts
- scripted agent scenarios
- event-store replay
- sqlite/http-log checks
- unzip/parse/Pillow/Poppler checks when available
- no Chromium gate

Homelab/self-hosted runner:

- Playwright smoke only
- LibreOffice conversions
- OCR/VLM judging
- real provider-key scenarios
- local-model scenario matrix
- repeated nondeterministic workflows

Pin validator containers: Python, LibreOffice, Poppler, fonts, Node, browser version.

**Agent Pre-Done Loop**

Add a repo verification map:

```yaml
slides:
  paths: ["**/slides/**", "**/image_gen/**"]
  commands:
    - "uv run pytest -m boundary"
    - "uv run disco-verify run slides_default_format --mode scripted"
    - "uv run disco-verify run slides_from_research_report --mode scripted"

agent_loop:
  paths: ["**/agent*/**", "**/sandbox/**"]
  commands:
    - "uv run disco-verify run missing_file_sandbox_error --mode scripted"

frontend_progress:
  paths: ["frontend/**/progress*", "frontend/**/events*"]
  commands:
    - "pnpm test progress-event-replay"
```

Before claiming done, the AI agent runs mapped checks for touched areas and reports any off-box checks it could not execute locally.

**Highest-Leverage First Piece**

Do not start with Playwright or VLMs.

Build `disco-verify` plus three regression scenarios first:

```bash
uv run disco-verify run slides_from_research_report --mode scripted
uv run disco-verify run missing_file_sandbox_error --mode scripted
pnpm test progress-event-replay
```

That first slice directly targets failures #1, #2, #3, and #4 while staying deterministic, cheap enough for PRs, and grounded at the real input/output boundaries the current suite misses.

---

## Prompt facts given to codex (round 1)

# Question
Given the facts below, how exactly would you produce the ideal automated testing / verification suite for this system? Be concrete and specific (architecture, layers, tooling, what to assert on, what to run where, CI shape). These are facts/observations only — no preferred solution is implied.

# System facts
- "Disco" is a local-first agentic LLM application. Backend: a Python uv monorepo of 5 packages (core ← retrieval ← tools ← {agent_server, app_server}). Frontend: React + TypeScript + Vite. It runs an LLM agent loop that, given a user request + a toolbox (file ops, shell, browser, slides_generate, image_gen, deep-research, etc.), takes non-deterministic actions; the same request produces different action sequences across runs. Surfaces: Build, Agent, Deep Research, plus deliverables (slide decks, PDFs, PPTX, audio, sheets).
- There are roughly 1800 backend unit tests plus frontend vitest tests. They are essentially all green at all times.

# Observed failure pattern (from a human using the running app)
Despite green tests, a human using the app finds breakages within minutes of normal use. Concrete examples from one session:
1. "Make slides from this research report" produced a deck whose image elements rendered as garish abstract placeholder graphics (nested colored rectangles + a diagonal line). Root cause: the deck pipeline auto-includes image elements; the image-generation provider defaulted to a keyless "procedural" backend that deterministically draws placeholder art; those placeholders were embedded as real base64 PNGs and shipped in the deck. Every internal unit was green (the embed embeds, the renderer renders, the procedural backend draws) — but no test asserted "real request in -> delivered deck has no garbage images."
2. A build run silently hung: status stayed RUNNING forever with the model idle and no UI error. Root cause: a sandbox file read raised SandboxError (a renamed file), which subclasses Exception (NOT the native OSError/FileNotFoundError hierarchy). ~8 call sites across the loop already had `except (FileNotFoundError, OSError, ...)` handlers intending to tolerate missing files — none catch SandboxError, so against the real sandbox backend they are all dead code; the exception escaped, killed the loop task, and nothing converted the crash into a terminal ERROR status.
3. A plan-progress checklist showed 0/4 steps complete on a finished local-model build. Root cause: the frontend deriver early-returned an empty map when there was no "update_plan_progress" snapshot (which small models never emit), skipping the FINISHED->all-done reconciliation. Unit tests existed but only covered the with-snapshot path.
4. The slides tool's output format defaulted to "html", so the agent shipped a raw .html file rather than a presentable deck/pptx.

# The human's articulated diagnosis
The unit tests check the MIDDLE — internal transformations between mocked inputs and assertions on intermediate data structures — and to make a unit testable, both the real input and the real output are replaced with mocks/fixtures. So the suite is, by construction, blind to every real input and every real output, which are the two things the user actually cares about. Coverage of the interior can be total and prove nothing about input->delivered-output correctness. Adding more such tests does not help because they all point inward.

# Environmental / practical constraints (observations)
- Browser automation (Playwright + headless Chromium) was attempted repeatedly to capture rendered output and failed repeatedly: the workstation caps each command in a memory cgroup (~16 GB) and Chromium gets OOM-killed on the heavy SPA; also frequent timing flakiness (TargetClosedError, font-load races, networkidle vs domcontentloaded).
- The verifications that DID work reliably in this environment were direct artifact inspection: rendering a generated PDF to PNG via pdftoppm; grepping a generated deck's HTML for <img> tags and checking the active image-provider config. (Stated as observation, not recommendation.)
- Available compute: the workstation (memory-capped per command, GPU often held by a local LLM server), plus a homelab with a sandbox VM and other Linux hosts reachable over Tailscale (more RAM, no per-command memcap).
- The agent is non-deterministic, so exact-output golden files are not always stable across runs; outputs are often visual/subjective (does a deck "look right").
- Real provider keys + real local models are available; the system already has: a structured event store (sqlite) capturing every agent action/observation, a per-request HTTP log, and tools that emit deliverables as files.

# Deliverable
Describe the testing/verification architecture you would build, end to end. Be specific about: the layers and what each asserts on; how to handle the real-input and real-output boundaries the current suite mocks away; how to deal with non-determinism and visual/subjective outputs; what to run locally vs off-box; how it plugs into CI and into an AI agent's own pre-"done" verification loop; and what the highest-leverage first piece is.
