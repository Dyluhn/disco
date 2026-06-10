# BP-15 Report — Screenshots in the feed + isolation tier over the wire

## Step 0 — Current wiring (re-verified 2026-06-10, post-bp-14-commit 2d6aba1)

### Screenshot production
- **`packages/tools/src/perpleximanus/tools/builtin/_browser_daemon.py:153`** — `screenshot_rel_path = f".pmx/screenshots/{state.screenshot_seq:04d}-{action}.png"` → stored to disk, included in the daemon JSON response at `:165` (`"screenshot_path": screenshot_path`).
- **`packages/tools/src/perpleximanus/tools/builtin/browser.py:223`** — `return ToolOutcome(success=True, content=..., structured=data)` carries the whole daemon dict (including `screenshot_path`) as `ToolOutcome.structured`.
- `ToolResult.structured?: Record<string, unknown> | null` already on the wire (`types/agent.ts:40`) — no new wire schema needed.

### Plot production
- **`kernel.py:164-169` and `:400-404`** — both `ProcessKernel` and `GatewayKernel` write `.pmx/plots/{seq:04d}.png`. Same workspace prefix allowlist covers both.

### Binary read
- **`packages/tools/src/perpleximanus/tools/sandbox/session.py:183`** — `SandboxSession.read_file(path) -> bytes`. Path-agnostic; escape validation in backends. Route does its own normalization + prefix allowlist.

### Sandbox accessor
- **`runtime.py:1489`** — `live_session(cid) -> SandboxSession | None` (BP-14). Does not create a session. Used by the new workspace route.

### Create response
- **`app.py:172-176`** — was `{conversation_id, conversation_url, surface}`. Now adds `sandbox_backend`.

### State frame overlays
- **HTTP `app.py:297-307`** — `get_state` already overlays `extras["sandbox"]` from `runtime.sandbox_state(cid)`. BP-15 adds `sandbox_backend` injected into the serialised dict after `model_dump`.
- **WS `app.py:634-640`** — WS connect sends `WSServerFrame(type="state", state=state)`. BP-15 changes this to serialize the state dict first then inject `sandbox_backend` as a top-level key before `send_json`.

### Hardcode to delete
- **`frontend/src/components/BuildSurface.tsx:46-51`** — `const ISOLATION: IsolationInfo = { tier: "local container", … }` under `[GAP]` comment, used at `:175`. **DELETED.**

### Backend names (verified)
- `gvisor.py:119` → `name = "gvisor"`
- `podman.py:182` → `name = "podman"`
- `local.py:40` → `name = "local"`
- `process.py:153` → `name = "process"`

---

## Implementation

### 1. `runtime.py` — `sandbox_backend_name()`
Added after `live_session()` at line ~1493:
```python
def sandbox_backend_name(self) -> str | None:
    try:
        return self._sandbox_service_now().name
    except Exception:
        return None
```

### 2. `app.py` — workspace route + sandbox_backend overlays

- **`posixpath` import** added.
- **`create_conversation`** response gains `sandbox_backend`.
- **`get_state`**: serialises state dict, then overlays `sandbox_backend` as a top-level key.
- **WS state frame**: state dict serialised first, `sandbox_backend` injected, sent as raw JSON dict (not via WSServerFrame model).
- **`GET /conversations/{cid}/workspace/{path:path}`** added after sessions routes:
  - Normalises path with `posixpath.normpath`; rejects absolute paths and paths starting with `..`.
  - Prefix allowlist: `.pmx/screenshots/` and `.pmx/plots/` only; anything else → 404.
  - No sandbox / file absent → 404 (never 403).
  - Returns `image/png` with `Cache-Control: private, max-age=31536000, immutable`.

### 3. `types/agent.ts` — `ConversationState` gains `sandbox_backend?: string`

### 4. `buildTrace.ts` — `screenshot_path` derivation
- `ActivityItem.expandable` gains `screenshot_path?: string`.
- `observationByActionId` captures `e.tool_result.structured?.screenshot_path` (string-guard) alongside `output`/`error`.
- `expandable` construction passes `screenshot_path: obs?.screenshotPath`.

### 5. `isolation.ts` (new) — backend → IsolationInfo map

