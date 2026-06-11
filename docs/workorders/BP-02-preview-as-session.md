# BP-02 — Preview becomes a visible session; kill the dual-owner race

**Read `README.md` first. Requires BP-01 merged.**

## Why (diagnosed; receipts)

Port 8000 currently has TWO owners racing each other:

1. Container PID-1 keepalive: `while true; do python3 -m http.server 8000 >/dev/null 2>&1;
   sleep 1; done` — `_keepalive_command()` in `sandbox/gvisor.py` (~line 50, reused by
   `local.py`).
2. `run_server`'s script does `pkill -f 'http.server'` then `setsid nohup` its own server
   (`builtin/preview.py`, `RunServerTool`, ~line 177).

When the agent's app crashes, the keepalive silently recaptures :8000 within ≤1s, and
`preview_status` — whose probe checks **bind only** (`_LISTEN_PROBE`, preview.py ~line 31)
— reports a FALSE "serving". The agent then reaches for `pkill`, which the analyzer
hard-denies. This order removes the second owner, makes the preview an agent-visible
session, and makes "serving" mean *who* is serving.

## The decided design

- Containers' main process is **always** plain `sleep infinity`. Nothing hidden ever
  binds :8000.
- The static auto-serve becomes tmux session **`preview`** (via BP-01's
  `ShellSessionManager`), running `python3 -m http.server 8000 -d <workspace>`. The agent
  can `shell_view('preview')`, and `shell_kill_process('preview')` when it wants :8000
  for its own dev server — sanctioned.
- `preview_status`, `restart_preview`, `run_server` are **deleted**. One new read-only
  tool `server_status` replaces them, reporting sessions + true port ownership.
- Port ownership is read from `/proc` (what `lsof` does) — no new packages.

## Implementation

### 1. Backends: delete the hidden server

- `gvisor.py` `_keepalive_command(...)`: delete the `previewable` branch entirely; the
  function returns `["sleep", "infinity"]` unconditionally (then inline/remove the
  parameter and fix callers). Same for `local.py`'s reuse. `podman.py` already complies.
- Grep for `previewable` and remove the now-dead plumbing.
- Port publishing at container create is UNCHANGED in this order (still PREVIEW_PORT
  binding — BP-10 widens it).

### 2. `SandboxSession.ensure_preview()` (`sandbox/session.py`)

```python
async def ensure_preview(self) -> bool:
    """Start (idempotently) the static preview as visible session 'preview'.
    Returns False without side effects if :8000 is already bound (someone — maybe
    the agent's own dev server — owns it; that is fine and not ours to fight)."""
```
Uses BP-01's manager: if `port_owner(8000)` is None → `sessions.exec("preview",
"python3 -m http.server 8000 -d <workspace>")` (returns running=True; that's success).
Workspace path: the container workspace (`/workspace`) or the process backend's temp dir
— take it from the instance, do not hardcode.

### 3. Port-owner probe: `sandbox/port_owner.py`

Embedded python script (run via `exec_shell`, pattern: `_CODEACT_RUNNER_SRC` in
`builtin/system.py`) — pure stdlib, deterministic:

- Parse `/proc/net/tcp` (+`tcp6`) for `LISTEN` (state `0A`) rows whose local port matches;
  collect socket inodes.
- Scan `/proc/[pid]/fd/*` symlinks for `socket:[inode]`; map to pid.
- Print JSON: `{"port": 8000, "pid": 1234, "cmdline": "python3 -m http.server 8000 …"}`
  or `{"port": 8000, "pid": null}`.

Expose as `async def port_owner(instance, port) -> PortOwner | None`.

### 4. New tool `server_status` (`builtin/server.py`)

read_only=True, LOW risk, `needs={Capability.SHELL}`. Output (exact shape — the model
reads this; ambiguity here recreates the old confusion):

```
SERVER STATUS
sessions:
  - preview: idle|running — last: <last output line>
  - dev: running — last: <last output line>
ports:
  - 8000: OWNED by pid 1234 (python3 -m http.server 8000 -d /workspace) [session: preview]
  - 8000: FREE
```
Session attribution: a port maps to a session when the owning pid (or an ancestor, via
`/proc/<pid>/stat` ppid walk, max 5 hops) lives in that tmux session — implement the
ancestor walk in the same embedded script. Probe ports: 8000 plus (after BP-10) the
published set; until then, just 8000.

### 5. Delete the preview tool trio

- Delete `builtin/preview.py` (all three tools + `_LISTEN_PROBE`).
- `builtin/__init__.py`: remove the three from `build_default_registry()`, add
  `ServerStatusTool`. Update `AGENT_TOOLS` (remove 3 names, add `server_status`).
- Grep the whole repo for `preview_status|restart_preview|run_server` — every reference
  (prompts text is BP-03's job, but **code** references die now: runtime, tests, fixtures).

### 6. Rewire the agent-server preview endpoints (`agent_server/app.py`, `runtime.py`)

- `GET /conversations/{cid}/preview`: response becomes
  `{"available": bool, "reason": str|None, "owner": {"pid": int, "cmdline": str, "session": str|null} | null}`.
  `available` = :8000 bound. `owner` = the new probe. The frontend type
  (`frontend/src/hooks/useBuildPreview.ts` + `types/`) gains the `owner` field, and
  PreviewPane shows `Serving: <cmdline>` as a small caption under the iframe — the user
  (and tests) can now SEE who is serving. No more false "serving": when `owner.session ==
  "preview"` and the agent built an app that should be on :8000, that's visible truth.
- `POST /conversations/{cid}/preview/restart` → calls `SandboxSession.ensure_preview()`
  (replaces the old pkill-based `runtime.restart_preview`; keep route + button).
- `runtime.preview_upstream` unchanged.

## Acceptance

1. **Unit**: port-owner script parsing against a canned `/proc` fixture tree (tmpdir);
   `server_status` formatting; `ensure_preview` no-op when port owned.
2. **Integration (process backend, real tmux)**: (a) fresh build sandbox →
   `ensure_preview()` → `server_status` shows `preview … OWNED … [session: preview]`;
   (b) `shell_kill_process('preview')` → `server_status` shows `8000: FREE` — **no
   resurrection within 5s** (assert with a sleep+recheck; this is the race's tombstone);
   (c) agent-style flow: kill preview, `shell_exec("dev", "python3 -m http.server 8000")`,
   `server_status` attributes :8000 to session `dev`.
3. **Integration (gvisor, VM-201)**: container main process is `sleep infinity` (`docker
   inspect` Cmd/Entrypoint assertion); repeat 2(a)–(b) in-container.
4. **UI surface (live, Firefox)** — `frontend/e2e/preview-truth.spec.ts`: live build that
   writes an `index.html`; Preview tab iframe renders it AND the caption shows
   `python3 -m http.server` as owner. Then (via a second prompt) have the agent kill the
   preview session; UI preview reports unavailable (no false serving), Restart-preview
   button brings it back, caption reappears. Screenshots: `preview-owner.png`,
   `preview-down.png`, `preview-restarted.png` → `test-record/screenshots/bp-02/`, sent
   to user.

## Prohibitions

- No `pkill`/`fuser`/`lsof`/`ss` shell-outs for ownership — the `/proc` script only
  (those binaries aren't in the image; adding packages for this is forbidden).
- No keepalive loops, no auto-restart of a crashed preview behind the agent's back. A
  crashed/killed preview stays down until the agent or the user's Restart button acts.
- Do not edit prompt text or analyzer rules here (BP-03).
