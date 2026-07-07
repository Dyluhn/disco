# BP-16 Marathon Gate — RECORD

**Run window**: 2026-06-10 (all times UTC unless marked CDT = UTC−5).
**Environment**: agent-server on the workstation (loopback :8000, tmux
`pmx-agent-server`, `PMX_LOG_JSON=1 PMX_DRIVER_VISION=1 PMX_SANDBOX=gvisor
PMX_LOCAL_SOCKET=ssh://sandbox@100.81.82.115 PMX_LOCAL_RUNTIME=runsc`,
`PMX_DB=test-record/pmx-run.db`); sandboxes on VM-201 (gVisor/runsc, Docker,
`pmx-sandbox:base`); driver Qwen3.6-27B-UD-Q5_K_XL via local llama.cpp
(Vulkan + MTP, :18080); UI vite on :5174 (self-managed harness instance);
browser Playwright Firefox. Harness: `harness/marathon/` (test-only — the
gate observes and injects faults; it never patches system code).

## Verdict

| Phase | Verdict | One line |
|---|---|---|
| **A — build + mid-run fault** | **PASS** (attempt 8) | agent noticed the kill through instruments, restarted the server, re-verified console-clean in the browser, finished honestly |
| **B — restart survival** | **SYSTEM FAIL — DEFECT-4 (deterministic 2/2)** | infrastructure (kill→respawn→sweep→UI resume) all works; the *agent* never issues another tool call after resume and lands a work-less FINISHED |
| **C — kernel + masking endurance** | **PASS** | prompt series bounded (no hard reset) in both phases, incl. across Phase B's server-life boundary |

Partial credit does not exist: the marathon's headline question — "does a
task survive an agent-server restart end-to-end?" — is answered **NO** today,
and that answer, with its evidence trail, is this gate's product. Five system
defects were filed (DEFECTS.md), none patched, per order discipline.

## Scenario (frozen for all attempts)

> Build a small 'sensor dashboard' web app. I've uploaded sensor_readings.csv.
> Backend: a Python (or Node) API on port 3000 that serves the CSV as JSON at
> /api/readings. Frontend: a Vite + React app on port 8000 that fetches
> /api/readings and renders a table plus a summary card (count, min, max).
> Install whatever you need. Verify it in the browser before finishing.

**CSV provenance**: `harness/marathon/sensor_readings.csv` — 200 REAL rows
captured from this workstation's hwmon sensors (NVMe, AMD GPU, CPU), columns
`timestamp, sensor, temperature_c`. Assertable truths derived from the file
itself: count=200, min=32.75, max=62.85, probe cells {32.75, 43.85, 62.85}.
No synthetic data anywhere in the gate.

---

## Phase A — the build, with a mid-run fault — PASS (attempt 8)

### Attempt ledger (honest accounting)

| # | Outcome | Adjudication |
|---|---|---|
| 1 | fail | **harness bug #1** — fault fired at the first instant of port ownership; the death landed as the launch command's own exit-137 observation, nothing for instruments to discover. Fix: settle ≥30 s + agent-moved-on (≥2 events) before killing |
| 2 | fail | **harness bug #2** — WAITING_FOR_CONFIRMATION gate stalled the run; harness didn't act as the user. Fix: `answer_gates` approves through the UI. Also surfaced **DEFECT-1** (/sessions 500s on broken pipe) |
| 3 | fail | **harness bug #3** — AWAITING_USER_DECISION (alternatives dialog) unhandled. Fix: alertdialog → "Continue anyway". Surfaced **DEFECT-2** (opaque `tool failed` into a dead session burns the 5-failure breaker) |
| 4 | fail | **harness bug #4** — deliverable battery ran after FINISHED; the sandbox reaps at FINISHED so all live surfaces were gone. Fix: battery moved live, post-recovery (deviation #2) |
| 5 | fail | **harness bug #5** — fault killed the PLATFORM preview server (`http.server -d /workspace`, owns :8000 from container start), not the agent's server: a vacuous fault. Fix: `port_owners` cmdline attestation + preview-pattern refusal + kill-log guard. DEFECT-2 recurrence filed (busy-session variant, pure system behavior). One manual gate click recorded as intervention |
| 6 | fail | **harness bug #6** — battery looked for port pills while the Terminal tab was active; pills render inside PreviewPane, live mode only, >1 port. Fix: Preview tab → "Live server" → pills. Recovery 3a–3e verified PASS offline from this attempt's events (system behaved correctly) |
| 7 | fail | **harness bug #7** — battery matched verbatim CSV strings; the app renders "32.8 °C" (1-decimal). Fix: numeric tolerance matcher (±0.051). Also discovered + filed **DEFECT-3** (path-prefix proxy = blank user preview; agent's in-sandbox verification honestly passes — user and agent see different apps) |
| 8 | **PASS** | clean end-to-end; all hard assertions green |

