# De-complexity Wave (DC) — replace compensating mechanisms with the real solutions

**Status**: green-lit by Dylan 2026-06-10 ("if there are better solutions, do them").
**Placement**: Wave 0 of the next-fix-set campaign — runs after the bp-16 commit,
before RP wave 1. RP's ratified scope/decisions are unchanged; this wave removes
three architectural divergences from the Manus model that RP orders would
otherwise keep compensating for.

**Origin**: the Manus gap re-analysis (2026-06-10). Three mechanisms in the
current system are *compensations* for self-inflicted divergences, each carrying
its own UX + defect surface:

| Divergence | Compensating mechanism it forced | Cost observed |
|---|---|---|
| Path-prefix preview proxy | rendered/live mode split, port pills, "Live server" toggle, srcdoc static render | DEFECT-3: blank preview for every real dev-server app — incl. bp-16's own scenario |
| Sandbox reaped at FINISHED | live-deliverable race in tests, bp-15 feed thumbnails as the only post-run record | bp-16 battery had ~2.5 min usable window; users lose the preview the moment the build succeeds |
| Confirmation gates on in-sandbox ops | 4-way gate machine, harness `answer_gates` in every poll loop | needless human-in-loop pauses; composes badly with DEFECT-2 into circuit-breaker stalls |

---

## DC-01 — Origin-true preview proxy (hostname-per-conversation)

**Kills**: DEFECT-3 root cause (`app.py:404-446` plain GET forwarders).

**Design**: Host-header routing on the existing agent-server listener.
`http://{cid8}-{port}.localhost:8000/` reverse-proxies **all** methods +
WebSocket to the sandbox's `{port}`, preserving origin-root semantics —
absolute asset paths (`/src/main.jsx`, `/@vite/client`), `fetch('/api/…')`,
and Vite HMR's websocket all just work. No HTML rewriting, no `<base>`
injection, no fetch shim — the entire rewrite-machinery branch is avoided.
This is the Manus model (hostname-per-sandbox) scaled to localhost.

- `*.localhost` resolves to loopback in Firefox & Chromium (RFC 6761 behavior).
  **Verification step 1 of the order**: Playwright check that a
  `foo.localhost:8000` iframe loads before building anything on top.
- Middleware: if `Host` matches `^(?P<cid8>[0-9a-f]{8})-(?P<port>\d+)\.localhost`,
  stream-proxy (httpx streaming for HTTP; `websockets`/starlette WS bridge for
  upgrade requests) to the conversation's sandbox upstream. Else fall through
  to the normal app router.
- Port allowlist unchanged (USER_PORTS); :8899/:8901 stay non-routable.
- Frontend: PreviewPane live iframe src becomes the hostname URL. The
  path-prefix routes stay for one release as deprecated fallbacks
  (single-file pages), then die.
- **De-complexity payoff**: with an origin-true live preview, the
  rendered/live mode split exists only as a fallback for no-server snapshots;
  pills survive as plain port links. Net-negative LOC target in PreviewPane.
- Share-links interplay (RP, tailnet-only): MagicDNS hostnames are the same
  pattern; DC-01 keeps the resolver pluggable (one host-pattern constant).

**Acceptance**: bp-16's frozen scenario (Vite+React :8000 + API :3000) shows
the REAL table with CSV values in the user's preview iframe — the exact check
that fails today (`preview_cells`), flipped green, pixel-verified.

## DC-02 — Idle-suspend/resume instead of reap-at-FINISHED

**Kills**: the task-as-resource model's sharpest edge — the sandbox (and the
working preview) vanishing at the moment of success.

**Design**: FINISHED no longer reaps. The sandbox enters `IDLE` with a TTL
(default 30 min, config `sandbox.idle_ttl_s`); TTL expiry → suspend
(stop container, workspace volume persists — it already does, bp-13 proved
survival); any preview hit / session open / resume → transparent re-create
from the workspace, same cid. Hard reap only on user Delete or disk pressure.

- Reuses bp-13's orphan-reconciliation machinery for the suspend/resume
  bookkeeping (it already sweeps containers ↔ conversation status).
- Closes two carry-forwards at once: "preview lingers after suspend"
  (now a *defined* state with a real answer: wake it) and the open-gaps
  "full-G auto-suspend/resume".
- Test harnesses stop racing FINISHED for live surfaces; bp-16-style
  batteries gain an honest post-run window.

**Acceptance**: build → FINISHED → wait 2 min → preview still serves; suspend
(TTL forced to 10 s in test) → preview hit wakes the sandbox → same app, same
workspace; orphan sweep stays clean across a server restart in IDLE.

## DC-03 — Gates scoped to the real blast radius

**Kills**: confirmation prompts for operations whose worst case is confined to
the sandbox — the sandbox IS the blast radius; that's why it exists.

**Design**: gate policy becomes surface-based, not command-based:

- **Auto-approve + journal** (no gate): anything executing inside the
  sandbox — `rm -rf` in /workspace, package installs, process kills, file
  writes. Each gets a journal event (feed-visible) instead of a modal.
- **Gate kept**: egress beyond the allowlist proxy, host-affecting ops,
  publishing/share-link creation, anything spending money, plan approval
  (that one is a product feature, not a safety valve).
- Circuit-breaker (5-failure) stays — it guards the LLM loop, not the user.

Composes with the DEFECT-2 fix (below): fewer gates *and* the remaining
failures carry actionable observations, so the breaker fires rarely and
informatively.

