# FINDINGS: stream watchdog

## Implemented

- Added a 45s staleness watchdog in `frontend/src/api/agent.ts`. It arms on
  WebSocket open and resets on every received frame, including heartbeat-style
  frames such as `pong`. If the active socket receives no frames for the window,
  the client force-closes that socket and lets the existing `onclose` reconnect
  path replay history-then-live.
- Replaced the six-attempt terminal failure with retry-forever. Backoff remains
  exponential and capped at 15s; the old exhaustion point now emits a non-fatal
  `{ type: "connection", state: "degraded" }` frame instead of the previous
  fatal "lost connection" error.
- Reset the reconnect attempt counter when `document.visibilityState` changes
  back to `visible`.
- Preserved queued sends while disconnected and verified they drain when a later
  WebSocket successfully opens.
- Threaded degraded connection metadata through `useBuildStream` to
  `AgentStatusBar`, where it renders as a subtle `reconnecting...` badge without
  blocking input or changing the run status.

## Code-Reality Notes / Deviations

- The shared path named in the spec is `subscribeConversation` ->
  `subscribeLive` in `frontend/src/api/agent.ts`. Deep Research already reuses
  that path for conversation streams via `frontend/src/api/deepResearch.ts`, so
  this change covers Build and Deep Research conversation surfaces. I did not
  change the older standalone `/ws/research` transport in
  `frontend/src/api/research.ts`.
- The transport emits `{ type: "connection", state: "connected" }` when a
  previously degraded socket opens, so direct transport observers can tell the
  retry succeeded. The Build UI deliberately keeps showing `reconnecting...`
  until the next real stream frame arrives, matching the UI-hint requirement.

## Verification

- `cd frontend && npm ci`
- `cd frontend && npx vitest run src/api/ src/hooks/`
- `cd frontend && npx vitest run src/components/build/cluster6.test.tsx`
- `cd frontend && npm run typecheck:build`

The Vitest slice passed. It still logs pre-existing jsdom navigation warnings in
`src/api/deepResearch.test.ts` and an existing React `act(...)` warning in
`src/hooks/useResearch.lazy.test.tsx`.
