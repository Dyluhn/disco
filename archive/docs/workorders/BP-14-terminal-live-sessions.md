# BP-14 — Terminal tab = live session views

**Read `README.md` first. Requires BP-01 + BP-02.**

## Why

The Terminal tab today (`TerminalPane`, `frontend/src/components/build/
ExecutionCanvas.tsx`) renders a *derivation of past events* (`deriveTerminal(events)`) —
a transcript, not a terminal. After BP-01 the truth is tmux sessions the backend can
capture live. The user should watch exactly what the agent watches (`shell_view`'s
source), including a dev server's output BETWEEN agent steps — that is the cockpit.

## The decided design

Polling REST (not a new WS channel): capture-pane is cheap, 1 Hz is plenty, and polling
keeps the WS protocol untouched. (The existing `file_stream` ephemeral channel is for
watch-it-write; do not overload it.)

### 1. Routes (`agent_server/app.py` → `runtime` → `SandboxSession.sessions`)

```
GET /conversations/{cid}/sessions
→ {"sessions": [{"name": "preview", "busy": false, "last_line": "Serving HTTP on …"},
                 {"name": "dev", "busy": true, "last_line": "VITE ready in 312 ms"}]}

GET /conversations/{cid}/sessions/{name}/view?tail_chars=10000
→ {"name": "dev", "busy": true, "content": "<capture-pane tail>"}
```

- Backed by `ShellSessionManager.list()` / `.view()` (BP-01). No sandbox/session yet →
  `{"sessions": []}` (200, not 404 — the UI treats empty as "nothing running").
- `name` validated against the manager's list (404 otherwise). Sessions starting with
  `__` (internal: `__browser`, `__kernel`) are EXCLUDED from `list` output — internal
  plumbing stays internal.
- Throttle guard: at most one in-flight capture per (cid, name); concurrent requests
  coalesce (an `asyncio.Lock` + 0.5 s result cache in runtime — exec_shell per request
  per client would hammer the sandbox).

### 2. Frontend (`ExecutionCanvas.tsx`)

- New hook `useSessions(cid, active)`:
  poll `/sessions` every 2 s while the Terminal tab is visible AND the conversation is
  in an active status (same `active` predicate `useBuildPreview` uses); poll the
  SELECTED session's `/view` every 1 s.
- TerminalPane becomes two-mode:
  - **Sessions exist** → a chip row (session name + green/idle dot, busy = pulsing) above
    a monospace pane rendering the selected session's `content` (preserve the existing
    pane styling; autoscroll to bottom unless the user scrolled up — reuse the
    follow-the-stream pattern from the feed if exported, else implement the same
    near-bottom rule).
  - **No sessions** (empty list, e.g. pre-BP-01 history or research mode) → the current
    `deriveTerminal(events)` transcript, labeled with a muted caption `history (no live
    sessions)`. The old behavior remains the fallback, never the default when live data
    exists.
- Tab badge: when any session is busy, the Terminal tab label shows a small activity dot
  (the user can SEE a server is alive without opening the tab).

## Acceptance

1. **Unit (python)**: route shapes; `__`-prefix exclusion; 404 on unknown name; coalescing
   (two concurrent view calls → one exec_shell on a counting fake).
   **Unit (frontend, vitest)**: hook polling start/stop on tab visibility; two-mode
   render switch; autoscroll near-bottom rule.
2. **Integration (process backend)**: live conversation, agent-started
   `python3 -m http.server 8000` in session `dev` (drive via the tools directly, no LLM
   needed): `/sessions` lists `preview` + `dev` with correct busy flags; `/view` content
   contains the http.server banner; curl the app → a request-log line appears in the
   NEXT `/view` poll (live output between steps — the point of this order).
3. **UI surface (live, Firefox, real driver)** — `bp-14-terminal.spec.ts`: a live build
   that starts a dev server; open Terminal tab; assert chips render, select `dev`,
   assert the pane shows real server output and UPDATES (poll for a content change after
   hitting the preview to generate a request log). Screenshots: `terminal-chips.png`,
   `terminal-live-output.png` → `test-record/screenshots/bp-14/`, sent to user.

## Prohibitions

- No xterm.js / PTY-over-WebSocket interactive terminal (read-only view; user input to
  sessions is NOT in scope and must not be half-shipped as a non-wired input box).
- Internal `__*` sessions never reach the UI.
- Do not remove `deriveTerminal` — it is the documented fallback.
