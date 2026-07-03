# disclaude PR / order status

**Branch:** `disclaude/experimental-20260628T025508Z`
**HEAD:** `45e74def` — REL-6 PASSED (2026-07-02)
**Snapshot written:** 2026-07-02 (post REL-6, post live-UI smoke)

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
| REL-1e | Verifier **authority flip** behind `DISCO_HOST_VERIFY_AUTHORITATIVE` | 🔵 READY-BUT-OFF (100% shadow agreement banked) |
| REL-2a | Passive shared artifact **manifest** — fold + projection + write-tool evidence | 🟡 shadow-proven; **reader promote pending** |
| REL-3 | Audit mode — deny-log drove phase-neutral tool-set tuning | ✅ ACCEPTED |
| REL-4 | Terminal-conversation cleanup — destroy orphan sandbox + egress containers | ✅ SHIPPED (2→0 containers proven) |
| REL-5 | Fail-closed terminal-cleanup adjudication on the headless soak | ✅ SHIPPED (Codex APPROVE) |
| REL-5b | Provider-attribution by `has_tools` (exclude benign post-terminal auto-title) | ✅ ACCEPTED |
| REL-6 | Full scenario matrix on production-valid backend | ✅ **PASSED 95/95** |

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
| **P6** Finalizers | P6-FINALIZERS | Contract-verification finalizer as a finish-alias | 🟢 landed; builder/verifier **context split** + wake-on-fail still a gap |
| **P7** Kits | P7-KITS | Starter kit registry + 2 scaffolds (`app_shell`, `lead_form`), path-safety, single-source | ✅ ACCEPTED — but 🟡 **thin catalog** (2 of 6 spec'd; no deck_stage / image-slot / device-frames / animation / metrics-overlay under the funnel) |
| **P8** Semantic Direct Manipulation | P8B (`data-disco-*` metadata) landed | Point-at-element editing, comment anchors, edit-overrides | ⚪ **core NOT started** (P8B conventions only; playbook §10 is the spec seed) |
| **P9** TweakSpec | P9B (`.disco/tweaks.json` IO), P9C (`app_set_tweak`) | Tweak IO + typed tweak tool; small-change / versioning-by-copy discipline | 🟡 tweak plumbing ✅; the prompt-side small-change discipline is cheap + remaining |
| **P10** Export/Handoff | P10b (export smoke, in REL-6) | Export path + capture-side validation flags the model must judge | 🟡 PARTIAL (smoke passes; validation flags remaining) |
| **P11** Feature + release | — | The actual product features + open-source release engineering | ⚪ **UNBLOCKED** by REL-6, not started |
| (new) questions_v2 | — | Structured pre-plan clarification intake | ⚪ NOT STARTED (small) |

---

## What's uncommitted right now

- Live UI build smoke (`frontend/e2e-live/live-smoke.spec.ts` + `live-smoke.config.ts`)
  — **PASSED** (exit=0, 2.6m, MiniMax-M3, artifact rendered). Hook-based, not text-based.
- This status doc + the UI-readiness ledger entry in `disclaude.md`.
- (No production code changed today — verification + docs only.)

## Suggested next sequence (Dylan's week ends ~2026-07-08)

1. **P9 prompt discipline** — cheap, kills rewrite-thrash.
2. **P10 export validation flags** — small delta on passing smoke; closes export false-affordances.
3. **P7 catalog depth** — `image-slot` (the purest anti-false-affordance kit) + fold in existing `deck_stage`.
4. **P8 semantic direct manipulation** — the one genuinely large build; decide in-scope-this-week vs first-after-release.
5. **P11 release engineering** — open-source the result.

Optional flips (evidence already banked): REL-1e authority, REL-2a reader promote.
