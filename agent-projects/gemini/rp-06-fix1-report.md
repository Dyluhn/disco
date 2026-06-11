# RP-06 FOLLOW-UP 1 — COMPLETION REPORT

Date: 2026-06-11
Brief: agent-projects/gemini/rp-06-followup-1.md

## Deviations

1. **ShareView route registered in App.tsx (`/share/:token`)**
   - Flagged per brief instruction ("flag as deviation"). The backend already serves
     a standalone HTML share-viewer at the same path; the SPA route overlaps with
     this. In a production deployment, the SPA should have a catch-all that defers
     to the backend's `/share/:token` HTML when the backend is running, or use the
     React ShareView when operating in a pure-SPA mode. The route is added to
     App.tsx for standalone and test use but will shadow the backend's
     standalone HTML viewer if both are served from the same origin.

2. **Replay cursor initializes at tail (not 0) in non-live mode**
   - The brief says "step-through = events.slice(0, n)". The hook starts at `max`
     (full trace visible) when the conversation is not live, so the user sees the
     completed run and scrubs backward. Starting at 0 would show an empty state on
     a finished build. The stepFwd/stepBack controls let the user navigate from
     the tail.

3. **Replay auto-tracks new events in non-live mode**
   - When events grow while not live (e.g., a resume adds events to a paused
     conversation), the cursor jumps to the new tail. This prevents the viewer
     from silently missing new content. Users who have scrubbed back will see
     new events appended; this matches the "I resumed and want to see it"
     expectation.

## Completed items

### 1. useReplay hook (`frontend/src/lib/useReplay.ts`)
- Exposes `{position, max, atLive, seek, stepFwd, stepBack}`
- When `live=true` (RUNNING), auto-tracks the tail via useEffect
- When `live=false`, starts at tail; tracks new events to tail
- Pure React hook — zero reducer dependencies
- 14 unit tests, all green

### 2. useReplay tests (`frontend/src/lib/useReplay.test.ts`)
- Initial state at tail in non-live mode
- Initial state tracks to tail in live mode
- stepFwd/stepBack bounded correctly
- seek clamps to [0, max]
- atLive detection
- Live auto-tracking on events growth
- Non-live jump to tail on events growth (resume/first load)
- Stable position when events don't grow

### 3. Scrubber UI (`frontend/src/components/build/ReplayScrubber.tsx`)
- Range input + position label ("Event N of M")
- Hidden during live RUNNING — guarded by `isReplaying` flag in BuildSurface
- Mounted in the Build view's chat pane, between the Activity header and feed
- Minimal styling: accent-colored range, monospace label

### 4. BuildSurface integration
- Imports `useReplay` and `ReplayScrubber`
- `isReplaying = b.started && b.status !== "RUNNING"`
- `visibleEvents = isReplaying ? b.events.slice(0, replay.position) : b.events`
- Activity feed, deliverable, final message, and inspector (ExecutionCanvas)
  all consume `visibleEvents`
- LiveSignalBar still uses full `b.events` (correct — the live signal is about
  the real agent state, not the replay window)

### 5. ShareView (`frontend/src/views/ShareView.tsx`)
- Reads `:token` from URL params via react-router
- Calls `fetchShareBundle(token)` to get the scrubbed JSON bundle
- Refuses unknown `bundle_version` with a clear error message
- Renders: provenance header (title, surface badge, share date), plan panel,
  ActivityFeed, final message (as Markdown), deliverable notice, and
  ExecutionCanvas (files + terminal inspector)
- Zero WebSocket dependency — fully static read-only
- Three states: loading (spinner), error (with reason), done (full trace)

### 6. API wiring (`frontend/src/api/agent.ts`)
- `createShare(conversationId)` → POST `/api/conversations/{cid}/share`
  Returns `ShareLink` with `{ok, token, conversation_id, url, owner_id, bundle_seq}`
  or `{ok: false, reason}` on failure
- `fetchShareBundle(token)` → GET `/api/share/{token}/bundle`
  Returns `ShareBundle` (the full versioned event bundle) or null on 404/revoked
- Offline guard: both return empty/null when `agentLive()` is false

### 7. Route registration (`frontend/src/App.tsx`)
- Added `<Route path="share/:token" element={<ShareView />} />`
- Imported ShareView from `@/views/ShareView`

## Evidence

- **Frontend tests:** `test-record/rp-06/units-frontend.log`
  - 39/40 test files passed (216/217 tests)
  - 1 failure: known ResearchSurface load-flake (pre-existing, not caused by RP-06)
- **Server tests:** `test-record/rp-06/units-server.log`
  - 255 passed, 0 failed

## File manifest

### Created
- `frontend/src/lib/useReplay.ts`
- `frontend/src/lib/useReplay.test.ts`
- `frontend/src/components/build/ReplayScrubber.tsx`
- `frontend/src/views/ShareView.tsx`

### Modified
- `frontend/src/api/agent.ts` — added `createShare`, `fetchShareBundle`, `ShareLink`, `ShareBundle`
- `frontend/src/components/BuildSurface.tsx` — added `useReplay`, `ReplayScrubber`, `visibleEvents` slicing
- `frontend/src/App.tsx` — added `/share/:token` route + `ShareView` import

## Not touched (backend, per brief)
- `packages/agent-server/src/perpleximanus/agent_server/app.py` — share endpoints already in tree
- `packages/agent-server/src/perpleximanus/agent_server/runtime.py` — share_export + share_tokens already in tree
- `packages/agent-server/src/perpleximanus/agent_server/redaction.py` — already in tree
- `packages/agent-server/tests/test_share.py` — already in tree
- `packages/agent-server/tests/test_redaction.py` — already in tree