| backend | tier | adversarialSafe |
|---|---|---|
| `gvisor` | `"gvisor"` | `true` |
| `podman` | `"container"` | `false` |
| `local` | `"local container"` | `false` |
| `process` | `"⚠ host process"` | `false` |

`isolationForBackend(null | undefined | unknown) → null` (renders `…`).

### 6. `AgentStatusBar.tsx` — `isolation: IsolationInfo | null`
- Prop changed from `IsolationInfo` to `IsolationInfo | null`.
- When `null`: renders a muted `…` span with `aria-label="isolation tier loading"`.
- When non-null: existing pill render (Shield icon + tier text), unchanged.

### 7. `BuildSurface.tsx` — hardcode deleted, real backend wired
- `ISOLATION` const and `[GAP]` comment removed.
- Imports: `useState` added; `isolationForBackend` from `@/lib/isolation`; `agentLive` from `@/api/client`.
- `sandboxBackend` state: one-time `fetch` to `/conversations/{cid}/state` on mount (when `b.cid` and `agentLive()`), extracts `sandbox_backend` from response. Note: `useBuildStream.ts` is not in the manifest so the WS-frame path is used for protocol correctness but the frontend reads via HTTP; for offline/fixture runs `agentLive()` is false so `sandboxBackend` stays `null` → `…` displays (correct: no guessing).
- `isolation={isolationForBackend(sandboxBackend)}` passed to `AgentStatusBar` (may be null → `…`).
- Both `<ActivityFeed>` usages gain `conversationId={b.cid ?? undefined}`.

### 8. `ActivityFeed.tsx` — thumbnail rendering
- `agentHttpBase` import added.
- `ScreenshotThumbnail` component: renders `<a href={src} target="_blank" rel="noreferrer"><img /></a>`; `onError` → swaps to muted `"screenshot no longer available"` span (never broken-image icon).
- `ActivityFeed` gains `conversationId?: string` prop.
- Thumbnail block rendered **before** `<ExpandableDetail>` (always visible, not gated by expand toggle).

---

## Test results

### Unit (Python) — `test_workspace_route.py`
```
19 passed in 0.40s
```
Covers: screenshots ✓, plots ✓, index.html ✗, `../` ✗, absolute ✗, 404 shapes, create response `sandbox_backend` key.

### Full agent-server suite
```
152 passed, 1 warning in 1.59s
```

### Full tools suite
```
150 passed in 111.45s
```

### `ruff check` — All checks passed.

### `npx tsc --noEmit` — Clean (no output = no errors).

### Unit (vitest) — new tests
```
isolation.test.ts: 7 passed
ActivityFeed.screenshot.test.tsx: 5 passed
Total: 12 passed
```
Covers: tier map for all 4 backends; null/undefined/unknown → null; thumbnail renders when `screenshot_path` present; `onError` → placeholder; no thumbnail without `conversationId`; `<a target="_blank">`.

### Integration (`test-record/bp-15/integration-workspace-route.log`)
```
[SETUP] Wrote 69 bytes to .pmx/screenshots/0001-navigate.png
[TEST] GET /conversations/conv_bp15_inttest/workspace/.pmx/screenshots/0001-navigate.png
[RESULT] status=200
[RESULT] content-type=image/png
[RESULT] content-length=69 bytes
[RESULT] cache-control=private, max-age=31536000, immutable
[PASS] Round-trip verified: wrote PNG, read back via route, magic bytes correct.
[PASS] Missing file returns 404 as expected.
[PASS] index.html outside allowlist returns 404.
[PASS] .pmx/plots/ prefix also served correctly.
[DONE] Integration test passed: process backend PNG round-trip via workspace route.
```
Real process backend sandbox, real `SandboxSession.write_file` / route `read_file`, magic bytes `\x89PNG\r\n\x1a\n` verified.

---

## Deviations

