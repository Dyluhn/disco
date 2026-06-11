# BP-12 — First-class Build resume

**Read `README.md` first. Independent; BP-13 builds on it.**

## Why

Resume for Build is second-class: the runtime's `resume()` path is Deep-Research-specific
(known defect: DR resume re-runs completed sub-questions; resumed view shows a
"(resumed)" placeholder), and the Build UI's Resume button rides whatever
send-message side effects exist. A PAUSED or interrupted build must resume into the SAME
loop state the event log describes — this is exactly what the event-sourced architecture
is for.

## Step 0 — verify the current wiring (report before coding)

Read and report (file:line) what `AgentStatusBar`'s Resume (`onResume`,
`frontend/src/components/build/AgentStatusBar.tsx`) actually calls today, and what the
runtime does for a Build conversation on that call. Your implementation replaces guesses
with the explicit path below; if a real `POST /conversations/{cid}/resume` already
exists, you are upgrading it, not duplicating it.

## The decided design

### 1. Runtime: `async def resume_conversation(cid) -> dict` (`agent_server/runtime.py`)

Mode-agnostic, event-log-driven:

- Load events; `ConversationState.reconstruct(...)` (the loop already does this — reuse,
  don't re-derive).
- Legal from: `PAUSED`, and `IDLE`/interrupted-with-unfinished-plan (a PlanEvent approved
  but `FINISHED` never reached). Illegal from RUNNING (409), FINISHED/ERROR (409 with
  reason — a follow-up message, not resume, is the path there).
- Append `MessageEvent` (environment): `"Resumed by user."` — so the model knows time
  passed and re-orients from its plan recitation (GAP D already pins
  objective+checklist at the tail; that is the re-orientation mechanism — no new
  machinery).
- Re-kick the loop exactly the way a user message does (same task-spawn path —
  locate the send-message handler and share its code path; do NOT spawn a second loop if
  one is alive).
- Sandbox: nothing special — `SandboxSession` lazily `_ensure()`s and `_recreate()`s; a
  resume after agent-server restart gets a fresh sandbox and the workspace is restored
  from the persisted workspace dir (verify this restore path exists from the
  persistent-workspaces work [42249bb]; if workspace rehydration into a NEW sandbox is
  missing for your backend, STOP and report — that is a real gap to surface, not to
  paper over).

### 2. Route: `POST /conversations/{cid}/resume` (`agent_server/app.py`)

Returns `{"ok": true, "status": "RUNNING"}` or 409 `{"ok": false, "reason": …}`.

### 3. Frontend

- `agent.ts`: `resumeConversation(cid)` → the route. `AgentStatusBar` Resume button calls
  it; button shows for PAUSED **and** for the interrupted-with-unfinished-plan state
  (derive from the state frame; if the wire state can't distinguish it, extend the state
  frame — list the change).
- Resumed view correctness: after resume, the feed shows the REAL history (event replay
  over WS already does this on subscribe) — assert no placeholder text. (The "(resumed)"
  placeholder defect is DR-side; if your changes touch shared code, do not regress DR —
  run the DR e2e spec.)

### 4. Deep Research stays out of scope

Do not refactor DR's checkpoint resume here. Only ensure `resume_conversation` routes DR
conversations to the EXISTING DR resume behavior unchanged (dispatch on conversation
mode), so the route is uniform.

## Acceptance

1. **Unit**: legality matrix (each status → allowed/409); environment message appended
   exactly once per resume; double-resume race (second call while RUNNING → 409).
2. **Integration (process backend)**: start a real scripted build, Stop it mid-run
   (graceful stop → PAUSED), call resume → loop continues from the event log: the next
   actions build on prior state (assert the plan-step pointer did not reset and no
   completed step re-executes).
3. **Restart-survival (the hard one)**: same as 2 but `kill -TERM` the agent-server
   process between Stop and Resume; restart it; resume. Assert: orphan reconciliation
   didn't mangle the conversation, workspace files survived, build completes. Save logs →
   `test-record/bp-12/`.
4. **UI surface (live, Firefox)** — `bp-12-resume.spec.ts`: drive Stop → Resume through
   the real UI on a live build; assert the Resume button appears on PAUSED, the feed
   continues (event count strictly grows, no duplicate rendering of old steps), and the
   build reaches FINISHED. Screenshots: `paused.png`, `resumed-running.png`,
   `finished-after-resume.png` → `test-record/screenshots/bp-12/`, sent to user.

## Prohibitions

- No state reconstruction outside the event log (no side-channel "resume files").
- No re-planning on resume — the approved plan stands unless the model itself revises it.
- Do not "fix" DR sub-question re-runs here (owned by a later order; report if you see
  it, leave it).
