# Autonomous feature campaign (Dylan mandate 2026-06-19)

**Mandate:** complete ALL §A feature backlog autonomously, end every feature surface. Don't stop
for approval between waves. Decisions locked with Dylan:
- **Visual evidence = REAL Playwright/Firefox screenshots** (stand up the app per UI feature). Firefox
  is the only working browser on this host (headless chromium can't rasterize text).
- **Scope = §A features (A1–A7) + the engine prereqs a feature strictly needs** (e.g. A4's LLM-judge).
  NOT the broad §E/§F cleanup.
- **Full send on A1 + A4** — I (Opus) drive the architectural cores myself; fan out decomposable backend
  sub-parts to harness workers (MiniMax/Sonnet/codex-spark) where they fit.

## Standing discipline (every wave)
1. **Snapshot first:** `cp --reflink=auto -a ~/projects/disco ~/projects/disco-snapshots/<wave>-<ts>`
   (btrfs CoW = instant). Recovery point before any wave touches the tree.
2. Plan the feature → **ChatGPT gpt-5.5 plan review** (mandatory gate) → implement (me + workers).
3. Gate: basedpyright 0 · lint-imports · arch · pytest(not integration) · frontend tsc+vitest.
4. **Real screenshot:** stand up the stack, drive Playwright/Firefox, capture the feature working in the
   real app (the visual-evidence mandate). Evidence saved + SendUserFile to Dylan.
5. **ChatGPT gpt-5.5 diff review** (mandatory) → merge to `build-surface-recovery-ux` (no remote; never main).
6. **Post-wave contamination check:** `git -C ~/projects/disco status` — revert any worker leak into the
   main repo (`claude -p` workers are NOT jailed). Then snapshot again.
7. On a FLAKY full-suite red (e.g. `test_no_secret_in_the_box`), RE-RUN before treating as real.

## Feature wave sequence (small→big, prove the screenshot pipeline early)
- **FW-A (momentum + pipeline proof):**
  - **A3 image-gen** — backend done (3 tiers real, `select_image_backend()` `image_gen.py:482`). Build the
    missing `frontend/src/components/settings/ImageGenSection.tsx` (mirror `AudioSection.tsx`) + hooks +
    mount in `SettingsView.tsx`. Verify procedural image deliverable + Settings CRUD screenshot.
  - **A7 DR export** — mostly done (lazy md/pdf/docx `routes/report.py:192`); verify + optional in-feed card.
  - **A6 podcast** — pipeline EXISTS (`audio_overview.py`+`report_audio.py`+Kokoro cached). A6.1 RP-09 live
    accept + A6.2 TTS toggle + A6.3 agent-surface "Make a podcast" card.
- **FW-B:**
  - **D1 file-delivery** prereq (thread run-cid into `AnswerDocument`; `useResearch.ts:73`).
  - **A5 DR→agent handoff** — `NeedMoreCard` button → `createBuildConversation(autonomous=true)` + seeded
    report (reuses `engine.py:838` auto-approve). Riskiest decision: whole-convo autonomous vs one-shot.
  - **A2 in-app deck editor** — `DeckEditor` exists orphaned. A2.1 `GET /deck/editor` route → A2.2 mount in
    `AgentCanvas` → A2.3 patch round-trip (gate on DISCO_INSPECT trace, not vitest).
- **FW-C — A1 website click-to-edit (the differentiator, opus-heavy):**
  - A1.1 server-side `data-oid` writer (`preview.py`, KEYSTONE) → A1.2 sourceResolver → A1.3 overlay edit
    affordance → A1.4 apply→steer wire → A1.5 deck slice → A1.6 app-builder slice (the differentiator).
- **FW-D — A4 iterative research (opus-heavy):**
  - A4.0 LLM-judge claim gate FIRST (the reranker over-credits → loop would stop early) → A4.1 persist
    claim objects → A4.2 surgical re-search loop (≤3 rounds / 80%) → A4.3 coherence rework → A4.4 wire.

## State
- 2026-06-19: Wave 0 (lint, 426→154 ruff + secret-leak ignore) MERGED to build-surface-recovery-ux `b8330e6`.
  Harness proven live (5/6 autonomous). F-split wave drafted (`disco-harness/WAVE1.txt`) — DEFERRED (it's §F,
  not a feature). Starting FW-A → A3.
- Per-feature progress is checked off here as each lands (implemented + gated + screenshotted + merged).
