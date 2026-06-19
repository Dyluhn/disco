# A1 — Website/app click-to-edit: design + decomposition

## The keystone decision (the hard part)
Click an element in a built-website preview → edit in place → the edit maps to source → agent re-renders.
The crux is mapping **rendered DOM → source**. Decision:

- **Scope v1 = STATIC HTML deliverables** (the common build output). Stamp each element with
  `data-oid="{workspace-relpath}:{sourceline}"` via **lxml** (`.sourceline` is exact — verified). The
  "source" IS the served HTML file; an edit → steer `"In {file} near line {N}: {instruction}"` → the agent
  edits that file. Tractable, valuable, no build instrumentation.
- **React/JSX live apps = documented FOLLOW-UP.** Mapping rendered DOM → JSX source needs a build-time
  babel/SWC plugin (à la react-dev-inspector) in the agent's generated project. Out of v1 scope; the live
  preview-proxy (preview.py) stays as-is for now. (This is why preview.py is NOT the v1 injection point.)

## Reuse — A2/prior work already built ~90% of the substrate
- `SELECTION_AGENT_SCRIPT` (selectionAgent.ts) — in-frame; `resolveRef()` ALREADY parses
  `data-oid="file:line"` → `SourceRef{kind:'source', oid, file, line}`. NO change needed.
- `SelectionOverlay.tsx` + `useElementSelect.ts` — arm/click/walk-up overlay. Reused as-is.
- `selectionBridge.ts` — `SelectionEnvelope` already reserves `edit_instruction`.
- `steer()` (useBuildStream.ts:267) + WS `{type:"steer", steer_text}` — the edit→agent wire. Reused.

## Decomposition (status)
- **A1.1 (KEYSTONE, NEW):** server-side `data-oid` stamper. New helper `stamp_oids(html, relpath) -> html`
  in tools (pure, lxml-based: iterate elements, set `data-oid="{relpath}:{el.sourceline}"`, skip
  html/head/script/style, don't clobber an existing data-oid). Serve via the inline-artifact path
  (`files.py` `?inline=true`) gated by a new `?oid=1` param (or always for HTML) so the preview iframe gets
  stamped HTML. Multi-file sites: the existing client `deriveSrcDoc` inlines siblings; for v1 stamp the
  entry HTML (css/js carry no editable text). Unit-test the stamper (idempotent, correct lines, skips).
- **A1.2 (NEW, small):** `appResolver.ts` — `parseOid(oid) -> {file,line,col}` + a human label. Mirror
  deckResolver.ts.
- **A1.3 SelectionOverlay — DONE (A2).** Reuse.
- **A1.4 edit→steer (NEW, small):** an edit affordance on a `kind:'source'` selection (inline input/modal)
  → `steer("In {file} near line {line}, change/edit: {instruction}")`. The bridge already carries the ref.
- **A1.5 deck slice — DONE (A2).** The deck path already uses the same bridge.
- **A1.6 app-builder slice (wire):** the AgentCanvas artifacts pane already mounts SelectionOverlay +
  SELECTION_AGENT_SCRIPT; ensure the preview HTML it shows is the STAMPED one (A1.1) and the source-kind
  selection shows the A1.4 edit affordance. Real Firefox screenshot: click an element in a built static
  site → edit → steer fires → (agent edits). 

## No false affordance
The edit affordance appears only on a `kind:'source'` selection carrying a real `data-oid` (stamped). A
non-stamped element (no data-oid) → no edit affordance (walk-up still works). Live React preview (no
stamping in v1) → the existing inspect/select works but the "edit here" affordance is gated off (honest).

## Gates
basedpyright 0 · lint-imports · arch · pytest (stamper) · typecheck:build 0 · vitest (appResolver + the
edit affordance) · eslint 0 · REAL Firefox screenshot of the click→edit→steer round-trip on a static site.
