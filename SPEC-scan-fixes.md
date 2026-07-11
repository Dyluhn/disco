# SPEC: backend scan-fix batch — prompt surgery, refusal sweep, observation caps

Four items from docs/deep-scan-report-7-9-26.md (scans 1-3). All backend.

## Item A — prompt surgery (packages/core/src/disco/core/llm/prompts.py)

1. F1 CONTRADICTION: in _EXECUTION_DRIVER_PROMPT's <file_rules>, replace
   "Read a file immediately BEFORE editing it. If a tool returns
   FRESH_READ_REQUIRED or STALE_FILE_CONTEXT, `file_read` it and retry..."
   with: "A successful edit's observation shows the updated region with
   CURRENT line numbers and grounds your next edit to that file — do not
   re-read after your own successful edit. If a tool returns
   FRESH_READ_REQUIRED or STALE_FILE_CONTEXT, follow its instructions (the
   refusal shows the live content to anchor on)."
2. VERIFY MANDATES: all four mandate constants
   (_SELF_VERIFY_MANDATE_CAPABLE/_SMALL, _HOST_VERIFY_MANDATE_CAPABLE/_SMALL)
   must lead with `verify_web_app` — one structured pass/fail call — instead
   of teaching the manual preview+browser+console flow. Keep the "verify once
   and stop" discipline and the never-guess-:8000 warning. The browser tool
   remains for INTERACTION/visual work, not the finish check.
3. DE-DUP: (a) "three distinct tools" header — count is wrong (five bullets);
   reword without a count. (b) The monolith rule appears 3x in the execution
   prompt — keep ONE statement (the separate-files bullet), delete the other
   two. (c) update_plan_progress: REMOVE the duplicated JSON schema (the tool
   description owns it) and change the cadence to "update at step BOUNDARIES
   (a step completed / a new step started), not per action".
4. PLANNING NEGATIVE: add one line to _PLANNING_DRIVER_PROMPT's read-tools
   section: "shell, code_exec, and all write tools are LOCKED until the plan
   is approved — do not call them in planning."
5. pnpm CLAIM: verify against the sandbox image definition in this repo
   (grep the sandbox/docker build files for pnpm). If pnpm is genuinely
   preinstalled, keep the claim; if not or unverifiable, change to "npm and
   uv are available; prefer uv for python" (follow what the image actually
   provides).
6. Any prompt token savings beyond these are welcome ONLY where content is
   duplicated verbatim — do not rewrite style.
   Update packages/core/tests prompt tests in lockstep.

## Item B — dual-field failure sweep + catch-all recipes (scan 1 D1/D2)

1. Shared helper: lift `_fail(msg)` (see shell_sessions.py — message goes in
   BOTH content and error) into a shared location and use it at every
   failure site that currently returns content="" with error-only:
   browser.py (all 4), verify_app.py catch-all, _deck_patch.py (6),
   image_gen.py, slides.py, document.py, audio_overview.py catch-alls.
   Preserve each site's existing machine error code where one exists
   (keep error=<code> and put the human message in content in that case —
   the invariant is: content is NEVER empty on failure).
2. Recipes for the high-traffic catch-alls:
   - browser generic failure: append "If this repeats, check the preview
     with preview_status / preview_logs, or verify with verify_web_app —
     do not retry the identical call."
   - verify_web_app catch-all: append "Check preview_status (is a preview
     running?) and preview_logs, then re-run verify_web_app once."
   - browser daemon failure (exit-code path): include the recipe line too.

## Item C — observation caps (scan 3)

1. shell / shell_exec observations: cap stdout+stderr fed back at ~4,000
   chars — keep HEAD 1,000 + TAIL 3,000 with an elision line in the middle
   stating how many chars were dropped and: "full output not retained —
   rerun redirected to a file (e.g. `... > out.log 2>&1`) and file_read it
   if you need it all." Follow the existing bounded-view helpers' style.
2. design_lint: cap the rendered findings at the top 20 by severity with a
   final line "…and N more findings — fix the above first, then re-run."
   structured= keeps the full list (it is not context-fed).

## Item D — shell vs shell_exec when-lines (scan 2)

- shell description: add "One-shot: runs to completion and returns output —
  the default for installs, builds, tests, git."
- shell_exec description: add "Session-based: use ONLY when you need a
  process that outlives the call (watchers, daemons) or stdin interaction —
  otherwise use `shell`."

## Tests
- Item A: prompt tests updated in lockstep (assertions on the new wording).
- Item B: one test per swept module family asserting content non-empty on the
  failure path (parametrize where natural).
- Item C: shell cap test (head+tail+elision line + hint); design_lint cap
  test (structured full, content top-20).
- Item D: none needed beyond spec-build.

## Verification
- PYTHONPATH=$(ls -d $PWD/packages/*/src | tr "\n" ":") /var/home/dylan/projects/disclaude/.venv/bin/python -m pytest packages/core/tests packages/tools/tests packages/agent-server/tests -q

## Constraints
- Smallest coherent diffs; do NOT touch running servers or the main checkout.
- Commit on wt-scanfixes with --no-verify; FINDINGS-SCANFIXES.md with
  deviations (spec yields to code reality — especially the pnpm verdict).
