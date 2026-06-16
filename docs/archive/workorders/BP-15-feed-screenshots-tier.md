# BP-15 — Screenshots in the feed + isolation tier over the wire

**Read `README.md` first. Requires BP-04 (screenshots exist).**

## Why

Two cockpit-truth items. (1) BP-04's browser observations carry
`structured.screenshot_path` — the user should SEE what the agent saw, inline in the
feed (Manus renders screenshots in the task stream). (2) `BuildSurface.tsx` hardcodes the
isolation tier (`const ISOLATION: IsolationInfo = { tier: "local container", … }` with
the comment `[GAP] the server should send the live tier over the wire`) — a false
affordance the project's own rules forbid; the wire must carry the real backend.

## Implementation

### 1. Workspace-file route (`agent_server/app.py`)

```
GET /conversations/{cid}/workspace/{path:path}
```
- ONLY serves paths under `.pmx/screenshots/` and `.pmx/plots/` (BP-08's images ride the
  same route). Anything else → 404. Reject `..`, absolute paths, symlink escapes
  (resolve via the sandbox: read through `SandboxSession.read_file` which already
  validates escapes — do not re-implement; just allowlist the two prefixes on top).
- Content-Type `image/png`; `Cache-Control: private, max-age=31536000, immutable`
  (screenshot files are write-once by construction — seq-numbered names).

### 2. Feed rendering (`frontend/src/components/build/ActivityFeed.tsx`)

- Where observation items render: if the item's observation carries
  `structured.screenshot_path` (extend the activity-item derivation + the
  `types/agent.ts` ObservationEvent typing — `structured` is already on the wire inside
  `tool_result`; verify and type it), render a thumbnail `<img>` (max-h ~200px,
  rounded, border) below the observation text, `src` = the workspace route. Click →
  open full-size in a new tab (plain `<a target="_blank">` — no lightbox component).
- Broken image (sandbox suspended → file gone): `onError` swaps to a muted placeholder
  `screenshot no longer available` — never a broken-image icon.

### 3. Isolation tier over the wire

- Backend: the conversation create response AND the WS `state` frame gain
  `sandbox_backend: "process" | "local" | "podman" | "gvisor"` (runtime knows —
  `_sandbox_service_now()` / the service's `name` attribute, e.g. gvisor.py `name =
  "gvisor"`).
- Frontend: delete the hardcoded `ISOLATION` const; map backend → tier copy in one
  place (`frontend/src/lib/isolation.ts`):

| backend | tier label | adversarialSafe |
|---|---|---|
| `gvisor` | "gVisor (user-space kernel) — strong isolation" | true |
| `podman` | "Rootless container — container-grade isolation" | false |
| `local` | "Container-grade isolation (shared host kernel)" | false |
| `process` | "⚠ No isolation — host process (dev mode)" | false |

- `process` renders in the warning style (red/amber) — per the no-false-affordances
  rule, dev mode must look like what it is.
- Until the first state frame arrives, render a muted `…` — never a guessed tier.

## Acceptance

1. **Unit (python)**: route allowlist (screenshots ✓, plots ✓, `index.html` ✗,
   `../` ✗). **Unit (frontend)**: thumbnail renders when `screenshot_path` present;
   onError placeholder; tier mapping table; no-state-yet renders `…`.
2. **Integration (process backend)**: real browser observation (BP-04 integration
   fixture) → GET the route → valid PNG bytes round-trip.
3. **UI surface (live, Firefox, real driver)** — `bp-15-screenshots.spec.ts`: live build
   where the agent browser-verifies its page (BP-05 makes this routine); assert
   (a) a thumbnail `<img>` is visible in the feed and its natural size > 0 (actually
   loaded, not just present); (b) the isolation badge shows `process`-tier warning copy
   when run on the process backend, and — run the spec once against the gvisor-backed
   server too — the gVisor label on that backend (the hardcode is provably gone).
   Screenshots: `feed-thumbnail.png`, `tier-process.png`, `tier-gvisor.png` →
   `test-record/screenshots/bp-15/`, sent to user.

## Prohibitions

- The workspace route never serves user code/files (download-zip already exists for
  that); the two-prefix allowlist is the whole surface.
- No tier guessing client-side; the wire value or `…`.
- Do not build a gallery/lightbox; thumbnail + new-tab only.
