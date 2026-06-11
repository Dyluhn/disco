# BP-01 — Persistent named shell sessions (tmux-backed)

**Read `README.md` in this directory first. Its rules apply.**

## Why (context — already diagnosed, do not re-litigate)

Every shell call today is fire-and-forget (`ShellTool` → `SandboxInstance.exec_shell`,
`packages/tools/src/perpleximanus/tools/builtin/system.py`). Long-running processes only
exist via `run_server`'s `setsid nohup` hack (`builtin/preview.py`), which the agent can
never inspect. This is the root of the "model kills its own port" spiral. The proven fix
is Manus's process-as-durable-resource model: named sessions the agent can exec into,
view, write stdin to, wait on, and kill — kill being **sanctioned**, not prohibited.

## The decided mechanism: tmux (production provenance: OpenHands, 76k★, ships exactly this)

tmux gives us persistent PTY sessions over a bare `exec_shell` interface — no new Python
dependencies, works identically on every backend (the tmux *server* persists inside the
container/host between `exec_shell` calls). Command-completion detection uses a **PS1
marker** (OpenHands' production technique): the session's prompt is set to a sentinel
containing the exit code, so "is the foreground command done" and "what did it return"
are parsed deterministically from `capture-pane` output. Do not invent a different
completion-detection scheme.

## Implementation

### 1. Image + backends get tmux

- `deploy/sandbox/Dockerfile`: add `tmux` to the existing `apt-get install` list (the one
  with `python3 … ripgrep procps less nano`). Rebuild + push the image to VM-201
  (`docker -H ssh://… build` or the existing deploy script — find it under `deploy/`).
- Process backend runs on the host: acceptance step 0 verifies `tmux -V` ≥ 3.x on the
  host and FAILS the order with a STOP-report if absent (do not silently degrade).

### 2. New module: `packages/tools/src/perpleximanus/tools/sandbox/shell_sessions.py`

A `ShellSessionManager` that drives tmux **through** `SandboxInstance.exec_shell` (so it
works on all backends unchanged). One manager per `SandboxSession`; attach it there
(`session.py`): `SandboxSession.sessions: ShellSessionManager` created lazily, surviving
`_recreate()` (on recreate, the manager's known-sessions set resets — sessions died with
the instance; that is correct and must be reported by `shell_view` as "session lost:
sandbox was recreated").

Constants (module-level, exactly these values):

```python
_PREFIX = "pmx"                 # tmux session name = f"{_PREFIX}-{name}"
_PS1_MARKER = "__PMX_PS1__"     # prompt sentinel
_PS1 = f"{_PS1_MARKER}$?__$ "   # bash renders $? at prompt time → exit code in marker
# AMENDED 2026-06-09 (was `__\$ \s*$` with a literal space — defective: capture-pane
# without -J strips trailing whitespace per line, so the idle prompt is captured as
# `__PMX_PS1__0__$` and a literal-space regex can never match. `\$\s*$` tolerates both
# the captured and raw forms; a prompt line with an echoed command after it still
# correctly fails to match because of the $ anchor.)
_MARKER_RE = re.compile(r"__PMX_PS1__(\d+)__\$\s*$")
_VIEW_TAIL_CHARS = 10_000       # Manus-equivalent truncation for shell_view
_EXEC_RETURN_CHARS = 6_000      # tail returned inline by shell_exec
_POLL_S = 0.5
_EXEC_WAIT_S = 15.0             # shell_exec waits this long before returning "still running"
_TMUX_TIMEOUT_S = 10            # timeout for the tmux control commands themselves
```

Public API (all async; every tmux invocation is one `exec_shell` with
`timeout_s=_TMUX_TIMEOUT_S`; always `shlex.quote` user input):

```python
class ShellSessionManager:
    def __init__(self, instance_getter: Callable[[], Awaitable[SandboxInstance]],
                 namespace: str = "") -> None: ...
        # namespace: "" in containers (one container per conversation);
        # f"{conversation_id[:8]}-" on the process backend (shared host tmux server).

    async def ensure(self, name: str, exec_dir: str | None = None) -> None:
        # tmux new-session -d -s <full> -x 250 -y 50 [-c <exec_dir>]   (idempotent:
        # run `tmux has-session -t <full> 2>/dev/null` first)
        # then ONCE per new session, configure the marker prompt + disable wrapping noise:
        #   tmux send-keys -t <full> -l "export PS1='__PMX_PS1__$?__$ ' PS2='' PROMPT_COMMAND=''; history -c; clear"
        #   tmux send-keys -t <full> Enter
        # then wait until the marker appears (poll capture-pane, ≤5s) so the session is
        # provably ready before exec returns.

    async def is_busy(self, name: str) -> bool:
        # capture-pane tail does NOT end with _MARKER_RE  → busy.
        # (Do NOT use pane_current_command — interactive shells-in-shells defeat it;
        # the marker is the single source of truth.)

    async def exec(self, name: str, command: str, exec_dir: str | None) -> ExecOutcome:
        # ensure(); if is_busy → raise SessionBusy (message below, verbatim).
        # snapshot = capture-pane BEFORE sending (to delimit this command's output)
        # tmux send-keys -t <full> -l <command>; tmux send-keys -t <full> Enter
        # poll every _POLL_S up to _EXEC_WAIT_S for a NEW marker after the snapshot.
        # done   → ExecOutcome(running=False, exit_code=int(marker), output=delta-tail)
        # not yet→ ExecOutcome(running=True, exit_code=None, output=delta-tail,
        #                      note="still running after 15s — use shell_view / shell_wait")

    async def view(self, name: str, tail_chars: int = _VIEW_TAIL_CHARS) -> SessionView:
        # tmux capture-pane -t <full> -p -S -   → full scrollback; return tail + busy flag.

    async def wait(self, name: str, seconds: int) -> SessionView:
        # poll until idle or `seconds` elapsed (cap 300); return final view().

    async def write(self, name: str, text: str, press_enter: bool) -> None:
        # tmux send-keys -t <full> -l <text>  [+ Enter]. Raise if session absent.

    async def kill_foreground(self, name: str) -> str:
        # send C-c (tmux send-keys -t <full> C-c); poll ≤3s for idle.
        # still busy → tmux kill-session, then ensure() a FRESH session under the same
        # name (the id stays usable). Return a one-line description of what happened.

    async def list(self) -> list[SessionInfo]:
        # tmux list-sessions -F '#{session_name}' filtered by prefix+namespace;
        # SessionInfo(name, busy, last_lines=last 3 lines of view).
```

`SessionBusy` message, **verbatim** (mirrors Manus): `"Previous command not finished in
session '<name>'. Wait for it (shell_wait), interact with it (shell_write_to_process),
kill it (shell_kill_process), or use a different session name."`

Output delimiting rule (no wiggle room): the delta = full capture-pane text minus the
pre-send snapshot prefix; strip the echoed command line (first line of the delta) and the
trailing marker line. If tmux echoes differently than expected, fix the stripping — never
return marker garbage to the model.

### 3. Five new tools: `packages/tools/src/perpleximanus/tools/builtin/shell_sessions.py`

Follow the exact `ToolDef`/`Tool` pattern in `builtin/system.py` (`ShellTool` is the
template). The manager is reached via the sandbox: add `sessions` to `ToolContext`
plumbing — concretely, `DefaultToolExecutor._build_context` (`tools/executor.py`) holds a
`SandboxInstance`; the cleanest seam is: executor is constructed with the `SandboxSession`
already (check `runtime.py` `_compose_build_loop` — it passes `session` as `sandbox=`),
so expose `ShellSessionManager` from `SandboxSession` and pass it through `ToolContext`
as a new optional field `sessions: ShellSessionManager | None = None`. Update
`ToolContext` in `anatomy.py` accordingly.

| tool name | args | read_only | base_risk | behavior |
|---|---|---|---|---|
| `shell_exec` | `session: str` (default `"main"`), `exec_dir: str = ""`, `command: str` | False | MEDIUM | `manager.exec(...)`; format: header line `session '<name>' — exit <code>` or `… — still running`, then output |
| `shell_view` | `session: str` | True | LOW | `manager.view(...)`; header `session '<name>' — running|idle`, then tail |
| `shell_wait` | `session: str`, `seconds: int = 30` | True | LOW | `manager.wait(...)` |
| `shell_write_to_process` | `session: str`, `input: str`, `press_enter: bool = True` | False | MEDIUM | `manager.write(...)` |
| `shell_kill_process` | `session: str` | False | MEDIUM | `manager.kill_foreground(...)` — **sanctioned; the analyzer must NOT deny it** |

All five: `needs=frozenset({Capability.SHELL})`, `runs_in="sandbox"`. Descriptions must
teach the model the contract (one foreground process per session; view anytime; kill is
fine). Register all five in `build_default_registry()` (`builtin/__init__.py`) and add
the five names to the agent scope allowlist (grep `AGENT_TOOLS` in `tools/.../registry.py`).

Keep the existing one-shot `shell` tool — it remains correct for quick commands.

### 4. Cleanup on destroy

`ProcessSandboxInstance.destroy()` (`sandbox/process.py`): kill `pmx-{namespace}*`
sessions (`tmux kill-session` per match) before `rmtree`. Container backends need nothing
(container death kills the tmux server) — state this in a comment.

## Acceptance (all must pass; paste outputs)

0. `tmux -V` on host; image rebuilt with tmux; `docker -H … run --rm pmx-sandbox:base tmux -V`.
1. **Unit** (`packages/tools/tests/test_shell_sessions.py`, fake sandbox echoing canned
   capture-pane output): marker parsing (exit 0 and exit 7), busy detection, delta
   stripping, SessionBusy verbatim message, kill-then-recreate path.
2. **Integration, process backend** (`PMX_SANDBOX=process`, real tmux, pytest mark it
   `integration`): (a) `exec("main", "x=42; echo started")` then a SECOND call
   `exec("main", "echo $x")` returns `42` — state persists; (b) `exec("srv", "python3 -m
   http.server 8123")` returns running=True; `view("srv")` shows the serving banner;
   `curl 127.0.0.1:8123` succeeds from the test; `kill_foreground("srv")` frees it;
   (c) `exec("main", "read -p 'name? ' n && echo hi-$n")` → busy → `write("main",
   "dylan")` → view shows `hi-dylan`; (d) busy session + second exec → SessionBusy.
3. **Integration, gvisor backend on VM-201** (`PMX_SANDBOX=gvisor`): repeat 2(a) and 2(b)
   inside the container. If VM-201 is unreachable, STOP and report — do not skip silently.
4. **UI surface (live, Firefox)**: new spec `frontend/e2e/shell-sessions.spec.ts` run
   against the live agent-server (`VITE_AGENT_BASE=http://localhost:8000`): start a Build
   conversation whose prompt is exactly *"In session 'demo', run: for i in 1 2 3; do echo
   tick-$i; sleep 1; done — then view the session and finish."* Approve the plan. Assert
   the Activity feed shows a `shell_exec` action and a `shell_view` observation containing
   `tick-3`. Screenshot the feed → `test-record/screenshots/bp-01/feed-shell-session.png`.
   Send the screenshot to the user.

## Prohibitions

- No pexpect, no asyncio-pty, no custom daemon — tmux only.
- No completion detection via `pane_current_command`, sleeps-and-hope, or output silence.
- Do not modify `preview.py`, prompts, or analyzers in this order (BP-02/BP-03 own those).
- The `integration` tests must run against real tmux — no mocking tmux in them.

Fill in the Definition-of-done template from README.md in your report.
