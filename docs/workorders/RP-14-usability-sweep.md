# RP-14 — Usability debt sweep (post-recon scope: theme persistence + Cmd+K)

Parent plan: docs/next-fix-set-plan.md §2 RP-14, ADJUSTED by the 2026-06-10
recon (agent-projects/gemini/rp-wave1-recon-report.md items 11-15):

- Isolation hardcode — ALREADY FIXED (BuildSurface.tsx:44-53 fetches /state).
  DROPPED from this order.
- History search — ALREADY EXISTS (HistoryView.tsx:60-72). DROPPED.
- Cost meter — BLOCKED: token usage is NOT in events (only tracing spans,
  agent.py:136-137). Needs an event-side usage field first — deferred to
  wave 2 (rides the RP-04 engine work). NOT in this order.

Remaining scope: two frontend-only items.

## Rung 1 — Theme persistence

- `frontend/src/lib/useTheme.ts:4` — theme state lives only in the DOM and
  resets on reload. Persist to localStorage (`pmx.theme`), hydrate on load,
  default to existing behavior (system/current default) when unset.
- Respect whatever toggle UI already consumes the hook — no visual changes.
- NEW colocated `useTheme.test.ts`: set → reload (re-mount) → persisted;
  unset → default; invalid stored value → default (no crash).

## Rung 2 — Cmd+K command palette

- NEW `frontend/src/components/CommandPalette.tsx` — NO new dependencies
  (no cmdk). Hand-rolled: fixed overlay + fuzzy-ish substring filter +
  keyboard nav (↑/↓/Enter/Esc). Match the app's existing styling (look at
  existing modals/overlays for tokens/classes; keep it minimal).
- Commands v1 (keep it to these):
  - "New conversation" → navigate to the home/new surface
  - "History" → navigate to History
  - Theme toggle (light/dark) → uses the rung-1 hook
  - Open recent conversation (list from the existing conversations API the
    History surface already uses — reuse its fetch hook, read-only)
- Mount once at the app root (`frontend/src/App.tsx` or wherever the router
  shell lives) with a global Cmd+K / Ctrl+K listener. Do NOT touch
  HistoryView.tsx (RP-01 owns it this wave).
- NEW colocated `CommandPalette.test.tsx`: opens on hotkey, filters, Enter
  triggers the command (assert navigation/callback), Esc closes.

## Run ONLY

`cd frontend && npx vitest run` — full suite green (known pre-existing flake:
ResearchSurface.test.tsx — if it fails alone, note and move on). Tee to
`test-record/rp-14/units-frontend.log`. Do NOT start vite, do NOT run e2e —
screenshots are the reviewer's rung.

## Anti-scope

- No new npm dependencies.
- No backend changes of any kind.
- No HistoryView.tsx / conversations.ts edits (RP-01 owns them this wave).
- No restyling beyond the palette component itself.

## Manifest (the ONLY files you may touch)

- frontend/src/lib/useTheme.ts
- frontend/src/lib/useTheme.test.ts
- frontend/src/components/CommandPalette.tsx
- frontend/src/components/CommandPalette.test.tsx
- frontend/src/App.tsx (or the actual router shell — name it in the report)
- test-record/rp-14/units-frontend.log
- agent-projects/gemini/rp-14-report.md

## Report

`agent-projects/gemini/rp-14-report.md`: what changed per file, test counts,
deviations flagged at top with justification.
