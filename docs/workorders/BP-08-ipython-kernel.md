# BP-08 — Persistent IPython kernel for code_exec (delete the pickle runner)

**Read `README.md` first. Requires BP-01 (kernel gateway runs in a session) and BP-10
(container kernel port published).**

## Why (measured, not theoretical)

`CodeExecTool` (`builtin/system.py`) runs every cell as a fresh `python3` that loads the
whole pickled namespace, execs the cell, then **probe-serializes every variable**
(`_CODEACT_RUNNER_SRC` — the `for _k, _v in list(_ns.items()): _ser.dumps(_v)` loop) and
rewrites `_codeact_state.pkl`. Measured consequence: 41KB→346KB state over 43 calls, each
call creeping toward the 295s ceiling — the v2 build "pseudo-hang". Unpicklables
(sockets, file handles, threads) are silently dropped between cells. A timeout kills the
process mid-write. `dill` isn't even installed (falls back to stdlib pickle).

The production fix — what Jupyter, E2B, AutoGen, and the CodeAct paper all ship — is a
**persistent in-memory kernel**: state lives in the process; nothing is serialized;
interrupt is a signal, not a kill.

## The decided design

Two transports behind ONE interface, chosen by backend:

- **Process backend** (host dev): `jupyter_client.AsyncKernelManager` spawned directly,
  `cwd=<workspace>`. (The process backend already executes on the host; a host kernel
  subprocess is the same trust boundary.)
- **Container backends** (gvisor/podman/local): `ipykernel` + `jupyter_kernel_gateway`
  inside the image; gateway started lazily in tmux session `__kernel`:
  `jupyter kernelgateway --KernelGatewayApp.api=kernel_gateway.jupyter_websocket
  --ip 0.0.0.0 --port 8899` ; reached from the agent-server over the BP-10-published
  host mapping of 8899 via `websockets`/HTTP (create kernel: `POST /api/kernels`; execute
  over the kernel's `/api/kernels/{id}/channels` websocket, standard Jupyter wire
  protocol 5.x). Port 8899 is **internal plumbing** — it must never appear in
  `expose_port` results shown to users (BP-10 defines the split).

New deps (`packages/tools/pyproject.toml`): `jupyter_client`, plus `websockets` if not
already transitively present. Image (`deploy/sandbox/Dockerfile`):
`pip3 install --break-system-packages ipykernel jupyter_kernel_gateway`.

## Implementation

### 1. `packages/tools/src/disco/tools/sandbox/kernel.py`

```python
class KernelSession:
    """One persistent IPython kernel per conversation. State lives in kernel RAM."""
    async def start(self) -> None: ...
    async def execute(self, code: str, *, timeout_s: int) -> KernelResult: ...
        # stream iopub: collect stream(stdout/stderr), execute_result/display_data,
        # error(traceback). display_data image/png → save to
        # <workspace>/.pmx/plots/{seq:04d}.png, put the path in KernelResult.images.
    async def interrupt(self) -> None: ...
    async def restart(self) -> None: ...
    async def shutdown(self) -> None: ...

@dataclass
class KernelResult:
    ok: bool
    stdout: str; stderr: str
    result_repr: str | None
    error_traceback: str | None      # ANSI-stripped
    images: list[str]                # workspace-relative paths
    timed_out: bool = False
    restarted: bool = False
```

**Timeout protocol (exact — this fixes the timeout deadlock):** on `timeout_s` expiry →
`interrupt()` → wait ≤5s for the kernel to come back idle → if it does, return
`KernelResult(ok=False, timed_out=True, error_traceback="KeyboardInterrupt …",
stdout=<partial collected output>)` — **namespace intact**; if it does not →
`restart()` → return `timed_out=True, restarted=True` with a message that state was lost
and files survive (file-rehydrate is the model's move, per prompt).

Attach to `SandboxSession` (like the ShellSessionManager): lazy `kernel()` accessor;
on `_recreate()` the kernel is gone — next `execute` starts fresh and the result carries
`restarted=True` with the note.

### 2. Rewire `CodeExecTool` (`builtin/system.py`)

- `language=python` → `ctx.kernel.execute(code, timeout_s=min(ctx.timeout_s-5, 120))`.
  Render: stdout, then stderr, then `→ {result_repr}`, then traceback, then
  `plot saved: .pmx/plots/0003.png` lines. `structured` carries the KernelResult fields.
- DELETE: `_CODEACT_RUNNER_SRC`, the dill/pickle fallback comments, `_codeact_state.pkl`
  handling, the cell-file write/exec path. `language=node` path unchanged.
- `ToolContext`: add `kernel: KernelSession | None` (same plumbing seam as BP-01's
  `sessions`).
- Prompt (`prompts.py`, the code_exec guidance if any mentions state limits): update to
  "variables, imports, sockets, and open files persist across code_exec calls; a timeout
  interrupts the cell but keeps your state".

### 3. cgroup/memory cap

Container backends: the container's existing `mem_limit` covers the kernel — state this
in a comment. Process backend: spawn the kernel under the same memcap discipline the
host uses for commands if a wrapper exists; otherwise pass
`resource.setrlimit(RLIMIT_AS, 4GiB)` via a kernel startup env (`JUPYTER_…` won't do it —
use a sitecustomize-style `--KernelManager.kernel_cmd` wrapper ONLY if simple; if not
simple, report the gap honestly instead of hacking it).

## Acceptance

1. **Unit**: KernelResult rendering; timeout-protocol state machine with a fake kernel.
2. **Integration (process backend, REAL kernel)** —
   `packages/tools/tests/test_kernel_session.py::integration`:
   (a) **state**: cell1 `x = 41`; cell2 `x + 1` → `42`;
   (b) **fidelity** (the pickle-killer): cell1 opens a real `socket.socket` bound to a
   port and a `threading.Thread` appending to a list; cell2 uses BOTH successfully;
   (c) **interrupt**: cell `while True: pass` with timeout_s=3 → returns timed_out, NOT
   restarted; next cell still sees prior variables;
   (d) **flat latency** (the regression test for the 41KB→346KB bug): 60 cells, each
   `data_{i} = list(range(200_000))`; assert median(cell time of cells 50–60) ≤ 2× median
   (cells 2–12). Print the timing table into the report;
   (e) **plots**: matplotlib cell → PNG exists under `.pmx/plots/` (add matplotlib to the
   image, and to the host dev venv extras, for this test — if you choose not to add it,
   the test must use `IPython.display.Image` raw-bytes instead; pick one and say which).
3. **Integration (gvisor, VM-201)**: 2(a) and 2(c) over the kernel-gateway transport.
4. **Behavioral (live driver)**: a build prompt requiring cross-cell state (e.g. "load
   this CSV in one step, analyze it in later steps"); event log shows ≥3 code_exec calls
   with no state-loss complaints. Save → `test-record/bp-08/`.
5. **UI surface (live, Firefox)** — `bp-08-kernel.spec.ts`: the step-4 run through the
   UI; Terminal/feed shows code_exec outputs across steps; screenshot →
   `test-record/screenshots/bp-08/kernel-cells.png`, sent to user.

## Prohibitions

- No serialization of the namespace anywhere — not as backup, not "just for resume".
  Restart semantics are: RAM state lost, files survive, result says so.
- No nbclient/papermill/jupyter-server detours — `jupyter_client` direct + kernel
  gateway, as specified.
- Do not keep the pickle runner behind a flag. It is deleted.
