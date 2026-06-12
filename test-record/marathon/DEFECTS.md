# BP-16 Marathon Gate — defect reports

Filed per the order's discipline: the gate OBSERVES; fixes belong to follow-up
orders, never to bp-16 itself.

## DEFECT-1: `/sessions` route 500s on transient sandbox broken pipe

- **Observed**: 2026-06-10, Phase A attempt 2 (cid `conv_5bc5d990f4834ad3ab19891a96fa4036`),
  also present in attempt 1 (cid `conv_bbf8ed78225e424b8215ef3302c884c6`).
- **Symptom**: `GET /conversations/{cid}/sessions` intermittently returns
  `500 Internal Server Error` while the build is healthy. `/tmp/pmx-marathon.log`
  shows ~20 tracebacks in the first hour, terminal exceptions:
  - `disco.tools.sandbox.base.SandboxError: sandbox op failed in sbx_…: [Errno 32] Broken pipe`
  - `disco.tools.sandbox.base.SandboxError: sandbox session is closed`
  - raw `BrokenPipeError: [Errno 32] Broken pipe`
- **Evidence**: pmx-marathon.log lines 1049/1305/1562 (500s on /sessions),
  tracebacks at lines 134, 971, 1167, 1685, 1731.
- **Read**: the sessions route calls a live sandbox op (tmux list/view over the
  ssh socket to VM-201) with no retry/degrade — a single dropped ssh pipe
  becomes a user-facing 500. The op succeeds again on the next poll, so this is
  transport flakiness amplified into an API error.
- **Suspected owner**: BP-14 (sessions routes) + the sandbox transport layer
  (`packages/tools/src/disco/tools/sandbox/_container.py:182-185
  exec_shell`). Matches the standing "Broken-pipe sandbox-op recurrence watch"
  carry-forward — this is its first reproducible high-frequency sighting.
- **Severity**: medium — polling clients (Terminal tab useSessions) see gaps;
  no data loss observed; build itself unaffected.
- **Gate impact**: none on assertions (harness treats non-200 as empty and
  re-polls), but recorded as a finding because a real UI polling at 2s would
  surface intermittent Terminal-tab blanking.

## DEFECT-2: `shell_exec` into a dead session fails with bare "tool failed", burning the failure streak

- **Observed**: 2026-06-10, Phase A attempt 3 (cid `conv_52681dc934bb40519bb77983b17a7839`),
  seq 59–71, ~35s after the harness SIGKILLed the agent's `:3000` server.
- **Symptom**: the agent correctly noticed the death via its instruments
  (seq 56 `shell_view` → "Session not found or error", seq 58 `server_status`
  → `- 3000: FREE`) and tried to restart into the SAME (now dead) `backend`
  session. Four consecutive `shell_exec {"session": "backend"}` actions failed
  with `agent_error: "tool failed"` — **no content, no diagnostic, no hint that
  the session name no longer exists**. The fifth failure (`command exited 2`)
  tripped the 5-failure circuit breaker → `AWAITING_USER_DECISION` with only
  "Continue anyway".
- **Evidence**: events-conv_52681dc9….json seq 59–72; alternatives summary
  "I've hit 5 failures in a row…".
