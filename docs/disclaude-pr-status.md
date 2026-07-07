> **SUPERSEDED / HISTORICAL (as of 2026-07-07).** Old disclaude PR / ship-ladder tracker, pinned to an experimental branch/HEAD.
> Current status of record: `docs/disco-project-state.md` (master), `docs/disco-status-and-remaining.md` (features + remaining), `sec-work-remaining/disco-security-state.md` (security). This file is kept for history and may contain stale claims.

# disclaude PR / order status

**Branch:** `disclaude/experimental-20260628T025508Z`
**HEAD:** `b1e9ded6` — REL-1e FLIPPED: host-verify authoritative is the code default; `unavailable` degrades to the inline gate (2026-07-03)
**Snapshot written:** 2026-07-03 (post P10 convergence + finish-path-drift fix `7043fcab` + REL-1e authority flip)

Status of every disclaude campaign work item (the P-phases + the REL reliability
ladder). Gate legend:

- **✅ ACCEPTED** — landed + Codex gpt-5.5 APPROVE (or Fable-ruled done).
- **🟢 DONE (no external gate)** — landed + Fable-verified; Codex not a binding gate.
- **🟡 PARTIAL** — mechanism landed, depth/semantics incomplete (gap noted).
- **🔵 READY-BUT-OFF** — code landed behind a flag; evidence banked, not flipped.
- **⚪ NOT STARTED** — spec exists, no code.
- **📋 PLAN-ONLY** — plan/spec committed, implementation not begun.

> Governance reminder: Codex review is *pushback, not a binding APPROVE gate*.
> "ACCEPTED (Codex APPROVE)" in commit subjects records convergence, not permission.
> Fable drives with final say (see [[disclaude-claude-design-pivot]]).

---

## Reliability gate (REL ladder) — COMPLETE ✅

The whole reason for the campaign: structurally kill random pauses, loops, and
false completeness. **This bar is met** — REL-6 passed 95/95 at 100% per class,
0 OpenRouter (9245 MiniMax calls), 0 orphans, under 3-lane parallel load.

| Item | What it did | Status |
|---|---|---|
| REL-1a | Extract host-callable verifier helpers (pure refactor) | ✅ ACCEPTED |
| REL-1b | Verifier events + manifest verification fields (schema) | ✅ ACCEPTED |
| REL-1c | Host verifier in **shadow** (fallthrough-only, universal injection) | ✅ ACCEPTED |
| REL-1d | Host-verify canary — verdicts drive phase tracker + manifest | ✅ ACCEPTED |
| REL-1e | Verifier **authority flip** — host verify gates app finishes | ✅ **FLIPPED, default ON** (`b1e9ded6`, 2026-07-03) — see note below |
| REL-2a | Passive shared artifact **manifest** — fold + projection + write-tool evidence | 🟡 shadow-proven; **reader promote pending** |
| REL-3 | Audit mode — deny-log drove phase-neutral tool-set tuning | ✅ ACCEPTED |
| REL-4 | Terminal-conversation cleanup — destroy orphan sandbox + egress containers | ✅ SHIPPED (2→0 containers proven) |
| REL-5 | Fail-closed terminal-cleanup adjudication on the headless soak | ✅ SHIPPED (Codex APPROVE) |
| REL-5b | Provider-attribution by `has_tools` (exclude benign post-terminal auto-title) | ✅ ACCEPTED |
| REL-6 | Full scenario matrix on production-valid backend | ✅ **PASSED 95/95** |

**REL-1e flip note (2026-07-03).** The prior "100% shadow agreement banked" claim was
**unsubstantiated** — no data existed, because a finish-path drift meant host-verify never ran
on `completed_via_notify` builds (how most autonomous MiniMax builds finish). Fixed first
(`7043fcab`: shared `run_finish_verify_gates()` — both finish paths run host+browser+export
gates), THEN fresh live evidence was gathered (all MiniMax-M3 direct, 0 OpenRouter):
2/2 shadow builds host-ran + agreement=true; 2/2 authoritative good builds finished clean
(no false-block); a deliberately-broken app (load-time ReferenceError) was refused 2× by the
host verifier, **repaired by the model**, and passed — the gate drives repair. The flip
(`b1e9ded6`) also hardened two footguns found in review: an `unavailable` host verdict
(verifier infrastructure absent — browserless install, timeout) degrades to the inline
browser gate instead of burning refusal cycles, and browser-gate delegation now requires a
**recorded host pass/fail** (previously mode-only — `unavailable` would have bypassed BOTH
gates). `DISCO_HOST_VERIFY_AUTHORITATIVE=off` restores the REL-1c shadow posture.

