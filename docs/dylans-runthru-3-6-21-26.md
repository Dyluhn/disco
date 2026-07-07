> **SUPERSEDED / HISTORICAL (as of 2026-07-07).** June-era runthru #3 punch list, frozen mid-execution.
> Current status of record: `docs/disco-project-state.md` (master), `docs/disco-status-and-remaining.md` (features + remaining), `docs/disco-security-state.md` (security). This file is kept for history and may contain stale claims.

# Dylan runthru #3 — punch list (2026-06-21, laptop-over-Tailscale)

Running items surfaced during the 3rd walkthrough. Each ends with VISUAL PROOF in the
running app (per the standing rule). Append new items below as they're found.

## W3-1 — Live "thinking" stream during a build turn (no more black box)
**Problem (Dylan):** "there should be a visual there to know what's actually happening.
I've found it really weird that you only get updates from the model after it's completed
something." During a slow model turn (esp. local Qwen, 1–3.5 min/turn, sometimes 10 min+),
the build trace shows a static "thinking…" label with NO content until the whole turn lands
as one completed `action` event.

**Root cause:** the loop consumes the driver's token stream INTERNALLY and only emits the
finished action event to the UI. Today only two things stream live: watch-it-write (file
*contents*) and a coarse `deriveLiveSignal` status label. The model's reasoning tokens are
dropped on the floor. (Deep Research ALREADY streams its tokens — `ws.py:222` — so Build is
the odd one out.)

**Fix (wiring, not new infra):** forward the driver's `delta_text` reasoning deltas onto the
existing ephemeral bus that watch-it-write uses (`ws.py:162`) → drain over the WS → render a
live thought panel under the active step (mirror DR's token-stream UI + the existing
watch-it-write rendering). Three seams: engine emits deltas → WS drains them → UI renders.
One open decision: stream raw reasoning verbatim vs a lightly-cleaned version.
**Proof:** open a slow (Qwen) build and watch the model reason in real time, not a frozen
label.

**Same gap on Deep Research (live, conv_05a87b20):** the DR `iterate` phase (A4 grounding)
fires a continuous burst of LLM verification calls — 15+ OpenRouter POSTs/min, confirmed in
the log — but emits NO granular progress event, so the UI sits on a static "iterate" and
LOOKS hung for minutes while it's grinding. DR streams tokens during SYNTHESIS but not during
the iterate/grounding burst. Same fix shape: emit a progress signal (calls done / sections
re-checked) during iterate so the surface shows life.

## W3-2 — Silent hang: sandbox errors erase native exception types → crash + no backstop
**Symptom (live, conv_ee747708, local Qwen macOS-clone build):** build sat at status
RUNNING for 15+ min with the model slot IDLE and NO new events / no error in the UI. The
agent-server log showed the truth at 10:45:03 UTC:
`asyncio: Task exception was never retrieved … _run_with_persistence (runtime.py:1421)
exception=SandboxError("read_file 'style.css': cat: /workspace/style.css: No such file or directory")`
The loop TASK died; status was never moved off RUNNING → forever-"Building…".

**ONE deeper root cause (not two surgical bugs): the sandbox boundary ERASES native
exception semantics, silently defeating ~8 existing error handlers.**

`SandboxError(Exception)` (`sandbox/base.py:39`) subclasses `Exception` DIRECTLY — it is NOT
in the `OSError`/`FileNotFoundError`/`TimeoutError` hierarchy. But the loop is full of
handlers that were CAREFULLY written to tolerate benign file conditions (missing / renamed /
unreadable / dead-sandbox → "skip, not fatal") by catching the NATIVE types:
`file_state.py:144` `except (FileNotFoundError, TimeoutError, PermissionError, OSError)` (with
the literal comment "cannot read → treat conservatively as not stale" — the author
ANTICIPATED this), plus `recitation.py:270`, `observe.py:117`, `view_render.py:89/99`,
`bootstrap.py:69`, `plan_conditions.py:215`, `runtime.py:417` — **8 sites. ZERO catch
`SandboxError`.** Against a LOCAL filesystem they all work; against the SANDBOX backend they
are ALL DEAD CODE, because the sandbox hands them a type none of them catch. So ANY benign
file condition during the harness's own bookkeeping reads — a file the agent renamed
(`shell mv`), deleted, transiently unreadable, a dead sandbox — escapes every handler and
crashes the loop. The `style.css` rename was just the spark; the same crash is latent in
recitation, observe, view_render, etc.

The "silent hang" is the SECOND FACE of the same root: because these escapes were never
supposed to happen (the handlers *look* correct), nobody built a backstop. `kick()`
(`runtime.py:1394`) launches the loop with `create_task(...)` and NO `add_done_callback` /
try-except converting an escaped exception into a terminal status — so the crash leaves
status stuck RUNNING forever. (Docstring claims failures "never crash the task" — true only
for the snapshot/rehydrate hooks, not an exception escaping `loop.run()`.) The
stuck/no-progress breakers can't help — they run INSIDE the loop that just died.

**Root fix (one boundary + one backstop, NOT 8 patches):**
1. **Type the sandbox error model.** `read_file`/exec map the failure onto the NATIVE
   exception: missing file → `class SandboxFileNotFoundError(SandboxError, FileNotFoundError)`,
   timeout → also-a-`TimeoutError`, permission → `PermissionError`, dead sandbox → its own
   typed error. Then ALL 8 existing handlers immediately work against the sandbox — fixed at
   the boundary, everywhere at once, instead of bolting `SandboxError` onto one `except`
   (whack-a-mole that leaves the other 7 broken).
2. **Task-supervisor backstop (defense in depth).** `kick()` attaches an `add_done_callback`
   that, on any unhandled exception, emits `StatusEvent(ERROR)` + a system-reminder, so the
   loop ALWAYS resolves to a terminal status and the next unanticipated escape is a visible
   ERROR, never a silent hang.
**Proof:** repro (build that `shell mv`s a written file) ends ERROR with a clear message, not
a silent hang; unit tests: a sandbox missing-file read raises a `FileNotFoundError`-typed
error AND each of the 8 handlers now catches it; an exception in `loop.run()` yields terminal
ERROR.
