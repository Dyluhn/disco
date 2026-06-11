# DC-04b — /sessions retry + degrade-to-stale (DEFECT-1, agent-server half)

**Read `README.md` first. De-complexity Wave 0 (docs/decomplexity-wave-plan.md DC-04).
Scope = packages/agent-server ONLY — the DEFECT-2 (tool diagnostics) half already
landed as dc-04a. Do NOT touch packages/core or packages/tools.**

## Why (read the evidence first: test-record/marathon/DEFECTS.md, DEFECT-1)

During the bp-16 marathon the UI's session-tab poll (`GET
/conversations/{cid}/sessions`) hit a dropped ssh pipe to the gvisor host and
the endpoint returned **500**: `runtime.sessions_list` calls
`session.sessions.list()` with no protection, so one transient transport
hiccup propagates straight through FastAPI. The UI poll loop then renders an
error state for something that fixes itself a second later. A *listing* is
read-only and cacheable — the honest degraded answer is the last list we saw,
marked stale, never a 500.

Code as of HEAD:
- `packages/agent-server/src/perpleximanus/agent_server/runtime.py:1537` —
  `sessions_list()`: `live_session()` → None ⇒ `[]`; otherwise
  `await session.sessions.list()` unguarded.
- `packages/agent-server/src/perpleximanus/agent_server/app.py:342` — the
  `/sessions` route; `app.py:363` — `/sessions/{name}/view` (calls
  `sessions_list` for its existence check).

## The decided design (locked)

### 1. `runtime.py` — snapshot with retry + last-known cache

- Add `self._last_sessions: dict[str, list[SessionInfo]] = {}` in `__init__`
  (next to the other per-cid dicts) and pop the cid in `_teardown_sandbox`
  (a suspended/killed sandbox must not ghost its session list into the next
  incarnation).
- New method directly above `sessions_list`:

  ```python
  async def sessions_snapshot(self, conversation_id: str) -> tuple[list[SessionInfo], bool]:
      """Session list + staleness. Fresh on success (cache updated); on
      transport failure retry twice (0.25 s apart), then degrade to the
      last-known list marked stale=True — a read-only listing must never
      500 the UI poll loop (DEFECT-1). No sandbox -> ([], False)."""
  ```

  Semantics: `live_session()` is None → `([], False)` (unchanged contract —
  not stale, there is genuinely nothing). Otherwise attempt
  `session.sessions.list()` up to **3 times total** (initial + 2 retries,
  `await asyncio.sleep(0.25)` between); on success filter the `__`-internal
  names (same filter as today), store the filtered list in `_last_sessions`,
  return `(fresh, False)`. If all attempts raise → log one warning with the
  cid + final exception (`_LOG.warning`, no stack spam) and return
  `(self._last_sessions.get(conversation_id, []), True)`.
- `sessions_list` becomes a thin compat wrapper:
  `return (await self.sessions_snapshot(conversation_id))[0]` — every existing
  caller keeps working, degraded instead of raising.

### 2. `app.py` — surface the staleness

- `/sessions` route calls `sessions_snapshot` and adds `"stale": stale` to the
  response JSON (additive field; the frontend may ignore it for now — do NOT
  touch the frontend in this order).
- `/sessions/{name}/view` keeps calling `sessions_list` (its existence check
  now rides the same degrade path; no signature change needed there).

### Anti-scope

- No frontend changes. No changes to `session_view` caching/coalescing. No
  changes to the sandbox packages. No new config knobs — the retry count and
  backoff are constants (`_SESSIONS_LIST_RETRIES = 2`,
  `_SESSIONS_LIST_BACKOFF_S = 0.25` class attributes, mirroring the existing
  `_SESSION_VIEW_*` style).
- Do not "fix" anything else you notice in runtime.py — file it in the report.

## Acceptance ladder

1. **Unit — `packages/agent-server/tests/test_sessions_degrade.py`** (NEW).
   Follow `test_sessions_routes.py`'s fixture pattern (real
   `ConversationRuntime` with a mocked `live_session` whose
   `sessions.list` is an `AsyncMock`; and `create_app(store, runtime=...)`
   TestClient for the route layer). MUST cover:
   - transient: `list` raises once then succeeds → `(fresh, False)`, exactly
     2 calls, cache updated;
   - dead pipe with history: seed one successful call, then `list` raises
     persistently → `(last_known, True)`, exactly 3 calls on the failing
     snapshot, payload equals the earlier fresh list;
   - dead pipe cold: persistent raise, no prior success → `([], True)`;
   - no sandbox: `live_session` → None ⇒ `([], False)` and `_last_sessions`
     untouched;
   - teardown hygiene: populate the cache, run `_teardown_sandbox`, cache no
     longer holds the cid;
   - route layer: `/sessions` returns 200 with `"stale": true` and the
     last-known names when the runtime degrades — and NEVER 500 (assert
     status_code == 200 explicitly on the degraded path);
   - replay the DEFECT-1 SHAPE: a runtime whose `sessions.list` raises
     `ConnectionResetError("ssh pipe dropped")` mid-poll → two consecutive
     `/sessions` polls both 200, second one stale.
2. Run ONLY the agent-server tests you added plus
   `test_sessions_routes.py` (`uv run pytest
   packages/agent-server/tests/test_sessions_degrade.py
   packages/agent-server/tests/test_sessions_routes.py -x -q`) — never the
   whole repo, never other packages' suites. Log →
   `test-record/dc-04b/units.log` (create the dir).
3. **Report** — `agent-projects/sonnet/dc-04b-report.md`: what changed,
   verbatim test output, any deviation declared honestly. Do NOT commit; do
   NOT run git commands other than read-only ones.

## Manifest (orders.yaml `dc-04b` — touch nothing outside it)

- packages/agent-server/src/perpleximanus/agent_server/runtime.py
- packages/agent-server/src/perpleximanus/agent_server/app.py
- packages/agent-server/tests/test_sessions_degrade.py
- test-record/dc-04b/units.log
- agent-projects/sonnet/dc-04b-report.md