### REL-RC (root-cause fixes found during the matrix) — all ✅

Each killed one build-loop defect category; zero recurrence after fix.

| Item | Defect killed |
|---|---|
| REL-RC-G / G2 | Line-edit refusals now carry fresh anchors + count as reads (self-recovering) |
| REL-RC-H | Replan gate holds on terminal-idle follow-up pickups |
| REL-RC-I | Planning-mode refusals escalate to steering → narrowing |
| REL-RC-J | Finish gate counts **outcomes**, not attempts |
| REL-RC-K / L | Pair-atomic history rendering; no-op edit escalation |
| REL-RC-M | Byte-identical writes refused as `no_op_write` |
| REL-RC-N | Prose-plan harvest + force-submit ladder hole |
| REL-RC-O | Dictated-content finish floors (quoted literals gate FINISHED) |
| REL-RC-P | Synthetic finish after actionless-pause loop (the "dawdle" cure) |
| old_text_not_found | Refusals carry ground truth (edit-family complete) |
| detail-cap | Delivered-content refusals escape the 600-char cap (80KB) — was silently gutting ALL self-recovering refusals |
| _shell_removes | Strict pure-rm exact-path (export false-absent finding #5) |
| kill-pair-closure | Kill closes in-flight action pairs (DISCONNECT class) |

**BAR MET intermediate:** attempt 7 = 10/10 PASS, 0 STUCK, 0 INVALID (`da2b2039`).

---

## Playbook phases (P0–P11)

| Phase | Item(s) | What it adds | Status |
|---|---|---|---|
| **P0** Context Runtime | CXT-1..7 | Ledger/pack/compaction models, durable `.disco/context`, agent-driven deferred **snip**, byte-stable renderer, recoverable compression, `todo.md` live memory, runtime integration | 🟢 **P0 gate GREEN** (CXT-1..7 all landed) |
| **P1** Harness | P1B-LIVE-1/2/3a/3b + STABILITY | Product-evidence TS bridge, scenario runner, **MiniMax relay → repo**, live MiniMax-M3 build proven, capture→classify automation, fail-closed stability | 🟢 **P1 DONE** (live-proven) |
| **P2** Contract Runtime | (contract registry/models) | Artifact contract exists | 🟡 PARTIAL — needs single-mandated-format + host-assembles-document semantics |
| **P3** WorkflowPromptPack | WPP-1/2/3 | Prompt-pack format + 5 bundled packs, kernel-neutral assembly, skill-mount policy (no global skill soup) | ✅ ACCEPTED |
| **P4** Mutation Tools | TOOL-1 (AppKit) | Specialized `app_*` mutation tools | 🟡 landed; needs streaming edit + hot-reload + blunt-tool removal |
| **P5** Delivery | P5-DELIVERY | Host-owned delivery shape (app\|files) per contract | ✅ landed |
| **P6** Finalizers | P6-FINALIZERS | Contract-verification finalizer as a finish-alias | 🟡 **~80%**. Finalizer real; host verifier **GATES by default** since the REL-1e flip (`b1e9ded6`, 2026-07-03). Remaining: true builder/verifier **context split** (verify runs in-band in the builder's own context) — planned as §G of the gap-close plan |
| **P7** Kits | P7-KITS | Starter kit registry + 2 scaffolds (`app_shell`, `lead_form`), path-safety, single-source | ✅ ACCEPTED — but 🟡 **thin catalog** (2 of 6 spec'd; `image-slot` genuinely missing — the purest anti-false-affordance kit; `deck_stage` exists elsewhere, not under the funnel) |
| **P8** Semantic Direct Manipulation | selection_edit wire (2026-07-03) | Point-at-element editing | ✅ **WIRED + LIVE-PROVEN**. Click element → `EditAffordance` → `selection_edit` frame → host-owned scoped-edit directive (`core/selection_edit.py`) → targeted `file_edit` on ONLY that element. Live: h1 `NightOwl Coffee`→`NIGHTOWL_HERO_EDITED`, rest unchanged, no rewrite, MiniMax-only. `selection_agent.js` now reads `data-disco-*` (semantic enrichment). Dead `formatEditSteer` prose path removed. ⚠️ Open finding: a selection-edit that contradicts a build's dictated literal is reverted by the REL-RC-O finish floor (separate fix) |
| **P9** TweakSpec / small-change | P9B (`.disco/tweaks.json` IO), P9C (`app_set_tweak`) | Small-change discipline + versioning-by-copy | ✅ **DONE + deep (audited)**. Multi-layer prompt + "Targeted edit law" in every pack + 5 tool guards (F1/RC-M/RC-L/CD-TOOLS-1/otnf) + AppKit semantic tools + **REL-6 TARGETED 30/30**. Residue (minor): no generic versioning-by-copy snapshot; `file_write` desc over-nudges rewrites; F6 rewrite-directive computed-but-unwired (intentional, assist-only) |
| **P10** Export/Handoff | export render-correctness gate + Codex hardening (2026-07-03, `3185e46d`) | Export path + capture-side render check the finish gate consults | ✅ **LIVE-PROVEN + hardened (7 adversarial Codex rounds)**. `core/contract/export_render.py` parses ACTUAL output bytes (PDF pages / PPTX slides+text-or-media / HTML distinct sections+text-or-media) → `ExportRenderFacts` stamped at a single read-back choke point in `SlidesGenerate.run` → `gate_export_render` refuses FINISHED for blank/truncated/corrupt (bounded cap → honest release). First real EXECUTOR of the once-inert `ExportContract.validate`. Live: MiniMax-M3 6-slide C3 deck (ok, FINISHED, 0 refusals, 0 OpenRouter). **Codex adversarial rounds: 8 defects→5 fixed+3 doc'd (`7fd8aa0e`); 4 second-order regressions→Codex-implemented (`f9e94644`); truncation ±1 + object-data (`4efd6a01`); SVG catastrophic-backtracking→linear rewrite (`921db935`); then a measured-driven STRUCTURAL pass (`378fa250`): every lazy `.*?</tag>` scan made linear — script/style is now a `str.find` walk (NOT a cap: a real deck ships a 2.48MB inline three.js bundle in one `<script>`, so a small cap would re-inflate content), chrome/pptx inner-scans bounded; round-6 review then found a REAL in-threat correctness false-pass → Codex-implemented single-source brand-chrome fix (`3185e46d`).** The round-6 finding: a genuinely BLANK deck on the branded template rendered to PPTX passed as ok=True (56 chars of pure chrome) because the PPTX path stripped chrome by a hand-copied token list that DRIFTED from the renderer (missed the "Disco" wordmark + the "LATIN · VERB /ˈdɪs.koː/" colophon). Fix: `core/brand/mark.py` `BRAND_CHROME_TEXTS` is now the SINGLE SOURCE both the renderer chrome and the checker consume; `_strip_chrome` is case-insensitive + whitespace-normalized; a REAL-RENDER drift test refuses a blank branded deck. Proven vs the real renderer: blank branded 56ch/ok=True→7ch/ok=False (refused); content deck still passes (326ch). **Stall-safety: Codex confirmed PASS** — every scan linear on realistic/truncated input; only residual O(n²) is `_TAG_RE` on adversarial `"<"*N`, out of the trusted-renderer threat model. **Then a 4-lane PARALLEL convergence panel (blank / truncation / gate / regression — replacing the serial one-finding-per-round loop) caught + fixed, in one batch (`b18563ee`, `a19c150f`): 3 REGRESSIONS my own hardening introduced** (İ length-changing-lowercase index-misalign dropping content; bare "disco" substring gutting "Discovery"; media-only PDF false-refuse), **a blank-completeness class** (strip `<head>`/`<title>`; new `RENDER_PLACEHOLDERS` "[image]"/"(no table data)"/"[no data]" subtracted so empty-placeholder decks read blank), **and 2 gate holes** (G2 per-export refusal cap so an old deck's refusals don't release a new one; G4 `export_render_facts_for_path` binds facts to the delivered file so delivering a known-bad export can't clear on a newer good sibling). Now: blank-BRANDED refused on html+pptx+pdf, real "disco*" words/İ decks pass, image-only decks pass, stale-app can't mask a bad deck, gate bound to path + per-export cap, 2.48MB body stripped. ~76 unit + real-loop + real-render tests (incl. 3 SIGALRM stall-guards). **Documented residuals (in threat model, kept as tradeoffs):** marp ±1 slack, media-by-existence, single CRC-bad slide, marp-PDF byte-floor, bounded cap-release; **+ threat-model limits:** crafted non-renderer bytes (P10-4/5), client-supplied source refs (P8-1) |
| **P2** Contract Runtime | contract registry/enforce/scopes | Artifact contract | 🟡 **~65% (audited)**. REAL host-owned **tool-scope + delivery-shape** enforcement runtime (not thin). Missing: host-assembles-document-from-parts + single-format schema validation (model still hand-writes whole files); `ExportContract` pipeline tuple is **inert data, no executor** |
| **P11** Feature + release | — | Open-source release engineering (README, secret scrub, self-host, packaging, CI) | ⚪ **UNBLOCKED, not release-grade — the real ship gate.** A README + ci.yml/e2e-live.yml exist but no secret-history scan, no clean-box self-host verification, no packaging/release workflow. License blocker RESOLVED (MIT reranker default). Full plan: gap-close §J |
| (new) questions_v2 | — | Structured pre-plan clarification intake | ⚪ NOT STARTED (small) |

---

## What's uncommitted right now

- (P8 committed as `c0a3f5aa` — see the P8 row above. All gates green except the
  pre-existing arch-budget god-object red, which P8 does not touch.)

## Corrected next sequence (ground-truth audit, 2026-07-03)

The 6-phase re-audit (P2/P6/P7/P8/P9/P10) found the stale doc wrong in 4 places:
P9 is DONE (not remaining), P8 is 60% scaffolded (not unstarted), P6/P2 are real
runtimes (not thin), and the license blocker is already resolved. So the features
are more built than believed — **the real remaining work is (1) closing the last
false-affordances-of-completeness and (2) release engineering.**

**Tier 1 — kill the remaining false-completeness (ON-MISSION: Dylan's #3 hated mode):**
1. ~~**P10 export-correctness gate**~~ — ✅ **DONE + live-proven (`43b1c196`, 2026-07-03).** A
   capture-side render check the finish gate consults; a blank/truncated/corrupt export can't
   report FINISHED. First real executor of `ExportContract.validate`.
2. ~~**Flip REL-1e host-verify authoritative**~~ — ✅ **DONE + live-proven (`b1e9ded6`,
   2026-07-03).** Default ON; broken apps are refused and driven to repair; `unavailable`
   degrades honestly. Required fixing the finish-path drift first (`7043fcab`).

**Tier 2 — honesty pass (Dylan hates dead scaffolding "written to look done"):**
3. Remaining seams (P8 semantic layer WIRED `c0a3f5aa`; `ExportContract.validate` got its
   real executor via P10): F6 rewrite-directive (wire weak-assist-only OR delete — plan
   §C1), the P8×RC-O selection-edit-revert conflict (plan §C2), and honest disposition of
   the rest of the `ExportContract.pipeline` tuple (plan §E). Don't ship dead code that
   reads as a feature.

**Tier 3 — P11 release engineering (the actual ship gate):**
4. README, secret scrub, self-host verification, packaging, CI. License already clear.

**Deferred / optional bigger builds (not release-blocking):**
5. P8 full semantic scoped-edit wire (~1-2d, marquee UX) · P7 `image-slot` kit ·
   P6 builder/verifier context split · P2 host-assembles-document.

Optional flips: ~~REL-1e authority~~ (✅ flipped `b1e9ded6`), REL-2a reader promote (evidence banked).
