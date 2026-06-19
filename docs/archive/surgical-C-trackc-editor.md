# Surgical Plan — Track C / Phase 5: In-Browser Editor + Element→Agent Substrate

**Date:** 2026-06-18
**Scope:** Track C **Phase 5** (the deferred phase). ONE shared substrate (selection overlay + postMessage
bridge + selection envelope + agent edit-loop) with TWO pluggable resolvers (`DeckResolver`, `SourceResolver`)
serving BOTH the slide editor AND the app/site builder.
**Status:** BUILD-READY where schema-INDEPENDENT (4.1 / 4.2 / 4.6-source-plumbing). The schema-BOUND parts
(`deck_patch`, `DeckResolver` ref fields, `DeckEditor`) wait on **C4** (`docs/slides-experiment-verdict.md`)
to freeze the authoring/deck schema.
**Supersedes-for-this-area:** `docs/disco-direction-and-decisions-6-17-26.md` §4 (verified against current
code below; file:line re-located 2026-06-18). Companion: `docs/surgical-B-trackc-slides.md` (the C1–C3 deck
schema + renderer; **this plan assumes C3 emits `data-element-id`/`data-slide-id`** — see §Dependencies).

> **CAUTION on line numbers:** every `file:line` below was re-located against current code on 2026-06-18 and
> WILL drift. Before editing, re-locate the symbol (Serena `find_symbol`, e.g. `deriveSrcDoc`, `artifact_file`,
> `agent_scope`) or by name; the **symbol + behavior** is authoritative, the line number is a hint.

---

## 0. Grounding — what is TRUE in the code today (verified 2026-06-18)

These are the substrate facts the plan is built on. Each was re-confirmed against current source.

| Claim (from §4) | Verified state | Evidence (file:line) |
|---|---|---|
| Zero `postMessage` in the frontend; the bridge is net-new | **TRUE** — grep clean across `frontend/src` | (no hits) |
| No element-identity tagging on rendered decks/preview | **TRUE** — only `data-pmx-index` exists, stamped by the browser tool's DOM walker (unrelated) | `_browser_daemon.py:162,179,184,252,291`; `browser.py:186` |
| Preview iframes the overlay attaches to exist | **TRUE**, three of them | below |
| ↳ Live-proxy iframe (build surface) | `sandbox="allow-scripts allow-forms allow-same-origin allow-popups"`, `key={reloadKey}`, `src={proxySrc}` | `PreviewPane.tsx:203-209` (proxySrc `:92`, reloadKey state `:55`) |
| ↳ Static srcdoc iframe (build surface) | `srcDoc={srcDoc}`, `sandbox={untrusted ? "" : "allow-scripts"}`, `key={reloadKey}` | `PreviewPane.tsx:238-246` (guard `if (srcDoc != null)` `:215`) |
| ↳ Artifacts srcdoc iframe (Agent surface) | `srcDoc={srcDoc}`, `sandbox={untrusted ? "" : "allow-scripts"}` | `AgentCanvas.tsx:217-224` (srcDoc `:204`, guard `:209`) |
| `deriveSrcDoc` is the srcdoc injection point we control | **TRUE** — returns inlined entry HTML or `null` | `buildTrace.ts:370-401` (`deriveFiles` `:316-365`; server-side artifacts get `content=""` at `:346,:352`) |
| Live-proxy serves through our origin (script-inject point for live path) | **TRUE** — `Response(content=r.content, …)` we can rewrite | `preview.py` `preview_app` proxy (~`:58-63`), `port_app` (~`:75+`) |
| `previewHostUrl(cid, port)` builds the proxy origin | **TRUE** | `client.ts:68` |
| Artifact route serves EVERY type as `attachment`; no inline path | **TRUE** | `files.py` (routes) `artifact_file:165`, `attachment` header `:208`, allowlist `_ARTIFACT_TYPES:176` ← `_common.py:48` |
| Slides today are Marp/HTML-fallback only, NO data model / no tagging | **TRUE** — `renderer:"marp"`/`"fallback"`; "editable PPTX is v2" | `slides.py:9,312,408-409`, `SlidesTool.run:322`, `_render_with_marp:372` |
| Deck editor today = read-only prev/next; inline render deferred | **TRUE** | `SlidesBlock.tsx:22-27` (deferred-inline note), component `:29` |
| The deck schema / renderer modules | **NOT built yet** (correct — C4-gated) | `_deck_schema.py`, `_pptx_render.py` absent |

