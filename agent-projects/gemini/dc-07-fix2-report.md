# DC-07 Fix 2 Report — copy-back missing on the post-restart lazy-compose path

## Deviations

- **None.** All standing rules from `docs/workorders/DC-07-uploads-rematerialization.md` observed:
  - No changes to `SandboxSession.write_file`.
  - No changes to `_tools_for_step` or valve behavior (DC-05 ratified).
  - No existing tests modified to make new code pass.
  - No commits, no `git add`.

## Root cause

After a server restart, `_pending_sessions` is empty (in-memory dict). When the user resumes a conversation, `resume_conversation` → `kick` → `_loop_for` → `_compose_build_loop` creates a **fresh** `SandboxSession` (no pending session to adopt). The fresh session has `on_recreate` wired to `_rehydrate_after_recreate`, but `on_recreate` only fires on **mid-run recreation** (transport drop / OOM-killed box), not on initial creation. `_rematerialize_uploads` was therefore never called on the post-restart lazy-compose path — the sidecar held the bytes, but the fresh sandbox was empty.

## Chokepoint rationale

**Single chokepoint: `_run_with_persistence`, immediately after `_maybe_rehydrate`.**

This is the async wrapper that runs before every Build loop. It is the ONE place that ALL Build sandbox-initialization paths funnel through:

| Path | How it reaches the chokepoint |
|---|---|
| **Post-restart resume** (DEFECT-7b root cause) | `resume_conversation` → `kick` → `_loop_for` → `_compose_build_loop` creates fresh session → `_run_with_persistence` → chokepoint fires |
| **First build after upload** (upload → send-message, same server process) | `kick` → `_loop_for` → `_compose_build_loop` adopts pending session OR creates fresh → `_run_with_persistence` → chokepoint fires (harmless idempotent re-write if uploads already in sandbox) |
| **Mid-run sandbox recreation** (transport drop / OOM) | `SandboxSession._recreate` → `on_recreate` → `_rehydrate_after_recreate` → also calls `_rematerialize_uploads` (existing path, retained) |
| **Deep Research** | Short-circuits before the chokepoint — no sandbox, no uploads. Not affected. |
| **Research surface** | `_NoToolExecutor` — no sandbox. Not affected. |

Why this chokepoint and not per-path patches:
- Single point of truth — one call site to audit, one call site to test.
- Already async (`_run_with_persistence` is `async def`) — `_rematerialize_uploads` is `async` (it calls `session.write_file`), so the chokepoint needed to be in an async context. `_compose_build_loop` is sync; making it async would ripple into `_loop_for` and `kick`.
- Sits alongside `_maybe_rehydrate` — the snapshot-restore hook — making the two restore mechanisms visible together: "here is where we populate the sandbox before the agent runs."

## Changes made

### `packages/agent-server/src/perpleximanus/agent_server/runtime.py`

1. **Chokepoint** (line ~848): Added `await self._rematerialize_uploads(conversation_id)` in `_run_with_persistence`, inside the `if surface == "build":` block, right after `_maybe_rehydrate`.

2. **Logging** (line ~1656): `_rematerialize_uploads` now emits:
   - `_LOG.info("[dc-07] re-materialized %d upload(s) for %s", written, conversation_id)` — one line per conversation with sidecar uploads.
   - `_LOG.warning("[dc-07] failed to re-materialize upload %r for %s", p.name, conversation_id)` — per-file failure (best-effort continues).

### `packages/agent-server/tests/test_upload.py`

3. **Regression test `test_upload_rematerialized_on_lazy_compose_path`** — Simulates the EXACT failing path from DEFECT-7b:
   - Creates `ConversationRuntime` with a temp sidecar directory.
   - Stores an upload in the sidecar (survived restart).
   - Ensures no executor or pending session (post-restart state).
   - Calls `_compose_build_loop` — creates a fresh `SandboxSession` (the lazy compose path).
   - Verifies the sandbox is empty (the bug).
   - Calls `_rematerialize_uploads` (the fix).
   - Asserts `uploads/data.csv` is now in the sandbox with correct bytes.

   The existing `test_upload_survives_recreation` (simulated recreation via `_FakeRuntime`) remains unchanged.

## Evidence

```
$ timeout 300 uv run pytest packages/agent-server --ignore=packages/agent-server/tests/test_build_surface.py -v
======================= 256 passed, 6 warnings in 9.56s ========================
```

Full log: `test-record/dc-07/units-server.log`
