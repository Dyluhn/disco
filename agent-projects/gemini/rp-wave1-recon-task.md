# RP wave-1 recon — verify plan anchors against the current tree (READ-ONLY)

You are doing **read-only reconnaissance**. Do NOT edit, create, or delete any
file except the single report file named at the bottom. Do NOT run any servers,
tests, or build commands — a regression harness is running in this repo right
now; you must only READ source files.

## Task

`docs/next-fix-set-plan.md` (§2) cites file:line anchors for the wave-1 orders
RP-01, RP-02, RP-12, RP-14. Those citations were taken before several commits
landed. For EACH claim below, open the cited file, find the cited code, and
report: **CONFIRMED at line N** (give the current line number) or **DRIFTED**
(describe what you actually found and where, or state it is absent).

### RP-01 (dashboard lite)
1. `packages/core/src/perpleximanus/core/store/sqlite.py` around lines 34-56 —
   a `conversations` table schema with a `status` column that is never written
   by any code path (search the whole repo for writes to it).
2. `ConversationSummaryDTO` lacks a `status` field — find the DTO definition
   (agent-server package) and confirm.
3. `ConversationState.reconstruct()` — pure function at
   `packages/core/src/perpleximanus/core/state.py` around line 59.
4. In-process runtime-tracking dicts at
   `packages/agent-server/src/perpleximanus/agent_server/runtime.py` around
   lines 228-241.

### RP-02 (deep-report rich blocks)
5. All 5 AnswerBlock kinds have renderers in the frontend `blocks.tsx`, table
   renderer around lines 168-210; backend never emits `kind: "table"`.
6. `_to_blocks()` at
   `packages/*/retrieval/streaming.py` lines 109-171 (find the exact package).
7. `DeepReportView.tsx` lines 147-160 split markdown on blank lines (the
   raw-pipes table bug).
8. AnswerBlock union at frontend `grounded.ts` lines 48-59 — confirm NO chart
   kind exists.

### RP-12 (B-series residue)
9. Find the B-series doc in the repo (search docs/ for B2/B3/B9 — "tail
   variation", "prefill masking", kernel output discipline). Report its path
   and confirm B2/B3/B9 acceptance criteria exist in it.
10. Confirm zero code exists for tail variation and prefill masking (search
    for plausible identifiers: tail_variation, prefill, mask).

### RP-14 (usability debt)
11. Isolation hardcode at frontend `BuildSurface.tsx` line ~44.
12. Theme persistence: confirm whether any theme state persists to
    localStorage today (search the frontend for theme).
13. Cmd+K / command palette: confirm absent.
14. History search: confirm the History surface has no search input.
15. Cost meter: confirm token usage data exists in events (find the field) but
    no UI consumes it.

## Report

Write your findings to `agent-projects/gemini/rp-wave1-recon-report.md` as a
numbered list matching the claims above (1-15), each marked CONFIRMED/DRIFTED
with current file:line. Keep it terse and factual. That report file is the ONLY
file you may write.
