# BP-16 Marathon Gate harness

End-to-end endurance gate for the BP campaign (development/notes/workorders/BP-16-marathon-gate.md).
This is a **test-only** order: if the system fails, file the failure in
`test-record/marathon/DEFECTS.md` — never patch the system from here.

## Scenario (frozen)

The agent builds a sensor dashboard from a REAL 200-row CSV
(`sensor_readings.csv`, live hwmon sampling — provenance in
`provenance.json`, regenerate with `make_csv.py`): API on :3000 serving the
CSV as JSON at `/api/readings`, Vite+React UI on :8000 with a table + summary
card (count/min/max), browser-verified before finish.

## Phases

| Phase | What it proves | Fault |
|---|---|---|
| A (`test_phase_a.py`) | build + recovery + deliverables | SIGKILL the port-owning process inside the sandbox (docker exec, /proc prober) |
| B (`test_phase_b.py`) | restart survival + UI Resume (BP-12/13) | SIGTERM the agent-server mid-install, respawn, resume |
| C (`test_phase_c.py`) | no context hard-reset (BP-06) + flat code_exec latency (BP-08) | none — pure log/event analysis |

## Preconditions

* agent-server on :8000 with `PMX_LOG_JSON=1`, stdout tee'd to
  `/tmp/pmx-marathon.log` (Phase C parses `agent.step` end-spans from it;
  Phase B's respawn appends with `tee -a` so the series spans both lives)
* VM-201 reachable: `ssh sandbox@100.81.82.115`
* vite on :5174 is harness-managed (never :5173 — that one is the user's)

## Run

```sh
uv run pytest development/harness/marathon/test_phase_a.py -s
uv run pytest development/harness/marathon/test_phase_b.py -s
uv run pytest development/harness/marathon/test_phase_c.py -s
```

State flows through `test-record/marathon/state.json`; witnesses land in
`test-record/marathon/phase-*-witness.json`; screenshots in
`test-record/screenshots/marathon/`.

## Documented deviation

The order's "create → upload → submit" isn't reachable as three user actions —
the UI creates the conversation ON first submit. Honest equivalent (in
`drive.py`): submit (creates + plan-gates) → upload while
AWAITING_PLAN_APPROVAL → approve. The upload announce lands before any
execution step.
