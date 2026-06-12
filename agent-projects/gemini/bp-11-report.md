# BP-11 Implementation Report — File upload into the Build workspace

**Branch:** `build-surface-recovery-ux`  
**Date:** 2026-06-10

---

## What was done

### §1 — Backend endpoint (`app.py`)

`POST /conversations/{cid}/files` is implemented in `packages/agent-server/src/disco/agent_server/app.py`.

- **Sanitization** (`_sanitize_name`): `Path(raw).name` → NFC → `lstrip(".")` → `re.sub(r"\s+", "-", name.strip())` → reject empty.
- **Limits**: 25 MB/file, 20 files/request, 100 MB/conversation (via `list_dir` + `read_file` of existing `uploads/` files).
- **Collision suffix**: `-2`, `-3`, … tracking the working `existing_names` set within the request.
- **Write path**: `session.write_file(f"uploads/{name}", data)` through the sandbox — never host fs.
- **Event**: ONE `MessageEvent(source=EventSource.ENVIRONMENT, ...)` appended after all saves, format `"User uploaded: uploads/data.csv (18,234 bytes), …"` (thousands separators via Python's `:,` format spec).
- **State legality**: `ConversationStatus.ERROR` → HTTP 409; all other states (IDLE, RUNNING, WAITING_FOR_CONFIRMATION, FINISHED, etc.) → allowed.
- **Sandbox accessor**: `runtime._executors.get(cid)._sandbox` — the same pattern used by the `preview` and `port_upstream` routes.
- **No sandbox** (conversation not yet kicked): HTTP 409 `no_active_sandbox`.

### §2 — Frontend

**`frontend/src/api/agent.ts`**: `uploadFiles(cid, files)` — raw `fetch` POST with a `FormData` body (no `Content-Type` header; browser sets the `multipart/form-data; boundary=…` automatically). Returns `UploadResult { saved, rejected }`. Passes 413 bodies through without throwing (partial rejection is not an error). Offline mode returns `{ saved: [], rejected: [] }`.

**`frontend/src/components/build/BuildSurface.tsx`** (new): `UploadComposer` component. Paperclip button (hidden `<input type="file" multiple>`), drag-and-drop onto the component group with a visible "Drop files to upload" overlay while dragging (`data-dragging` attribute + blue outline). Success → `useToast().show(...)`, per-rejection red toast. Disabled when `cid` is null.

**`frontend/src/components/BuildSurface.tsx`** (deviation — see below): `UploadComposer` is imported from `build/BuildSurface.tsx` and rendered inside the `{steerable && ...}` block, above `SteerInput`.

**`frontend/src/components/build/ExecutionCanvas.tsx`**: **No changes made.** See Files-tab note below.

---

## Deviations

### 1 — Manifest path mismatch: `build/BuildSurface.tsx` vs `BuildSurface.tsx`

The work-order manifest lists `frontend/src/components/build/BuildSurface.tsx` as the composer target, but the actual BuildSurface lives at `frontend/src/components/BuildSurface.tsx` (the root of `components/`, not in the `build/` subdirectory). The `build/` directory contains sub-components imported by the main surface.

**Resolution**: Created `build/BuildSurface.tsx` as specified (containing `UploadComposer`), and also modified `frontend/src/components/BuildSurface.tsx` to import and render it. The second file is outside the manifest; the change is a two-line addition (import + one JSX element). The tests in `BuildSurface.upload.test.tsx` test `UploadComposer` from the manifest path.

### 2 — `prompts.py` not touched

Per §3 of the brief: "orchestrator owns prompts.py this wave."

### 3 — Behavioral rung + `bp-11-upload.spec.ts` not done

Per §3: "orchestrator's."

### 4 — Files-tab query: no existing query to invalidate

The brief says "invalidate the Files-tab query (find the existing query key in `ExecutionCanvas.tsx` FilesPane)." No such query exists. The FilesPane derives its file list from `useMemo(() => deriveFiles(events))` — a pure derivation over the in-memory event stream, with no `useQuery` and no query key. `deriveFiles` tracks `file_write`, `file_append`, and `file_edit` action events emitted by the agent's tool executor; files written directly to the sandbox via the upload endpoint do NOT produce such events.

**Effect on UX**: Uploaded files appear in the Activity Feed immediately via the environment `MessageEvent`. They appear in the Files tab only if/when the agent subsequently calls `file_write`/`file_read` on them (which it will — the event message tells it to `file_read` before using them). No fix was applied to `ExecutionCanvas.tsx`; this behaviour is noted.

**The "lister filtering directories" check**: The FilesPane does not filter directories — it simply never tracks them (it's event-derived, not filesystem-browsing). `uploads/` as a directory won't appear; the files inside it appear when the agent touches them. No fix needed or applied.

---

## Acceptance results

### 1 — Python unit tests (`test_upload.py`)

26 tests, all pass:

| Area | Tests |
|------|-------|
| Sanitization table (traversal, dotfile, NFC, whitespace) | 10 + 1 |
| Collision suffixing (-2, -3) | 1 |
| Per-file size limit (reject / boundary) | 2 |
| Per-request file count | 2 |
| Per-conversation quota | 2 |
| Event text format (single, multi, no-event-on-all-rejected) | 3 |
| State legality (ERROR blocked, FINISHED allowed, IDLE allowed, no-sandbox) | 4 |
| Partial reject returns 200 | 1 |

### 2 — Frontend unit tests (vitest)

- `BuildSurface.upload.test.tsx`: 5 tests (paperclip render, disabled-when-cid-null, drag-highlight, drop-clears-highlight, uploadFiles called on drop) — all pass
- `agent.upload.test.ts`: 5 tests (FormData POST, file field append, payload return, offline no-op, 413 no-throw) — all pass
- **Full vitest suite**: 117/117 tests pass (no regressions)

### 3 — Integration test (`test-record/bp-11/integration-upload.log`)

- Posted 37-byte CSV + 30 MB blob; CSV saved, blob rejected (25 MB limit reason surfaced)
- `list_dir("uploads")` returns `['data.csv']`
- ONE environment MessageEvent with correct text
- All assertions PASSED

### 4 — ruff / tsc / pytest

| Check | Result |
|-------|--------|
| `ruff check app.py test_upload.py` | ✅ clean |
| `pytest packages/` | ✅ exit 0 (all packages) |
| `npx tsc --noEmit` | ✅ clean |
| `npx vitest run` | ✅ 117/117 |

---

## Follow-up — upload on a FRESH/IDLE conversation (2026-06-10)

### Root cause

`_compose_build_loop` always created a new `SandboxSession` keyed by `instance_id`
(`gvisor.py:329`). A session created by the upload route (before any loop) and the
session the build loop ran in were different objects with different `instance_id`s, so
files written through the pre-kick session landed in a workspace the build loop never
mounted.

### What changed

| File | Change | Lines |
|------|--------|-------|
| `runtime.py` | Added `self._pending_sessions: dict[str, SandboxSession] = {}` next to `_executors` | ~193 |
| `runtime.py` | Extracted egress-posture block into `_build_sandbox_spec(self) -> SandboxSpec` | ~501–512 |
| `runtime.py` | Added `upload_session(self, conversation_id) -> SandboxSession` — returns the live executor's sandbox when a loop exists; otherwise lazily creates a pending session | ~513–527 |
| `runtime.py` | `_compose_build_loop` adopts pending session via `self._pending_sessions.pop(cid, None)`; creates a fresh session only when none exists | ~545–551 |
| `runtime.py` | `_teardown_sandbox`: pops + `await session.destroy()` on `_pending_sessions[cid]` | ~771–775 |
| `runtime.py` | `kill`: same pop + destroy on `_pending_sessions[cid]`, best-effort `contextlib.suppress` | ~1483–1487 |
| `runtime.py` | `aclose`: iterates `_pending_sessions.values()` and destroys each, best-effort | ~1499–1501 |
| `app.py` | Upload route: replaced `_executors.get` / `getattr(_sandbox)` / 409 with `runtime.upload_session(cid)`; kept `runtime is None → 409` guard and ERROR-state 409 | ~209–212 |

### Teardown sites found (grep `_executors`)

Three sites in `runtime.py` that pop `_executors` for a conversation; all three now also pop `_pending_sessions`:

1. `_teardown_sandbox` (~756) — called on clean FINISH after snapshot
2. `kill` (~1452) — the hard kill-switch path
3. `aclose` (~1483) — server shutdown (iterates all values)

### Tests

| Test | Change |
|------|--------|
| `test_upload_no_sandbox_returns_409` | **Renamed + flipped** → `test_upload_no_executor_lazily_creates_session`: upload to a never-kicked conversation now returns 200 and records `cid` in `_pending_sessions` |
| `test_upload_no_runtime_returns_409` | **Added**: 409 still fires when `runtime is None` |
| `test_pending_session_adopted_by_build_loop` | **Added**: `upload_session(cid)` on a fresh runtime creates a pending session; subsequent `_compose_build_loop(cid, …)` adopts it — `loop.executor._sandbox is session` (object identity) and `cid not in rt._pending_sessions` |
| `test_pending_session_destroyed_on_kill` | **Added**: pending session created, then `kill(cid)` — `session.destroy` is called once and `cid` is removed from `_pending_sessions` |

### pytest output (packages/agent-server/tests/ only)

```
collected 93 items

packages/agent-server/tests/test_build_surface.py ..........             [ 10%]
packages/agent-server/tests/test_health.py ..                            [ 12%]
packages/agent-server/tests/test_model_pick_pipeline.py ...              [ 16%]
packages/agent-server/tests/test_override_persistence.py .........       [ 25%]
packages/agent-server/tests/test_projects_endpoints.py ...........       [ 37%]
packages/agent-server/tests/test_research.py .....                       [ 43%]
packages/agent-server/tests/test_runtime.py ...                          [ 46%]
packages/agent-server/tests/test_sandbox_config.py ..........            [ 56%]
packages/agent-server/tests/test_upload.py ............................. [ 88%]
packages/agent-server/tests/test_wire.py ...........                     [100%]

======================== 93 passed, 1 warning in 1.14s =========================
```

### Deviations

None. The fix is exact object-identity adoption as specified; no directory re-reads.
