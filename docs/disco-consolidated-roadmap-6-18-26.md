# Disco — consolidated roadmap: EVERYTHING from here to shipping (reconciled 2026-06-18)

THE single living "what's left" doc. **Reconciled against git + code on 2026-06-18** —
every status cites evidence (commit SHA / file / "verified this session"). Supersedes all
archived per-doc lists. Confidence is marked per item; do not upgrade a status without
re-checking the cited evidence.

**The ship bar (north-star §8):** a clean **8 GB keyless** box runs `docker compose up`,
every surface works through the real UI, `disco verify` passes — **green + screenshotted**.
Everything in §B serves that gate; §A/§E are product + quality beyond the install spine.

Legend: ✅ done · ◐ partial · ○ open · ⬚ blocked/decision-gated · 🔎 in-code, needs live check.

---

## How this doc is worked — the v0.1 loop (the operating contract)
1. Claude implements the open work in order — **features (§A) → engine polish (§E/§F) →
   UI polish** — checking each item off **here** with evidence (commit/file) as it lands +
   gates green.
2. **Dylan tests the running site.**
3. Dylan surfaces issues + new feature ideas → **discuss before building.**
4. Claude appends them as a **NEW section immediately after the section just marked
   complete** (the doc grows in worked order; never lose the trail).
5. Work the new section → mark off → repeat (2–5).
6. **Publish-readiness (§B launch gates) is LAST.** Do NOT start §B until Dylan says the
   feature/engine/UI loop is cleared.
7. The loop ends only when **Dylan declares v0.1 complete.** Until then, "done" on an item
   means implemented + gated + (for UI) screenshotted — never "ready to publish."

---

## §0 — Shipped + verified (evidence; do not re-litigate)
- **Driver/provider reliability** ✅ `14fe27b` (P1/P2/P3) + `e875249` (P4) — live-proven.
- **Slides pipeline** ✅ — C4 verdict loose-hybrid `034045a`; C1/C2 `1a93bd3`; C3 native
  editable PPTX `cbb9101`; C8 charts `4662978`; reconcile `b200384`; C5 `5ca117a`;
  C6 artifact_mode wired `74019e7` (verified). Export: PPTX(editable)+PDF+HTML.
- **Editor selection plumbing** §4.1/4.2 ✅ `2d18c9d` (overlay+bridge mounted).
- **Deep-Research polish** ✅ — F1 `fcb117c`, steer/inject `fa9e3d9`, FILE-DELIVERY
  F1/F2 `c804f55`, F3 `d12fe4f`.
- **Attach in initial box** ✅ `99c2058`/`235dfa3` · **noVNC P1–P4** ✅ `f6d2b8f`/`ac4edef`
  · **RP-09 audio** ✅ `f65837b` + Dylan live-tested · **Tailscale B1–B7** + **PDF export** ✅.
- **Launch prep already done:** license = **Apache-2.0** (`LICENSE`) · **CI** (`ci.yml` +
  `e2e-live.yml`) · shipping `tsc`→0 · LAN-IP scrub · `PMX_`→`DISCO_` · model-cache volume
  + healthcheck · 8 GB RAM-fit work (encoder knobs, ctx 8K, KV quant, OOM error-frame) ·
  `SECURITY.md` · eval **harness** exists (`retrieval/evaluation.py` + `evals/`) ·
  **bundled driver model scrapped** (`.env.example`: "NO bundled driver model" → BYO).
- **This session:** attach2 merge · node_modules/novnc fixes `62625d2` · deck_patch
  `e3cd1d4` + deckResolver `d94ce55` tests restored · 21 worktrees pruned · 20 docs culled.

---

## §A — Product features (open, verified)
- **A1 Website/app click-to-edit ○** — source-tag pass (§4.3) + selection→agent edit wire
  (§4.4) + app-builder reuse (§4.6). *Verified: no `data-oid` writer; selection dead-ends.*
  Plan: `archive/surgical-C-trackc-editor.md`. Effort: large (the differentiator).
- **A2 In-app deck editor ○** — mount `DeckEditor` + `LoweredDeck` route + patch round-trip
  (§4.5). Plan: `archive/deck-editor-integration-plan-6-18-26.md`. Effort: med.
- **A3 Image generation — real backend ○** — replace `_PILProceduralBackend`; the
  `DiffusersBackend` slot is empty (verified). C7 seam done `313d357`. Effort: high.
- **A4 Iterative research mode ○** — NLI-verdict-gated re-research of weak claims (no commits).
  Open design: section-coherence. Effort: high.
- **A5 DR closing-card → agent handoff ○** — seed Build w/ report + pre-approved plan (no commits).
- **A6 Real two-host podcast ○** — dialogue → multi-voice TTS → mix → deliver (no commits; unblocked).
- **A7 FILE-DELIVERY F4 ○** — on-demand DR export (deferred; needs export-on-demand model).

## §B — Launch gates (the ship bar)
- **B1 Run the clean 8 GB keyless gauntlet → green + screenshot ⬚** — north-star §8, *the*
  release gate. Pieces landed + VM-proven; the final clean-box end-to-end proof is unrun.
  Box available (blackbox LXC 199).
