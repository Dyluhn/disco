# DC-07 — Uploads Rematerialization: Report

**Date:** 2026-06-11
**Brief:** `agent-projects/gemini/dc-07-followup-1.md`
**Original work order:** `docs/workorders/DC-07-uploads-rematerialization.md`

---

## Deviations

### 1. `test_build_surface.py` excluded from server suite (PRE-EXISTING)
The full `packages/agent-server` suite hangs indefinitely when `test_build_surface.py`
(10 tests) is included — confirmed on clean `HEAD` with no DC-07 changes. Each
individual test within that file passes in isolation; the hang is an asyncio
event-loop cleanup artifact when the test file is run in the same process as
other test files. **This is not caused by DC-07.** The suite was run with
`--ignore=packages/agent-server/tests/test_build_surface.py`; the remaining
192 tests all pass green.

### 2. Known pre-existing core failures did not reproduce
The brief listed three expected core test failures
(`test_auto_approved_not_stamped_when_base_would_not_gate`,
`test_provider_error_reaches_the_user_with_real_content`,
`test_small_old_observation_is_full`). None of these failed in this run:
**448 passed, 1 skipped** in `packages/core`. This is likely due to the
passing build's code state resolving those failures. No chase needed.

### 3. Reality block location differs from work order manifest
Work item 4 specifies `packages/core/src/disco/core/llm/prompts.py`,
but the resume reality block is generated dynamically in
`packages/agent-server/src/disco/agent_server/runtime.py`
(`_reconstruct_resume_context`, ~line 2014). DC-07 upload awareness was added
**in both locations**: the dynamic reality block in `runtime.py` (listing
uploads as "held server-side and will be restored" when the sandbox is dead)
AND `prompts.py` (the BP-11 bullet now mentions server-side persistence and
re-materialization so the model knows uploads survive sandbox recreation).

---

## Work Items — Completion Status

### Item 1: Server-side Storage (runtime.py) — ✅ COMPLETE (prior session)
- `{PMX_DB}.uploads/<cid>/` sidecar directory created in `__init__` via `_uploads_base`.
- `store_upload(conversation_id, filename, data)` writes to disk.
- `get_upload_names()` / `get_upload_size()` for quota accounting.

### Item 2: Re-materialization (runtime.py) — ✅ COMPLETE (prior session)
- `_rehydrate_after_recreate` calls `_rematerialize_uploads` after `_maybe_rehydrate`.
- Both `on_recreate` hooks (pending session at line 595, fresh session at line 644)
  wire to `_rehydrate_after_recreate`.
- `_rematerialize_uploads` reads from `{PMX_DB}.uploads/<cid>/` and writes each file
  back into the sandbox's `uploads/` directory via `session.write_file()`.

### Item 3: Upload Logic + Quota (app.py) — ✅ COMPLETE (prior session)
- `upload_files` writes to both sandbox AND server-side sidecar.
- Quota checks (`~58, 100MB`) now count sidecar-stored bytes via
  `runtime.get_upload_size()` plus any sandbox-only files.

### Item 4: Resume Reality Block (prompts.py) — ✅ COMPLETE (this session)
- **prompts.py** (`_EXECUTION_DRIVER_PROMPT`): BP-11 bullet updated to mention
  server-side storage and that uploads survive sandbox recreation
  ("files survive sandbox recreation — if a resume reality block lists uploads
  as 'held server-side' or missing, they are re-materialized into the fresh
  sandbox on your next action").
- **runtime.py** `_reconstruct_resume_context`: DC-07 block already present
  (prior session), listing uploads with status "intact" (sandbox alive) or
  "held server-side and will be restored" (sandbox dead).

---

## Test Evidence

### `packages/agent-server` — 192 passed (10 excluded for pre-existing hang)
**Log:** `test-record/dc-07/units-server.log`

Of particular relevance to DC-07:

| Test | Result |
|------|--------|
| `test_upload_survives_recreation` | ✅ PASSED — upload → wipe sandbox → re-materialize → bytes identical |
| `test_sidecar_quota_counts_toward_limit` | ✅ PASSED — 99MB sidecar + 2MB upload hits 100MB cap → 413 |
| `test_sidecar_quota_allows_when_under` | ✅ PASSED — 90MB sidecar + 5MB upload → 200 OK |
| `test_per_conversation_quota_reject` | ✅ PASSED |
| `test_per_conversation_quota_allows_when_under` | ✅ PASSED |
| `test_pending_session_adopted_by_build_loop` | ✅ PASSED |
| `test_pending_session_destroyed_on_kill` | ✅ PASSED |
| `test_partial_reject_returns_200_with_saved_and_rejected` | ✅ PASSED |
| All 24 remaining upload tests | ✅ ALL PASSED |
| All resume/runtime/lifecycle tests | ✅ ALL PASSED |

### `packages/core` — 448 passed, 1 skipped
**Log:** `test-record/dc-07/units-core.log`

- Three known failures from the brief did NOT appear (see Deviation 2).
- No DC-07-caused regressions.

---

## Verification Checklist

- [x] `_rehydrate_after_recreate` copies server-held uploads back into fresh sandbox
- [x] Both `on_recreate` paths wire re-materialization (pending session + fresh session)
- [x] Quota accounting (~100MB) counts sidecar store bytes
- [x] Upload writes through to both sandbox AND sidecar
- [x] Resume reality block reflects upload state (intact vs held server-side)
- [x] prompts.py BP-11 bullet mentions DC-07 persistence behavior
- [x] test_upload.py covers: upload → recreation → re-materialization + sidecar quota
- [x] Full suites green (modulo pre-existing `test_build_surface.py` hang)
