# BP-16 — The Marathon Gate (cluster acceptance — run last)

**Read `README.md` first. Requires ALL other BP orders merged. This order writes and
runs the harness that decides whether the Build Parity Cluster is DONE. It is a test
order: if the system fails it, you file precise defect reports — you do not patch the
system inside this order.**

## The scenario (one continuous run, live 27B driver, gvisor backend on VM-201)

**Task prompt given through the real UI** (verbatim, including the uploaded file):

> "Build a small 'sensor dashboard' web app. I've uploaded sensor_readings.csv. Backend:
> a Python (or Node) API on port 3000 that serves the CSV as JSON at /api/readings.
> Frontend: a Vite + React app on port 8000 that fetches /api/readings and renders a
> table plus a summary card (count, min, max). Install whatever you need. Verify it in
> the browser before finishing."

`sensor_readings.csv` = 200 rows of REAL data (pull from any public dataset or generate
from real measurements on the host — not lorem ipsum; record provenance in the report).

## Harness: `harness/marathon/` (pytest-driven, Playwright for all UI assertions)

### Phase A — the build, with a mid-run fault

1. Create the conversation through the UI (Firefox), upload the CSV (BP-11), submit the
   prompt, approve the plan when gated.
2. **Fault injection**: once `server_status`/the sessions route shows a non-`preview`
   session owning a port (the agent's dev server or API is up), the harness kills that
   process from OUTSIDE: `docker exec pmx-sbx-<id> pkill -f <server cmd>` (kill the
   PROCESS, not the tmux session — the session must survive showing the death output).
3. **Required recovery evidence** (asserted from the event log; ALL must hold):
   - an observation from `shell_view` or `server_status` AFTER the fault that reflects
     the dead server (the agent NOTICED via its instruments — not via a failed curl
     guess);
   - a recovery action sequence that restarts the server (legitimate
     `shell_kill_process` use is allowed and expected);
   - ZERO hard-denied actions in the entire run;
   - a BP-05-valid browser verification (console-clean against :8000) AFTER recovery;
   - FINISHED status, not via the 3-refusal ⚠ valve.
4. **Deliverable assertions** (harness acts as the user): UI Preview renders the table
   with REAL values from the CSV (assert ≥3 known cell values); port pills show 8000 +
   3000 (BP-10); `/port/3000/api/readings` returns the JSON; Terminal tab shows live
   session output (BP-14); at least one screenshot thumbnail in the feed (BP-15).

### Phase B — restart survival

5. While Phase A's conversation is FINISHED, start a SECOND run of the same scenario; at
   the moment the event log shows the install step running, `systemctl`/`kill -TERM` the
   agent-server; restart it; resume via the UI Resume control (BP-12).
6. Assert: orphan reconciliation ran clean; the workspace survived (the partially-built
   files are still there — compare file list before/after); the build completes to the
   same deliverable assertions as Phase A step 4; completed plan steps did not
   re-execute (assert plan-step pointer monotonicity across the restart).

### Phase C — the kernel + masking endurance probes (same harness run)

7. From the event logs of A+B: per-step prompt-token series shows no context hard-reset;
   if any `code_exec` was used, per-cell latency is flat (no monotonic growth) — BP-06/
   BP-08 holding under real load.

## Evidence package (mandatory)

- `test-record/marathon/RECORD.md`: timeline, every assertion with pass/fail, fault
  timestamps, recovery sequence excerpt from the event log.
- Screenshots (Firefox, live): plan approval, mid-run Terminal with dead-server output,
  recovery in the feed, final Preview with the table, port-3000 JSON, resumed run's
  FINISHED state → `test-record/screenshots/marathon/`. Send the full set to the user.
- The two event-log JSONs.

## Pass/fail discipline

- The gate passes only if EVERY assertion holds in a single honest run per phase (retries
  allowed for harness bugs, not for system flakes — a system flake is a finding).
- Each failure → one defect report in `test-record/marathon/DEFECTS.md`: assertion,
  event-log excerpt, suspected owning work order. Do NOT fix the system in this order.
- Partial credit does not exist. "Mostly passed" = failed, with defects filed.

## Prohibitions

- No prompt-tuning the task to dodge a failure (the task prompt above is frozen).
- No fixture/replay mode anywhere in this order. No synthetic CSV.
- No editing system code in this order — defects go to DEFECTS.md and back to their
  owning BP order.