- **B2 Benchmark report ○** — harness exists; run it → grounding accuracy, task success,
  latency, cost (Dylan's "before launch" mandate).
- **B3 Keyless-story coherence ○** — with no bundled driver model, define/verify what
  "keyless" means at launch (BYO-first?) so §8's promise is actually true on the gauntlet.
- **B4 Release-eng residue ◐** — `git tag v0.1.0` + changelog · demo gallery · mobile-
  responsive pass. (Done: self-host/provider-matrix/SECURITY/disco verify/CI.)
- **B5 Lint debt ○** — ruff F821/B904, eslint → 0 (dishonest-green traps).
- **B6 Production hygiene ○** — strip everything that shouldn't ship in a production app.
  Verified in root today: `disco.db`, `run_manual{,2-5}.py`, `test_debug/jupyter/probe/
  pwd/re.py`, `selected.csv`/`sensor_readings.csv`/`tmp_weather.csv`, `validate.py`,
  stray `disco-config.json.bak-*`. Plus an audit sweep: no dev/debug endpoints exposed,
  no secrets/keys in the image, demo fixtures honestly labeled (per north-star §2).

## §C — Infrastructure / hardware-blocked
- **C1 noVNC P5 — live jail/security acceptance ⬚** — needs the destroyed VM rebuilt.
- **C2 D7 gVisor egress allowlist ○** — prereq for P5.

## §D — Verify-live (shipped in code, may be inert — confirm in the running app)
- **D1 FILE-DELIVERY cid renders the download on the Research surface 🔎** (truth.md D12).
- **D2 DoD evaluator inert 🔎/○** — verified: `set_dod_spec` has no production caller that
  *creates* a spec at submit_plan → finish-gate judge likely never runs (truth.md C1a holds).
- **D3 E8 podman/local egress — live-verify 🔎** (VM-202 class).
- **D4 UI screenshot acceptance ○** — every UI feature this run (decks, attach, build-surface
  fixes, noVNC toggle) owes a real Firefox-in-app screenshot; none have it.

## §E — Engine / correctness / debt (reconciled this pass)
- **E1 Single stable system prompt ○ (unverified)** — stop mutating the tools array per mode
  (KV-prefix stability). Not re-checked against code this pass.
- **E2 Auto-spill large observations to disk ○ (unverified).**
- **E3 `deploy_preview` detached long-running serve ○** — verified deferred/absent; build
  the real tool w/ a 300 s serve ceiling.
- **E4 One-feature-per-iteration scope enforcement ○ (unverified).**
- **E5 Epochal observation masking vs KV stability (HS-06) ⬚** — design-blocked (Dylan call).
- **E6 Grammar-constrained tool calls (B9) ⬚** — infra-blocked (llama.cpp `--jinja` relaunch).
- **E7 Rebuild DR-lifecycle harness from real captures ○** · **E8 real-sample backfill for
  streaming/provider harnesses ○** (test-debt; unverified scope).
- **E9 UX micro-polish ◐** — `LiveSignalBar` exists; footer-pin + main-feed auto-scroll +
  Build session-stash persistence + adjustable-plan/visual design-edit not found (likely open).
- **E10 → moved to §B6.** Root/production hygiene is a launch-prep concern (per Dylan),
  not engine polish — it lives in the publish phase now.
- *Resolved/dropped:* edit-tool demote (line tools deliberately KEPT) · package import cycles
  (lint-imports: 0 cycles) · split-monoliths + arch/import gates (all done).

## §F — Standing engineering goal
- **F1 god-function decomposition ○** — functions ≤80 LOC / classes ≤500 (gate only enforces
  ≤200/≤800). Verified still open. Plan: `archive/god-function-decomposition-plan.md`.

## §G — Decisions still owed by Dylan
1. Iterative-mode coherence approach (A4) · 2. Benchmark scope/datasets (B2) ·
3. Image-providers v1 set — confirm `openai-compatible | comfyui` (A3).
*Resolved: license=Apache-2.0 · slides=loose-hybrid · default driver=sticky · bundled model=scrapped(BYO).*

---

## §H — Sequence (per the §loop: features/polish FIRST, publish LAST)
1. **Decide** §G (cheap; unblocks features).
2. **Features §A** — A1 website click-to-edit (differentiator) → A2 deck editor → A3 image-
   gen backend → A5 handoff → A6 podcast → A4 iterative mode → A7 F4. *(Dylan tests between;
   surfaced issues get a new section after the completed one.)*
3. **Engine polish §E + standing §F** (root hygiene, prompt stability, harnesses, god-fn).
4. **UI polish** (§D4 screenshots, §E9 micro-polish) + clear **verify-live §D** as the app runs.
5. **— GATE: Dylan declares the feature/engine/UI loop cleared. —**
6. **— Dylan: "ready to push towards v0.1." THEN publish-readiness §B** — B1 single-command
   `docker compose up` gauntlet + B2 benchmarks + B5 lint + B6 production hygiene (strip
   dev/exposed files) + B4 release-eng + B3 keyless-story → v0.1. Plus §C (noVNC P5) when
   the VM is rebuilt.

*SINGLE SOURCE OF TRUTH: **this doc**. The only other file in `docs/` is
`architecture.generated.md` (auto-generated, required by the `gen_arch_diagram --check`
gate). Everything else — vision (`north-star`), user/reference docs (`self-host`,
`provider-matrix`), the `truth.md` audit, the decision record (`slides-experiment-verdict`),
and the per-feature implementation specs (`surgical-C-trackc-editor`,
`deck-editor-integration-plan-6-18-26`, `god-function-decomposition-plan`,
`design-novnc-live-browser`) — now lives under **`docs/archive/`**, referenced by archive
path where this roadmap points to detail. Restore any with `git mv docs/archive/<f> docs/`.*
