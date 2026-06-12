# DC-05b — resume-path context reconstruction (DEFECT-4 root cause)

**Read `README.md` first. De-complexity Wave 0 (docs/decomplexity-wave-plan.md DC-05).
Scope = packages/agent-server ONLY — the loop-side breakers/dedup/retry are
dc-05a (packages/core, runs in parallel; do NOT touch packages/core or its
tests).**

## Why (evidence: test-record/marathon/DEFECTS.md, DEFECT-4)

Deterministic 2/2: after SIGTERM → respawn → orphan sweep → UI Resume, the
agent NEVER tool-calls again — it narrates, revises plans, spams knowledge —
because the reconstructed context is pathological:
1. the interrupted in-flight action (seq-20 npm install, mid-flight at
   SIGTERM) has **no observation** — the transcript leaves it in an ambiguous
   done/not-done state;
2. nothing tells the model the CURRENT environment reality (sandbox was
   reclaimed and will rehydrate; which sessions exist NOW; what files
   survived);
3. the resume nudge is a bare "Resumed by user." — the full narrative history
   then teaches the model to keep narrating.

The same model tool-calls correctly from step 1 of every fresh run — this is
the resume prompt's shape, not the model.

Code as of HEAD (`packages/agent-server/src/disco/agent_server/runtime.py`):
- `resume_conversation()` ~1755: checks status, appends
  `MessageEvent(source=ENVIRONMENT, message=LLMMessage(role="user",
  content="Resumed by user."))` (~1790-1793), flips
  `StatusEvent(RUNNING, detail="resumed")` (~1807-1810), re-kicks the loop.
- Context is rebuilt by `View.of(events)`
  (packages/core/src/disco/core/view.py:228) — renders events as-is;
  an ActionEvent with no correlated ObservationEvent/AgentErrorEvent just
  dangles. (You are NOT changing view.py — you append real events instead.)
- Event shapes: packages/core/src/disco/core/events.py (ActionEvent,
  ObservationEvent + their correlation fields — read it; match the
  tool_call_id pairing exactly so View renders the pair correctly).
- Workspace snapshot listing: `self._project_store_now()` → `store.get(cid)`
  / manifest (see `wake_for_preview` / `_maybe_rehydrate` ~1354 for the
  established access pattern). Session list: `self.sessions_snapshot(cid)`
  (dc-04b, degrades instead of raising).

## The decided design (locked)

All three pieces happen inside `resume_conversation()` BEFORE the status
flip to RUNNING, in this order, as a new private method
`_reconstruct_resume_context(self, conversation_id, events) -> list[Event]`
that RETURNS the events to append (so it is unit-testable on an archived
event list without a live runtime):

### 1. Synthesize terminal observations for dangling actions

- Scan `events` for ActionEvents whose tool_call id has no matching
  ObservationEvent / AgentErrorEvent later in the log.
- For each (normally 0 or 1), build an ObservationEvent paired to that
  tool_call id with content:
  `"<system-reminder>This action was interrupted by a server restart — its
  outcome is UNKNOWN. Re-verify its effect before assuming it completed.
  </system-reminder>"`
- Use the exact source/correlation conventions other observations use so
  `View.of` renders a clean action→observation pair.

### 2. Environment reality block (replaces "Resumed by user.")

One ENVIRONMENT MessageEvent (role="user") composed of:
- header: "Resumed by user after an interruption. Current environment
  reality:"
- sandbox: "- The previous sandbox was reclaimed. A fresh sandbox is created
  on your next action and your saved workspace files are restored into it
  automatically." (this is what `_maybe_rehydrate` guarantees)
- workspace: "- Files that will be restored: …" — up to 30 paths from the
  project-store snapshot for this cid (from the store record/manifest; if no
  snapshot: "- No saved files — the workspace starts empty.")
- sessions: from `await self.sessions_snapshot(cid)` — list names or
  "- No shell sessions are running."

### 3. Restate the active plan step as the next actionable instruction

- Find the latest plan in `events` and its first undone step (reuse the
  same plan_step bookkeeping the log uses — see how plan events + plan_step
  done-marks are recorded; packages/core loop emits them).
- Append to the SAME reality-block message: "Next actionable step (N):
  '<step text>'. Do not re-plan and do not summarize — execute this step
  now using tools."
- No plan / all steps done → omit this line.

### De-scoped (do not build)

- The driver health-gate on resume: dc-05a's in-loop LLMTransientError
  retry already covers a dead driver at the first post-resume step.
- No changes to view.py, no frontend, no orphan-reconciliation changes
  (its boot-time message stays as-is).

## Acceptance ladder

1. **Unit — `packages/agent-server/tests/test_resume_reconstruction.py`**
   (NEW). MUST cover:
   - dangling action → exactly one synthesized ObservationEvent, correctly
     paired (id + ordering), content carries "interrupted by a server
     restart";
   - no dangling action → no synthetic observation;
   - reality block: contains the restore-files list (seeded fake project
     store), the sessions line (mock `sessions_snapshot`), and the sandbox
     reality sentence;
   - plan restatement: seeded plan with steps 1-2 done of 4 → names step 3
     verbatim; no plan → line absent;
   - **DEFECT-4 replay**: load
     `test-record/marathon/events-conv_c1b4675689484c63b1f45d92d45b95be-attempt3-loop-snapshot.json`,
     slice events up to (and including) the first
     StatusEvent PAUSED, deserialize through the store's event codec, run
     `_reconstruct_resume_context`, assert: the seq-20 install action gets
     the synthesized observation; the reality block is present; the
     restatement points at the first undone step. (This is the archived
     REAL defect register — the test proves the reconstructor fixes the
     exact captured shape.)
   - `resume_conversation` integration: appended events land in the store in
     order (reconstruction events BEFORE the RUNNING status flip).
2. Run ONLY `uv run pytest
   packages/agent-server/tests/test_resume_reconstruction.py
   packages/agent-server/tests/test_resume.py
   packages/agent-server/tests/test_lifecycle.py -x -q`.
   Log → `test-record/dc-05/units-server.log` (create the dir if needed).
3. **Report** — `agent-projects/sonnet/dc-05b-report.md`: what changed,
   verbatim test output, deviations declared honestly. Do NOT commit; no
   git commands beyond read-only.

## Manifest (orders.yaml `dc-05b` — touch nothing outside it)

- packages/agent-server/src/disco/agent_server/runtime.py
- packages/agent-server/tests/test_resume_reconstruction.py
- test-record/dc-05/units-server.log
- agent-projects/sonnet/dc-05b-report.md
