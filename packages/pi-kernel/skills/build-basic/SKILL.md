---
name: build-basic
id: build-basic
version: 1.0.0
surface: [build, agent]
allowed-tools: [think, submit_plan, file_list, file_read, file_write, file_replace_lines, file_insert_lines, shell_exec, preview_start, preview_status, preview_logs, finish]
risk: low
description: Build a static or multi-file web app from a plain-language brief. Plan first, then write the files (HTML/CSS/JS or a small framework project), start the preview, and confirm it serves before finishing. Use when the user asks to create or scaffold a website, landing page, or small front-end app.
---

# build-basic

A minimal, generic skill for turning a short brief into a working static or
multi-file web app. It is Disco-owned and reviewed — it never installs Pi
packages, never reads project-local `.pi` resources, and only uses the
Disco-bridged tools listed in `allowed-tools`.

## When to use

The user wants a new web app, landing page, dashboard, or small front-end —
anything that can run as static files or a single dev server.

## Workflow

1. **Plan before writing.** Call `submit_plan` with a short, concrete plan:
   the pages/components to create, the tech choice (plain HTML/CSS/JS unless a
   framework is clearly required), and how the preview will run. Do not write
   any files until the plan is approved.
2. **Scaffold the files.** After approval, create files with `file_write`.
   Prefer the smallest stack that satisfies the brief:
   - Static brief → `index.html` + `styles.css` + `app.js`.
   - Multi-file/interactive brief → a small framework project, but keep the
     dependency surface minimal.
   Use `file_list` / `file_read` to inspect existing files and
   `file_replace_lines` / `file_insert_lines` for edits rather than rewriting
   whole files.
3. **Run the preview.** Call `preview_start` with the run command and root.
   Never pick a host port yourself — the platform owns port selection. Use
   `preview_status` to confirm the preview is healthy and `preview_logs` to
   diagnose a failed start.
4. **Verify, then finish.** Only call `finish` once `preview_status` reports a
   healthy preview (or the static entry point is confirmed present). Disco —
   not this skill — is the final judge of whether the app works.

## Guardrails

- Keep `index.html` (or the framework entry point) as the obvious entry.
- Do not run package-install or network commands beyond what the brief needs.
- If the brief is ambiguous, state the assumption in the plan rather than
  guessing silently.
