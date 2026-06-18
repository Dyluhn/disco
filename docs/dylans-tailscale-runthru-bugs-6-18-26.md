# Dylan's Tailscale runthru — bug report (2026-06-18)

Live testing of the merged campaign over Tailscale (`http://100.94.219.2:5173`).
Captured verbatim from Dylan, then triaged. This is the source of truth for the
follow-up fix set. Fix order: **export first** (Dylan is verifying it), then dig
into the rest (traces required for #4/#5/#6).

## §1 — Verbatim notes (DO NOT EDIT)

1. On Deep Research, the export doesn't work (already tracking).
2. There's a bug with the revise plan. I sent a revision and it appeared to
   infinitely load instead of actually revising the plan.
3. A gripe: when I'm in a search and I click on the New button, that should take
   me to a new search or build or whatever. Currently it keeps me in the same
   surface. Not a fan of that.
4. In the build surface, the agent had a few issues. It kept saying the system is
   telling it that a file didn't exist anymore. Isn't this what we set out to fix?
   You'll need to look at the traces to determine what's going on.
5. There is also a weird issue where on the last step of a build, the session
   pauses itself. You saw this occur with gpt-oss. I don't know why that is.
6. I tried to iterate on a project and added an idea for a new plan. The agent
   tries to just build immediately instead of re-entering plan mode and producing
   a plan accurate in revision numbers. This makes it loop more.
7. Is there a way we can give the agent better access to errors that surface? More
   verbosity and specificity — specifically the browser console errors. It spent
   quite a few turns trying to trace a bug down that more verbose errors would have
   helped with.

## §2 — Triage (initial; deepen with traces)

- **B1 — DR export 404** *(FIXING FIRST)*. Root cause CONFIRMED: `NeedMoreCard.tsx`
  `_fetchExportBlob` fetches a RELATIVE url `/api/conversations/{cid}/report/export`
  (no base) → resolves against the page origin (vite :5173) → 404. The audio export
  (same file) correctly uses `${agentHttpBase()}/conversations/...`. Fix: prefix the
  export fetch with `agentHttpBase()`. Introduced by the DR-1 "route exports through
  the server endpoint" change; unit tests mocked `fetch`, so the wrong origin passed
  green — the visual-evidence gap. Shared helper → fixes md/pdf/docx at once. NOT a
  Tailscale issue (would 404 on localhost too, no vite /api proxy).

- **B2 — revise-plan infinite load**. A plan revision hangs (spinner, never resolves).
  Surface TBD (DR plan-iterate vs build plan-approval). Trace the revision request +
  the loop's handling of a re-submitted/revised plan. Suspect: the revision endpoint
  or the loop not re-kicking after a plan revision, or a state stuck in a pending
  status with no resolution.

- **B3 — "New" stays in the same surface (UX)**. The New button should let the user
  start a fresh conversation on ANY surface (or surface-pick), not pin them to the
  current surface. Frontend routing/New-action bug.

- **B4 — build "file doesn't exist anymore" (HIGH — core fix area)**. The agent is
  repeatedly told a file is gone/stale. This is the area W2 (FileStateTracker
  stale-notice) + C18 (file_exists done-condition) addressed — so either (a) the
  FileStateTracker is emitting a FALSE "changed on disk / re-read" signal, or (b) C18
  still resolves against the wrong cwd and reports a present file as missing, or (c) a
  sandbox path/identity mismatch. MUST read Dylan's build trace. Risk: a regression or
  an incomplete fix in the very thing we set out to fix.

- **B5 — last-step self-pause**. On the final step the run goes PAUSED (seen on the
  gpt-oss acceptance: the model declared "All set!" via `notify_user` ×3 without
  calling `finish()`, so the W5 actionless valve paused it). The valve behavior is
  CORRECT (no thrash), but the UX is wrong: a completed build shouldn't look paused.
  Fix direction: treat "build complete + notify_user, plan steps done" as a clean
  FINISH (or prompt the model to call finish), not a pause; OR auto-finish when the
  DoD/plan is satisfied and the model goes actionless.

- **B6 — iterate without re-planning**. Adding a new idea to an existing project makes
  the agent build IMMEDIATELY instead of re-entering PLANNING and producing a revised,
  revision-numbered plan → more looping. Fix direction: a new instruction on an
  existing build conversation should re-enter plan mode (or at least produce a plan
  delta) before executing; the plan revision numbers must increment.

- **B7 — verbose/specific errors for the agent (esp. browser console)**. The agent
  should receive richer error detail — particularly browser console errors — so it can
  debug in fewer turns. Today the browser tool likely surfaces a truncated/generic
  error. Fix direction: pipe full console errors (and richer tool errors) into the
  observation the agent sees; consider a structured error block.

## §3 — Fix order
1. **B1 export** (now; Dylan verifying).
2. Trace-driven: **B4** (file-doesn't-exist — core), **B5** (last-step pause), **B2**
   (revise-plan hang), **B6** (iterate re-plan) — these need the live build/DR traces.
3. UX: **B3** (New button), **B7** (verbose errors — agent-effectiveness win).
