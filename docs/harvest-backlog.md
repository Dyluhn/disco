# OSS-harvest backlog — the steals that have NO order id yet

Created 2026-06-11. Purpose: the multi-project harvest survey
(`docs/next-fix-set-plan.md` §6 OpenHands, §7 ranked top-10, §8 OpenCode)
identified mechanisms worth stealing, but several were tagged "future order /
DC follow-up" with no id — they were evaporating. This ledger gives each one an
`HS-` id, a source ref, and a status so it survives to brief-authoring.

ALREADY SHIPPED (do not re-schedule): harvest #1 (arg coercion + JSON repair),
#6 (requery-outside-log typed errors), #7 (grammar-constrained tool calls) →
`rp-12` (committed `be5b4ff`). Harvest #10 (session status enum + CR record) →
`rp-14` (committed). Tool-withholding (DC-05) is ours, validated 4×.

## Status legend
`backlog` = id assigned, no brief. `design-blocked` = needs a Dylan decision
before a brief can be written. `verify-overlap` = a partial implementation may
already exist in-tree; the brief author MUST diff against current code first.

| ID | Mechanism (source) | Area | Effort | Status | Notes / current-tree overlap |
|---|---|---|---|---|---|
| HS-01 | **Shell spill-to-file**: rolling tail buffer + full-output temp file + path in result (50KB threshold; tail-keep for shell, head-keep for reads) — pi `executeShellWithCapture` (harvest #3) | E | S | backlog | The canonical E-area (output-truncation) order. No spill-to-file in tree (grep-confirmed 2026-06-11). Prescriptive truncation marker: say WHAT TO DO next, not just "truncated" (SWE-agent). Highest-value floater. |
| HS-02 | **Anchored-checkpoint compaction template**: fixed Markdown template (Goal/Constraints/Progress/Key Decisions/Next Steps/Critical Context/Files) as an UPDATE target — pi `compaction.ts` + OpenCode anchored-summary (harvest #5, §8) | B+D | M | verify-overlap | The BP reality-block work may already template some of this. Brief author: diff against the reality-block builder before writing. |
| HS-03 | **Scheduled facts re-grounding**: re-survey facts every N steps AND once immediately after restart — smolagents `planning_interval` (harvest #8) | B+D | S | verify-overlap | Composes with HS-02. Check whether post-restart re-grounding already fires in the resume path (BP resume work). |
| HS-04 | **Duplicate-content stuck-detector upgrade**: exact-match → n-gram/format similarity; + OpenHands scenarios (period-2 A-B-A-B, action-error ×3, ID-insensitive repeated action-obs) feeding OUR graduated valve — OpenManus `is_stuck`, OpenHands sdk `stuck_detector.py` (harvest #9, §6) | A+F | S | verify-overlap | **`packages/core/src/perpleximanus/core/loop/stuck.py` EXISTS.** This is an UPGRADE to it, not a new file. Keep our nudge→cap→pause valve; theirs hard-halts — port scenarios as detectors, not the halt. |
| HS-05 | **Critic finish-gate**: score the finish; sub-threshold finish → followup prompt instead of finishing, bounded max_iterations — OpenHands `critic_mixin.py` + `AgentFinishedCritic` (§6). Generalizes our serve gate + verify-on-finish; swap their "git patch non-empty" for "deliverables exist in workspace" | C | M | verify-overlap | BP shipped a serve/verify-on-finish gate. Brief author: confirm what the current finish gate scores before generalizing. |
| HS-06 | **Epochal observation masking**: batched (`polling`) boundary mask updates with keep/remove tags, to keep the llama.cpp KV prefix stable between mask epochs — SWE-agent `LastNObservations` (harvest #2) | D | S/M | design-blocked | **DECISION NEEDED:** per-step masking vs KV-cache stability are in direct conflict (survey §7). SWE-agent batches into epochs; pi avoids masking entirely (boundary-aligned compaction only). Pick ONE strategy before ordering — never mask per-step. Dylan call. |
| HS-07 | **ThinkTool**: no-op reasoning-dump tool — cheap prose-degeneration mitigation (§6) | F | XS | backlog | Tiny. Composes with the degeneracy-condensation we already have. Low priority. |
| HS-08 | **Reroute-to-hidden-`invalid`-tool repair**: malformed/unknown tool calls become an ordinary error tool_result via a registered-but-unoffered `invalid` tool — the turn never aborts, message invariants stay intact — OpenCode (§8) | A | S | verify-overlap | Composes with pi's repair ladder (now in rp-12). Check whether rp-12's requery path already preserves message invariants on malformed calls; if so this is redundant. |

## Sequencing suggestion (not ratified)
HS-01 is a clean standalone S — good warm-up / next-wave filler. HS-04 and
HS-08 are small and compose with the shipped rp-12 FC kit — natural follow-ups
to confirm-and-extend that work. HS-02/03/05 cluster around the
reality-block/finish-gate line and should be brief-authored together after a
single current-tree audit (they overlap). HS-06 is blocked on a Dylan design
call; HS-07 is fill-in-when-idle.

These are NOT in the RP-pack waves (§3 of the plan). They are a post-RP backlog;
promote individual HS- ids into a wave when capacity allows.
