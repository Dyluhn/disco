# Vitest Triage Findings

## Summary

All 23 initial frontend Vitest failures were stale tests. I did not find a real component bug requiring production-code changes. The fixes update test mocks, async expectations, and assertions to match the documented auth, CSRF, capability URL, and bounded-notice behavior.

## Per-Family Verdicts

- `current/frontend/src/api/agent.reconnect.test.ts`: stale test. `subscribeLive()` now awaits `ensureAgentSession()` before opening or reconnecting a socket, so the reconnect assertions needed microtask and async-timer flushing. The reconnect behavior remains covered.
- `current/frontend/src/api/agent.upload.test.ts`: stale test. Uploads now flow through authenticated `agentFetch`, which owns base URL resolution, credentials, and CSRF headers. The tests now set runtime `AGENT_BASE` and assert FormData payloads, absolute agent URLs, credentials, CSRF, and no multipart content-type override.
- `current/frontend/src/api/deepResearch.test.ts`: stale test. Export requests now use authenticated `agentFetch`, and create tests needed to bind to the same mocked client module instance as the API under test. The tests now assert the current URL/auth behavior without weakening payload checks.
- `current/frontend/src/api/projects.import.test.ts`: stale test. The auth/owner-scoping change intentionally strips client-owned `owner_id` and routes import calls through `agentFetch`. The tests now assert the sanitized payload and authenticated request shape.
- `current/frontend/src/components/build/AgentCanvas.live.test.tsx`: stale test. The live-browser flow now gets live URLs via POST and scoped preview capabilities via `previewBootstrapUrl`. The tests now mock that current flow while preserving assertions for auto-start, silent fallback, transient retry, hard latch, and owning-CID teardown.
- `current/frontend/src/components/build/DeckExportBar.test.tsx`: stale test. The component behavior is still valid, but the API mock was missing the new `agentFetch` export introduced by the auth refactor. The mock now delegates realistically to `fetch`.
- `current/frontend/src/components/build/canvas/PreviewPane.reload.test.tsx`: stale test. Preview URLs now resolve asynchronously through `previewBootstrapUrl`. The reload tests now mock that capability path and flush the async work while preserving W-42 reload behavior coverage.
- `current/frontend/src/components/research/DeepResearchSurface.test.tsx`: stale test. The fixture stream can exceed the default 5 second timeout, and `bounded_by: "rounds"` is intentionally not shown as a bounded/truncated notice. The lifecycle test now waits for the finished report and asserts that no false rounds notice is rendered.
- `current/frontend/src/components/settings/McpSection.live.test.tsx`: stale test. `apiFetch` normalizes request headers to a `Headers` object. The test now reads the header via `new Headers(init.headers)` while keeping the CRUD/refetch assertions intact.

## Verification

- `cd frontend && npm run test`: exit 0

```text
Test Files  142 passed (142)
Tests  954 passed (954)
Start at  01:41:46
Duration  23.56s (transform 2.09s, setup 4.93s, collect 22.11s, tests 65.55s, environment 37.69s, prepare 7.28s)
```

- `cd frontend && npm run typecheck:build`: exit 0
- `cd frontend && npx vite build`: exit 0
