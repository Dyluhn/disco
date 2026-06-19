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
0. **MANDATORY adversarial review by ChatGPT (gpt-5.5, xhigh) at TWO gates, every time:**
   (a) **every plan** is reviewed by ChatGPT before any code is written — must clear
   (`VERDICT: PROCEED`) to proceed; (b) **every diff** is reviewed by ChatGPT before it
   moves on / commits — must clear (`VERDICT: PASS`). Wired + structurally enforced in the
   harness (`hctl review-plan` gates `run`; `hctl review-diff` gates `commit`). Opus still
   does its own review; ChatGPT's is an additional, non-skippable gate.
1. Claude implements the open work in order — **features (§A) → engine polish (§E/§F) →
   UI polish** — checking each item off **here** with evidence (commit/file) as it lands +
   gates green. Sequence per item: plan → **ChatGPT plan review** → implement →
   disco gates → **ChatGPT diff review** → Opus review → commit.
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
- **A3 Image generation ◐ (DECIDED: verify-only)** — the 3 tiers are ALL built & real:
  `procedural` (bundled) · `comfyui` (self-host) · `openai`-compatible (paid), via
  `select_image_backend()` (`image_gen.py:482`), C7 seam `313d357`. **Ship these 3; in-process
  `DiffusersBackend` SCRAPPED** (Dylan 2026-06-19 — multi-GB weights would break the 8 GB ship target;
  ComfyUI is the local-GPU path). Stub references removed from `image_gen.py`. Remaining = live-verify
  each tier in the running app + Settings CRUD screenshot. Effort: low.
- **A4 Iterative research mode ○ (DECIDED: claim-surgical + driver rework)** — per weak/unsupported
  claim (NLI verdict), targeted re-search; converge at **~80% supported OR 3 rounds**; then a
  **driver-LLM coherence rework** of the whole report if prose no longer flows. Post-synthesis
  re-research is greenfield (the existing gather-refine loop is PRE-synthesis). **Prereq:** confirm
  the verdict gate is true entailment, not `bge-reranker` reframed (else it stops early / never
  converges). Claims today: `[[text]] [[ids]]` + verdict/score (streaming.py `_verify_claims`).
  Effort: high.
- **A5 DR closing-card → agent handoff ○** — seed Build w/ report + pre-approved plan (no commits).
- **A6 Real two-host podcast ○** — dialogue → multi-voice TTS → mix → deliver (no commits; unblocked).
- **A7 FILE-DELIVERY F4 ○** — on-demand DR export (deferred; needs export-on-demand model).

## §B — Launch gates (the ship bar)
- **B1 Run the clean 8 GB keyless gauntlet → green + screenshot ⬚** — north-star §8, *the*
  release gate. Pieces landed + VM-proven; the final clean-box end-to-end proof is unrun.
  Box available (blackbox LXC 199).
- **B2 Benchmark report ○ (DECIDED: full 4-metric)** — primitives exist (`evaluation.py`
  faithfulness scorer, `eval_runner`, DoD evaluator, `obs.py` spans, `DISCO_INSPECT`, `TokenUsage`);
  MISSING = suites + aggregation. Build: grounding-accuracy suite (10-15 `evals/research/*.yaml`,
  only `vcrpy.yaml` today) + task-success-rate (3-5 DoD build scenarios) + latency percentiles +
  cost table. (Dylan's "before launch" mandate.)
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
- **C1 noVNC P5 — live jail/security acceptance ✅ (2026-06-19)** — VM 201 is BACK (reach via
  `ssh 100.81.82.115`). Built the current image under `--runtime=runsc` and accepted ON REAL gVisor:
  stack starts under syscall interception, x11vnc serves RFB 003.008, vnc.html 200, 5901→127.0.0.1
  loopback + 6080→0.0.0.0 + `-viewonly` hold, teardown reaps, host Firefox rendered the live stream
  (`4.19.0-gvisor` kernel in-frame). Evidence: `.harness/evidence/novnc-live/novnc-p5-gvisor.png`.
  **Deploy step still open:** rebuild VM 201's base sandbox image (`pmx-sandbox:base`, pre-noVNC) with
  the current Dockerfile so production live-browser works there.
- **C2 D7 gVisor egress allowlist ◐** — already implemented (filtered-egress proxy sidecar). Found
  ORTHOGONAL to noVNC P5: the live browser runs OPEN egress + the noVNC port is inbound, so it never
  gated P5. A focused allowlist live-re-verify on gVisor remains optional.
- **C3 Default slides template = PDF design ✅ (2026-06-19, `7a1ce4d`)** — deck/PPTX now carries the
  PDF's brand marks (Disco. wordmark every slide + disco-Latin·verb colophon on cover), render-time
  chrome in `_pptx_render.py` (PPTX+HTML), gated on `theme.branded`. gpt-5.5 SHIP-WITH-FIXES.

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

## §G — Decisions (ALL RESOLVED 2026-06-19)
1. **A4 iterative-mode** → **claim-level surgical patch**: per weak/unsupported claim, targeted
   re-search; **converge when ~80% of claims are supported OR after 3 search rounds**. THEN a
   final **driver-LLM coherence rework** pass re-examines the whole report and reworks prose only
   if it no longer flows. (Surgical where possible; full rework only if stitching reads badly.)
2. **B2 benchmark** → **full 4-metric**: grounding-accuracy suite (10-15 eval tasks) +
   task-success-rate (3-5 DoD build scenarios) + latency percentiles (DISCO_INSPECT) + cost table
   (TokenUsage). Build the missing suites + aggregation.
3. **A3 image providers** → **ship the current 3** (`procedural`/`comfyui`/`openai`-compatible, all
   already built & real); **in-process DiffusersBackend SCRAPPED** (Dylan 2026-06-19; ComfyUI covers
   local GPU). A3 is now a **live-verify** task, not a build.
*Earlier-resolved: license=Apache-2.0 · slides=loose-hybrid · default driver=sticky · bundled model=scrapped(BYO).*

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
