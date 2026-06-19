# Deck Editor — end-to-end integration plan (2026-06-18)

**Status:** the deck-edit *pieces* are all on `build-surface-recovery-ux`, but the
loop is **open at the last hop**. This plan closes it. Everything here is
headless-implementable EXCEPT the final visual acceptance (§6), which must run
against the live stack with Firefox per `CLAUDE.md`.

Owner note: this was deliberately NOT blind-wired during the headless session —
wiring a select→edit loop you can't watch round-trip risks shipping a false
affordance. Execute §1–§5 headless (with the unit tests), then do §6 live.

---

## 0. Current state (verified)

**Present & wired**
- `deck_patch` tool — registered in `tools/registry.py` (lines ~98/137) and
  `builtin/__init__.py` (instantiated in the builtin list). `run()` reads authored
  JSON from a workspace file (`args.deck_file` via `ctx.sandbox.read_file`), applies
  an RFC-6902 patch, validates against `AuthoredDeck`, re-renders HTML+PPTX, writes
  back. Schema-or-revert on failure. Tested: `packages/tools/tests/test_deck_patch.py`.
- `lower_deck_for_editor(authored: AuthoredDeck) -> LoweredDeck` —
  `packages/tools/src/disco/tools/builtin/_deck_schema.py:1059`. Produces % geometry
  + `element_id` + `json_pointer` per element. This is the editor's data model.
- Frontend resolver `frontend/src/lib/resolvers/deckResolver.ts` (+ test, 25 cases) —
  maps `element_id` → RFC-6901 `json_pointer` (e.g. `slide-0:title` → `/slides/0/title`).
- `selectionBridge.ts` — envelope types (`SelectionEnvelope`, `DeckRef`), arm/disarm
  postMessage plumbing. `useElementSelect.ts` — arms one iframe, receives the
  selection envelope into `selection` state.
- §4.1 `SelectionOverlay` — mounted in `AgentCanvas.tsx` (~line 339) and
  `PreviewPane.tsx` over the preview iframe; the in-frame `selectionAgent.ts` script
  is injected into the trusted srcdoc.
- §4.5 `DeckEditor.tsx` (+ `ElementBox`, `LayersPanel`, `SlideCanvas`) — a richer
  drag/resize canvas over a `LoweredDeck`, emits RFC-6902 via `onPatch`.

**The two open hops (this plan closes them)**
1. **Selection dead-ends.** `useElementSelect`'s `selection` is passed only to
   `SelectionOverlay` for highlight. Nothing forwards it to the agent or forms a
   `deck_patch` call. (`AgentCanvas.tsx` consumes `artifactSelection` only as an
   overlay prop.)
2. **`DeckEditor` is mounted nowhere**, and there is **no backend route** that
   returns a `LoweredDeck` for a produced deck (only the in-process function exists).

