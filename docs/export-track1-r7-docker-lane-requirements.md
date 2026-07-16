# R7 Docker lane — evidence requirements (binding)

Status: **RECORDED** for R7 (Docker matrix), pending execution in the campaign sequence.
Purpose: fix the acceptance bar for the R7 Docker lane so it cannot be satisfied by a
single HTTP response or by placeholder evidence files.

## Why this bar exists

Track-1 `candidate` verdicts are **explicitly unverified** by the static proof (see
[`export-track1-python-detection-limitations.md`](./export-track1-python-detection-limitations.md)).
The Docker lane is the *only* place a candidate export is actually stood up and observed,
so it is the real backstop — and it must behave like one. Two concrete failure modes make a
weak Docker check worthless:

- **Exit-code masking.** A dependency importing `os._exit(0)` exits the container process
  with **return code 0**. Any check that trusts "process exited 0" or "container started"
  is fooled. Readiness must be proven by the app actually *serving*, not by an exit code or
  a container reaching "running".
- **One-shot HTTP flukes.** A single `curl` returning 200 once proves nothing about
  stability — it can pass against a container that crash-loops one second later, or against
  a stale/placeholder response. A one-response check is not lifecycle evidence.

## R7 must prove ALL of the following (not one HTTP response, not placeholder files)

1. **Actual Compose health.** The service must reach and *hold* a healthy state under
   `docker compose` — a real `healthcheck` transitioning to `healthy` (not merely
   `created`/`running`), observed via `docker compose ps` / `docker inspect` health status,
   captured verbatim.
2. **Stable restart behavior.** The container must survive a real restart cycle
   (`docker compose restart` and/or `restart: unless-stopped` under an induced exit) and
   return to healthy — with evidence that it is **not crash-looping** (bounded, non-climbing
   restart count over a sustained observation window, from `docker inspect` `RestartCount`
   sampled over time).
3. **Real lifecycle evidence.** Timestamped, reproducible capture of the full lifecycle —
   `up` → healthy → serve → restart → healthy again → `down` — from live commands against a
   real Docker daemon. Readiness is demonstrated by the app **serving real responses across
   multiple probes over a sustained window**, not a single request and not a fabricated log.

## Anti-patterns that FAIL R7 acceptance

- A single `curl` / one HTTP 200 taken as proof of health.
- Trusting container "started"/"running" or process exit code 0 as readiness.
- Placeholder, hand-authored, or copied evidence files not produced by live commands
  against a real Docker daemon this run.
- Health asserted from logs alone without the daemon's own health/restart state.
- Any check that would pass against a container that crash-loops after first response, or
  against a dependency that calls `os._exit(0)` at import.

## Acceptance

R7 Docker lane is accepted only when the captured evidence shows, from live commands:
Compose `healthy` reached and held, a real restart survived without crash-loop, and the app
serving real responses across multiple probes over a sustained window — each reproducible by
re-running the lane. Anything less reopens R7.
