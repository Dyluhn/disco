> **SUPERSEDED / HISTORICAL (as of 2026-07-07).** June-era deck-editor real-WYSIWYG-render plan, frozen mid-execution.
> Current status of record: `docs/disco-project-state.md` (master), `docs/disco-status-and-remaining.md` (features + remaining), `docs/disco-security-state.md` (security). This file is kept for history and may contain stale claims.

# Deck editor: real WYSIWYG via iframe-substrate + measured overlays (#5d)

Decision (Dylan): build the REAL substrate. The cheap versions are all half-measures — codex proved
the editor's EMU geometry matches the PPTX, while what the user SEES (local-llms.html) is a DIFFERENT
render path (`_html_for_c1_slide`, CSS flexbox) that ignores those positions. Mirroring geometry by
hand = wrong target + drift + still misses wordmark/colophon/outlines/line-heights. The only thing
that's truly faithful is to show the REAL render and overlay editable boxes measured FROM it. This is
exactly what the §4.5 "overlay substrate works inside and outside the iframe" comment was designed
for and never wired.

## Architecture
The render already stamps `data-element-id` + `data-slide-id` on editable elements
(`_pptx_render.py:972`). So:

1. **Visual layer** — an `<iframe srcDoc={realRenderHTML}>` inside the editor canvas, showing the REAL
   rendered deck (brand fonts, accent, bg, chrome, exact layout). One slide visible (the render's
   `.slide.active`); the editor drives which slide is active.
2. **Edit layer** — on iframe load + on resize/slide-change, query
   `iframe.contentDocument.querySelectorAll('[data-element-id]')`, measure each
   `getBoundingClientRect()` relative to the iframe, and position TRANSPARENT `<ElementBox>` overlays
   at those measured rects. The overlay carries selection outline + click-to-edit + drag; the iframe
   carries all visual styling. No geometry mirroring — positions come from measuring the real render.
3. **Edit write-back** — join each iframe `data-element-id` to the lowered element's `json_pointer`
   (from GET /deck/editor, which already supplies element_id→json_pointer). On text edit → PUT
   /deck/editor patch (unchanged) → re-fetch the render HTML → re-inject srcDoc → re-measure.

Data flow: GET /deck/editor (element_id + json_pointer = the edit map) + the inline render HTML
(visual + measured geometry). srcDoc is same-origin so `contentDocument` access works; CSP is
same-document.

## Backend — packages/agent-server/src/disco/agent_server/routes/deck_editor.py
- The existing GET /deck/export?fmt=html returns the render but with
  `Content-Disposition: attachment`. Add an INLINE variant (or a `?inline=1` flag / a dedicated
  GET /deck/editor/render route) returning `render_html(lower_deck(authored, theme_override))` as
  `text/html` WITHOUT the attachment header, so the frontend can `fetch()` it for srcDoc. Re-themes
  via the same lower_deck path the export uses (theme/template honored).

## Frontend — frontend/src/components/build/editor/
- `DeckEditor.tsx` / new `SlideSubstrate.tsx`:
  - Fetch the inline render HTML for the deck; hold it in state; `<iframe srcDoc=…>` sized to the
    canvas (16:9), `sandbox` allowing same-origin so we can read `contentDocument` (NOT allow-scripts
    unless needed for the render's nav — we drive active-slide from the parent instead).
  - On load + ResizeObserver: set the active slide in the iframe (toggle `.slide.active` by
    `data-slide-id`), then measure `[data-element-id]` rects → produce overlay specs
    `{element_id, json_pointer, rect}`.
  - Render `<ElementBox>` overlays positioned by measured rect (px), TRANSPARENT (no dashed outline
    unless selected; the real text shows through from the iframe).
- `ElementBox.tsx`:
  - Position from the measured rect (absolute px) instead of the lowered %-geometry.
  - Background transparent; show the selection outline ONLY when selected (remove the always-on dashed
    purple). On double-click edit, overlay an editable field; commit → PUT json_pointer patch.
  - Non-editable element ids (none stamped → simply no overlay) need no chrome handling — the iframe
    renders chrome; we only overlay the stamped editable elements.
- Keep `json_pointer` write-back exactly as today (PUT /deck/editor).

## Risks / handled
- Only title/subtitle/bullet are stamped editable → those are the editable overlays; everything else
  (charts/images/wordmark/accent) is shown by the iframe, not editable (acceptable — matches today's
  editable surface, just now visually real).
- Re-measure on resize (ResizeObserver, debounced) + after each edit re-render.
- srcDoc same-origin for measurement; fonts are inline base64 in the render (no external load).

## Acceptance (VISUAL PROOF = the gate)
- Open `conv_7ae43b420a724332bc8bf758bf14fdea` (`local-llms`, disco-light, 14 slides) in the deck
  editor in the running app. Screenshot the editor → it must look like `local-llms.html` (the real
  render): real Fraunces/brand fonts, accent, layout, chrome — a SLIDE, not a wireframe. Click an
  element → selection overlay aligns with the real text. SendUserFile the editor screenshot + the real
  render for comparison. Cover a title slide + a bullets slide.
- Tests: the inline render route returns text/html (no attachment); a component test that overlays are
  positioned from measured rects + edit issues the correct json_pointer patch (jsdom measurement is 0,
  so assert the wiring/patch, not pixels — pixels are the screenshot's job).
- tsc + eslint + ruff + basedpyright clean; existing editor/deck tests green (or updated for the new
  overlay model); codex re-review of the diff.
