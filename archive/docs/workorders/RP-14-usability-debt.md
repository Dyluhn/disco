# RP-14 — Usability debt sweep

Parent plan: docs/next-fix-set-plan.md §2 RP-14 (locked). Four small,
independent items sharing one order so the sweep gets ONE verification pass.
Number your work and report per-item so the reviewer can accept piecemeal.

NOTE — plan drift, verified 2026-06-11:
- The plan's first item ("isolation hardcode at BuildSurface.tsx:44") is
  ALREADY FIXED — BP-15 (`09318e8`) shipped the isolation tier over the wire
  (`frontend/src/lib/isolation.ts`, "no client-side guessing"). Skip it.
- Harvest steal #10 (Suna session-status enum, plan §7) "feeds rp-14" but is
  M-effort and collides with RP-01's files — DEFERRED to a follow-up order.
  Do not touch status chips or HistoryView status logic here (RP-01 owns it).

## 1. Theme persistence

`frontend/src/lib/useTheme.ts` keeps the theme in the DOM only (its own
comment: "No storage"). Persist the choice to localStorage (key `pmx-theme`)
and read it on boot — initialization belongs where the html class is first
set (check `frontend/src/main.tsx` / `index.html`); apply BEFORE first paint
to avoid a theme flash. System-preference fallback when the key is absent.
**Accept:** toggle → reload → theme retained; key visible in localStorage;
no flash of wrong theme on load (eyeball the screenshot).

## 2. Cmd+K / Ctrl+K command palette

Global palette: navigation (New build, History, Projects, Settings) + actions
(Toggle theme). Build it on the EXISTING `@radix-ui/react-dialog` dependency —
do NOT add `cmdk` or any new dependency. Simple substring filter, arrow-key +
Enter selection, Escape closes. Mount in `frontend/src/shell/Shell.tsx`.
**Accept:** Ctrl+K opens from any view; typing filters; Enter navigates;
Escape closes; no key handler leaks into chat inputs (guard when a
text field is focused — opening is fine, hijacking typed text is not).

## 3. History search

Client-side text filter over conversation titles in
`frontend/src/views/HistoryView.tsx` (the data already loads client-side).
Debounced input above the list; clearing restores the full list.
ONLY the search box: do not touch the status-chip work (RP-01 owns it) — if
you find merge conflicts with rp-01's changes, STOP and flag, don't resolve.
**Accept:** known keyword filters live; clear restores; empty-result state
shows a "no matches" line, not a blank panel.

## 4. Cost meter

Cumulative session cost in the Build conversation header
(`frontend/src/components/build/AgentStatusBar.tsx` area). Source of truth:
token usage already present in the event stream + the per-model rates that
ALREADY exist in `frontend/src/lib/cost.ts` / `frontend/src/types/models.ts`
(`price_in_per_m`, `price_out_per_m`, `isFree`).
- Local/free models show "Free" — NEVER a fabricated dollar figure
  (no-false-affordances rule).
- Paid models: cumulative $ from in/out token counts × catalogue rates,
  updating as usage events arrive.
- If a turn's usage data is missing, show "≥" prefix (undercount is honest;
  a precise-looking wrong number is not).
**Accept:** paid-model conversation shows a cumulative figure that grows
turn-over-turn; local-model conversation shows "Free"; mixed shows the paid
portion with the "≥" marker when any usage gap exists.

## Run ONLY

`cd frontend && npx vitest run`, teed to `test-record/rp-14/units-frontend.log`.
Do NOT start vite servers (:5173 is the user's, :5174 is harness-managed) and
do NOT run e2e — the reviewer does the live pass + screenshots.

## Anti-scope

- No status chips / status enum work (RP-01 + deferred steal #10).
- No new npm dependencies.
- No backend changes of any kind.
- No theme redesign — persistence only.
- No search backend / fuzzy library — substring + debounce only.

## Manifest (the ONLY files you may touch)

- frontend/src/lib/useTheme.ts
- frontend/src/lib/useTheme.test.ts
- frontend/src/main.tsx
- frontend/index.html
- frontend/src/shell/Shell.tsx
- frontend/src/components/CommandPalette.tsx
- frontend/src/components/CommandPalette.test.tsx
- frontend/src/views/HistoryView.tsx
- frontend/src/views/history.test.tsx
- frontend/src/components/build/AgentStatusBar.tsx
- frontend/src/components/build/AgentStatusBar.cost.test.tsx
- frontend/src/lib/cost.ts
- frontend/src/lib/cost.test.ts
- test-record/rp-14/units-frontend.log
- agent-projects/gemini/rp-14-report.md

## Report

`agent-projects/gemini/rp-14-report.md`: per-item summary, test counts,
deviations flagged at top with justification.