**Dependency status (the gates §4.0 names):**

- **K1 (`_snip_args` execution-guard) — DONE & WIRED.** `find_elided_arg_markers` exists (`events.py:313`,
  marker family `_ELISION_MARKER_RE:297`, `_snip_args:300`) and is enforced in the loop at
  `loop/observe.py:230` (`_k1_bad = find_elided_arg_markers(action.tool_call.arguments)`), covered by
  `core/tests/test_k1_elision_guard.py`. **The K1 gate for Track C is satisfied** — new tool args (incl.
  `deck_patch`) pass through the same guard automatically.
- **W3 / W4 source-edit tools — ALREADY BUILT** (the §4.4 SourceResolver path reuses them, no new tool):
  `_gated_write` (syntax-gate + auto-revert) `files.py:146`; `FileStrReplaceTool` (anchored unique-match)
  `files.py:696`, registered `builtin/__init__.py:82`, capability-gated via `Requirement.ANCHORED_EDIT`
  (`types.py:52`) in `agent_scope()` (`registry.py:119-140`); `FileEditTool` `files.py:473`.
- **C4 verdict — PENDING.** Freezes `AuthoredSlide`/`Element`/`Slide`/`Deck` field shape. Until it lands,
  every item touching `slide.elements[i]` is PROVISIONAL.

**Tool registration mechanism (where `deck_patch` slots in):** tools are instances appended in the loop at
`builtin/__init__.py:73-106` then `registry.register(tool)`; visibility is set by `ToolScope.allowed_tools`
(`registry.py:19-32`); surfaces pick a scope via `agent_scope()` / `research_scope()` (`registry.py:115-140`).
`deck_patch` = one new `Tool` instance + membership in `AGENT_TOOLS` (`registry.py:63`) / the future
`artifact_scope()` (Track C §3.6 C6).

**Host→loop inject path (where the host "apply" lands):** a user/steer message is appended to the event log
via `POST /conversations/{cid}/messages` (`conversations.py:66`) or the WS `steer` frame
(`ws.py:52-53` → `_user_message(text, steer=True)`, `_common.py:107`). The §4.4 apply handler reuses this
exact seam — it posts a structured steer carrying the envelope, NOT a new transport.

---

## Message envelope (the canonical contract — §4.2, versioned, origin/nonce-guarded)

One envelope shape for BOTH resolvers. Lives in `frontend/src/lib/selectionBridge.ts`; mirrored to the
Python `deck_patch` tool input (§4.4).

```ts
// Every frame carries this header; the host validates it before acting.
interface BridgeFrame { v: 1; channel: "disco-select"; nonce: string }

// host → iframe:  disco:overlay:arm | disco:overlay:disarm | disco:overlay:walkup | disco:apply
// iframe → host:  disco:hover {rect} | disco:selection (the ENVELOPE)

interface SelectionEnvelope extends BridgeFrame {
  type: "disco:selection";
  selection_ref: DeckRef | SourceRef;   // resolver-tagged target (§4.3)
  human_label: string;                   // e.g. "Title text — 'Q3 Revenue'"
  rect: { x: number; y: number; width: number; height: number };  // host overlay placement
  screenshot_crop?: string;              // dataURL crop, captured host-side, ONLY if a vision-capable verifier exists (§2)
  edit_instruction?: string;             // filled when the user submits the edit
}
type DeckRef   = { kind: "deck";   slide_id: string; element_id: string };   // schema-frozen-by-C4
type SourceRef = { kind: "source"; oid: string; file: string; line: number };
```

