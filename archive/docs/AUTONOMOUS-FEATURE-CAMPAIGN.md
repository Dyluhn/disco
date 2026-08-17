> **SUPERSEDED / HISTORICAL (as of 2026-07-07).** June-era autonomous feature-campaign tracker, frozen mid-execution.
> Historical only. Not a source of current status or operating instructions.

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

## noVNC "watch the agent's browser live" — SECURITY-HARDENED (2026-06-19)
Pre-built P1–P4 (`f6d2b8f`/`ac4edef`) shipped with green gates but had **never had the mandatory
adversarial review**. Running it returned **BLOCK** (4 blockers + major + minor) — the latent-broken
pattern. Hardened over 5 gpt-5.5 rounds (BLOCK→BLOCK→BLOCK→BLOCK→**SHIP-WITH-FIXES**), each fix carrying
a regression test:
- **B1** route handed the browser a *raw* sandbox `host:port` → bypassed the cid-scoped auth proxy.
  Now returns `{ready, port}`; client builds `{cid8}-6080.localhost` via `previewHostUrl`.
- **B2** 6080 stayed proxiable when Live disabled → gated `NOVNC_PORT` in `port_upstream` (the one
  chokepoint all 3 proxy consumers flow through).
- **B3** "accept anything listening" could bridge to a foreign non-view-only VNC → fail-closed (reuse
  only our tracked processes).
- **B4** teardown couldn't kill `-bg`/`--daemon` children + frontend never stopped → foreground
  process-groups + `killpg` + **reap** (caught a zombie leak empirically) + `live-stop`/`live-touch`
  routes + close/disable/conv-switch/unmount hooks (all tied to the **owning cid**) + idle watchdog
  (restart-safe; tears down partial-death survivors).
- **MAJOR** websockify loopback unreachable via publish → bind `0.0.0.0:6080`; x11vnc stays
  `127.0.0.1:5901 -viewonly` (the sensitive layer never leaves the container).
- **MINOR** Podman mislabeled as supported → corrected Settings copy.

**Live-proof (closed the "P5 hardware-deferred" gap):** built the real `disco-sandbox` image on rootless
podman, ran the hardened `live_view.py`, and a host Firefox rendered the live view-only stream end-to-end
(`.harness/evidence/novnc-live/`). P5 was only ever blocked by the *wrong* hardware (destroyed remote VM) —
the invariants are provable on any container backend. Gates: 46 noVNC py + frontend 30 + pyright/lint/tsc
clean. gVisor-specific syscall isolation + D7 egress remain genuinely hardware/infra-deferred.

## §A CAMPAIGN COMPLETE — all 7 features done (2026-06-19)
A3 · A7 · A6 · A2 · D1 · A5 · A1 · A4 — every §A feature implemented, gated, and committed on
build-surface-recovery-ux. Two documented stack-dependent live-screenshot gaps remain (A1 click→edit→steer
round-trip; A4 iterative RUN) — both need a running agent-server + real build/research, our pre-agreed flag.

### FW-C status (DONE 2026-06-19)
- **A1 click-to-edit — DONE** (`e561000` keystone stamper + `8c661ff` wiring). Server-stamped preview-edit
  route + appResolver + EditAffordance + PreviewPane edit-mode. 2 gpt-5.5 rounds (fixed a symlink jail escape +
  a frame-ancestors CSP). 32 tests. Live editor screenshot done (A2 deck); A1 website round-trip = stack gap.

### FW-D status (DONE 2026-06-19)
- **A4 iterative research — DONE** (`3cd1ea2` judge + `1d1ace1` loop + `03c4ec3` extractor + `3b46c27` engine
  glue + `a6f467c` UI toggle). Calibrated LLM judge (the mDeBERTa NLI under-credits → never converges) drives a
  re-search/re-judge convergence loop; flag-gated, OFF byte-identical. 4 gpt-5.5 rounds (combined-corpus-under-
  embedder, fresh-evidence-merge, regression-guard, all_hits-audit). Toggle screenshotted live on the DR surface.

### FW-B status (DONE 2026-06-19)
- **A2 in-app deck editor — MERGED** `56f8d94`. Orphaned DeckEditor wired (sidecar persist + GET/PUT route +
  DeckEditorPane + AgentCanvas tab). 6 gpt-5.5 review rounds, ~13 real defects fixed (each with a test).
  Live Firefox round-trip screenshot. Follow-up: multi-client edit needs a server per-deck lock.
- **D1 run-cid threading — DONE** `86de65a`. useResearch exposes runCid → AnswerDocument → in-block sheet/
  slides downloads wired (appear only with a real cid; no false affordance).
- **A5 "Build a deck from this report" — DONE** `ddeaed7`. NeedMoreCard button → autonomous build seeded with
  the serialized report → /build/{cid}, kicked once. Re-seed-on-refresh guard (history-state clear).

### FW-A status (DONE 2026-06-19)
- **A3 image-gen — DONE, MERGED** `a477eec` (11 gpt-5.5 rounds; honest fallback warning; review #12 PASS).
- **A7 DR export — DONE** `a05ef68` (verify-task; real md+pdf artifacts/WeasyPrint; fixed 1 stale e2e locator).
- **A6 podcast — DONE (DR-only).** A6.1 proven: real 25.4s 2-host MP3 from bundled Kokoro (af_heart+af_bella,
  ffprobe-verified real speech, full-pipeline live test 8/8). A6.2: 4-tier Settings AudioSection screenshot.
  **A6.3 STRUCK from scope** (Dylan decision 2026-06-19): podcast is intentionally Deep-Research-ONLY — a
  build deliverable has no narratable report text. No build/agent closing-card podcast. Evidence:
  `.harness/evidence/a6-podcast/` (audio_overview_podcast.mp3 + proof script).
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
