> **SUPERSEDED / HISTORICAL (as of 2026-07-07).** June-era evidence-harness campaign tracker, frozen mid-execution.
> Current status of record: `docs/disco-project-state.md` (master), `docs/disco-status-and-remaining.md` (features + remaining), `sec-work-remaining/disco-security-state.md` (security). This file is kept for history and may contain stale claims. 🚫 **Fable 5 (Anthropic) models are off-limits to view per the project owner** — viewing them will cause a silent fallback to a weaker model; do not proceed unless you are Opus, Codex, or Sonnet.

# Disco Evidence-Harness Campaign — autonomous build

**Authorized by Dylan, 2026-06-21, full autonomy granted.** This is the durable plan of
record. I (Claude) consult and update this file continuously to stay accurate. The
synthesized plan below IS the bar — no skipping because something is hard.

## The directive (verbatim intent)
1. **Repurpose VM 201 (`sandbox`, 100.81.82.115) as the Playwright browser host.** All
   Playwright runs go there (7.8G RAM, no memcap) — never the workstation. Reach it via
   `ssh 100.81.82.115` (or `ssh blackbox` → the Proxmox host). The workstation services
   bind to 127.0.0.1, so a **reverse SSH tunnel** exposes them on VM 201's localhost (no
   workstation rebinding).
2. **Synthesize** the three plans (codex round 1, codex round 2, the evidence-harness
   design) into one refined plan. Build it **surgically** once refined.
3. **Submit the synthesized plan to codex for review; refine until solid.**
4. **Fan out MiniMax (via Pi) + Sonnet (via Agent) subagents** to implement, in parallel
   where possible. The plan is the bar. No skipping. If a plan item provides real value and
   solves our issues, do it. No excuses.
5. **Verify rigorously as items complete** — typecheck, lint, tests, real artifact checks.
6. **When the campaign is complete, BEFORE committing, have codex review the implementation.**
   If it surfaces issues, fix and resubmit. Repeat **until codex is splitting hairs.**
7. **Then exercise it ALL** — every single item — and **save all evidence** for Dylan.
8. Save this campaign + the 3 source plans + the synthesized plan to **memory and this
   markdown**. Continually reference this file.