- **Read**: two stacked problems —
  1. tool layer: exec into a missing tmux session should return an actionable
     observation ("session 'backend' not found — it died; create a new one or
     use server_start"), not an opaque `tool failed` agent_error;
  2. the silent failure makes the agent repeat the identical action, so a
     single dead session converts directly into a circuit-breaker pause that
     needs a human click.
- **Suspected owner**: sessions/shell tool surface (BP-14 adjacent) +
  engine error surfacing (BP-02/BP-07 territory for observation quality).
- **Severity**: high for autonomy — any mid-run process death near a named
  session risks a needless human-in-the-loop pause; recovery itself was fine
  once "Continue anyway" reset the streak.
- **Gate impact**: handled by the harness acting as the user (alternatives
  gate → Continue anyway), recorded in the witness as an intervention. NOT a
  gate failure (no hard-deny, no valve), but it is the gate's single most
  actionable finding.
- **Recurrence (attempt 5, cid `conv_e1467239c9d841c89cd5da9b94cc7c65`, seq
  58–73)**: same opaque-failure → circuit-breaker chain with a NEW root: the
  session was not dead but **busy** — the agent ran `python3 server.py`
  (foreground Flask) in `backend`, then every subsequent
  `shell_exec {"session": "backend"}` failed with bare `"tool failed"` (seq
  61–67), plus one `command exited 2`, tripping the 5-failure breaker at seq
  73 (the attempt's only injected fault had hit the unrelated preview static
  server at ~seq 27 — this streak is pure system behavior). Refined
  read: `shell_exec` into an UNAVAILABLE session — dead (attempt 3) **or**
  occupied by a foreground process (attempt 5) — returns no diagnostic at
  all. The fix owner should make the observation say WHICH: "session
  'backend' is running `python3 server.py`; use shell_write_to_process, a new
  session, or server_start". Evidence:
  `events-conv_e1467239c9d841c89cd5da9b94cc7c65-attempt5.json`.

## DEFECT-4: post-restart resume enters an actionless planning loop — agent never calls a tool again

- **Observed**: 2026-06-10, Phase B attempt 3 (cid `conv_c1b4675689484c63b1f45d92d45b95be`) —
  the first attempt where the harness executed cleanly end-to-end (attempts 1–2
  were harness bugs #8/#9, see RECORD).
- **Sequence**: build ran normally to the install step (last action: seq 20
  `shell_exec` npm install); harness SIGTERM'd the agent-server mid-install;
  restart + bp-13 orphan sweep ran clean (conversation → PAUSED, container
  swept); harness clicked UI Resume; conversation → RUNNING.
- **Symptom**: from resume onward the agent NEVER issues another tool call.
  ~35 min / 360+ events of pure prose: 231 `knowledge` events (only 16
  distinct snippets — one CSV-schema snippet repeated **179×**), 80 `message`
  events (many empty, the rest plan restatements). `agent.step` spans complete
  normally (2.3–4.3 s, Qwen3.6-27B), every completion has `tool_calls: null`.
- **The agent is situationally aware but inert**: seq 361 message — "Plan
  (revision 3): Restarting the build from scratch since the sandbox was
  reclaimed — need to recreate all files, install deps, and build end-to-end."
  Three plan revisions accumulate (1→3); none is followed by an action.
- **Evidence**: `events-conv_c1b4675689484c63b1f45d92d45b95be-attempt3-loop-snapshot.json`
  (382 events); /tmp/pmx-marathon.log agent.step spans ~23:20–23:26Z; no
  ERROR/WARNING lines during the loop.
- **Read**: two stacked problems —
  1. **resume-path context**: after restart+sweep, the rebuilt driver context
     leads the model to plan/extract-knowledge instead of acting — suspect the
     interrupted in-flight action (seq 20 install never got its observation)
     and/or the reclaimed-sandbox state leaves the prompt in a shape where
     tool use never resumes (vs. fresh runs, where the same model tools
     normally from step 1);
  2. **the loop's only brake makes it WORSE**: nothing trips on actionless
     steps until the 3-auto-continue ceiling, which then **lands the run as
     FINISHED with "partial-plan detail"** (seq 385) — steps [1,2,3,4] all
     undone, zero post-resume actions, workspace never rebuilt, yet the
     conversation reads as a success and the ⚠ browser-verification valve
     stays silent (it only guards verification-less finishes, not work-less
     ones). A user returning to a resumed task sees FINISHED over an empty
     result. The knowledge store also accepts the same snippet 179× (no
     dedup).
- **Suspected owner**: resume/reconciliation path (BP-12/BP-13 seam: PAUSED-
  with-swept-container → RUNNING) + engine step-policy (actionless-step
  breaker) + knowledge dedup.
- **Severity**: CRITICAL for the restart-survival story — restart+resume is
  exactly the headline capability bp-16 Phase B gates; it currently produces
  an infinite silent token burn instead of a rebuilt app.
- **Gate impact**: Phase B attempt 3 FAILS at the harness ports-live timeout —
  recorded as a SYSTEM failure (the harness executed its part cleanly:
  SIGTERM at install, respawn, clean sweep, UI resume all verified). One
  reproducibility re-run performed per the flake-vs-deterministic
  adjudication mandate; see RECORD.
- **DETERMINISTIC — reproduced 2/2 clean attempts** (attempt 4 voided to a
  host OOM, see DEFECT-5). Attempt 5 (cid `conv_a8e17d46521649c98a1d2564f5e0a10e`,
  evidence `events-conv_a8e17d46521649c98a1d2564f5e0a10e-attempt5.json`):
  same trajectory to the seq-20 install, SIGTERM + respawn + sweep + UI
  resume all clean, then **zero actions again** — this time as a "Back on
  track! Step 1 (Python API) is done…" message-restatement loop plus 12+
  `deliverable` events with **null payloads** (a second engine oddity: empty
  deliverable records emitted during the loop), landing FINISHED via the same
  3-auto-continue ceiling at seq 167 (168 events, 7 min 54 s — faster than
  attempt 3's 386/19:46 but structurally identical). The two attempts loop in
  DIFFERENT surface registers (knowledge spam vs message/deliverable spam),
  so this is the resume-path context itself, not a sampling fluke: the
  post-resume prompt reliably yields prose-only completions from the same
  model that tool-calls correctly from step 1 of every fresh run.

## DEFECT-5: a transient driver outage is terminal — `LLMTransientError` ERRORs the conversation after ~12 s, no backoff, no PAUSED

- **Observed**: 2026-06-10, Phase B attempt 4 (cid `conv_3434a16b128c482ea1b7c70dd46ef166`):
  the workstation llama-server was kernel-OOM-killed at 18:29:35 CDT (exit 137 —
  host global OOM while saving a 908 MiB prompt-cache state for attempt 3's
  35,240-token DEFECT-4 loop context; systemd restarted it within seconds,
  model reloaded by ~18:30). The new conversation's FIRST driver call landed in
  that window: seq 4 `error code=model_error` —
  `LLMTransientError [qwen]: connection error: All connection attempts failed`,
  conversation → ERROR ~12 s after upload.
- **Read**: the engine *names* the error transient, then treats it as fatal.
  A model-server restart takes ~60–90 s (weights reload); 12 s of connection
  attempts cannot bridge it. There is no exponential backoff, no
  PAUSED-pending-driver state, no auto-resume when the driver returns — the
  user's task dies and must be recreated by hand.
- **Severity**: medium-high for self-hosted deployments — local model servers
  restart (updates, OOM, crashes) far more often than cloud APIs; every such
  blip currently kills every in-flight conversation that takes a step.
- **Suspected owner**: driver retry policy (engine step loop) + conversation
  lifecycle (a TRANSIENT classification should map to PAUSED/retry-with-
  backoff ≥2 min, not ERROR).
- **Gate impact**: attempt 4 voided as an infrastructure casualty (not a
  system adjudication — the system never got a healthy driver to act with);
  retried as attempt 5. Filed because the 12-s-to-terminal behavior is itself
  a resilience defect bp-16 exists to surface.

## DEFECT-3: path-prefixed preview proxy cannot serve real dev-server apps — user's preview renders blank/dataless

- **Observed**: 2026-06-10, Phase A attempt 7 (cid `conv_9c3f0ed082274f32837c500d6154151e`):
  the deliverable battery saw the live iframe but NO table text during the
  ~34s the sandbox was still alive; the agent's own in-sandbox verification
  (seq 83–86, `http://localhost:8000/`) shows the full working dashboard
  ("Sensor Dashboard", Readings 200, Min 32.8 °C…). The user and the agent see
  DIFFERENT apps.
- **Root cause (code-confirmed)**: `preview_app`/`port_app`
  (`packages/agent-server/src/disco/agent_server/app.py:404-446`) are
  plain GET forwarders — no HTML rewriting, no <base> injection, no
  POST/PUT/WS. A page served at `/conversations/{cid}/preview-app/` that
  references absolute paths resolves them against the AGENT-SERVER origin:
  - Vite dev `index.html` → `/src/main.jsx`, `/@vite/client` → 404 → blank shell;
  - scaffolded SPAs → `fetch('/api/readings')` → 404 → empty data (attempt-7
    `frontend/src/App.jsx` seq 51 does exactly this).
  The route docstring concedes the limit: "good for a built page
  (single-origin assets)".
- **Blast radius**: every realistic dev-server app — INCLUDING the bp-16
  frozen scenario itself (Vite+React on :8000 + API on :3000). The agent
  verifies at origin root in-sandbox (honestly console-clean, BP-05-valid),
  so neither the agent nor any existing check notices that the USER-facing
  preview is broken. Single-file/inline-asset pages are unaffected.
- **Suspected owner**: BP-10 (per-port proxy). Industry-standard fixes:
  subdomain-per-port proxying (origin-root semantics preserved), or HTML
  rewriting + <base> for assets plus a fetch-shim; GET-only forwarding also
  needs POST/WS for real apps (vite HMR uses a websocket).
- **Severity**: HIGH — the flagship "watch the agent build a real app, then
  use it in Preview" deliverable silently shows a blank page for the most
  common app shape.
- **Gate impact**: Phase A/B deliverable check "preview table shows real CSV
  values" FAILS as a system finding (recorded per-check in the witness, not
  patched). Port pills, per-port JSON proxy (`/port/3000/api/readings`),
  Terminal, and feed thumbnails are unaffected surfaces.

## DEFECT-6: a provider 400 mid-conversation is terminal — malformed-request rejection has no requery/salvage path

- **Found**: 2026-06-11, Phase B rerun on the OpenRouter free driver
  (`or-gpt-oss-120b-free`, sole upstream OpenInference), conversation
  `conv_cd1f9385cbfc484d8368bb9a4f5d1d8b`, evidence
  `test-record/marathon/phase-b-rerun-postcrash.log` + `/tmp/pmx-marathon.log`.
- **Chain**: (1) driver hallucinated tool name `shell.exec` (dot; the offered
  set has `shell_exec`) → engine correctly emitted `agent_error` feedback
  (seq 15); (2) the NEXT driver request was rejected HTTP 400 by the upstream
  ("Provider returned error"); (3) the 400 maps to a non-transient `LLMError`
  → terminal `ErrorEvent` (seq 16) per llm-router v1.3 reactive surfacing.
  One rejected request killed the whole build; Phase B never reached its
  SIGTERM/resume subject.
- **Probes**: dotted name in echoed history alone is NOT the trigger (clean
  A/B probe with proper tool descriptions → 200/200); a tools array entry
  missing `description` deterministically 400s on OpenInference (strict
  pydantic `ToolDescription`), but all engine ToolSpecs carry string
  descriptions. Exact offending field in request #6 unknown — needs a
  verbatim capture (recording router) on next repro. Suspect surface: the
  post-`agent_error` history shape and/or the meta-tool unlock between
  request 5 and 6 (DC-05 withholding boundary).
- **Sibling**: DEFECT-5 covered *transient* outages (now retried); this is
  the *rejected-request* class — retrying the SAME payload cannot help, the
  payload must be repaired/requeried OUTSIDE the event log (harvest #6,
  SWE-agent `forward_with_handling`; OpenCode hidden-`invalid`-tool reroute).
  Both are RP-12 FC-kit scope.
- **Severity**: HIGH on weak/strictly-validated drivers (the dev default per
  the OpenRouter-only decision); masked on the local 27B (lenient server).
- **Gate impact**: Phase B FAIL-before-subject; rerun blocked until RP-12
  lands or driver changes.

## DEFECT-7: uploaded files are not re-materialized into a recreated sandbox — post-resume agent is data-orphaned

- **Found**: 2026-06-11, Phase B attempt 3 on the free driver
  (`conv_1d576294a1e74f2e807ac5a70629f302`, evidence
  `test-record/marathon/phase-b-rerun-postcrash3.log` + event log).
- **Chain**: upload announced seq 3 (`uploads/sensor_readings.csv`, 10,368 B,
  bytes held server-side per the upload-cap design) → SIGTERM mid-first-install
  (seq ~16, before the agent copied the CSV anywhere) → resume reality-check
  (seq 20) correctly declares the fresh sandbox → agent rebuilds backend +
  frontend, then discovers `uploads/` does not exist (seq 140), tells the user
  it needs the file (seq 138/144 — honest), stuck-escape fires, conversation
  parks STUCK (seq 147). Deliverable battery cannot pass without the data.
- **What worked (don't relitigate)**: restart survival, orphan sweep, UI
  Resume, post-respawn auth (after the `_or_key.py` harness fix), wiped-sandbox
  reinstalls (seq 60-85), FC kit absorbing 13 agent_errors non-terminally
  (DEFECT-6 fix acceptance-proven live), valve/stuck protections all correct.
- **Expected**: resume reconciliation re-injects stored uploads into the
  recreated sandbox (server still holds the bytes), or at minimum the resume
  reality block lists uploads as lost-and-recoverable so the agent can request
  re-injection instead of stalling.
- **Severity**: HIGH for any upload-dependent build that restarts before the
  agent persists the data inside its workspace deliverables.
- **Owner direction**: rides the resume/lifecycle work (wave 2); pair with the
  checkpointed-DR-resume gap in the open-gaps list.

## DEFECT-7b: upload copy-back not wired on the post-restart lazy-compose path

- **Found**: 2026-06-11 dc-07 live acceptance (`test-record/dc-07/repro-witness.json`,
  conv_f985cb34). Sidecar store WORKS (data.csv held server-side); reality block
  promised restoration; agent acted; sandbox recreated (container label verified)
  — but `/workspace/uploads/` does not exist and data.csv is nowhere in the
  container. The reality block's promise is now a FALSE one (worse than honest
  absence). Units passed because they simulate recreation directly; the REAL
  post-restart resume creates the fresh sandbox via the lazy compose path
  ("created on your next action"), which never fires the on_recreate hooks
  dc-07 wired (runtime.py ~595/~644). Also: re-materialization has no log line
  (zero observability — the witness grep found nothing because nothing exists).

## DEFECT-8: free text-only driver dies at the BP-05 verify gate (vision)

- **Found**: same run, seq 39: `NoEligibleModel: Request contains images but
  model 'or-gpt-oss-120b-free' does not support VISION` — terminal ERROR.
  The build verify gate REQUIRES a browser screenshot observation; on the
  OpenRouter-free driver (text-only) every build that reaches verification
  dies. PMX_DRIVER_VISION applies to driver-local only. Options: route the
  VISION requirement to a vision-capable model (catalog has
  or-gemma-4-31b-free, tools+vision) per the build-parity "VISION role"
  recommendation; or degrade gracefully (text-only verify) instead of
  terminal NoEligibleModel. Blocks all build live-rungs in the free-driver era.
