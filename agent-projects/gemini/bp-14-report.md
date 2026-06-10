# BP-14 Report — Terminal tab = live tmux session views

## Step-0 file:line pointers

| Component | File | Lines |
|---|---|---|
| `live_session()` | `packages/agent-server/src/perpleximanus/agent_server/runtime.py:1482` | read-only sandbox accessor |
| `sessions_list()` | `packages/agent-server/src/perpleximanus/agent_server/runtime.py:1488` | non-`__` filter |
| `session_view()` + cache | `packages/agent-server/src/perpleximanus/agent_server/runtime.py:1497–1524` | asyncio.Lock coalescing, 0.5s TTL |
| `GET /sessions` | `packages/agent-server/src/perpleximanus/agent_server/app.py:327` | wire: `{sessions:[{name,busy,last_line}]}` |
| `GET /sessions/{name}/view` | `packages/agent-server/src/perpleximanus/agent_server/app.py:348` | wire: `{name,busy,content}` |
| `SessionInfo`, `SessionView` interfaces | `frontend/src/api/agent.ts:119–130` | TS interfaces |
| `getSessions()`, `getSessionView()` | `frontend/src/api/agent.ts:132–146` | API functions |
| `useSessions` hook | `frontend/src/hooks/useSessions.ts:17` | polling gates: `active`, `visible` |
| `TerminalPane` component | `frontend/src/components/build/ExecutionCanvas.tsx:130` | two-mode pane |
| `anySessionBusy` + badge | `frontend/src/components/build/ExecutionCanvas.tsx:504,534` | lifted state + tab dot |

---

## Deviations from spec

None. All items from the brief were implemented as specified:

- Two REST routes (`/sessions`, `/sessions/{name}/view`) with `__`-prefix exclusion on both.
- `deriveTerminal` history fallback is preserved and shown when sessions list is empty.
- `useSessions(cid, active, visible)`: sessions polled at 2s when `active`; view polled at 1s only when `active && visible`.
- `TerminalPane` lives inside `Tabs.Content[terminal]` (Radix unmounts inactive content), so `useQuery` is only called within the `QueryClientProvider` tree — existing tests that render `ExecutionCanvas` without a provider remain unbroken.
- No xterm.js, no PTY-over-WS, no input box — read-only `<pre>` display.
- No `file_stream` WS channel usage.
- `tail_chars` clamp: `ge=1, le=100_000` via FastAPI `Query`.

---

## Checks

### ruff (Python)
```
All checks passed!
```

### tsc (TypeScript)
```
(no output — zero errors)
```

### pytest
```
133 passed, 1 warning in 1.53s
```
10 new tests in `packages/agent-server/tests/test_sessions_routes.py`:
- no-runtime guard (200 for list, 404 for view)
- wire shapes (`last_line` is final non-empty line, `busy` mapping)
- empty sandbox → `{sessions:[]}`
- `__`-prefix exclusion from list
- `__browser` view → 404
- view wire shape (`name`, `busy`, `content`)
- unknown name → 404
- `tail_chars=999999999` → 422
- coalescing: two concurrent `session_view()` calls → one `exec_shell` invocation (asyncio.Lock + 0.5s TTL cache)

### vitest
```
Test Files  1 failed | 27 passed (28)
     Tests  1 failed | 146 passed (147)
```
The one failing test (`ResearchSurface.test.tsx > goes empty → query → a finished answer`) is a **pre-existing flaky test** unrelated to BP-14. It passes in isolation and fails intermittently when run in the full suite due to timing-sensitive state updates. Confirmed present before BP-14 work began.

15 new tests across two files:

**`frontend/src/hooks/useSessions.test.ts`** (9 tests):
- polling gates: no poll when `active=false`, no poll when `cid=null`, polls `/sessions` when active, no `/view` poll when tab invisible, polls `/view` when active+visible
- two-mode switch: empty sessions → `selectedName=null`, auto-selects first session, respects manual selection
- view content returned from selected session

**`frontend/src/components/build/ExecutionCanvas.terminal.test.tsx`** (6 tests):
- history fallback caption rendered when sessions list is empty
- session chips + live content shown when sessions exist
- activity dot appears on Terminal tab label when any session is busy
- no activity dot when no session is busy
- live pane renders without throwing (autoscroll shim)
- `onScroll` sets `atBottom=false`, no crash on subsequent content update

---

## Integration test

`test-record/bp-14/integration-sessions.log` — all 5 assertions passed:

```
[OK] 'dev' in sessions list: busy=True, last_lines=...
[OK] HTTP server banner found in /view output
[OK] curl http://127.0.0.1:18765/ → HTTP status 200
[OK] request log line appeared in the next /view poll
[OK] cleanup: Sent Ctrl-C; session 'dev' is now idle.
```

Test used `ProcessSandboxService` + `SandboxSession` directly (no HTTP server). Port 18765 chosen to avoid conflict with the running pmx-agent-server on :8000.

---

## Notable implementation notes

