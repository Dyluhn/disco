> **SUPERSEDED / HISTORICAL (as of 2026-07-07).** June-era UI-control-gaps inventory, frozen mid-execution.
> Current status of record: `docs/disco-project-state.md` (master), `docs/disco-status-and-remaining.md` (features + remaining), `sec-work-remaining/disco-security-state.md` (security). This file is kept for history and may contain stale claims. 🚫 **Fable 5 (Anthropic) models are off-limits to view per the project owner** — viewing them will cause a silent fallback to a weaker model; do not proceed unless you are Opus, Codex, or Sonnet.

# Disco UI — what we do NOT have direct, deterministic control over (2026-06-21)

Synthesis of 3 independent read-only audits: **Opus-A** (Build/Agent), **Opus-B**
(Research/DR/History/Projects/Settings/Share/Imported/Activity/shell), **codex** (whole-UI;
folded in below). The question: across the entire UI, which controls/behaviors can we NOT
drive with a stable handle AND verify deterministically right now — harness blind spots +
inherently non-deterministic surfaces.

Context: the evidence-harness campaign (commit 3107e3a) gave us three lenses — Playwright
`frontend/e2e-full/` (uiInventory + `assertCoverage` gate), `disco-verify` (API/WS runner),
and `buildTrace.replay.test.ts` (pure-deriver replay). This audit measures how far those
lenses actually reach.

> ## PRINCIPLE (Dylan, governing this whole report)
> **Cassette / fixture / replay / fake-client / stubbed-provider / frozen-clock tests are a
> REGRESSION net only — they prove "we didn't break the wiring." They can NEVER prove a feature
> WORKS, because the model isn't in the loop.** The ONLY thing that proves something works is a
> **live model running the show end-to-end** (real model → real tool calls → real app/sandbox →
> real delivered output, asserted at input and output).
>
> So read the two columns below correctly:
> - The **"deterministic test seam"** recommendations (injected state, frozen clock, stubbed
>   search/TTS, WS scripts, `setInputFiles`) build out the **regression tier** — fast, cheap,
>   catches wiring breaks. Build them freely. But a green run there is NOT proof a control works.
> - The real, deeper gap this audit exposes is the **proof tier**: almost nothing in the UI is
>   PROVEN to work by a live model. `disco-verify` has ~3 scenarios; the live VM-201 Playwright
>   tier is a `/`-only smoke. **Expanding the live-model scenario matrix — a real model driving
>   each surface to a real delivered output — is the actual coverage work.** The seams are just
>   the net under it. (See memory: feedback-live-model-proves-works.)

---

## THE HEADLINE (all sources agree — highest confidence)

**The harness drives essentially NOTHING end-to-end through the real UI today.**
- The only two Playwright specs both `page.goto("/")` (Search empty state) and click ONE
  arbitrary control. **No spec ever navigates to `/build`, `/agent`, DR-running, History,
  Projects, Settings, Activity, Share, or Imported.** (`smoke.full.spec.ts:29`,
  `click-provenance.smoke.spec.ts:92`)
- **`HitMap.assertCoverage()` (`uiInventory.ts:342`) is implemented but called by ZERO
  specs** — so the coverage gate that would fail on a declared-but-unclicked control never
  runs. Every one of the ~36 `data-disco-control` handles W6 added is **declared but never
  exercised.**
- `disco-verify` has 3 scenarios, all Build/Agent, driving only 2 of ~17 backend commands
  (`send_message`, `approve_plan`) — and as WS frames, bypassing the actual buttons.
- The genuinely solid, deterministic foundation is the **pure buildTrace derivers** +
  replay fixtures + `FakeVerifyClient` (W9). That layer is real. Everything above it (the
  integration boundary: real UI → real backend) is the blind spot.

So: W6 added the handles, W12 built the gate, W15 built the runner — but the wiring that
makes them *enforce anything on the live UI* isn't connected yet.

---

## P1 — real control/coverage holes & false affordances

### Coverage / wiring (the structural gaps)
1. **No spec visits any route except `/`** → History, Projects, Settings, Activity, Build,
   Agent, DR-running, Share, Imported are entirely undriven.