**Two regressions to fix in the same pass** (surfaced by the test restoration,
documented in `test_deck_patch.py`'s module docstring):
- (R1) `strip_element_ids` was dropped in reconciliation → `render_html` stamps
  `data-element-id`/`data-slide-id` and `slides.py` writes that HTML as the
  *download* artifact with no clean variant → exported decks carry editor attrs.
- (R2) `render_html` stamps **no id on image elements** (verified for both the
  MinimalDeck compat path and the authored `image_prompt` path) → images are
  selectable in the DeckEditor canvas but not via the in-preview overlay.

---

## 1. Architecture decision (recommended)

Two selection surfaces, **one mutation route**:

- **Primary (lighter): in-preview selection → context-attached instruction.**
  Selecting an element in the preview iframe attaches a `DeckRef`
  `{deck_file, element_id, json_pointer}` to the build composer as a "selected
  element" chip. The next user instruction is sent WITH that ref as structured
  context; the BuildAgent reads it and issues a `deck_patch` on that pointer. This
  reuses the attach2 precedent (pre-created cid + context attached to a message in
  `useBuild`), keeps humans and the agent on the SAME `deck_patch` vocabulary, and
  requires no new "apply patch outside the loop" route (avoids a second mutation
  path that could diverge from the agent's).

- **Secondary (richer): `DeckEditor` canvas.** A toggle in the build canvas swaps
  the preview iframe for `<DeckEditor deck={LoweredDeck} onPatch=… />`. `onPatch`
  goes through the SAME context-attach route (emit the patch as the agent's next
  `deck_patch` arg), so both surfaces converge.

Rejected alternative: a direct `POST /deck/patch` route that calls `deck_patch`
outside the agent loop. It's simpler to demo but creates a second source of truth
for deck mutations (UI vs agent), bypasses the plan/approve gate, and desyncs the
event log. Keep edits flowing through the agent.

---

## 2. Backend — `LoweredDeck` route (for the §4.5 canvas)

File: `packages/agent-server/src/disco/agent_server/routes/preview.py`
(extend `make_preview_router`; it already resolves `conversation_id → sandbox`).

Add:
```
GET /conversations/{conversation_id}/deck/editor?deck_file=<workspace-rel path>
  → 200 { lowered: LoweredDeck-as-json }
  → 404 if deck_file missing; 422 if not valid AuthoredDeck JSON
```
Implementation: resolve the conversation's sandbox (same helper the other preview
routes use) → `read_file(deck_file)` → `json.loads` → `AuthoredDeck.model_validate`
→ `lower_deck_for_editor(authored)` → return `dataclasses.asdict(...)`.

Layering: `agent_server` may import `tools` (downward) — OK. Add a serializer for
`LoweredDeck`/`LoweredSlide`/`LoweredElement` (they're dataclasses; `asdict` is fine).

Tests (headless): `packages/agent-server/tests/test_deck_editor_route.py` — happy
path returns lowered geometry + json_pointers; missing file → 404; non-deck JSON →
422. Use the existing route test fixtures (see `test_novnc_live.py` / preview tests
for the sandbox-mock pattern).

---

## 3. Backend — fix R1 (export attribute stripping)

Restore `strip_element_ids(html_str)` into `_pptx_render.py` (pure regex helper;
port verbatim from reflog commit `05f60b9`). Then decide the preview-vs-export split:

- **Preview HTML** (what `SelectionOverlay` reads) MUST keep ids.
- **Download/export HTML** must be clean.

Wire: in `slides.py` where the `.html` deliverable is written for download
(lines ~438/465), write the STAMPED html for the preview artifact but pass the
download copy through `strip_element_ids`. If preview and download are the same
file today, split them (e.g. `deck.html` = preview-with-ids served to the iframe;
`deck.export.html` = stripped, linked from the Download button). Confirm which file
the preview iframe loads before choosing — this is the one spot needing a live check.

Re-add the dropped test (`test_strip_element_ids_*`) once restored.

---

## 4. Backend — fix R2 (image element ids), optional but closes the gap

In `_pptx_render.py`'s HTML render, stamp `data-element-id="slide-{i}:image_prompt"`
and `data-slide-id` on the image element of `image_right`/`full_image` layouts
(mirror how title/body are stamped). Verify it does NOT leak into the PPTX path.
Add the image-id assertion back to `test_deck_patch.py::test_image_right_*`.

(Lower priority than §2/§3 — images become selectable in the in-preview path; they
already are in the DeckEditor canvas.)

---

## 5. Frontend — close the selection→instruction hop + mount the editor

5a. **Selected-element chip + context attach** (closes open-hop #1).
- In `AgentCanvas.tsx`, consume `artifactSelection` (already in scope): when a
  selection arrives, resolve its `json_pointer` via `deckResolver` and render a
  dismissible "Editing: <element_id>" chip near the composer.
- Thread a `selectionRef: DeckRef | null` into `useBuild`'s submit so the next
  instruction is sent WITH `{deck_file, element_id, json_pointer}` as context.
  Reuse the attach2 message-context mechanism (the pre-created-cid + attached-corpus
  path in `useBuild.ts`). Clear the chip on send.
- Backend: the BuildAgent's turn must surface the attached `selectionRef` in the
  model context (so the agent knows the target pointer for `deck_patch`). Confirm
  the message-context field added by attach2 (`uploads_ingest` / ws message schema)
  and extend it with `selection_ref`, or add a parallel field.

5b. **DeckEditor toggle** (mounts the §4.5 canvas — open-hop #2).
- Add a "Edit deck" toggle in the build canvas (next to the preview). When on,
  fetch `GET …/deck/editor?deck_file=…` (§2), render
  `<DeckEditor deck={lowered} onPatch={…} onElementSelected={setSelectionRef} />`.
- `onPatch` → same context-attach route as 5a (emit as the next `deck_patch`).

5c. **Tests (headless, vitest):** simulate a `disco:selection` envelope →
assert the chip renders with the resolved pointer; assert submit includes
`selectionRef`. Mock the `/deck/editor` fetch → assert `DeckEditor` mounts with the
lowered deck. (Component-level; the real round-trip is §6.)

---

## 6. Live acceptance (REQUIRED — not headless)

Run the real stack (`agent-server` :8000 + `app-server` :8800 + Vite) on a model
that reliably tool-calls (gpt-oss-120b non-free per the Chutes note, or a local
server). Firefox/Playwright per `CLAUDE.md` (Chromium can't rasterize oklch here).

1. Build a deck (slides_generate) on the build/agent surface.
2. Arm selection, click the title element → chip shows `slide-0:title`.
3. Type "change this to 'Q3 Results'" → send → agent calls `deck_patch` with
   `{op:replace, path:/slides/0/title, value:"Q3 Results"}` → preview re-renders.
4. Toggle the DeckEditor canvas → drag an element → `onPatch` round-trips the same way.
5. Download the deck → confirm exported HTML has NO `data-element-id` (R1).
6. Screenshot each step + `SendUserFile`. Green vitest is NOT acceptance.

DISCO_INSPECT=1 + `GET /api/debug/trace/{cid}` to prove UI→loop→deck_patch→render.

---

## 7. Sequencing

§2 (route) ∥ §3 (R1) ∥ §4 (R2) are independent backend tasks — parallelizable.
§5a depends on confirming the attach2 message-context field; §5b depends on §2.
§6 last, gated on the live stack. Re-run the four fitness gates + vitest after each.

## 8. Open decisions for Dylan
- Preview vs export file split (§3): one file with a stripped download copy, or two
  artifacts? (Needs a 30-second live check of what the iframe loads.)
- Is the in-preview "select → attach to next instruction" UX the one you want, or
  should selection open the DeckEditor canvas directly? (§1 recommends the former as
  primary, latter as secondary.)