Seven of seven failures were harness defects, each fixed and re-run; the
system's recovery behavior itself passed every time it was reached (attempts
6 and 8). No prompt tuning, no fixture/replay, no synthetic CSV at any point.

### Attempt 8 timeline (cid `conv_2d96fd065eb84a809eed5a1c85ce14ee`)

| t (UTC) | seq | event |
|---|---|---|
| 22:40:18 | 1 | task submitted through the real UI (create → upload → plan approval) |
| 22:41:5x | ~23 | agent's `python3 server.py` first owns :3000 (settle window starts) |
| 22:41:57 | pre_seq 40 | **FAULT**: SIGKILL pid 365 (`python3 server.py`) via docker exec from outside the agent — kill log: `killing pid 365: python3 server.py`; preview-server guard negative |
| 22:43:21 | 67 | **3a noticed**: `server_status` observation — `backend: idle — last: __PMX_PS1__137__$` (exit 137 visible to the agent's instruments) |
| 22:44:09 | 78 | **3b recovery**: `shell_exec {"command": "cd /home/user/frontend && npx vite --port 8000 --host 0.0.0.0", "session": "frontend"}` (full restart sequence incl. backend relaunch) |
| 22:45:33 | 101 | **3d verification**: browser observation on :8000, structured.ok, zero console error-level entries — AFTER recovery |
| 22:55:49 | 177 | **FINISHED** (3e: not via the ⚠ valve) |

### Assertions — every one, pass/fail

**Hard (recovery gauntlet)**

| assertion | result | evidence |
|---|---|---|
| 3a instruments noticed the death | **PASS** | seq 67 server_status excerpt above |
| 3b recovery action after noticing | **PASS** | seq 78 excerpt above |
| 3c zero hard-denied actions (whole run) | **PASS** | 0 in 177 events |
| 3d console-clean :8000 browser verification after recovery | **PASS** | seq 101 |
| 3e FINISHED, not via ⚠ valve | **PASS** | final status FINISHED, valve marker absent |
| fault targeted agent's process, not platform preview | **PASS** | owners={365: python3 server.py}, agent_launch_in_log=true, kill-log guard |

**Deliverable battery (soft-recorded per evidence mandate, run LIVE post-recovery)**

| check | result | note |
|---|---|---|
| live_view (Preview → Live server → live iframe) | **PASS** | |
| port_pills (Bound ports tablist, :8000 + :3000) | **PASS** | |
| api_json (/port/3000/api/readings == 200 rows) | **PASS** | per-port proxy serves the real data |
| preview_cells (table shows real CSV values) | **FAIL — system** | **DEFECT-3**: path-prefix proxy can't serve a Vite dev app; iframe stays blank while the agent's in-sandbox view works. Visual evidence: `phase-a-final-preview-table.png` (deliberately kept: the blank preview IS the finding) |
| terminal (live tabpanel non-empty) | **PASS** | |
| feed_thumbnail (bp-15 screenshots in feed) | **PASS** | |
| json_screenshot (raw :3000 JSON via proxy) | **PASS** | `phase-a-port-3000-json.png` |

Confirmation approvals consumed by the harness acting as the user: 0 in
attempt 8 (earlier attempts: 1 manual alternatives-gate click in attempt 5,
recorded as an intervention).

---

## Phase B — restart survival — SYSTEM FAIL (DEFECT-4, deterministic)

### Attempt ledger (honest accounting)

| # | Outcome | Adjudication |
|---|---|---|
| 1 | fail | **harness bug #8** — `pgrep -f` matched the tmux pane's bash pipeline wrapper, not python: killed the wrapper, server died by SIGPIPE (wrong shutdown semantics). Fix: `/proc/<pid>/comm` filter. Post-mortem also revealed the agent-server had been running as an orphaned pipeline with NO tmux server |
| 2 | fail | **harness bug #9** — SIGTERM → pipeline exit → pane death → window close → session close → **tmux server exit** (remain-on-exit was off; it's a WINDOW option and had been set without `-w`). `respawn-pane` then found "no server running". Fix: arm `remain-on-exit -w` before the kill + `has-session ? respawn-pane : new-session` fallback; both proven empirically before re-run |
| 3 | **SYSTEM FAIL** | first clean harness execution. Kill at install (seq 20), respawn OK, bp-13 sweep clean, workspace survived, UI Resume clicked — then **DEFECT-4**: zero tool calls ever again; 231 knowledge events (one snippet ×179), 80 messages, 3 plan revisions; 3-auto-continue ceiling lands a work-less FINISHED at seq 385; harness fails at "run ended before ports served" (19:46) |
| 4 | **voided — infrastructure** | host kernel OOM-killed llama-server at 18:29:35 CDT (saving a 908 MiB prompt-cache state inherited from attempt 3's 35,240-token loop context); the new conversation's first driver call landed in the reload window → `LLMTransientError` → conversation ERROR in ~12 s. Filed **DEFECT-5** (transient ≠ terminal). Not a system adjudication — the system never had a healthy driver |
| 5 | **SYSTEM FAIL — reproduction** | clean harness execution again (kill 23:37–38, respawn, sweep #4, resume). **DEFECT-4 reproduces in a different register**: "Back on track! Step 1 done…" message-restatement loop + 12 null-payload `deliverable` events, zero actions, same ceiling → FINISHED at seq 167 (7:54). Deterministic 2/2 → no further retries |

### What restart survival PROVED before the agent went silent (attempts 3 & 5)

| sub-assertion | result |
|---|---|
| SIGTERM hit the PYTHON server process (graceful shutdown premise) | **PASS** |
| health went down, respawn brought it back ≤120 s | **PASS** |
| 6a orphan reconciliation ran clean on startup (bp-13) | **PASS** — `reconciled 1 orphaned RUNNING conversation(s)` + container sweep, zero tracebacks |
| 6b workspace survived (every pre-kill source file on disk) | **PASS** — `server.py` visibly intact in the UI file pane (`phase-b-resume-control.png`) |
| UI Resume control present and functional (bp-12) | **PASS** — Paused → RUNNING |
| post-resume: agent resumes WORK | **FAIL — DEFECT-4** |
| both ports live again → deliverable battery | **unreached** |
| plan-step monotonicity across restart | **unreached** (no post-resume plan_step actions exist to violate it) |
| FINISHED with work, no valve | **FAIL in substance** — FINISHED arrived, but via the 3-auto-continue partial-plan landing with zero post-resume work; the ⚠ valve check is blind to this escape hatch (filed inside DEFECT-4) |

The infrastructure half of restart-survival (BP-12/BP-13's machinery) passed
five sub-assertions; the agent-loop half failed absolutely. The fix order
inherits both DEFECT-4 and the valve-taxonomy gap it exposed.

---

## Phase C — kernel + masking endurance — PASS

Token series parsed from `agent.step` end-spans in `/tmp/pmx-marathon.log`
(`tee -a` preserved one log across all server lives).

| | phase A (`conv_2d96fd06…`) | phase B (`conv_a8e17d46…`) |
|---|---|---|
| steps | 80 | 142 |
| in_tokens first 3 | 2585, 2684, 8678 | 2585, 2687, 8681 |
| in_tokens last 3 | 24475, 27651, 27610 | 17080, 17132, 17156 |
| in_tokens max | 30160 | 17156 |
| hard reset (<25 % of running max after warmup) | **none** | **none — including across the kill/respawn boundary** |
| code_exec latency | skipped (<4 cells) | skipped (<4 cells) |

BP-06's masking BOUNDS the prompt without ever collapsing it; the Phase B
series additionally shows the restart re-prime registering as growth, not a
reset. The code_exec check self-skipped honestly: the scenario never used ≥4
kernel cells, so no claim is made about kernel endurance (BP-08 remains
covered only by its own order's tests).

---

## Deviations from the order (all process, none substantive)

1. **Front-door create-on-submit**: conversations are created by driving the
   real UI (type → submit → upload → approve), not a backdoor POST — closer
   to the order's "as the user" spirit; recorded because the order's literal
   step list implied API creation.
2. **Battery runs LIVE, post-recovery** (not after FINISHED): the sandbox
   reaps at FINISHED (task-as-resource), so pills/preview/proxy/terminal are
   only witnessable while the run is alive. Attempt-4 lesson; late-retry pass
   keeps polling failed checks until FINISHED.
3. **Battery checks soft-recorded**: the order mandates "every assertion
   pass/fail" in evidence — converting deliverable checks to per-check
   recorded results (hard asserts retained for the recovery gauntlet
   3a–3e/FINISHED/no-deny/no-valve) is what makes a FAIL row (preview_cells,
   DEFECT-3) recordable instead of an abort.

**Manual interventions**: one alternatives-gate "Continue anyway" click,
Phase A attempt 5 (during a DEFECT-2 system streak; attempt was already
being discarded for harness bug #5). None in the passing/adjudicated runs —
attempt-8 and Phase B gates were answered by the harness acting as the user,
approvals count logged per run.

## Defects filed (never patched here — order discipline)

| # | severity | one line |
|---|---|---|
| DEFECT-1 | medium | `/sessions` 500s on transient sandbox broken pipe (no retry/degrade) |
| DEFECT-2 | high (autonomy) | `shell_exec` into an unavailable session (dead OR busy) returns bare "tool failed" → burns the 5-failure breaker (seen twice) |
| DEFECT-3 | HIGH | path-prefix preview proxy cannot serve real dev-server apps — user's preview blank while agent's in-sandbox verification honestly passes |
| DEFECT-4 | CRITICAL | post-restart resume = actionless planning loop → 3-auto-continue ceiling lands a work-less FINISHED (deterministic 2/2; + null-deliverable emission; + valve blind spot; + knowledge dedup absent) |
| DEFECT-5 | medium-high | `LLMTransientError` is terminal in ~12 s — no backoff/PAUSED; any local model-server restart kills in-flight conversations |

## Evidence index

- Witnesses: `phase-a-witness.json`, `phase-c-witness.json`, `state.json`
  (phase_b witness intentionally absent — the run never reached its save
  point; the attempt logs + event archives are the Phase B record)
- Event archives (verbatim API dumps): `events-conv_2d96fd06….json` (A,
  PASS), `events-conv_c1b46756….json` + `-attempt3-loop-snapshot.json` (B
  attempt 3), `events-conv_a8e17d46….json` (B attempt 5),
  `events-conv_3434a16b…` not archived (4 events, quoted in DEFECT-5), plus
  Phase A attempts 3–7 archives used in adjudications
- Run logs: `phase-a-run-attempt{1..8}.log`, `phase-b-run-attempt{1..5}.log`,
  `phase-c-run.log`; server log `/tmp/pmx-marathon.log` (continuous via
  `tee -a` across all server lives)
- Screenshots (`test-record/screenshots/marathon/`, all pixel-decoded before
  acceptance): `phase-a-plan-approval`, `phase-a-terminal-dead-server`,
  `phase-a-recovery-feed`, `phase-a-final-preview-table` (blank preview =
  DEFECT-3 visual), `phase-a-port-3000-json`, `phase-a-gate-manual`,
  `attempt1-terminal-dead-server`, `phase-b-plan-approval`,
  `phase-b-resume-control` (Paused + Resume + intact workspace)
- Defects: `DEFECTS.md` (5 entries)