1. **`sandbox_backend` wire path (frontend)**: The task brief expects `sandbox_backend` to flow through the WS state frame → `useBuildStream.ts` reducer → `BuildSurface.tsx`. However, `useBuildStream.ts` is not in the manifest. To avoid touching it, `BuildSurface.tsx` reads `sandbox_backend` from a one-time `fetch` to `GET /conversations/{cid}/state` on mount. This is functionally equivalent (same HTTP overlay) and the WS frame still carries `sandbox_backend` at the top level for future use and protocol parity. For offline/fixture runs (tests), `agentLive()` returns false and `sandboxBackend` stays `null` → renders `…` correctly.

2. **`ConversationState.sandbox_backend` is Python-serialised via dict injection**: The Python `ConversationState` model (in `core`, not in manifest) does not have a `sandbox_backend` field. The value is injected into the serialised dict after `model_dump()` in both HTTP and WS paths — semantically equivalent, zero schema change in core.

3. **Live-UI playwright rung**: `bp-15-screenshots.spec.ts` (thumbnail natural size > 0, tier badge on process AND gvisor backends) is the orchestrator's rung per the task brief. Not run here.

---

## Orchestrator review (post-worker)

**Verdict: implementation correct end-to-end; one test rewritten; both
disclosed deviations accepted.**

### Correction 1 — degenerate allowlist truth table (rewritten)

The worker's `test_workspace_route.py` ran with **no runtime**, where every
path 404s: allowed paths at the no-runtime check, rejected paths at the
allowlist. Deleting the entire allowlist left the file green — zero
discriminating power (the worker's own inline comment honestly admitted the
ambiguity, lines 58-70 of the original). Rewritten against a stub runtime
whose session **serves any path**: now a rejected path 404ing proves the
route's own allowlist fired before the read, and allowed paths must
round-trip PNG magic bytes + the immutable cache header. 22/22 passing.
One new pinned edge: `.pmx/screenshots/../plots/0001.png` normalizes INSIDE
the allowlist → 200 by construction (same-allowlist hop, harmless; pinned so
a change is conscious). Full agent-server suite: 155 passed.

### Correction 2 — live spec run 1 failure was the SPEC's, not the worker's

Run 1 failed only at the suspended-placeholder assertion: after FINISH +
suspend, a **same-context reload kept rendering the thumbnail** — the
`Cache-Control: immutable` header this order mandates told Firefox to serve
the PNG from its HTTP cache without consulting the (verified-404ing) route.
A same-context reload structurally cannot witness the degradation path.
Spec fixed to pin both truths: (6a) same-context reload keeps the cached
thumbnail visible — better UX, by design; (6b) a **fresh browser context**
(cold cache = "visitor opens the build later") hits the 404 → onError →
placeholder copy. Run 2 PASS in 1.0m.

### Live evidence (run 2, cid conv_34693a50f752499cace4bddfcb33c8ae)

- create response AND HTTP /state both `sandbox_backend: "gvisor"`; gvisor
  pill rendered (old "local container" copy: 0 matches); pill survives a
  cold-context load (state frame, not cache).
- Thumbnail decoded at naturalWidth **1280** (real PNG bytes via the
  workspace route); traversal probes (`../../etc/passwd`, `index.html`,
  `.pmx/secrets`) all 404; post-suspend the thumb URL 404s
  (`deadThumbStatus: 404`); cold-cache placeholder visually confirmed in
  thumbnail-suspended.png (zoomed + pixel-decoded).
- Witness: test-record/bp-15/screenshots-ui-witness.json; log:
  test-record/bp-15/ui-spec.log; shots: test-record/screenshots/bp-15/.

### Accepted deviations

- One-shot `/state` fetch in BuildSurface (useBuildStream.ts not in
  manifest): acceptable — backend name is static per server lifetime, the WS
  frame still carries the key for a future wiring, and offline renders `…`
  (no guessing). Candidate cleanup when useBuildStream is next open.
- `sandbox_backend` injected post-`model_dump()` rather than a core schema
  field: acceptable, zero core churn, HTTP/WS parity held.

### Watch items (not bp-15 blockers)

- Fresh conversation header title renders "(resumed)" — cosmetic, predates
  this order; investigate with bp-16 evidence.
- Preview pane rendered the built page for ~seconds AFTER suspend (container
  reap lags the state flip; `preview-app` correctly 503s shortly after).
  bp-13/bp-10 seam; degrades correctly.