**Acceptance**: bp-16 scenario replayed with `answer_gates` instrumentation —
zero soft-confirm gates fired for in-sandbox ops across a full build;
egress-violation attempt still gates; journal entries present for each
auto-approved destructive op.

## DC-04 (fold-in) — DEFECT-1 + DEFECT-2 remediation

Small, adjacent, both filed by the bp-16 gate:

- **DEFECT-1**: `/sessions` wraps its sandbox op in retry(2)+degrade — a
  dropped ssh pipe returns the last-known session list (marked stale), never
  a 500.
- **DEFECT-2**: `shell_exec` into an unavailable session returns a diagnosis
  naming WHICH state: `session 'backend' not found — it died; create a new
  one or use server_start` / `session 'backend' is running 'python3
  server.py' in the foreground; use shell_write_to_process, a new session, or
  server_start`. Kills the opaque-failure → circuit-breaker chain (seen twice:
  attempts 3 and 5).

**Acceptance**: replay both defect scenarios from their archived event logs'
shapes (dead session, busy session, dropped pipe) → actionable observations,
no 500s, no breaker trip.

## DC-05 — Restart survival that actually survives (DEFECT-4 + DEFECT-5)

**Kills**: the bp-16 Phase B SYSTEM FAIL — the headline capability (a task
surviving an agent-server restart end-to-end) is broken at the agent-loop
layer even though the infrastructure half (BP-12/13 SIGTERM semantics,
respawn, orphan sweep, workspace survival, UI Resume) proved out 5/5.

**DEFECT-4 evidence (deterministic 2/2, attempts 3 & 5)**: post-resume, every
`agent.step` completes normally (2.3–4.3 s) but **all** completions return
`tool_calls: null` — the agent narrates ("Back on track! Step 1 is done…"),
revises the plan ("Restarting the build from scratch since the sandbox was
reclaimed"), emits null-payload `deliverable` events, and lands a work-less
FINISHED through the 3-auto-continue ceiling. Attempt-3 register: 231
knowledge events with ONE CSV snippet duplicated ×179; 80 messages.

Four sub-fixes, each independently testable:

1. **Resume-path context reconstruction** (the root cause). The interrupted
   action (seq-20 install, mid-flight at SIGTERM) never received an
   observation, and the reconstructed context tells the model the work is in
   an ambiguous done/not-done state over a reclaimed sandbox. Fix: on resume,
   (a) synthesize a terminal observation for any action without one
   ("interrupted by server restart — outcome unknown; re-verify"), (b) inject
   a fresh environment reality block (sandbox state: recreated/suspended →
   rehydrated, which sessions exist NOW, workspace file listing), (c) restate
   the active plan step as the next actionable instruction rather than
   replaying the full narrative history that teaches the model to narrate.
2. **Actionless-step breaker**: N consecutive completions with
   `tool_calls: null` while plan steps remain undone (N=3) → PAUSED with a
   diagnostic env message, never auto-continue to FINISHED. A narrating
   non-acting agent is a fault, not progress.
3. **Knowledge dedup**: content-hash knowledge events at append time; a
   duplicate raises a counter on the existing entry instead of a new event.
   Kills the ×179 CSV bloat → the 35K-token context → the llama prompt-cache
   OOM cascade (attempt 4's 908 MiB save killed llama-server).
4. **Valve taxonomy for partial-plan landings**: the 3-auto-continue ceiling
   may not land FINISHED when undone plan steps exist AND the run produced
   zero tool calls since the last resume — that combination is PAUSED +
   ⚠ valve, surfaced in the feed. FINISHED must imply work happened.

**DEFECT-5 fix**: `LLMTransientError` no longer terminal. Retry with backoff
(3 attempts: ~10 s/30 s/90 s — sized to the observed 60–90 s local-model
restart window); still failing → conversation PAUSED with detail
`driver-unavailable` (resumable — and DC-02's wake machinery makes that
cheap), never ERROR. A health-gate on resume retries the driver before the
first step.

**Acceptance**: the bp-16 Phase B harness (`harness/marathon/test_phase_b.py`)
re-run VERBATIM — SIGTERM mid-build → respawn → sweep → UI Resume → the agent
issues real tool calls, completes remaining plan steps, FINISHED with the
battery green. Plus: replay attempt-3's archived event log through the resume
reconstructor asserting the synthesized observation + reality block; unit
matrix for the breaker (3×null+undone → PAUSED) and dedup (×179 collapse);
DEFECT-5 sim = kill llama-server mid-step, restart within 90 s → conversation
survives to FINISHED, no ERROR.

---

## Ordering & verification discipline

1. DC-01 → DC-02 → DC-03 → DC-04 → DC-05 (01 and 04 are independent; 02
   before 03 so gate-relaxation tests run against the suspend lifecycle;
   05 last because its Phase-B regression re-run exercises 01–04's surfaces
   too — but 05 is the highest-VALUE order; pull it earlier if 02/03 stall).
2. Every order: real-sample harness (verbatim captured samples), live Firefox
   pixel-verified screenshots, SendUserFile. No fixture-only green.
3. The bp-16 marathon harness (`harness/marathon/`) is the wave's regression
   gate: after DC-01+02+03, re-run Phase A expecting `preview_cells` PASS and
   a post-FINISHED battery window; after DC-05, re-run Phase B expecting
   actual restart survival (the SYSTEM FAIL flipped green).
4. Then RP wave 1 starts, unchanged.
