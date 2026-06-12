# BP-03 — Environment-contract prompt rewrite + retire the pkill hard-deny

**Read `README.md` first. Requires BP-01 + BP-02 merged.**

## Why

The current prompt manages the agent's confusion with prohibitions, and the analyzer
backs them with hard-denies. Both exist because the environment was hidden and racy.
After BP-01/BP-02 the environment is owned and observable, so the prompt must describe a
**contract**, and the denies that guard the dead race must go (a hard-deny on `pkill …
http.server` would now block the agent from legitimately managing its own session).

## Implementation

### 1. Replace the DEPLOY/PREVIEW prompt section

File: `packages/core/src/disco/core/llm/prompts.py`, inside
`_EXECUTION_DRIVER_PROMPT`. Locate the block starting with the exact string
`"DEPLOY / PREVIEW — read carefully` and ending before the next section. Replace it with
**exactly** this text (same string-concatenation style as the surrounding code):

```
"YOUR ENVIRONMENT — processes, ports, serving (read carefully):\n"
"  • You own a set of persistent shell SESSIONS. `shell_exec(session, command)` runs a "
"command in a named session; the process KEEPS RUNNING between your steps. "
"`shell_view(session)` shows its live output anytime. `shell_write_to_process` sends "
"input (stdin) to it. `shell_kill_process(session)` stops it — killing your own "
"processes is a normal, expected action. One foreground command per session; use "
"another session name for parallel work.\n"
"  • Check reality with `server_status`: it lists your sessions and WHO owns each "
"port (pid + command + session). Never guess whether a server is up — look.\n"
"  • Static sites: the workspace is auto-served on port 8000 by the session named "
"'preview' (a simple static file server). Write an index.html and it is live. The "
"preview the user sees proxies to port 8000.\n"
"  • Dev servers (Vite/Next/etc.): port 8000 is the user-visible port. First "
"`shell_kill_process('preview')` to free it, then start yours in its own session, "
"bound to 0.0.0.0:8000, e.g. `shell_exec('dev', 'npm run dev -- --host 0.0.0.0 "
"--port 8000')`. Then `shell_view('dev')` to confirm it actually started (read the "
"real output — startup errors appear there, not in your imagination).\n"
"  • If a server misbehaves: `shell_view` its session FIRST, read the error, fix the "
"cause, restart it. Do not fight processes blind.\n\n"
```

Also locate any other prompt text mentioning `run_server`, `preview_status`,
`restart_preview`, or "NEVER kill" semantics (grep `prompts.py` and
`_PLANNING_DRIVER_PROMPT`) and update or delete it consistently. The PLANNING prompt may
mention `server_status` as a read tool if the planning toolset includes it (check
`_planning_tools` wiring in `loop/engine.py`; if `preview_status` was in the planning
toolset, substitute `server_status`).

### 2. Retire the two preview hard-denies

File: `packages/core/src/disco/core/security/analyzers.py`, list `_SHELL_DENY`.
Delete exactly these two entries (and the `# E6: protect the live preview…` comment):

- `(re.compile(r"\b(pkill|killall)\b[^\n;|&]*http\.server"), …)`
- `(re.compile(r"\b(pkill|killall)\b[^\n;|&]*\b(8000|preview)\b"), …)`

Keep every other `_SHELL_DENY` entry (mkfs, dd-to-disk, fork bomb, rm-rf-root) — those
guard genuinely destructive actions, not the dead race.

### 3. Risk scoring for the new tools

In `RuleBasedAnalyzer._score_other`: `shell_view`/`shell_wait`/`server_status` → LOW;
`shell_kill_process` and `shell_write_to_process` → MEDIUM (gateable, never denied);
`shell_exec` routes through the same shell-command scoring as the `shell` tool (it
carries arbitrary commands — the `_SHELL_DENY` list and risk heuristics must inspect
`args["command"]` for `shell_exec` exactly as they do for `shell`). Verify by reading
`_score` — if it special-cases `tool_name == "shell"`, generalize to a
`{"shell", "shell_exec"}` set.

### 4. Update every test/fixture that asserted the old world

Grep tests for `pkill`, `http.server`, `preview_status`, `restart_preview`, `run_server`,
`NEVER kill`. Each hit: update to the new contract (e.g., the analyzer test that expected
a deny for `pkill -f http.server` now expects MEDIUM-not-denied; add a NEW test that
`shell_exec` with `rm -rf /` is still denied).

## Acceptance

1. **Unit**: analyzer — `pkill -f http.server` via `shell` and via `shell_exec` is no
   longer hard-denied; `mkfs` still denied through BOTH tools; new-tool risk levels as
   specified. Prompt — a test asserting `_EXECUTION_DRIVER_PROMPT` contains
   `shell_kill_process` and does NOT contain `NEVER kill`/`run_server`.
2. **Behavioral, live driver (the point of this order)**: run the scripted scenario on
   `PMX_SANDBOX=process` with the real 27B: prompt a build that requires a Vite dev
   server. Assert from the event log: the agent called `shell_kill_process('preview')` or
   killed it via its session, started its server in a session, called `shell_view` or
   `server_status` at least once before claiming success, and NO action was hard-denied.
   Save the event-log JSON as the run record under `test-record/bp-03/`.
3. **UI surface (live, Firefox)** — extend `frontend/e2e/preview-truth.spec.ts` or add
   `bp-03.spec.ts`: the scenario above driven through the real UI; screenshot the feed
   showing the kill→start→view sequence with no red denied-action entries →
   `test-record/screenshots/bp-03/contract-flow.png`, sent to user.

## Prohibitions

- Do not soften the remaining destructive denies.
- Do not add new prohibition language to the prompt ("never", "do not kill") about the
  agent's own sessions — the contract is ownership, not fear.
- The behavioral test uses the REAL driver model; no scripted fake-LLM responses.
