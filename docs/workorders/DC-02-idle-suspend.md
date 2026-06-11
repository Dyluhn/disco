# DC-02 — Idle-suspend/resume instead of reap-at-FINISHED

**Read `README.md` first. De-complexity Wave 0 (docs/decomplexity-wave-plan.md DC-02).
Builds on dc-01 (host proxy, COMMITTED). Scope = agent-server runtime lifecycle +
one config field. Do NOT touch packages/tools, the frontend, or any engine/loop code.**

## Why

The task-as-resource model's sharpest edge: a clean FINISHED tears the sandbox
down (`runtime.py` ~788-790, the "G safe leak fix"), so the working preview
vanishes at the moment of success. The dc-01 live rung had to assert the preview
*mid-run* because of this. Meanwhile the machinery for the right behavior
ALREADY EXISTS and is the reason this order is small:

- `sweep_idle_once()` + `_idle_sweep_loop()` (runtime.py ~1009-1052): a periodic
  sweep that suspends non-RUNNING, no-UI-connection sandboxes past a TTL
  (`PMX_IDLE_SUSPEND_S`, default 1800 s).
- `_suspend()` (~974-991): snapshot → teardown, exactly the suspend semantics.
- `ensure_preview()` (~1608-1637): the full WAKE path — re-composes the
  loop/executor, rehydrates the snapshot, starts the built-in preview server.
  Today only the UI "Restart preview" button reaches it.

DC-02 = stop reaping at FINISHED, let the existing sweep do suspension, make the
TTL a config field, and wire the host-proxy's no-executor path into the existing
wake. Mostly deletion + plumbing. Do not build new lifecycle machinery.

## The decided design (locked)

### 1. `runtime.py` — FINISHED no longer reaps immediately

In the `_ENDED` block (~lines 782-801): keep `_maybe_snapshot` exactly as is;
DELETE the immediate `_teardown_sandbox` on FINISHED (and the projects_root
warning branch that exists only to explain the immediate teardown's absence —
replace both with a short comment: FINISHED now rides the idle sweep like
STUCK/ERROR/PAUSED; suspend = `sweep_idle_once` → `_suspend`). The sweep
already covers FINISHED (it skips only RUNNING).

### 2. TTL becomes config: `sandbox.idle_ttl_s`

- `packages/core/src/perpleximanus/core/llm/config.py`: add
  `idle_ttl_s: int = 1800` to `SandboxSettings` (~lines 64-84), with a one-line
  comment (seconds a non-RUNNING sandbox may sit idle before suspend).
- `sweep_idle_once()`: TTL = `PMX_IDLE_SUSPEND_S` env if set (test/ops
  override, keeps every existing test green), else
  `self._config_store.load().sandbox.idle_ttl_s`. Same pattern as the other
  `self._config_store.load()` call sites.

### 3. Wake-on-preview-hit through the host proxy

- `runtime.py`: new `async def wake_for_preview(self, cid8: str, port: int)
  -> str | None`:
  1. `resolve_cid_prefix(cid8)` (live executor) → `port_upstream` → return.
  2. No live executor: resolve cid8 → full cid against the EVENT STORE's
     conversation list (same uniqueness rule: exactly one match else None).
  3. Guard a per-cid `asyncio.Lock` (dict, like `_session_view_locks`) so a
     poll storm can't spawn N container creates, then call the EXISTING
     `ensure_preview(cid)`; on True → `port_upstream(cid, port)` else None.
  Honest scope: wake restores the workspace + the built-in static preview
  server on 8000. It does NOT restart agent-started dev servers (vite/express)
  — a request for a port nothing listens on after wake proxies to a 502, and
  that is correct. Document this in the method docstring.
- `app.py`: `_preview_upstream_resolver` becomes
  `async def` returning `await runtime.wake_for_preview(cid8, port)` (which
  subsumes the live-path lookup — collapse the old body into wake_for_preview
  step 1).
- `host_proxy.py`: the middleware awaits the resolver if it returns an
  awaitable (`inspect.isawaitable`); plain return values keep working (the
  unit-test stubs stay sync).

### 4. What does NOT change

- `reconcile_orphaned_runs` / `_sweep_orphan_containers` (startup orphan sweep
  stays exactly as is — post-restart containers are unreachable and correctly
  destroyed; wake re-creates from snapshot).
- Hard reap on user Kill/Delete; `_teardown_sandbox` itself; snapshot/rehydrate
  internals; `_rehydrated`-flag discipline (ensure_preview already routes
  through `_maybe_rehydrate`).
- No Settings-UI knob, no frontend changes, no new env vars.

## Acceptance ladder

1. **Unit — `packages/agent-server/tests/test_idle_suspend.py`** (NEW; follow
   the stub/fixture patterns of test_lifecycle.py):
   - build run ends FINISHED with projects_root configured → executor STILL in
     `_executors` (the reap is gone);
   - `sweep_idle_once` past TTL suspends it (snapshot written, executor popped);
   - TTL precedence: env `PMX_IDLE_SUSPEND_S` wins over config
     `sandbox.idle_ttl_s`, config wins over the 1800 default;
   - `wake_for_preview`: no executor + snapshot record → ensure_preview path
     taken, upstream returned; no snapshot → None; ambiguous/unknown cid8 →
     None; concurrent calls share the lock (no double-create — assert
     ensure_preview called once).
2. **Update, honestly, the tests the reap-removal flips** — at minimum the
   FINISHED-teardown assertions in `tests/test_lifecycle.py` and
   `tests/test_override_persistence.py` (auto-suspend section). Flip them to
   assert the new lifecycle; never delete coverage.
3. **`tests/test_host_proxy.py`**: add an async-resolver case (returns a
   coroutine) proving the middleware awaits it; existing sync-stub tests stay
   untouched and green.
4. Run the agent-server package suite (`uv run pytest packages/agent-server -q`)
   — log → `test-record/dc-02/units.log` (create the dir). pytest per-package
   ONLY; never the whole repo.
5. **Report** — `agent-projects/gemini/dc-02-report.md`: what changed, verbatim
   test output, deviations declared honestly. Do NOT commit; no git writes.

The live rung (FINISHED → preview survives → forced 10 s TTL → suspend →
preview hit wakes → screenshot) is RUN BY THE ORCHESTRATOR after review — do
not attempt to drive the UI or start vite.

## Manifest (orders.yaml `dc-02` — touch nothing outside it)

- packages/agent-server/src/perpleximanus/agent_server/runtime.py
- packages/agent-server/src/perpleximanus/agent_server/app.py
- packages/agent-server/src/perpleximanus/agent_server/host_proxy.py
- packages/core/src/perpleximanus/core/llm/config.py
- packages/agent-server/tests/test_idle_suspend.py
- packages/agent-server/tests/test_host_proxy.py
- packages/agent-server/tests/test_lifecycle.py
- packages/agent-server/tests/test_override_persistence.py
- test-record/dc-02/units.log
- agent-projects/gemini/dc-02-report.md
