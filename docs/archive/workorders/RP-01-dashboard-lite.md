# RP-01 — Dashboard lite: status write-through + History chips

Parent plan: docs/next-fix-set-plan.md §2 RP-01 (locked). Recon verified
2026-06-10 (agent-projects/gemini/rp-wave1-recon-report.md items 1-4).

## Context — verified anchors

- `packages/core/src/disco/core/store/sqlite.py:57` — the
  `conversations` table has a `status` column that NO code path ever writes.
  History therefore cannot show live state without opening each conversation.
- `packages/app-server/src/disco/app_server/app.py:45-54` —
  `ConversationSummaryDTO` lacks a `status` field; the History surface fetches
  `GET /api/conversations` from THIS server (frontend/src/api/conversations.ts:20).
- `packages/agent-server/src/disco/agent_server/app.py:486` — the
  agent-server has its own `list_conversations`; extend consistently if it
  returns summaries (check what it returns first — if it returns bare ids,
  leave it alone).
- `packages/core/src/disco/core/state.py:64` —
  `ConversationState.reconstruct()` is pure; it is the repair path, NOT the
  hot path.
- Runtime tracking stays the in-process dicts (`runtime.py:210-259`). Do NOT
  add runtime machinery. The column is a projection/cache, not a source of
  truth.

## The decided design (locked)

1. **Write-through:** in the sqlite store's `append`, when the event being
   appended is a `StatusEvent`, also `UPDATE conversations SET status = ?`.
   Same transaction as the event insert. No new public API.
2. **Read repair:** in the store's conversation-listing path, when a row's
   `status` is NULL/empty (pre-RP-01 rows), reconstruct from events via
   `ConversationState.reconstruct()` and backfill the column once. Keep it
   lazy — repair only rows the query returns.
3. **DTO:** add `status: str | None` to `ConversationSummaryDTO` and populate
   it in the `/api/conversations` route.
4. **Frontend:** add `status` to `ConversationSummary`
   (frontend/src/types/conversation.ts) and render a status chip per row in
   `frontend/src/views/HistoryView.tsx` — RUNNING (pulse/accent), PAUSED
   (amber), FINISHED (green), STUCK/ERROR (red), unknown → no chip. Reuse the
   existing badge/chip styling already present in the app (look at
   AgentStatusBar for the established status colors) — do not invent a new
   design system.

## Acceptance ladder

1. Unit (core): NEW `packages/core/tests/test_store_status.py` —
   append StatusEvent → column updated; append non-status events → column
   untouched; NULL-status row + list → repaired via reconstruct; repair writes
   once (second list does not re-reconstruct — assert via monkeypatch/spy).
2. Unit (app-server): extend the existing app-server route tests (find them in
   packages/app-server/tests/) — summary includes status.
3. Frontend unit: HistoryView renders a chip for each status, no chip when
   status missing. Vitest, colocated `HistoryView.status.test.tsx`.
4. Run ONLY: `uv run pytest packages/core -q`,
   `uv run pytest packages/app-server -q`, and
   `cd frontend && npx vitest run src/views/HistoryView.status.test.tsx`.
   Tee outputs to `test-record/rp-01/units-{core,server,frontend}.log`.

Live spec (start a build → History shows RUNNING without opening it → flips
at terminal event; mid-run server restart → reconcile repairs the row) is the
REVIEWER's rung — do not start servers, do not run e2e, a harness may be live
on :8000.

## Anti-scope

- No new runtime dicts/threads; no websocket push of status (History polls).
- No changes to engine.py, view.py, runtime.py's tracking dicts.
- No History redesign — chips only.
- Do not touch frontend/src/api/agent.ts.

## Manifest (the ONLY files you may touch)

- packages/core/src/disco/core/store/sqlite.py
- packages/core/tests/test_store_status.py
- packages/app-server/src/disco/app_server/app.py
- packages/app-server/tests/ (existing route test file only)
- packages/agent-server/src/disco/agent_server/app.py (only if its
  list route returns summaries — else leave)
- frontend/src/types/conversation.ts
- frontend/src/api/conversations.ts
- frontend/src/views/HistoryView.tsx
- frontend/src/views/HistoryView.status.test.tsx
- test-record/rp-01/units-core.log
- test-record/rp-01/units-server.log
- test-record/rp-01/units-frontend.log
- agent-projects/gemini/rp-01-report.md

## Report

Write `agent-projects/gemini/rp-01-report.md`: what changed per file, exact
test counts, any deviation WITH justification. Deviations from this brief
require explicit flagging at the top of the report.
