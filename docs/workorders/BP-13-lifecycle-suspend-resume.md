# BP-13 — Idle-TTL suspend + reconnect resume (lifecycle G, completed)

**Read `README.md` first. Requires BP-12.**

## Why

Partial machinery exists: tab-close auto-suspend of idle sandboxes [a98d224], orphan
RUNNING reconciliation at startup [8bcdef9], WS auto-reconnect [b3694a0],
snapshot-on-terminal [56526be]. The missing legs (memory: "disco open gaps"):
a server-side **idle-TTL sweep** (tab-close never fires for crashed browsers / sleeping
laptops → GPU/RAM leak on the sandbox host), and a verified **reconnect-resume** path
(suspended sandbox must transparently come back when the user returns).

## Step 0 — verify the existing pieces (report file:line)

Read the a98d224 tab-close suspend implementation and the 8bcdef9 reconciliation; report
exactly: what "suspend" does today (snapshot? destroy? both?), what triggers it, and what
happens when a suspended conversation gets a new message. Build on those mechanisms —
duplicate nothing.

## The decided design

### 1. Idle-TTL sweep (`agent_server/runtime.py`)

- Background asyncio task started with the app (locate the app lifespan/startup hook),
  interval 60 s, TTL from env `PMX_IDLE_SUSPEND_S` (default `1800`).
- A conversation is **idle** iff: status NOT RUNNING (never suspend a live loop), zero WS
  subscribers (the store knows its subscribers — find the subscriber registry used by
  `publish_ephemeral`), and last event older than TTL.
- Sweep action = the SAME suspend path as tab-close (snapshot-first, then sandbox
  destroy) — one suspend implementation, two triggers.
- Log each sweep decision (structured logging already exists — match its style):
  `suspended idle sandbox cid=… idle_s=…`.

### 2. Reconnect resume

- On WS subscribe (or first message) to a conversation whose sandbox was suspended:
  nothing eager — `SandboxSession._ensure()` already lazily recreates. Your job is to
  VERIFY the chain end-to-end and fix what breaks: workspace rehydration into the fresh
  sandbox (from the persisted workspace dir / snapshot), preview session restart on
  demand (BP-02 `ensure_preview` via the existing preview endpoints), and the UI never
  showing a dead preview without its Restart affordance.
- The state frame the UI receives must carry `sandbox: "active" | "suspended"` so the
  user sees the truth (small badge near the isolation tier; BP-15 wires the tier — if
  BP-15 hasn't landed, put the badge in `AgentStatusBar` and note it).

### 3. Orphan reconciliation — extend, don't rewrite

At startup, reconciliation [8bcdef9] handles RUNNING orphans. Add: conversations whose
sandbox containers still exist on the backend but whose conversations are
terminal/suspended → destroy those containers (sweep `docker ps
--filter name=pmx-sbx-` against live conversation ids; gvisor + podman + local backends;
the process backend sweeps stale workspace temp dirs older than 7 days).

## Acceptance

1. **Unit**: idleness predicate truth table (status × subscribers × age); sweep never
   selects RUNNING; TTL env override.
2. **Integration (process backend, real time compressed)**: `PMX_IDLE_SUSPEND_S=5` —
   run a build to FINISHED, disconnect, wait 10 s → sandbox suspended (workspace
   persisted); send a follow-up message → sandbox recreated, `file_read` of a
   previously-built file succeeds (workspace rehydrated), preview restartable.
3. **Integration (gvisor, VM-201)**: same as 2 plus the orphan sweep: manually `docker
   run` a fake `pmx-sbx-zzz` container, restart agent-server, assert it is removed and
   real ones are not.
4. **UI surface (live, Firefox)** — `bp-13-suspend.spec.ts`: with TTL=5s, open a finished
   build, observe the `suspended` badge appear (poll the state frame), send a message,
   badge returns to `active`, preview works after Restart. Screenshots:
   `badge-suspended.png`, `badge-active-again.png` →
   `test-record/screenshots/bp-13/`, sent to user.

## Prohibitions

- Never suspend RUNNING. Never destroy a workspace dir as part of suspend (sandbox dies,
  workspace persists — that is the contract that makes resume possible).
- One suspend implementation — if you find yourself writing a second snapshot path, stop
  and reuse the first.
- No cron/systemd timers — in-process asyncio sweep only.
