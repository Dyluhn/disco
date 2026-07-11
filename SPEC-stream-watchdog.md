# SPEC: conversation-stream staleness watchdog + retry-forever (frontend)

## The gaps (scan 4, docs/deep-scan-report-7-9-26.md)

`frontend/src/api/agent.ts` `subscribeLive` reconnects only on `onclose` with
`_RECONNECT_MAX_ATTEMPTS = 6` (~45s window):
- G1: a SILENTLY dead socket (half-open TCP, proxy-eaten frames — no close
  event) never reconnects → the user watches a frozen "Working…" forever.
  This is byte-for-byte the wedge that froze three gauntlet runs before the
  test oracle got its watchdog; the product UI never got one.
- G2: after 6 failed attempts the stream is dead until a manual page reload —
  a dev-server restart can exceed the window.

## Required behavior (all in the shared subscribeLive path so build AND
research surfaces heal together)

1. STALENESS WATCHDOG: track last-frame-received time. While the subscription
   is open, if NO frame arrives for 45 seconds, force-close the socket
   (ws.close()) — the existing onclose path then reconnects and the server
   replays history-then-live (dedup already handles replays). The server may
   have genuinely quiet periods; a reconnect during quiet is harmless (replay
   is idempotent). If the server sends heartbeat/ping frames, count them as
   frames (follow code reality on whether they exist).
2. RETRY-FOREVER: remove the terminal give-up. Keep exponential backoff capped
   at 15s, but never stop retrying while the subscription is open. After what
   is TODAY the exhaustion point (6 failed attempts), surface a NON-FATAL
   "reconnecting…" state instead of the fatal error frame:
   - emit a distinguishable frame (e.g. {type:"connection", state:"degraded"})
     or reuse an existing mechanism — follow code reality; the fatal
     'lost connection' error frame must no longer be emitted for retryable
     closes.
   - Reset the attempt counter on document visibilitychange → visible.
3. UI HINT (minimal, honest): where the build surface renders status (the
   AgentStatusBar area), show a subtle "reconnecting…" affordance while the
   connection is degraded, and clear it on the next real frame. No false
   affordances: do not gray out working UI; do not block input (sends already
   queue — verify the queue drains on reopen).
4. Do NOT change frame semantics, reducers, or dedup behavior.

## Tests (extend the existing reconnect test — grep 'reconnect' in frontend tests)
1. Silent stall: no frames for >45s (fake timers) → socket force-closed →
   reconnect attempted.
2. Repeated failed reconnects → degraded state surfaced, retries CONTINUE
   past the old max; a later successful open clears degraded and drains the
   send queue.
3. Frames arriving normally → watchdog never fires.
4. visibilitychange resets the backoff attempt counter.

## Verification
- cd frontend && npx vitest run src/api/ src/hooks/ && npm run typecheck:build
- npm ci first if node_modules is absent (worktree).

## Constraints
- Smallest coherent diff; do NOT touch running servers or the main checkout.
- Commit on wt-streamwatch with --no-verify; FINDINGS-STREAMWATCH.md.