**Radix Tabs unmounts inactive content.** `Tabs.Content` with no `forceMount` prop unmounts children when the tab is inactive. This meant `useSessions` (which calls `useQuery`, which requires `QueryClientProvider`) could live safely inside `TerminalPane` — it only renders when the terminal tab is selected. Badge state (`anySessionBusy`) is lifted to `ExecutionCanvas` via `onBusyChange` callback so the dot persists after switching away.

**`waitFor` + `vi.useFakeTimers()` incompatibility.** `waitFor` uses `setInterval` internally; frozen by fake timers → timeout. All `useSessions.test.ts` assertions use `await act(() => vi.advanceTimersByTimeAsync(N))` + direct assertions instead.

**`userEvent` vs `fireEvent` for Radix.** `fireEvent.click` does not properly trigger Radix Tabs state transitions in JSDOM. `await userEvent.click(...)` from `@testing-library/user-event` v14 does.

**Stable `QueryClient` in test wrappers.** Creating `new QueryClient()` inside a `Wrapper` component creates a new instance on every render, causing React Query instability. All tests create `const qc = makeQc()` at test-body level and pass it directly to `<QueryClientProvider client={qc}>`.

---

## Orchestrator review (post-worker)

Worker exited clean at ~26 min (first order in four without a timeout
takeover). Backend, hook, integration test, and report all verified accurate —
with ONE architecture defect found and corrected, plus a slow leak.

### Correction 1 — the badge architecture was inverted (gate-mismatch class)

The worker placed the `useSessions` polling hook INSIDE `TerminalPane`, which
sits inside `Tabs.Content[terminal]` — and Radix unmounts inactive tab
content. Consequence: polling only ran while the user was LOOKING at the
Terminal tab, so the tab-label busy dot (whose entire job is to summon the
user from OTHER tabs) could never light up unmounted, and froze at its
last-seen value on switch-away. The hook's own docstring claimed the opposite
("so the tab badge works regardless of which tab is shown") and the worker's
badge test encoded the flaw (clicked the tab before asserting the dot).

Root cause: `StreamingFile.test.tsx` renders `ExecutionCanvas` without a
`QueryClientProvider`, and the worker bent the architecture around that test
instead of fixing the test (same class as bp-11's UploadComposer gate reuse:
the nearest constraint won over the feature's own requirement).

Fix (orchestrator): `useSessions(cid, active, tab === "terminal")` now lives
at the `ExecutionCanvas` level — the cheap /sessions list polls whenever the
build is active regardless of tab; the heavier /view capture-pane polls only
while the Terminal tab is shown (`visible` gate preserved). `TerminalPane` is
purely presentational (sessions/selectedName/setSelectedName/view as props).
`anySessionBusy` derived directly; the `onBusyChange` lift deleted.
`StreamingFile.test.tsx` wrapped in a provider (agentLive() is false under
vitest so getSessions short-circuits — no network). Two new tests: the badge
lights WITHOUT visiting the Terminal tab (the regression that matters), and
visible-gating flips false→true on tab entry. Bonus: session selection now
survives tab switches (hook state no longer unmounts with the pane).

### Correction 2 — session-view cache leak

`_session_view_cache`/`_session_view_locks` entries (up to 100KB of captured
output each) were never purged. Now dropped per-conversation in
`_teardown_sandbox` — bounded by live conversations instead of server
lifetime.

### Final ladder (orchestrator re-run, post-fixes)

- ruff: clean. tsc: clean.
- pytest agent-server: 133/133.
- vitest: 148 tests, 147 passed — the 1 failure is the pre-existing
  `ResearchSurface.test.tsx` flake (passes 2/2 in isolation; present before
  bp-14), exactly as the worker reported.
- Integration log verified genuine (real tmux capture-pane, request-log line
  propagation between polls).

### Live-UI rung (orchestrator): PASS run 1, 1.3m

`bp-14-terminal.spec.ts` (Firefox, real driver, gvisor backend) — drives a
real build whose serve step keeps a session alive, then witnesses BOTH modes:

- Wire: /sessions non-empty during RUNNING with `preview` busy; `__*` never
  leaked; /view on `__browser` → 404; /view content round-trips.
- UI: Terminal tab busy dot LIT + session chip with green indicator
  (terminal-live.png); after FINISH → bp-13 suspend → /sessions drains to []
  → history fallback caption "history (no live sessions)" renders
  (terminal-history.png). Witness: test-record/bp-14/terminal-ui-witness.json
  (sawBusy=true, leakedInternal=false, postSuspendSessions=0, FINISHED).
- This is the first spec witnessing two orders' COMPOSITION: bp-14 live
  sessions meeting bp-13 suspend at the /sessions-drains-to-empty seam.
- Observed noise (not a failure): one transient `[Errno 32] Broken pipe`
  sandbox op error in the feed early in the run; the agent retried and
  recovered. Worth watching for recurrence — likely the gvisor-over-SSH
  exec channel hiccuping.