2. **`assertCoverage()` never invoked** → ~36 `data-disco-control` handles are silent; the
   gate can't fail on what it never enumerates.
3. **`disco-verify` drives 2 of ~17 backend commands.** Unverified WS/backend effects: stop,
   kill, resume, steer, reject/approve-action, pick_alternative, answer (ask/clarify),
   upload, revise-plan, download/export, manifest, deck-edit, live-browser, preview-restart,
   DR lifecycle, report-export, audio, build-deck, delete/import.
4. **W6 bridge `runStatus` is hardcoded `null`** (`App.tsx:76,82`) — the one deterministic
   state seam never reports live run status, blocking every status-gated assertion on every
   surface. (Cheap, high-leverage fix.)

### Misclassified backend controls that ESCAPE the coverage gate (false negatives)
5. **noVNC "Live" toggle** (`AgentCanvas.tsx:235`) calls `/browser/live-url` +
   `/browser/live-stop` but the name-heuristic classifies it `local-ui-only` → no handle,
   escapes the gate.
6. **Click-to-edit "Apply"** (`EditAffordance.tsx:82`) emits a steer (backend) but classifies
   `local-ui-only` → escapes the gate.
7. **Handle-less backend buttons:** project Export-zip (`BuildSurface.tsx:289`,
   `ProjectsView.tsx:126`), Preview Refresh/Restart (`PreviewPane.tsx:162,443`) — classify as
   backend-command by name but get unstable `gen:` ids.

### Whole sub-surfaces with zero handles
8. **The deck editor** (`editor/DeckEditor`, `DeckEditorPane`, `ElementBox`, `LayersPanel`) —
   element edit / layers / save / patch / export, **zero `data-disco-control`/`data-testid`**,
   no scenario. A real backend path (deck patch/export) entirely uninstrumented.
9. **Settings — ~65 interactive controls, 0 handles** (only 2 testids). **No test asserts a
   saved setting round-trips or applies.** This is the highest false-affordance-risk surface:
   - OpenRouter "Browse models" NotWired when key locked (`OpenRouterSection.tsx:89`)
   - Encoder endpoint fields rendered-but-ignored when provider=bundled (silent)
   - ImageGen silently falls back to procedural when comfyui/openai unconfigured (`:166`)
   - LiveBrowser on-toggle persists but VNC availability untested
   - No "test key/connection/TTS/image" anywhere → validity only known at tool-call time
   - `ThinkToggle` aria-label literally says "backend handling in progress"

