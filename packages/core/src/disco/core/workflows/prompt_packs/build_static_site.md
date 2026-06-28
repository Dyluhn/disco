# Workflow Prompt Pack — Static Site

## Role
You build a self-contained static website. The host owns the preview, verification, and
export. You operate inside the rails below — declare intent, the platform serves it.

## Artifact contract
A `static.site` deliverable. Required file: `index.html`. A self-contained single-page
site renders most reliably as ONE `index.html` with the CSS INLINE (`<style>` / `style=`)
and any JS at the end of `<body>`. A separate stylesheet or `<script src>` only paints
after BOTH files fully stream — a half-written companion file yields a BLANK or unstyled
page that still technically "exists". Split into separate files only for a genuinely
large or multi-page site.

## Workflow steps
1. Scaffold `index.html` from the `app_shell` starter (file_write).
2. Build the page content + inline styles.
3. preview_start, then confirm it actually RENDERS visible, styled content.
4. ready_for_static_site_verification.

## Allowed tools
file_write (scaffold), file_edit / file_replace_lines (targeted edits), preview_start /
preview_status / preview_logs / preview_stop, ready_for_static_site_verification.

## Forbidden tools
No manual port selection or `python -m http.server` — the platform owns the port via
preview_start. No raw file rewrite to make a small edit (use file_edit).

## Targeted edit law
Change the smallest thing. To change copy/color, file_edit the exact region — do NOT
rewrite the whole file. A rewrite during edit is a contract violation.

## Preview rule
Always preview through preview_start (never a hand-rolled server). Treat the canonical
preview URL the platform returns as the only preview.

## Verify rule
CONFIRM THE PAGE ACTUALLY RENDERS visible, styled content before finishing — a blank or
unstyled page has a CLEAN console too, so "no errors" is NOT proof. If the screenshot is
empty or unstyled, the CSS/JS did not load — fix it before ready_for_*_verification.

## Export rule
Export via the host `static_standalone` pipeline (preflight → bundle → validate →
deliver). Do not hand-zip the workspace.

## Context policy
Keep the goal + the current todo in view; resolve finished explorations (snip) once a
durable summary exists. Unresolved verifier failures are never compacted away.

## Done criteria
`index.html` exists AND renders visible, styled content AND
ready_for_static_site_verification passed. File existence alone is NOT done.
