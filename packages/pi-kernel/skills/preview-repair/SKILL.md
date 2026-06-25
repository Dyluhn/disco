---
name: preview-repair
id: preview-repair
version: 1.0.0
surface: [build, agent]
allowed-tools: [think, file_list, file_read, file_write, file_replace_lines, file_insert_lines, shell_exec, preview_start, preview_status, preview_logs]
risk: medium
description: Diagnose and fix a dead or unhealthy preview server. Read the preview logs and status, find why the dev server failed to start or stopped serving, apply the smallest fix, and restart the preview until it is healthy. Use when the preview is down, returns errors, or never became healthy.
---

# preview-repair

A Disco-owned, reviewed skill for recovering a broken preview. The preview is
"healthy" only when the platform's health probe passes — this skill never
declares success on its own; it restarts and re-checks until `preview_status`
confirms health.

## When to use

The preview is unhealthy, stopped, or never came up: a blank page, a connection
error, a crashed dev server, or a `preview_start` that never reached healthy.

## Workflow

1. **Read the evidence first.** Call `preview_status` for the current state and
   `preview_logs` for the captured stdout/stderr. Do not guess — let the logs
   name the failure (missing dependency, syntax/build error, wrong entry,
   command that exits immediately, or a server that ignores the injected PORT).
2. **Locate the cause in the project.** Use `file_list` and `file_read` to
   inspect the start command, entry file, and config implicated by the logs.
3. **Apply the smallest fix.** Prefer `file_replace_lines` /
   `file_insert_lines` over rewriting files. Typical fixes: correct the start
   command, fix the broken entry/import, or make the server bind the
   platform-provided `HOST`/`PORT` instead of a hardcoded port.
4. **Restart and re-verify.** Call `preview_start` again (it is idempotent),
   then `preview_status`. Repeat read → fix → restart until status is healthy.
   If `preview_logs` shows the same failure twice after a fix, re-read the logs
   for the next distinct error rather than retrying blindly.

## Guardrails

- Never choose or hardcode a host port — the platform owns port selection and
  injects `HOST`/`PORT`. A server that ignores `PORT` is a bug to fix in the
  project, not a port to pick here.
- Use `shell_exec` only for diagnosis/repair the logs justify (e.g. installing a
  genuinely missing declared dependency), not for unrelated work.
- Do not call `finish` from this skill — recovering the preview is its only job;
  the surrounding build loop decides when the run is done.