### Model-gated panels with no injected-state seam (can't reach on demand)
10. **Confirm / Ask / Clarify / Alternatives gates** only render when the live model emits the
    triggering tool call (risky-action / `ask_user` / `clarify` / 4-consecutive-failures).
    With a real model these are flaky-to-impossible to reach. **There is no fixture that puts
    `useBuild` into `WAITING_FOR_CONFIRMATION`/`AWAITING_USER_QUESTION`/`AWAITING_USER_DECISION`
    to mount the panel + click its handle deterministically.** (Opus-A: "single
    highest-leverage missing seam.")

### Output paths invisible to the harness
11. **Report export (md/pdf/docx)** goes through `POST /conversations/{cid}/report/export`
    (`NeedMoreCard.tsx:133`) — NOT a tool observation/deliverable in the event log — so
    `_locate_deliverables` (`runner.py:315`) **cannot see or validate it.** PDF/DOCX also
    gated on server capability + delivered via blob/FSA download (no DOM-observable success).
12. **DR replay coverage = 0.** `buildTrace.replay.test.ts` covers Build derivers only; the
    Deep Research derivers (streaming/iterate/report) have NO replay fixtures.
13. **`deriveFiles`/`deriveTerminal`/`deriveDeliverable` have no dedicated replay spec**
    (only progress/activity/liveSignal do) — cheapest determinism win on Build.

---

## P2 — individually testable but unhandled/uncovered
- Projects Download-zip (blob) + Delete; History Import (OS picker; hidden input no handle) +
  Delete-confirm step (no handle on the ConfirmDialog button).
- CommandPalette (Cmd+K, 5 nav/theme commands, keyboard-only, zero coverage), ModeSlider,
  NavRail links, ThemeToggle, rail-collapse/mobile-drawer — deterministic but no handles.
- Share/Imported read-only viewers + their version-mismatch/revoked-link error paths —
  deterministic if fed a fixture bundle, untested.
- ScheduleSection create/preview/delete — handle-less; preview/save are wall-clock-dependent.
- Duplicate `controlId`: replan `QueryInput` reuses `send-message` (`BuildSurface.tsx:477` vs
  `QueryInput.tsx:110`); `HitMap` keys by controlId so a hit on one marks both covered.
- Cost meter / "N running" badge / DemoDataBadge — live-poll / usage / wall-clock dependent.

---

## Inherently non-deterministic surfaces → the deterministic test seam each needs

| Non-determinism source | Where | Seam to make it deterministic |
|---|---|---|
| Model output / token streaming | Search answer, DR report, build activity, all gates | Recorded **WS event-stream fixture** + fake driver (extend the W9 replay pattern + `FakeVerifyClient` to Search/DR derivers) |
| Live web-search results | Search + DR gather | Stubbed "fixture" search/extraction provider (Settings already models bundled/selfhost/paid — add a fixture tier) |
| WS lifecycle races (RUNNING/PAUSED/ERROR/AWAITING_*) | DR + build lifecycle + gate panels | Backend test-hook to force `execution_status`; **expose `runStatus` via the W6 bridge** so the harness can `await` it |
| Live preview / dev-server / http.server fallback | PreviewPane | **Stub `useBuildPreview`** → assert each branch (live iframe / rendered srcDoc / inline-artifact / empty). Backend body already covered by `fetch_preview`+`_app_body_problem` |
| noVNC live browser | AgentCanvas | Mock `useLiveBrowserConfig` + `/browser/live-url`; assert iframe src + that unmount fires `live-stop`; fake timers for the 240s heartbeat/reaping |
| watch-it-write streaming file | ExecutionCanvas | Inject a `StreamingFile` prop sequence; assert Files-tab focus + content growth |
| Report export downloads (blob/FSA/OS picker) | NeedMoreCard + DR top bar | Make export a discoverable deliverable OR a verify step that calls `/report/export` + validates bytes; `page.on('download')` / stub `showSaveFilePicker` |
| Audio / TTS generation (+300MB model dl) | generate-audio / export-audio | Stubbed TTS provider returning a fixed short MP3; freeze the voice-model download |
| Scheduled cron firing (wall-clock) | ScheduleSection + Activity recent_runs | **Frozen clock** + a "fire now" test endpoint; pin `now` for next-run assertions; static-validate the preset cron strings |
| OS file pickers | History Import, Projects Browse, upload | Drive the hidden `<input type=file>` via `setInputFiles` (give it a handle); stub `showDirectoryPicker`/`PathPickerDialog` |
| Live external services (OpenRouter catalogue, encoders, sandbox, MCP, ComfyUI, keys, fs-validation) | Settings | Mock the TanStack-Query hooks / fixture config-server; assert **config-payload shape + persistence round-trip ONLY**, never live connectivity; add handles so the gate sees them |
| Live polling (Activity running list, NavRail badge) | Activity, shell | Seed a fixed `/api/activity` fixture; assert rendered counts |

---

## The cheapest high-leverage moves (both agents independently recommend)
1. **Turn on `assertCoverage()` in a per-surface spec** + add `data-disco-control` to the
   misclassified/handle-less controls (noVNC Live, Apply, Refresh/Restart, Export-zip,
   deck-editor, every Settings save/toggle, Projects download/delete, History
   import-input/delete-confirm, ModeSlider, NavRail, CommandPalette, Schedule actions) — so
   declared-but-unclicked controls fail CI instead of silently passing.
2. **Wire `runStatus` into the W6 bridge** (`App.tsx:71-84`) — unlocks status-gated
   assertions everywhere.
3. **Add an injected-`useBuild`-state fixture** so the four model-gated panels (and DR
   lifecycle states) mount + click deterministically — the single biggest determinism seam.
4. **Author DR/Search/Settings replay fixtures + disco-verify scenarios** (mirror W9 for the
   research/DR derivers, which have zero replay coverage today).
5. **Make report-export + audio discoverable/validatable** by disco-verify (they bypass the
   event-log deliverable path today).

---

## codex (3rd source) — corroboration + unique adds

**codex independently confirms the entire headline + every P1 above** (it verified them
against the live code): `assertCoverage` never called (`uiInventory.ts:342`), `runStatus`
hardcoded null (`App.tsx:71,78`), only `/` ever visited, `disco-verify` = 3 API/WS scenarios
(no browser), deck editor handle-less, Settings unverified, duplicate `controlId`s, the
model-gated panels, and every non-deterministic surface. **All three sources agree → these are
high-confidence, not opinion.**

codex's UNIQUE additional findings (fold into the P1/P2 lists):
- **Primary text inputs have no stable role+name** — the shared query textarea has no
  `aria-label` (`QueryInput.tsx:78`), and the plan-revision (`PlanPanel.tsx:218`),
  command-palette (`CommandPalette.tsx:90`), and preview-edit (`EditAffordance.tsx:54`)
  textareas have only placeholders. `uiInventory` names controls by `aria-label`/`textContent`
  only (`uiInventory.ts:252`), so these inputs are unaddressable by the inventory. **(P1 handle
  gap — you can't even type into the main fields deterministically.)**
- **Client-side non-determinism inside the app itself:** optimistic IDs use
  `Date.now()`/`Math.random()` (`useBuildStream.ts:30,269`) — so even a fixed event stream
  yields non-reproducible DOM keys. Freezing `Date`/`Math.random` is required, not just the
  backend. **(P1)**
- **`injectSource` is implemented in the DR hook but has NO visible UI path**
  (`useDeepResearchStream.ts:226`) — a wired backend capability with no affordance (the inverse
  false-affordance: a real path you can't reach from the UI). **(P2 / product note)**
- **Native `confirm()` for model deletion** (`ModelCatalogue.tsx:291`) — can't be driven
  without Playwright dialog handling; replace with the in-app `ConfirmDialog`. **(P1 false/
  undriveable)**
- **CommandPalette empty-results keyboard bug risk** — arrow handling does `% filteredCommands
  .length` even when zero (`CommandPalette.tsx:59`), a real latent div-by-zero/NaN. **(P1 bug,
  not just coverage — worth fixing regardless of the harness.)**
- **Clarify choices are dynamic hidden radios** (`ClarifyPanel.tsx:89`); **IncludeFollowUps
  modal** controls have no data IDs + dynamic text (`IncludeFollowUpsModal.tsx:177`). **(P2
  handle gaps.)**

codex's consolidated seam recommendations (identical in spirit to the table above): traverse
every route + open every modal/gate from fixtures + actually call `assertCoverage`; replace
live model/search/TTS/MCP/OpenRouter with injected fixtures; **freeze `Date`, timezone, timers,
`Math.random`, and WS frame scripts**; add programmatic file/download seams (stable hidden
file-input IDs, mocked `showSaveFilePicker`, a download sink, no native `confirm()`); stub
preview/browser/sandbox endpoints (fixed ports/iframe HTML/screenshots/sessions + a noVNC iframe
stub); fake scheduler + frozen clock (never assert real cron firing).

## Bottom line (3-source consensus)
We have a **sound deterministic foundation** (pure derivers + replay fixtures + a fake API
client) but **almost no guaranteed deterministic control over the live UI**: the harness opens
`/`, inventories what's visible, and clicks one control — it does not traverse routes, open
modal/gate states, drive run states, or enforce hit-coverage. The four cheapest, highest-
leverage unlocks (all three sources converge on these): **(1) call `assertCoverage` in a
per-surface spec + add handles to the misclassified/handle-less controls; (2) wire `runStatus`
into the W6 bridge; (3) add an injected-`useBuild`/`useDeepResearch`-state fixture so gated
panels mount on demand; (4) freeze `Date`/`Math.random`/timers + script WS frames.** None of
this is built today — it's the natural next campaign after the harness itself.

---

# FIXES APPLIED — 98 discrete fixes (2026-06-22, regression tier)

5 Opus agents fanned out over disjoint file-areas; integrated + full-gate-verified by the main loop. **One discrete fix per gap (no collapsing).**

- **A1 Build/Agent (18):** #8-13,15,16,19-28 — handles for noVNC-Live, edit-Apply, preview refresh/restart, export-zip, deck-editor, activity-download, upload-input, assist/autonomous; unique pick-alternative; cost/stop/screenshot/active-tab state attrs; **#25 injectable OptimisticIdFactory + test**; gate `data-gate` markers.
- **A2 Search/DR (24):** #29,31-53 — search/DR handles + phase/stream state attrs; **#35 ThinkToggle wired (honest label, not false affordance)**; **#53 injectSource given a real UI affordance** (killed the dead capability); de-duped export/audio handles.
- **A3 History/Projects/Activity/Share (14):** #56-58,60-62,64-68,82-84 — row/search/download/delete handles + `data-storage-status`/`data-running-count`/`data-scheduled-runs`/`data-share-error`/`data-iso-timestamp`.
- **A4 Settings/Schedule/Shell (25):** #69-81,85-91,93-97 — every section save/toggle handle; **#71 native confirm()→ConfirmDialog** + **#91 CommandPalette NaN-on-empty guard** (both real bugs, both with tests); 4 false affordances disabled+`data-disco-flag`-flagged (OpenRouter-locked, Encoder-bundled, ImageGen-procedural, LiveBrowser-runtime).
- **A5 shared+harness+backend (17):** #1-7,14,17,18,30,54,55,59,63,92,98 — per-surface Playwright spec + `assertCoverage` wired; uiInventory name/dedup; QueryInput controlId+aria-label; ConfirmDialog handles+context; Toast attrs; disco-verify scripted WS commands + report-export + fire-now validators; gate-mount seam; **deriveFiles/Terminal/Deliverable replay + DR-deriver replay** (37 tests).

**Main-loop integration deltas (caller-side wiring the agents flagged):**
- #4 extracted `lib/runStatusBridge.ts` (avoids App↔surface circular import); wired `publishRunStatus(status)` into BuildSurface (covers Agent via delegation) + DeepResearchSurface — the seam is now LIVE, not inert.
- #18 wired the build replan `<QueryInput controlId="replan-send">` so it's no longer a duplicate of the main `send-message`.
- vite.config: excluded `e2e-full/**` from vitest (Playwright specs; the evidence-harness commit missed this).
- Fixed 3 campaign regressions where agent source edits broke pre-existing tests: EditAffordance name-query (loosened), ScopeControl (reverted a wrong `role="combobox"`), ThinkToggle title assertion (matched the new honest label). verify.py orphan removed (package/module collision from the evidence-harness commit).

**Regression gate (REGRESSION tier — proves wiring didn't break, NOT that controls "work"):** frontend tsc 0 · vitest 660 pass / 4 fail · Python 49/49 · ruff+basedpyright clean on campaign files.

**4 PRE-EXISTING failures (PROVEN at clean HEAD 3107e3a via a detached worktree — NOT this campaign):** PreviewPane "Static preview" (orphaned E2 bundler-detection — tied to the unused `isBundlerEntryHtml`), ResearchSurface + DeepResearchSurface UploadComposer-attach, NeedMoreCard "Build a deck" (source already `agent/false`, stale test asserts `build/true`). These are B1-B7-branch debt.

**Minor follow-ups (P2):** `build.deck-element-edit` (input+textarea share a handle) + `build.cost-meter` (visible vs hidden-sentinel) could be disambiguated; PlanPanel revise→send 2nd-step handle (part of #20) left for the owning surface.

**PROOF TIER (the real follow-up, per [[feedback-live-model-proves-works]]):** none of this is proven to WORK — it's the regression net. The live-model scenario matrix (a real model driving each surface to a real delivered output, on VM-201) is the proof campaign.