**Guards (mandatory):** validate `event.origin` against the known preview origin (`previewHostUrl(cid,port)`
for the live path; the iframe's own origin for srcdoc); check `nonce` matches the value handed in at
arm-time; drop malformed/extra-field frames. `screenshot_crop` is a host-side canvas crop of `rect`, taken
only when §2 vision is active (else omitted — no false vision evidence).

---

## 4.1 — Selection overlay (hover-highlight · click-select · walk-up-to-parent)  · **SCHEMA-INDEPENDENT**

- **ID:** C-EDIT-1.
- **Approach:** clean-room of **Onlook (Apache-2.0)** overlay + in-frame selection agent. Transcribe no
  upstream code — reimplement the behavior (hover ring, click box, ancestor walk-up).
- **Problem (to-be-built):** the preview iframes (`PreviewPane.tsx:238-246`, `AgentCanvas.tsx:217-224`) are
  inert render targets; nothing lets a user point at a rendered element and act on it.
- **Files (new + touch):**
  - NEW `frontend/src/lib/selectionAgent.ts` — the in-frame script (injected into the previewed document).
  - NEW `frontend/src/components/build/canvas/SelectionOverlay.tsx` — the host-side chrome (positioned layer
    over the iframe; mirrors the rect, draws the selection box + "↑ parent"/layers control + the inline
    edit-instruction affordance + the "Select element" arm toggle).
  - NEW `frontend/src/hooks/useElementSelect.ts` — arm/disarm state + envelope plumbing for a host pane.
  - TOUCH `PreviewPane.tsx` (mount overlay over the srcdoc iframe `:238-246` and live-proxy iframe
    `:203-209`); TOUCH `AgentCanvas.tsx` (mount over the artifacts iframe `:217-224`).
  - Injection: for **srcdoc** decks/apps, inject `selectionAgent.ts` (as an inlined `<script>`) inside
    `deriveSrcDoc` (`buildTrace.ts:370-401`) before returning HTML; for the **live-proxy** path, inject the
    `<script>` into the proxied HTML in `preview.py` (`preview_app`/`port_app` Response, ~`:58-63`).
- **How:** in-frame — on `mousemove` → `document.elementFromPoint` → hover ring; on armed `click` →
  `preventDefault`, capture `getBoundingClientRect()` + nearest tagged ancestor's resolver attr
  (`data-element-id` for decks / `data-oid` for source, §4.3) + a `human_label` → `postMessage` a
  `disco:selection` envelope to `parent`. Walk-up = re-resolve to `parentElement`, skipping untagged
  wrappers, re-emit. Host — a positioned div mirrors the rect (cross-frame can't read the child DOM
  directly), renders the box + controls + the edit affordance.
- **Sandbox honesty (no false affordance):** both srcdoc iframes are `sandbox="allow-scripts"` for trusted
  runs → injected scripts run. The **`untrusted`** (shared/imported) path keeps the empty sandbox
  (`sandbox=""`, `PreviewPane.tsx:244`, `AgentCanvas.tsx:222`) and MUST hard-disable the overlay/arm toggle
  with a visible "disabled for untrusted content" state — never a dead control.
- **Tests (vitest + Playwright/Firefox):** arming shows a hover ring; one click emits exactly one
  `disco:selection`; walk-up re-targets to the parent ref; the untrusted run hides/disables the toggle.
- **Acceptance (visual evidence MANDATORY):** Firefox screenshot of a slide element highlighted inside the
  Artifacts iframe, via SendUserFile.
- **Risk:** MED — cross-frame coordinate sync; Firefox-only host raster constraint (CLAUDE.md).
- **Dependencies:** §4.2 (transport). **Schema:** INDEPENDENT (does not touch `slide.elements[i]`).

## 4.2 — postMessage bridge protocol + selection envelope  · **SCHEMA-INDEPENDENT**

- **ID:** C-EDIT-2.
- **Approach:** clean-room of **bolt.diy (MIT)** selection-as-chat-context + structured apply, plus Onlook's
  DOM→tag resolution. Net-new transport (zero `postMessage` exists today — confirmed).
- **Problem:** no host↔iframe transport; the edit-loop needs a stable typed contract independent of the
  active resolver.
- **Files:** NEW `frontend/src/lib/selectionBridge.ts` (host transport + the `SelectionEnvelope`/`DeckRef`/
  `SourceRef` types above) + matching handling in `selectionAgent.ts` (§4.1). The envelope type is mirrored
  to the Python `deck_patch` input (§4.4).
- **How:** the message set + `{v:1, channel:"disco-select", nonce}` header + origin/nonce validation exactly
  as in the **Message envelope** section above. `host→agent disco:apply` posts the completed envelope toward
  the loop inject seam (§4.4). Drop any frame failing the header/origin/nonce check.
- **Tests:** malformed / origin-mismatched / wrong-nonce frames are dropped; a valid selection round-trips
  host→iframe→host; walk-up yields a parent ref; the envelope serializes to the Python tool-input shape.
- **Acceptance:** an end-to-end select→edit-instruction→apply round-trip logged in dev (console trace).
- **Risk:** MED — security-sensitive; origin + nonce MUST be correct (the iframe runs untrusted-authored
  HTML even in the "trusted" run).
- **Dependencies:** none (it DEFINES the contract). **Schema:** INDEPENDENT.

## 4.3 — The two resolvers + their data-attribute tagging  · **SPLIT** (DeckResolver schema-bound; SourceResolver independent)

- **ID:** C-EDIT-3.
- **Approach:** two pluggable resolvers behind one interface. `DeckResolver` = clean design over the §3 C3
  renderer's `data-element-id`. `SourceResolver` = clean-room of **Onlook (Apache-2.0)** `data-oid` build-time
  tagging + DOM→source resolution.
- **Problem:** a clicked DOM node must resolve to an editable target — a JSON element in `slide.elements[]`
  for decks, a `file:line` for apps. Neither tagging exists yet.
- **Files:**
  - NEW `frontend/src/lib/resolvers/deckResolver.ts` — `resolveFromTag(el) → DeckRef | null` reading
    `data-element-id` + `data-slide-id`. **[schema-bound — ref fields frozen by C4]**
  - NEW `frontend/src/lib/resolvers/sourceResolver.ts` — `resolveFromTag(el) → SourceRef | null` reading
    `data-oid="<file>:<line>:<col>"`. **[schema-independent]**
  - Tagging emitters: deck `data-element-id`/`data-slide-id` is emitted by the **§3 C3 renderer**
    (`surgical-B-trackc-slides.md` — this plan ASSUMES it lands there, on every rendered box). Source
    `data-oid`: a build-time tag pass — a Babel plugin (clean-room of Onlook's) for JSX, plus a
    server-side HTML tag pass at preview-serve time for plain-HTML apps (extend `preview.py` proxy /
    `preview_service.py`). **[schema-independent]**
- **Interface:** `interface Resolver { resolveFromTag(el: Element): DeckRef | SourceRef | null }`. Resolver
  selection is by surface: decks → `DeckResolver`; app-builder preview → `SourceResolver`. Everything else in
  the substrate is identical.
- **Export hygiene (mandatory):** `data-element-id` / `data-oid` are EDITOR-ONLY. The exported artifact
  (`.html` / `.pptx`) MUST be stripped of them so downloads stay clean — do the strip in the C3 export path
  (decks) and the Babel/HTML pass's production mode (source).
- **Tests:** a clicked deck element resolves to the right `slide_id`/`element_id`; a clicked app element to
  the right `file:line`; tags ABSENT from exported `.html`/`.pptx`; swapping the resolver changes only the
  ref kind, nothing else in the substrate.
- **Acceptance:** the same overlay+bridge drives a deck edit AND a source edit by swapping only the resolver
  (demoed; Firefox screenshots of each).
- **Risk:** MED-HIGH — the source tag pass (AST→`file:line` mapping, half-written-file robustness) is the
  genuinely-new fiddly part. It is OFF the slides critical path.
- **Dependencies:** `DeckResolver` ← **C4** (schema-frozen) + C3 renderer emitting the attrs;
  `SourceResolver` ← the app-builder preview (already has a live iframe, `PreviewPane.tsx:203-209`).

## 4.4 — Agent edit-loop: envelope → structured patch → re-render  · **SPLIT** (deck path schema-bound; source path independent, tools already exist)

- **ID:** C-EDIT-4.
- **Approach:** clean-room of **bolt.diy (MIT)** "selection-as-chat-context" + structured action-apply
  (`ActionRunner`). The model receives TARGETED context (`human_label` + `selection_ref` + `edit_instruction`
  + `screenshot_crop` when §2 vision present), not the whole artifact.
- **Problem:** the agent has no path to receive "the user highlighted X, do Y" and apply a SCOPED edit —
  today edits are whole-file/anchored tool calls with no element targeting.
- **Files:**
  - Host: a `disco:apply` handler in `selectionBridge.ts`/`useElementSelect.ts` → posts a structured steer
    via the existing inject seam (`POST /conversations/{cid}/messages` `conversations.py:66` or WS `steer`
    `ws.py:52-53` → `_user_message(..., steer=True)` `_common.py:107`). NO new transport.
  - Deck path: **NEW `deck_patch` tool** — `packages/tools/src/disco/tools/builtin/_deck_patch.py` (a `Tool`
    instance), registered in `builtin/__init__.py:73-106` and added to `AGENT_TOOLS` (`registry.py:63`) +
    the future `artifact_scope()` (C6). **[schema-bound — patches `slide.elements[i]`, frozen by C4]**
  - Source path: **REUSE the already-built** `FileStrReplaceTool` (`files.py:696`) / `FileEditTool`
    (`files.py:473`) — both already syntax-gated + auto-reverting via `_gated_write` (`files.py:146`). No new
    tool. **[schema-independent]**
- **How — deck path:** `deck_patch(slide_id, element_id, patch)` applies an **RFC-6902 JSON Patch** to
  `slide.elements[i]` in the STORED deck JSON, validates the patched element against the deck schema before
  commit (revert on invalid), then re-lowers + re-renders deterministically (§3 `lower_deck`) — NO model
  re-generation of unchanged slides. Args pass through the K1 guard automatically (`observe.py:230`, already
  wired). After patch, the host bumps `reloadKey` (`PreviewPane.tsx:55,239`) so the user sees the re-render;
  optional follow-up screenshot for §2 vision verification. **Source path:** resolve `file:line` → scoped
  `file_str_replace`/`file_edit` → `_gated_write` syntax-gate → save → HMR/preview reload.
- **Tests:** an envelope edit on a deck title → `deck_patch` mutates only that element, re-renders, other
  slides byte-stable; an invalid patch is rejected + reverted; a source edit lands at the right `file:line`
  and the preview reloads; the K1 guard rejects an elision-marker-bearing patch arg (reuse
  `test_k1_elision_guard.py` pattern).
- **Acceptance (visual evidence MANDATORY):** highlight a slide element → "make this the headline, bigger" →
  it changes in the re-rendered deck (Firefox screenshot, SendUserFile).
- **Risk:** MED for the deck patch + revert; the source path reuses hardened W3/W4 tools (LOW).
- **Dependencies:** K1 (DONE); `deck_patch` ← **C4** (schema-frozen) + §3 `lower_deck`/renderer; source path
  ← §4.3 `SourceResolver`; both ← §4.1/§4.2.

## 4.5 — Fresh React deck editor over the positional deck schema  · **SCHEMA-BOUND**

- **ID:** C-EDIT-5.
- **Approach:** a FRESH React editor (no vendored code). **PPTist (AGPL-3.0)** is MODEL/SCHEMA-CONCEPT ONLY
  (`src/types/slides.ts`) — study the data shape, write the editor clean.
- **Problem:** the only deck UI today is `SlidesBlock.tsx` — read-only prev/next, inline render explicitly
  deferred (`:22-27`). No editor exists.
- **Files:** NEW `frontend/src/components/build/editor/DeckEditor.tsx` + `SlideCanvas.tsx` + `ElementBox.tsx`
  + `LayersPanel.tsx`; mounts in the Artifacts/Build surface; extends/replaces `SlidesBlock.tsx` for editable
  decks; exports through the existing download route (`AgentCanvas.tsx:165-186`, the declared-artifact GET).
- **How:** render each `Slide` as a 16:9 canvas; each `Element` as an absolutely-positioned `ElementBox`
  (subtypes per §3). Stamp `data-element-id`/`data-slide-id` so the SAME overlay (§4.1) works INSIDE the
  editor. Direct manipulation (drag/resize/rotate/edit-text) emits an RFC-6902 JSON Patch on
  `slide.elements[i]` — the SAME shape `deck_patch` uses (§4.4) — so the deck JSON is the single source of
  truth and human+agent edits flow through ONE mutation route; the deterministic renderer means editor and
  export render the SAME JSON. `LayersPanel` is the non-spatial selection/walk-up list.
- **Tests:** loading a deck JSON renders elements positioned; dragging emits a patch and persists; editor +
  export render byte-identical layout; selecting in the editor opens the same highlight-mode affordance.
- **Acceptance (visual evidence MANDATORY):** create a deck via the agent → open in the editor → move/edit an
  element → export an editable `.pptx` that opens in PowerPoint/LibreOffice (Firefox screenshot + the opened
  `.pptx` via SendUserFile).
- **Risk:** MED-HIGH — a real editor is substantial; bounded by reusing the substrate + the deterministic
  renderer (no bespoke layout engine).
- **Dependencies:** **C4** (schema-frozen); K1 (DONE); §4.1-4.4; §3 C1/C3 (schema + renderer).

## 4.6 — Reuse the substrate for the app/site builder (second surface, near-free)  · **SCHEMA-INDEPENDENT**

- **ID:** C-EDIT-6.
- **Approach:** the app builder reuses §4.1-4.4 UNCHANGED and plugs in `SourceResolver` (§4.3). It already
  has a live-preview iframe, so it inherits highlight-mode for the cost of the tag pass + resolver.
- **Problem:** the app/site builder needs the same "highlight → tell the agent → it edits"; building it
  separately doubles the work.
- **Files:** TOUCH the build-surface preview (`PreviewPane.tsx:203-209` live-proxy iframe) to mount the SAME
  `SelectionOverlay`; ensure the `data-oid` pass runs in the build/preview pipeline (§4.3); select
  `SourceResolver` for that surface. Source edits flow through the already-built `file_str_replace`/
  `file_edit` (§4.4 source path).
- **How:** arm the SAME overlay over the build-surface live iframe; the in-frame agent reads `data-oid` →
  `SourceResolver` → `{kind:"source", file, line}` → the agent edits source → HMR/preview reload. No new
  overlay, bridge, or envelope.
- **Tests:** highlight a button in the running app preview → "make it primary blue" → the source at the
  resolved `file:line` changes → the preview HMR-reloads.
- **Acceptance (visual evidence MANDATORY):** a live-app element edited via highlight-mode; before/after
  Firefox screenshots, SendUserFile.
- **Risk:** MED — depends on the §4.3 source tag pass (the riskiest sub-part).
- **Dependencies:** §4.1-4.4 (built for slides first); §4.3 `SourceResolver` + the `data-oid` tag pass.
  **Schema:** INDEPENDENT.

---

## Adjacent prerequisite — C5 inline-render fix (NOT part of §4, but the overlay needs it)

The overlay needs the srcdoc deck to actually RENDER (not a blank/`""` frame). That is **Track C §3.5 (C5)**:
`deriveSrcDoc` returns `null` (not `""`) for content-empty artifacts; the `?inline=true` artifact-route
branch (`files.py:165` artifact_file, replacing `attachment:208` with `inline` for `.html` only, CSP +
`sandbox=allow-scripts` no-same-origin). C5 is INDEPENDENT and can land first. The overlay's srcdoc-injection
path (§4.1) builds directly on the fixed `deriveSrcDoc`. **Sequence C5 before C-EDIT-1's srcdoc injection.**

---

## Ordered build sequence

**Gates already satisfied:** K1 (DONE, `observe.py:230`); W3/W4 source-edit tools (DONE, `files.py`).
**Gate still pending:** C4 verdict (`docs/slides-experiment-verdict.md`) — freezes the deck schema.

### Phase 5a — schema-INDEPENDENT (build NOW, in parallel with C4 + Track A/B)

1. **C5** (Track C §3.5) — `deriveSrcDoc`→`null`, `?inline=true` route. *(prereq for srcdoc rendering)*
2. **C-EDIT-2 / §4.2** — `selectionBridge.ts` + the versioned envelope + origin/nonce guards. *(defines the contract; nothing else depends on schema)*
3. **C-EDIT-1 / §4.1** — `selectionAgent.ts` + `SelectionOverlay.tsx` + `useElementSelect.ts`; inject in `deriveSrcDoc` (srcdoc) and `preview.py` proxy (live); untrusted disabled-state. *(builds on 1+2; works against ANY tagged element)*
4. **C-EDIT-3 (source half) / §4.3** — `sourceResolver.ts` + the `data-oid` Babel pass + server HTML tag pass + export strip. *(schema-independent; the riskiest piece — start early)*
5. **C-EDIT-4 (source path) / §4.4** — wire the host `disco:apply` handler to the steer seam; route source edits through the EXISTING `file_str_replace`/`file_edit`. *(no new tool)*
6. **C-EDIT-6 / §4.6** — mount the overlay on the build-surface live-proxy iframe with `SourceResolver`. *(app/site builder gets highlight-mode end-to-end — full schema-independent vertical slice, demoable)*

### Phase 5b — schema-BOUND (build AFTER C4 freezes the deck schema)

7. **C-EDIT-3 (deck half) / §4.3** — `deckResolver.ts` reading `data-element-id`/`data-slide-id` (← C3 renderer emits them per `surgical-B-trackc-slides.md`). *(ref fields frozen by C4)*
8. **C-EDIT-4 (deck path) / §4.4** — the `deck_patch` tool (RFC-6902 on `slide.elements[i]` + validate-or-revert + re-lower/re-render), registered in `builtin/__init__.py` + `AGENT_TOOLS`/`artifact_scope()`. *(K1 guard already covers its args)*
9. **C-EDIT-5 / §4.5** — `DeckEditor` + `SlideCanvas` + `ElementBox` + `LayersPanel`; manual edits emit the SAME JSON Patch as `deck_patch`; overlay reused inside the editor; export editable `.pptx`. *(the Phase-5 acceptance)*

**Two acceptance milestones (both visual-evidence MANDATORY, Firefox + SendUserFile):**
- *Schema-independent (end of 5a):* highlight a button in the live app preview → "make it primary blue" →
  the source `file:line` changes → preview HMR-reloads (§4.6).
- *Schema-bound (end of 5b):* highlight a slide element → "make this the headline, bigger" → it changes in
  the re-rendered deck (§4.4); AND create deck → open in editor → move/edit element → export editable `.pptx`
  that opens in PowerPoint/LibreOffice (§4.5).

---

## Fitness gates (every item)

- Frontend: `npm run typecheck:build` + `npx vitest run` + `npx vite build`.
- Python (`deck_patch`, tag pass): `.venv/bin/python3 -m pytest -m "not integration"` (exit code is truth)
  + `uv run basedpyright` (0) + `uv run lint-imports` + `uv run python scripts/check_arch_budget.py`
  (no class >800 / func >200 LOC) + `gen_arch_diagram.py --check`.
- **Visual evidence MANDATORY** for every UI item: a real **Firefox** (not Chromium — host can't raster
  oklch/text) screenshot in the running app + SendUserFile. Green vitest ≠ evidence.
- License discipline: Onlook (Apache-2.0) + bolt.diy (MIT) = clean-room (describe behavior, transcribe no
  upstream code); PPTist (AGPL-3.0) = MODEL/SCHEMA-CONCEPT ONLY, vendor nothing.