## Operating rules (self-imposed, from Dylan's standing feedback)
- No false "done": every claim backed by a real artifact (the harness's own dossier).
- Verify at the real input/output boundary, not the mocked middle.
- Playwright ONLY on VM 201; the workstation does artifact inspection + non-browser checks.
- Maximize parallel agents; never leave workers idle; active monitoring heartbeats.
- Defer nothing as "too hard." Figure it out.

## Phase tracker
- [x] P0a — VM 201 reachable (ssh OK), Playwright install kicked off
- [x] P0b — VM 201 Playwright PROVEN (threejs deck that OOM'd locally rendered on VM 201, screenshot returned)
- [x] P0c — reverse tunnel PROVEN (VM201 localhost:5173/8000 → live workstation app, both 200)
- [x] P1  — campaign doc created with 3 source plans
- [x] P2  — synthesized plan written
- [x] P3  — codex reviewed (3 flaws fixed: test-first W18, W1/W10 reconcile, control-classification); refined below
- [~] P4  — fan-out IN PROGRESS: W4(sonnet⟳) W8(sonnet⟳) W18(DONE) | next: W1(me), W9/W14a after deps
- [ ] P5  — codex review of implementation (loop until splitting hairs)
- [ ] P6  — exercise EVERYTHING via the harness; save all evidence
- [ ] P7  — final memory + doc update; commit (after codex passes)

## Evidence index
(populated as runs complete — links to dossiers under `test-record/e2e-full/<run-id>/`)

---

# SOURCE PLAN 3 — the evidence-harness design (Dylan-provided)

The ideal harness is a full-stack "evidence harness," not just more Playwright tests.
Foundation already in the repo (VERIFIED present 2026-06-21): both Playwright configs
(`playwright.config.ts` fixture + `playwright.live.config.ts`), ~15 e2e specs in
`frontend/e2e/`, append-only event logs, WS streams, `/api/debug/trace/{cid}` behind
`DISCO_INSPECT=1` (`debug.py:42`), artifact/download routes, stampable event `meta` dict.
Missing piece = ONE unified runner tying: UI action → network/WS frame → conversation_id →
event log → router/span trace → sandbox/tool output → visible UI/output artifact, into one
readable run folder.

## Target layout
```
frontend/e2e-full/
  full.config.ts
  fixtures/discoHarness.ts
  scenarios/{research,deep-research,build,agent,settings,projects-history-share}.full.spec.ts
  support/{recorder,uiInventory,wsRecorder,artifactCollector,assertions}.ts
test-record/e2e-full/<run-id>/
  index.html  timeline.md  manifest.json
  ui-actions.jsonl  controls-discovered.json  controls-hit.json
  network.jsonl  websockets.jsonl  console.jsonl  page-errors.jsonl
  conversations/<cid>/{events.jsonl,state.final.json,inspect-trace.json,stream.frames.jsonl,workspace-manifest.json,artifacts/,screenshots/,preview/}
  playwright-trace.zip  screenshots/  downloads/
```
Principle: every step leaves a durable, human-readable evidence trail. A person opens
`timeline.md`/`index.html` and sees exactly what happened without rerunning.

## What it proves per interaction (10 records)
1 UI control discovered · 2 UI action performed · 3 HTTP/WS frame caused · 4 conversation_id
captured · 5 event-log rows appended · 6 state transition observed · 7 DISCO_INSPECT
router/span trace observed · 8 sandbox/tool/file side effect observed · 9 final UI renders
the result · 10 artifact saved to a readable folder.

## Three-layer harness
1. **Browser driver** — Playwright as the ONLY UI driver; accessible locators
   (getByRole/getByPlaceholder/getByLabel). A `disco.click(locator, {control, expectWs,
   expectStatus})` wrapper: record before-screenshot + accessible name/role/selector/route/
   disabled, perform, after-screenshot, wait for declared backend evidence, append to
   `ui-actions.jsonl`. Click-level provenance, not "clicked something, assertion passed."
2. **Transport recorder** — listen on request/response/console/pageerror/websocket/download;
   record WS `framesent`/`framereceived` (this app is WS-heavy); extract conversation_id
   from WS url/payload. HTTP: method/url/status/redacted-body/headers (no secrets).
3. **Disco evidence collector** — after each meaningful action + at scenario end, call the
   app's own truth endpoints: `/conversations/{cid}/events`, `/state`,
   `/api/debug/trace/{cid}` (DISCO_INSPECT=1), `/api/projects/{cid}/manifest`, `/sessions`,
   `/deck/editor`, `/browser/live-url`, `/workspace/{path}`,
   `/api/conversations/{cid}/report/export`. DISCO_INSPECT mandatory in full e2e mode.

## Correlation instrumentation (small product change)
- HTTP/WS headers: `X-Disco-Harness-Run: <run_id>`, `traceparent: 00-<trace>-<span>-01`.
- WS: `/ws/conversations/{cid}?last_seq=0&harness_run_id=<run_id>&traceparent=<tp>`.
- Stamp event meta: `{harness_run_id, trace_id, ui_action_id}`.

## Control inventory (test every surfaced affordance)
Enumerate at each stable screen state: `button, a[href], input, textarea, select,
[role=button|switch|radio|tab|menuitem|textbox], [data-testid]`. Record route/surface/state/
role/name/enabled/visible/test_id/control_id. Maintain a hit map {seen, clicked}. End of
suite: **FAIL if any enabled, visible, user-facing control was discovered but never tested**,
unless explicitly allowlisted with a reason.

## Modes
- `fixture-full`: deterministic, no network/model, existing fixtures/cassettes.
- `live-full`: real agent-server, real model, real sandbox, DISCO_INSPECT=1. (Playwright
  webServer launches the UI/server pair.)

## Scenario matrix (cover every control on every surface) — abbreviated
- **Search**: submit, streamed token/block/final, cited tab, source tabs, deny-domain,
  drop-weak, re-scope, model/think toggle, attach, sheet/chart/slides/code blocks, theme.
  Evidence: /ws/research frames, tokens-before-final, final JSON+md, non-empty citations,
  heading matches, no false download affordances.
- **Deep Research**: scope→DR, attach, submit, drafting skeleton, plan gate, request/edit
  plan, approve, stop, resume, mid-run steer, inject source, final report, source tiers,
  support/contradiction meter, follow-up, export MD/PDF/DOCX, audio overview modes, "Build a
  deck" handoff. Evidence: cid captured, plan/status/report/citation events, debug trace
  routing+spans, export files readable, PDF renders, disabled states honest.
- **Build**: mode switch, model picker, autonomous toggle, assist toggle, task submit,
  clarify gate, plan gate, reject/replan, risky confirm approve+reject, ask-gate answer,
  kill arm/cancel/confirm, pause/stop/resume, replan-after-finish, files pane, terminal
  pane, preview rendered + live-server + refresh + bound-port tabs, schedule
  create/preview/save/delete, artifact downloads, deck editor. Evidence: WS frames
  (send_message/request_plan/approve_plan/confirm/reject/cancel/resume), monotonic event log
  + correct terminal state, file_stream frames, sandbox session output, workspace manifest,
  every artifact downloaded, preview screenshot+iframe url, done steps checked in UI+trace.
- **Agent**: same as Build but verify surface=="agent", deck/report handoff lands on
  /agent/:cid, task framing, MCP approval flows, general artifacts visible.
- **Settings**: model matrix add/delete, provider key save/delete + redaction checks,
  OpenRouter catalogue fetch/error, sandbox config, project-storage path picker, encoders,
  data sources, image-gen, audio, MCP add/approve/disable/delete, skills add/delete.
  Evidence: each save → expected HTTP call, secrets never logged, persisted non-secret state
  on reload, disabled/unwired controls show honest status.
- **History/Projects/Share/Import/Resume**: conversation list, project list, reopen
  /build|/agent|/deep/:cid, imported read-only, share create/revoke/import, reload reconnect
  w/ last_seq, WS reconnect replays history-then-live. Evidence: old events replayed w/o
  dup, imported rejects revive/kick, share scrubbed, resume same cid+workspace+artifacts.

## Assertions that matter (FOUR truths per scenario)
1 **UI truth** (control/status/message/output/artifact visible) · 2 **Wire truth** (expected
HTTP/WS happened) · 3 **Event-log truth** (append-only log has expected sequence,
reconstructs to expected state) · 4 **Output truth** (downstream output exists, is readable,
matches the UI claim). Helpers: `disco.expectEventSequence`, `disco.expectInspectTrace`,
`disco.expectArtifactReadable`.

## Readable output: `timeline.md` (+ `index.html` rendering same data with links)
Per-step: screenshot, control role/name, WS sent, events appended (seq), state, inspect
routing.

## Minimal product instrumentation to add
1 harness_run_id + traceparent on HTTP+WS · 2 ui_action_id in WS frames/event meta · 3
stable `data-disco-control` ids on controls lacking strong accessible names · 4 debug-only
`/api/debug/evidence/{cid}` bundling state+events+inspect+manifest+sessions+preview · 5
frontend `window.__DISCO_E2E__ = {currentSurface, currentCid, controls}`. Gate behind
`DISCO_E2E=1 DISCO_INSPECT=1`, never prod-default.

## Runner commands (Make targets + package scripts)
`e2e-full-fixture`, `e2e-full-live` (DISCO_INSPECT=1 DISCO_E2E=1 PMX_LOG_JSON=1),
`e2e-full-report`.

## "Done" means a failed run answers (without rerunning): what clicked / what the browser
sent / which cid / what the server appended / what model-router path / what tool/sandbox ran
/ what artifact produced / what UI showed / where the screenshot/download/trace proving it is.

---

(Codex round 1 and round 2 plans appended below this line by the build script.)

# SOURCE PLAN 1 — codex round 1

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

# SOURCE PLAN 2 — codex round 2 (self-critique + refined)

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

# SYNTHESIZED PLAN — the bar (merge of all three)

**Architecture decision (reconciling the three):** the spine is codex's LAYERING; the
evidence/UI layer is the evidence-harness design's four-truths + dossier + control-inventory
+ correlation; Playwright is kept but **runs ONLY on VM 201** and is **off the PR gate**
(per codex r2 + Dylan's directive + the empirical memcap failures). PR-gate frontend
coverage comes from the **event-store REPLAY** layer (vitest, no browser), which is what
would have caught the #3 progress bug deterministically. Playwright on VM 201 is the
live-full / visual last-mile and the control-inventory coverage proof.

Two halves: (1) PRODUCT fixes the harness must assert on (and which are the real bugs), and
(2) the HARNESS itself. The campaign is "done" when the harness is green AND it exercises
every control on every surface with saved evidence.

## Work items (W#) — [group] deps → verify

### Group A — product instrumentation + the real-bug fixes (backend/py; mostly parallel)
- **W1 [A] SandboxError typing.** At the sandbox boundary (`packages/tools/.../sandbox/`),
  map failures onto native types: missing file → `SandboxFileNotFoundError(SandboxError,
  FileNotFoundError)`; timeout → also-`TimeoutError`; permission → also-`PermissionError`;
  dead sandbox → `SandboxUnavailableError(SandboxError)`. The 8 existing
  `except (FileNotFoundError, OSError, ...)` handlers then work. Verify: unit test that a
  real (Docker/local) sandbox missing-file read is caught by each of the 8 sites; ruff/pyright.
- **W2 [A] kick() crash supervisor.** `runtime.py:kick` → `add_done_callback` that, on an
  unhandled task exception, emits `StatusEvent(ERROR)` + a system-reminder and never leaves
  RUNNING. Verify: unit test — an exception in `loop.run()` yields terminal ERROR + a
  user-visible event. (Prereq for the "every request terminalizes" invariant.)
- **W3 [A] Slides correctness.** Default `slides_generate` to a presentable format
  (pptx/pdf) OR steer the agent; AND suppress procedural-placeholder images in decks —
  when `image_gen.provider == procedural`, omit images (text-only slide), never embed the
  test-pattern PNG. Verify: scripted-agent scenario asserts deck has no procedural images +
  default format is not raw html.
- **W4 [A] Correlation instrumentation (gated DISCO_E2E).** Accept/propagate
  `X-Disco-Harness-Run` + `traceparent` on HTTP + WS; stamp event `meta` with
  `{harness_run_id, trace_id, ui_action_id}`; WS query params `harness_run_id`/`traceparent`.
  Verify: a request with the headers produces events whose meta carries the ids.
- **W5 [A] `/api/debug/evidence/{cid}` (gated DISCO_INSPECT/DISCO_E2E).** One endpoint
  bundling state+events+inspect-trace+manifest+sessions+preview. Verify: route test returns
  the bundle; inert without the flag.
- **W6 [A] Frontend debug hooks.** `window.__DISCO_E2E__ = {currentSurface, currentCid,
  controls}` + stable `data-disco-control` ids on controls lacking strong accessible names,
  behind `import.meta.env.VITE_DISCO_E2E`. Verify: vitest the hook is populated; tsc/eslint.

### Group B — PR-gate tests, no browser (py + vitest; parallel; depend on A where noted)
- **W7 [B] Boundary contract tests (real adapters, not mocks).** slides default format;
  image provider never silently procedural in product profiles; sandbox missing-file raises
  the typed error (W1); agent loop terminalizes on tool/sandbox/provider exception (W2);
  every tool call emits observation-or-structured-error; status can't stay RUNNING after
  task end. Verify: pytest green.
- **W8 [B] Scripted-agent integration harness + scenarios.** Real loop/sandbox/event-store,
  scripted model emitting through the REAL LLM-adapter/tool-call boundary (not internal
  calls). Scenarios: `missing_file_sandbox_error`, `slides_default_format`,
  `slides_image_provider_disabled`, `tool_exception_terminalizes_request`. Asserts: terminal
  status, no orphan RUNNING, no uncaught task exception, user-visible error on failure,
  deliverables readable via the real sandbox. Verify: pytest green; catches #2 + #4.
- **W9 [B] Frontend event-store REPLAY tests (vitest).** Canonical event fixtures from real
  runs → frontend selectors/reducers/derivers. Cases: `finished_without_plan_snapshot`
  (→ all-done; catches #3), `running_then_error`, `tool_error_visible`,
  `finished_with_partial_plan_updates`, `cancelled_request`. Verify: vitest green.
- **W10 [B] Static check.** Lint/CI rule that flags sandbox call sites catching only
  `OSError`/`FileNotFoundError` without the sandbox error type. Verify: rule fires on a
  planted violation, clean on the fixed tree.

### Group C — the evidence harness (TS; runs on VM 201; depends on W4/W5/W6)
- **W11 [C] `frontend/e2e-full/` scaffold.** `full.config.ts` (+ live variant pointing at
  the VM-201 browser / reverse-tunnelled app), `fixtures/discoHarness.ts`, `support/{recorder,
  uiInventory,wsRecorder,artifactCollector,assertions}.ts`. Verify: tsc + a smoke spec runs
  on VM 201 producing a dossier folder.
- **W12 [C] `disco.click` wrapper + four-truths assertions + control inventory + dossier.**
  Click-level provenance (before/after screenshot, name/role/selector/route/disabled, declared
  backend evidence wait, `ui-actions.jsonl`); `expectEventSequence` / `expectInspectTrace` /
  `expectArtifactReadable`; control enumeration + hit map + END-FAIL-on-untested-control;
  `timeline.md` + `index.html` dossier writer. Verify: smoke run's dossier has all 10 records
  for one click.
- **W13 [C] Scenario specs (one per surface).** research, deep-research, build, agent,
  settings, projects-history-share — covering EVERY control listed in the scenario matrix
  above. Verify: each runs on VM 201 (live-full) and its dossier shows the four truths +
  control coverage for that surface.
- **W14 [C] Artifact validators.** decks (locate deliverable, reject raw-html-default, unzip
  pptx + inspect `ppt/media/*`, provenance: no procedural/placeholder, decode base64 imgs,
  reject placeholder signatures, python-pptx slide/relationship sanity, off-box LibreOffice→
  PDF→pdftoppm render + nonblank/topic-text); PDF (pdfinfo/pdftotext/pdftoppm/Pillow); audio
  (ffprobe duration/codec + non-silent); sheets (openpyxl opens, sheets/cols, no error cells);
  build (entry files exist, install/build/test in isolated workspace). Verify: validators
  catch the known-bad threejs deck (procedural images) and pass a clean deck.
- **W15 [C] `disco-verify` runner + Make/npm targets.** Unified CLI: real request via the
  app's API → wait terminal (hard timeout) → read event store → locate deliverables → run
  validators → write redacted dossier. Targets: `e2e-full-fixture`, `e2e-full-live`,
  `e2e-full-report`. Verify: `disco-verify run slides_from_research_report` reproduces the
  boxes failure on the unfixed path and passes once W3 lands.

### Group D — infra
- **W16 [D] VM 201 browser host.** DONE (Playwright 1.61 on VM 201, proven). Add: reverse
  tunnel helper (`ssh -R 5173/8000/8800` workstation→VM201) so the VM-201 browser hits the
  live app on its own localhost; `scripts/vm201-pw.sh` wrapper (done). Verify: VM-201 browser
  loads `http://127.0.0.1:5173` through the tunnel.
- **W17 [D] CI shape.** PR = unit + boundary(W7) + scripted-agent(W8) + replay(W9) + static(W10)
  + cheap artifact validators. Nightly (self-hosted on VM 201) = live-full evidence harness +
  artifact rendering + Playwright. Release = repeated nondeterministic soak + VLM judge
  (advisory). Verify: PR job runs locally green; nightly target documented + runnable on VM 201.

## Parallelization
- Wave 1 (parallel): W1, W2, W3, W4, W5, W6 (independent product changes), W16 reverse tunnel.
- Wave 2 (parallel, after their A-deps): W7, W8 (need W1/W2/W3), W9, W10, W11.
- Wave 3 (after W11 + W4/W5/W6): W12, W14 (W14 partly independent).
- Wave 4: W13 (needs W12), W15 (needs W12/W14), W17.
- Codex review gates between: synthesized-plan review (now), post-implementation review (loop).

## Acceptance (the campaign is done when)
1. PR-gate suite (W7–W10) green locally; static check active.
2. `disco-verify` + the evidence harness run on VM 201 against the live app and produce a
   dossier per surface with the four truths + control coverage; END-FAIL-on-untested-control
   passes (every enabled visible control exercised or allowlisted-with-reason).
3. The artifact validators catch the known bugs (procedural deck, format=html, silent hang,
   #3 progress) on the unfixed paths and pass once W1/W2/W3/W9 land.
4. Codex reviews the implementation and is reduced to splitting hairs.
5. EVERY item exercised; all dossiers saved under `test-record/e2e-full/` for Dylan.

---

# REFINED PLAN (post-codex-review) — supersedes ordering above

Codex flagged 3 flaws: (1) fixes scheduled before tests prove they catch the historical
failures → **test-first**; (2) control-inventory overvalued → **classify controls**, only
backend-command controls need four-truth evidence; (3) W1↔W10 conflict → contract-based.

**Discipline gate:** do NOT start broad W13 scenarios until (a) one VM Playwright smoke run,
(b) one `disco-verify` API run, and (c) the FOUR historical negative proofs (W18) are
demonstrably FAILING on bad inputs and PASSING on fixed behavior.

## Refined work items
- **W18 (NEW, FIRST): historical negative fixtures + fault injections** — raw-html deck,
  procedural-image deck, no-plan-progress terminal event stream, fake sandbox task crash.
  Must FAIL the new tests before any fix lands. Keep minimized, not giant recorded logs.
- **W4: evidence/correlation SCHEMA first** — schema_version, cid/request_id, trace_id,
  harness_run_id, ui_action_id, artifact_ids, error taxonomy, redaction rules. Then headers.
- **W1: sandbox exception CONTRACT** (impl-agnostic): a missing-file sandbox failure
  satisfies `isinstance(e, FileNotFoundError)` AND no untyped `SandboxError` leaks past the
  boundary. (Don't presume multiple-inheritance is safe until tested — translation or
  handler-update also acceptable.)
- **W2: request/task SUPERVISION CONTRACT** (not just a kick callback): every background
  task registered; uncaught → terminal `ERROR`; cancellation ≠ error; terminal states
  idempotent; no orphan `RUNNING`.
- **W3: slides/artifact CONTRACT** — default `pptx` (pick it, not "or steer"); product
  profiles must NOT silently fall back to procedural images; record artifact provenance.
- **W5: minimal SECURE `/api/debug/evidence/{cid}`** — redacted event stream + trace +
  artifact manifest only (no arbitrary previews); path-traversal + redaction + disabled-by-
  default all tested.
- **W6: frontend metadata + stable selectors** — DOM-derived inventory is source of truth;
  `window.__DISCO_E2E__` exposes ONLY current surface/cid/run metadata, not the control list.
- **W7: cheap PR boundary contracts** for W1/W2/W3 (deterministic, real adapters).
- **W8: scripted-agent integration harness** through the REAL LLM-adapter/tool-call boundary
  + real event store + real sandbox + watchdog timeouts.
- **W9 (EARLY, high value): frontend event-store replay** (vitest, minimized fixtures);
  cover the progress bug first.
- **W10: static guardrails** (after W1/W2 settle): flag UNTYPED sandbox errors escaping
  boundaries + UNSUPERVISED `create_task`/background tasks. Do NOT flag valid OSError handlers.
- **W11: VM Playwright scaffold** + one smoke spec producing a dossier.
- **W12: `disco.click` + evidence assertions + dossier + control CLASSIFICATION** (backend-
  command / navigation / form-input / local-ui / disabled); fail only on backend-command
  controls at first.
- **W13: risk-based live scenarios per surface** (not every DOM control until critical
  workflows + historical regressions covered).
- **W14a: cheap PR artifact validators** (mime/default-format, pptx unzip, placeholder/
  procedural provenance, raw-html rejection, known-bad fixtures).
- **W14b: heavy VM-only validators** (LibreOffice render, pdf text/render, audio ffprobe,
  sheets openpyxl, isolated build) — nightly, tolerant of font/tool drift.
- **W15: `disco-verify` API-FIRST runner** (NO Playwright dependency): submit request → wait
  terminal → fetch W5 evidence → locate artifacts → run W14 validators → redacted dossier.
- **W16: finish VM 201 infra** — reverse tunnel (DONE) + Vite allowedHosts + WS origin +
  LibreOffice/poppler/ffmpeg/fonts on VM 201 + artifact copy-back.
- **W17: CI/nightly with OBJECTIVE gates** — PR = W7/W8/W9/W10/W14a/W15 cheap paths;
  nightly(VM) = W11/W12/W13/W14b; release soak = advisory.

## Parallel build order (6 lanes; codex-recommended)
- **Lane A** (evidence spine): W4 → W5 → W15.
- **Lane B** (frontend truth): W18 → W9 → W6.
- **Lane C** (engine contracts): W8 → W1 → W2 → W7 → W10.
- **Lane D** (artifact truth): W18(deck fixtures) → W14a → W3 → W14b.
- **Lane E** (browser/VM): W16 → W11 → W12 → W13.
- **Lane F** (CI): W17 — only after W7/W8/W9/W14a/W15 stable.

## Live build log (autonomous)
- P0 DONE: VM 201 = Playwright host (node22/pw1.61/chromium + poppler/ffmpeg/soffice); reverse tunnel proven (VM201→live app 200); `scripts/vm201-pw.sh` helper.
- W4 DONE (sonnet): evidence schema (py + ts mirror), 27 tests, lint/type clean.
- W18 DONE (me): 4 negative fixtures incl. AUTHENTIC procedural PNG (real bug signature).
- W1 DONE (me): SandboxFileNotFoundError(SandboxError,FileNotFoundError) + raise_read_error boundary in _container/podman; PROVEN file_state's `except (FileNotFoundError,...)` now catches it (no more silent-hang crash); 10 tests green; test-first (RED→GREEN).
- W16 DONE: VM render deps + tunnel.
- RUNNING: W8 scripted-agent harness (sonnet), W9 frontend event-replay (sonnet), W11 e2e-full scaffold (sonnet), W14a-mech validators (minimax).
- NEXT (me): W2 request/task supervision contract (the silent-hang AMPLIFIER fix).

## Live build log (cont.)
- W2 DONE (me): task-supervision contract in kick() — `_on_run_task_done` add_done_callback → `_terminalize_crashed` emits terminal ERROR + reminder on an uncaught loop exception; idempotent (won't clobber FINISHED/ERROR/STUCK/PAUSED); cancellation != error; IDLE-crash DOES surface. 3 tests, test-first RED→GREEN, runtime+integration regression clean. SILENT HANG NOW FULLY CLOSED (W1 trigger + W2 backstop).
- W8 DONE (sonnet, verified by me): scripted-agent integration harness via the real ModelProvider.stream_complete→DefaultLLMRouter→RouterAgent.step boundary; smoke green; surfaced+isolated the TitleService-consuming-a-step interaction.
- W9 DONE (sonnet, verified by me): 17 frontend event-replay tests (deriveBuildProgress/LiveSignal/Activity) incl. the #3 no-snapshot→all-done lock-in.
- W11 DONE (sonnet): frontend/e2e-full scaffold (config, Recorder/wsRecorder/artifactCollector, discoHarness fixture, smoke spec); tsc/eslint clean. LIVE smoke run on VM 201 = pending (orchestrator).
- W14a-mech DONE (minimax, VERIFIED by me): validate_pdf/audio/sheet in tools/verify/; real PDF fixture → [] (valid), missing → error; ruff+pyright clean. MiniMax delivered correctly on a mechanical spec.

## Live build log (cont. 2)
- W3 DONE (me): slides/artifact contract. (a) _stage_assets skips the procedural placeholder backend (name=="pil-procedural") → no garish placeholder art in decks; (b) slides default format html→pptx (presentable deliverable). 3 tests test-first RED→GREEN; slides regression clean (83 tests); ruff clean. Matches docs/dylans-runthru-3-6-21-26.md W3-2 root-fix prescription.
- Wave 3 dispatched (sonnet): W5 evidence endpoint, W12 disco.click+assertions, W15 disco-verify API runner.
- VM 201: frontend synced (tar-over-ssh), npm ci running for the live harness.
- NOTE: runthru-3 W3-1 (live thinking-stream during build) is a FEATURE, out of THIS campaign's scope (harness). Logged for later.
- Heartbeat: re-setting ~270s ScheduleWakeup each turn per Dylan (never stop the loop).

## Live build log (cont. 3) — LIVE EVIDENCE PROVEN
- W11 LIVE-RUN DONE: first evidence dossier on VM 201 (Firefox→tunnel→live app). Dossier 20260621T204605: screenshot 78KB (real render), 398 network records, 141 console lines, ui-actions + timeline.md/index.html/manifest.json (schema_version 1). SENT to Dylan. The input→output evidence pipeline works end-to-end on the non-memcapped host.
- W5 DONE (sonnet, VERIFIED): GET /api/debug/evidence/{cid} gated on DISCO_INSPECT, full redact() bundle, planted-secret redaction tested; 3 tests, ruff+pyright clean.
- W10 DONE (me): static guardrails — no backend may raise a bare SandboxError on read (locks W1 across all/future backends); kick() must keep the add_done_callback supervisor (locks W2). Source-level, can't-be-quietly-deleted.

## Live build log (cont. 4)
- W15 DONE (sonnet, VERIFIED+FIXED by me): disco-verify API-first runner (run_scenario through real POST/WS/state/events/trace/manifest + validator delegation + redacted dossier); 26 tests. CAUGHT a real defect: the agent made verify.py→verify/ package but LEFT the flat verify.py (module/package collision, dead orphan) AND missing __main__.py. Fixed: added verify/__main__.py (python -m entry), removed orphan verify.py (content preserved verbatim in _setup_checks.py + re-exported). disco-verify console-script + `python -m` both verified working. (1 test_verify failure is PRE-EXISTING — git-confirmed unchanged this session.)

## Live build log (cont. 5)
- W14a-deck DONE (me): validate_deck_file (extract html base64 / pptx ppt/media images → _looks_procedural) + validate_deck_deliverable (reject raw_html_default). _looks_procedural is SEED-INDEPENDENT (≤12 colours AND corner-to-corner diagonal — the backend's structural invariants), so it catches ANY procedural placeholder, not just the fixture; real noise image NOT flagged. 5 tests on the W18 fixtures, ruff+pyright clean.
- W7 status: the W1/W2/W3 contract tests + W10 guardrails ARE the boundary contracts; W17 will tag them into the fast PR tier (pending).

## Live build log (cont. 6) — W3 PROVEN LIVE ON A REAL DECK
- W14b DONE (me): heavy validators validate_pptx_renders (LibreOffice→pdf) + validate_pdf_renders (poppler rasterize + blank-page check); soffice-missing guard tested locally, poppler path tested locally (3 tests), real soffice render PROVEN on VM 201.
- W3 LIVE PROOF: generated an authentic disco pptx (lower_deck+render_pptx) through the PROCEDURAL backend → image_assets={} → rendered on VM 201 via LibreOffice → 3 slides @960x540 (16:9). Slide-2 shows a CLEAN "[image]" placeholder frame (Disco-branded), NOT procedural nested-rect/diagonal junk. validate_deck_file CLEAN. Evidence: test-record/e2e-full/artifacts/{threejs-history.pptx, slide-1/2/3.png}. SENT slide-2 to Dylan.
- FOUND BUG (harness value!): slide-2 bullets OVERLAP ("Created by Ricardo Cabello (mrdoob), 2010" over "WebGL abstraction layer") — a pptx text line-spacing/positioning defect surfaced by the render-and-look heavy validator. Separate from W3; logged for a follow-up fix.

## Live build log (cont. 7)
- W7 DONE (me): `boundary_contract` pytest marker registered in pyproject + module-level pytestmark on the W1/W2/W3/W10 contract+guardrail files. `pytest -m boundary_contract` → 20 tests/2.73s (2860 deselected) = the fast PR-tier input/output contract gate.

## Live build log (cont. 8) — verification consolidation + git incident
- W7 DONE (boundary_contract marker, 20 tests/2.6s). W12 DONE+VERIFIED (disco.click/uiInventory/assertions/smoke; tsc+eslint clean). W6 DONE+VERIFIED (e2eBridge + 35 data-disco-control across surfaces; tsc clean; ATTRIBUTE-ONLY confirmed via NeedMoreCard diff → its vitest failures are pre-existing). W17 DONE (evidence-harness.yml: pr-evidence GH-hosted [boundary+validators+replay] + nightly-vm self-hosted [Playwright+W13+W14b]). W13 runner DONE (verify/scenarios_run.py).
- GIT INCIDENT (mine, recovered): accidental `git stash pop` applied unrelated stash@{0} → conflicts in engine.py/recitation.py/uv.lock (branch B1-B7 WIP). Resolved KEEPING OURS = the newer ModelExecutionPolicy branch WIP (+39 over HEAD); verified ours=newer via diff-vs-stash; old stash still in list; backups /var/home/dylan/conflict-backup/. Lesson: never blind `stash pop` with unrelated stashes — use file backups for A/B.
- BASELINE (pre-existing, NOT my campaign — proven via double A/B engine+runtime): test_build_surface.py = 3 failed/9 passed (build-planning tests the branch is mid-reworking: starts-in-planning, request-plan-after-finish, +1). These fail on HEAD engine AND with W2 reverted → branch WIP debt, untouched by the harness work.
- FOUND BUGS (logged, separate from harness): (1) pptx slide-2 bullet text overlap; (2) disco-verify scenarios needed approve_plan=True (FIXED — both scenarios; missing_file expect→FINISHED [graceful no-hang], slides expect→deck/pptx).

## CODEX REVIEW round 1 (gpt-5.5) — FINDINGS (full: docs/codex-evidence-harness-review-6-21-26.md)
NOT splitting hairs yet — real defects, several at the campaign's core (the harness amputates output too):
- P0-1 disco-verify never downloads delivered artifact bytes (only Path.exists in cwd) + never calls validate_deck_file → live deck check is theater. runner.py:355/55/360.
- P0-2 procedural-deck regression passes live (detects only via image_generate observation; slides stages internally). Fix via P0-1 (inspect real pptx).
- P0-3 hang PASSES: poll_until_terminal returns last state on timeout + expected_status None → any status ok. runner.py:184/512. RUNNING/AWAITING after timeout MUST fail.
- P0-4 missing_file scenario(FINISHED) vs unit test(ERROR) contradict → CI red or stale.
- P0/P1-5 Playwright helpers wrong API shapes: events {events:[]} not JSONL (discoClick:123, assertions:122), artifact URL /workspace vs /conversations/{cid}/artifacts (assertions:249), state omits cid (artifactCollector:31), trace span field 'span' not 'name' (assertions:211 vs obs.py:67); discoClick records but never FAILS on missing evidence.
- P1: CI `|| true` masks live failures + no real heavy-validator step (evidence-harness.yml:88/63); pptx default → Marp path fails if absent (slides.py:575); W1 raise_read_error only maps not-found, permission/dir stay bare SandboxError (base.py:74); guardrails text-only, container/podman/gvisor typed-read untested; validators crash (no FileNotFoundError/BadZipFile handling); supervision create_task dropped on shutdown.
- P2: redact() only dict/list (not tuple/set/model); legacy /api/debug/trace unredacted; UI inventory dedup drops duplicate controls.
FIX PLAN: me=runner P0-1/2/3/4 + W1 extension + CI + slides-fallback; agentA=Playwright shapes; agentB=validator-hardening+redact+trace-route.

## CODEX round-1 FIXES applied (verified)
- P0-1/2/3/4 (runner, me): disco-verify now DOWNLOADS real artifact bytes + runs validate_deck_file (procedural caught live); poll timeout sets _timed_out; passed requires reached_terminal (timeout/RUNNING/AWAITING → FAIL); missing_file scenario↔tests reconciled (FINISHED); split _run_validators→_run_forbid_checks(sync)+_run_file_validators(async download). 30 tests green incl. new procedural-via-download + timeout-fails + nonterminal-fails.
- P0/P1-5 (Playwright, agentA): events {events:[]} not JSONL (discoClick+assertions), artifact URL /conversations/{cid}/artifacts/{path}, state cid, trace span field 'span', discoClick THROWS on missing evidence. tsc+eslint clean.
- P1 (validators/redact/trace, agentB): validators return findings not crash (missing-binary, BadZipFile); redact() handles tuple/set/pydantic/__dict__; legacy /api/debug/trace now redacted. 45 tests green.
- P1 CI (me): removed `|| true` masking on live W13 + added LibreOffice/poppler install + heavy-validator step to nightly.
- P1 W1 extension (me): raise_read_error now maps permission→SandboxPermissionError(PermissionError) + ENOTDIR→SandboxNotADirectoryError(NotADirectoryError), not just not-found. 12 tests green.
- P1 slides-Marp (me): no-goal markdown caller in Marp-less sandbox now DEGRADES pptx/pdf→HTML fallback instead of hard-fail (compat-neutral default-pptx change).
- REMAINING for round-2: W1 behavioral container/podman typed-read tests; task-supervision shutdown-drop; P2 (UI inventory dedup, non-deck validators). Re-running codex to check convergence.

## CODEX round-2 (NO P0) → 7 P1 FIXES applied
- #1 disco-verify now ENFORCES expect.deliverable_type (FINISHED + no deck → FAIL); new test. (me)
- #2 slides Marp-absent degrade is now EXPLICIT/honest (content note + structured.degraded_to=html, requested_format); tests updated. (me)
- #3 procedural forbid check uses the REAL image_generate shape (placeholder/backend/backend_connected, not the dead `provider`); _VALIDATABLE_EXTS + validator now cover standalone .png/.jpg deliverables; test fixture corrected to real shape. (me)
- #4 redact() string-content scrubber for secrets in message.content/tool stdout (agent C, running).
- #5 CI: removed nightly-vm `continue-on-error: true` (live/heavy failures now turn it red). (me)
- #6 W1: container/podman list_dir now routes through raise_read_error(op="list_dir") → typed missing-dir error (not just read_file). (me)
- #7 Playwright event helpers read `kind` (the real discriminator, events.py:683), not event_type/type (agent D, tsc clean).
- VERIFY: 31 runner + 84 broad (W1/slides/evidence/debug/validators) green; lint/type clean; CI yaml valid.
- NEXT: collect agent C (redact); re-launch codex ROUND 3 to confirm convergence (splitting hairs).

## CODEX round-3 (NO P0) → 3 P1 FIXES applied (me)
- EISDIR: added SandboxIsADirectoryError(SandboxError, IsADirectoryError); raise_read_error maps "is a directory" (checked before ENOTDIR); read_file + list_dir both typed. Test added.
- _looks_procedural now catches procedural JPEGs (posterize-2 + dominant-bg-fraction OR crisp diagonal) — verified True for PNG+JPEG across seeds, False on real noise/gradient. Test added.
- disco-verify runner now RENDERS .pptx via LibreOffice (validate_pptx_renders, soffice-unavailable sentinel filtered → skips on PR host, renders on VM201); heavy test renders a real clean pptx + flags a corrupt zip.
- VERIFY: 120 tools/runner tests green; ruff+pyright clean.
- LIVE VM201 exercise: smoke dossier PASSED (home render + 398-record evidence); click-provenance spec FAILED = spec-calibration (the home-screen control it picks yields no backend evidence + discoClick now correctly THROWS on missing evidence) — a W12 spec target/expectation to refine, NOT a harness-code bug. Dossiers pulled to test-record/e2e-full-live/.
- Full campaign suite exercised: 20 boundary + 60 validators/schema/route + 35 runner/harness + 17 frontend replay = ~132 green (test-record/campaign-evidence/suite-results.txt).
- codex ROUND 4 running to confirm convergence.

## CODEX round-4 (NO P0) → P1 fixes
- #4 _looks_procedural flat-art FALSE-POSITIVE fixed (me): now requires a THIN corner-diagonal accent whose colour ≠ background AND is rare overall (<15%) — procedural PNG=True, flat art (rect/bars)/noise=False. JPEG byte-detection is best-effort; format-agnostic defense = PROVENANCE (placeholder field, runner forbid check) + W3 generation-time strip. Test → no-FP-on-flat-art.
- #2 raw_html_default no longer maskable by a companion (me): judges only DECK files (.html/.pptx/.pdf), ignores editable_source/json/image companions; test added.
- #1 Playwright dossier redaction (agent, running): TS redact() string-scrubber mirror + artifactCollector redacts truth-endpoint bodies before writing.
- #3 (full UI input→output workflow in browser tier) = ACCEPTED as designed: the API tier (disco-verify) IS the input→output proof; the browser tier is smoke/visual/control-inventory by plan. Documented, not a false-pass.
- W12 click-provenance spec FIXED (me): used tag-name as ARIA role in getByRole → invalid/hang; switched to page.locator(tag, {hasText}). LIVE on VM201: 1 passed (3.6s).
- codex ROUND 5 next to confirm convergence.

## COMMIT PLAN (when codex converges + agent #1 done)
- UNSTAGE branch WIP (NOT campaign): git restore --staged packages/core/src/disco/core/loop/engine.py packages/core/src/disco/core/loop/messages.py (+ recitation.py/uv.lock if staged). These are the uncommitted B1-B7 tailscale WIP; leave them as unstaged working-tree changes. NEVER stash-pop. Backups in /var/home/dylan/conflict-backup/.
- REVERT incidental ruff-isort noise on pre-existing files: git checkout -- packages/tools/tests/test_deck_schema.py packages/tools/tests/test_slides_pipeline.py (import-reorder only, not campaign).
- STAGE (explicit paths) the campaign:
  MODIFIED: frontend/src/App.tsx + the 14 data-disco-control component edits (QueryInput, build/{AgentStatusBar,AlternativesGate,AskPanel,BuildSurface,ClarifyPanel,ConfirmationPanel,DeliverablePanel,PlanPanel,SteerInput}, research/{DeepBoundedNotice,DeepResearchSurface,NeedMoreCard,ReportFollowUp}, views/HistoryView) + vite-env.d.ts + tsconfig.app.json; packages/agent-server/src/disco/agent_server/{routes/debug.py,runtime.py}; packages/tools/src/disco/tools/builtin/{_slides_pipeline,slides}.py; packages/tools/src/disco/tools/sandbox/{_container,base,podman}.py; packages/tools/tests/test_slides.py; pyproject.toml.
  UNTRACKED: .github/workflows/evidence-harness.yml; docs/{disco-evidence-harness-campaign,codex-evidence-harness-review-6-21-26,codex-testing-suite-design-6-21-26,dylans-runthru-3-6-21-26}.md; frontend/e2e-full/; frontend/src/lib/{buildTrace.replay.test.ts,e2eBridge.ts}; packages/agent-server/src/disco/agent_server/verify/; packages/agent-server/tests/{fixtures,integration,test_debug_evidence_route.py,test_disco_verify_runner.py,test_supervision_guardrail.py}; packages/core/src/disco/core/evidence/; packages/core/tests/test_evidence_schema.py; packages/tools/src/disco/tools/verify/; packages/tools/tests/{fixtures,test_artifact_validators,test_deck_validators,test_heavy_validators,test_sandbox_exception_contract,test_sandbox_read_guardrail,test_slides_procedural_contract}.py; scripts/vm201-pw.sh.
- Verify `git diff --cached --stat` excludes engine.py/messages.py/recitation.py/uv.lock before committing. Do NOT push.

## CODEX round-5 (NO P0) → 1 P1 FIXED (app output boundary)
- The Build surface's PRIMARY output for "make me a website" is a live-app handoff (DeliverableEvent artifact_kind="app"). disco-verify DROPPED them (_locate_deliverables only kept "files") + deliverable_type had no "app" → a scenario expecting an app could pass with nothing delivered. FIXED (me):
  - _locate_deliverables now collects app deliverables (path + deployment_url).
  - _deliverable_type_satisfied gates "app" by KIND (not extension).
  - new AbstractVerifyClient.fetch_app + Http impl + _validate_app_deliverables: a present deployment_url must be reachable (2xx) + non-empty; apps served via in-app preview (no URL) are valid.
  - app_from_build scenario added + wired into scenarios_run.
  - 5 new tests (locate, pass-when-served, fail-when-no-app, fail-when-unreachable, fail-when-empty). 37 runner tests green, ruff+pyright clean.
- codex ROUND 6 next to confirm convergence.

## CODEX round-6 (NO P0) → 2 P1 FIXED
- Path-escape read now TYPED (me): process.py + _container.py `path escapes workspace` → SandboxPermissionError (PermissionError) instead of bare SandboxError → bookkeeping handlers catch it (completes the W1 contract: NO bare SandboxError from any read/resolve). Test added.
- URL-less app deliverables now OUTPUT-PROBED (me): _validate_app_deliverables fetches the built index.html via the artifacts endpoint when there's no deployment_url (declared-but-empty app → FAIL). 2 tests added.
- 50 tests green; my files ruff+pyright clean (the 1 E501 is pre-existing kernel.py:392).
- codex ROUND 7 next.

## CODEX round-7 (NO P0) → 1 P1 FIXED (app-output grounded in the REAL boundary)
- codex caught a flaw in my round-6 fix: /artifacts is jailed to DECLARED artifacts, so probing index.html there is not a real production boundary for a live-served app (would 404 → false-fail a legit app). FIXED (me): _validate_app_deliverables now probes the LIVE PREVIEW (GET /conversations/{cid}/preview-app/ — the proxy the UI iframes; 503=unavailable→fail) via a new client.fetch_preview, preserving the artifact jail. URL'd apps still require 2xx+non-empty. Tests updated (preview 200→pass, 503→fail). 39 runner tests green, ruff+pyright clean.
- codex verified EVERYTHING ELSE clean in R7: redaction (recursive+string-scrub), procedural detector (PNG-precise+provenance), frontend replay (production derivers not mocks), slides pptx-default compat.
- codex ROUND 8 to confirm the preview fix.

## CODEX round-8 (NO P0; "other areas converged") → 1 P1 FIXED (directory-listing false-pass)
- codex: a URL-less app preview can return a 200 NON-EMPTY python3 -m http.server DIRECTORY LISTING (the preview's fallback when there's no real app entry file) → passed the 2xx+non-empty check = false pass. FIXED (me): fetch_app/fetch_preview now return (status, BODY); shared _app_body_problem rejects a bare "Directory listing for" page (+ 503/non-2xx/empty). 40 runner tests green incl. the directory-listing-fails test; ruff+pyright clean.
- codex explicitly: "No remaining P0 found. The other reviewed areas look converged." This directory-listing hole was the SOLE remaining P1.
- codex ROUND 9 = final convergence confirmation.
