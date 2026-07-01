# Claude Design Playbook — distilled architecture reference

**Provenance:** Distilled 2026-07-01 from the released/leaked Claude design-artifact system prompt
("Omelette" runtime: Design Components, deck-stage, starter components, verifier protocol), pasted
by Dylan into the disclaude session. This doc captures the *architecture and mechanisms*, not the
verbatim prompt text.

**IP hygiene (release gate):** For the open-source release we harvest **ideas and architecture
only** — no verbatim Anthropic prompt text ships in Disco's prompts or docs. Same discipline as the
OSS-harvest audit. Verbatim source stays out of the repo; if a raw reference copy is needed for
implementation workers, keep it in an untracked local file.

**Why this exists:** Disco Build's three release-blocking failure modes, per Dylan (2026-07-01):
1. **Random pauses** — the model stalls / goes actionless mid-build.
2. **Loop-stuck** — rewrite-thrash, verify-spin, fix-recheck-fix cycles.
3. **False affordances** — output that *looks* done/usable but isn't wired or correct.

Every mechanism below is annotated with which failure mode(s) it structurally kills
[P]=pauses [L]=loops [F]=false affordances.

---

## 1. Core thesis: selection over generation, host owns truth

The runtime converts open-ended generation into **constrained selection**:

- The model does not invent a document format, a scaffold, a verification procedure, or an export
  path per run. It **selects** host-defined primitives (one artifact format, a fixed starter
  catalog, skill recipes, one finalizer call) and fills in content.
- The host owns everything that must be *true*: document assembly, preview, load-checking,
  verification, export capture, printing, slide navigation, context compaction bookkeeping.
- Worst case degrades to "plain but working," never "broken but pretty." [F]

This is the same principle behind Disco's proven wins (host-owned finalizer, declarative
plan-progress, recoverable excerpts): every place the model used to freely emit state, replace it
with a host-defined move the model picks.

## 2. The artifact contract — Design Component (DC)

ONE mandated container: `Name.dc.html` — a single file that opens directly in a browser AND can be
imported by other DCs. Not "HTML page or React app or whatever fits" — one format, always.

The model authors exactly three pieces; the **host assembles the document** around them:
1. **Template** — markup that goes between host-owned wrapper tags. The model never writes
   doctype/head/script wrappers. A whole class of malformed-document failures becomes impossible. [F]
2. **Logic class** — one plain-JS class with a fixed name, React-style lifecycle minus render();
   a `renderVals()` method returns the template's inputs by name. Empty for static designs.
3. **Props metadata** (optional JSON) — declared tweakable knobs; the host renders the tweak UI.
   The model never hand-rolls a controls panel for declared props. [F]

Template language is deliberately weak:
- `{{ path }}` holes are **dotted lookups only** — no expressions, no function calls. Anything
  computed lives in `renderVals()` and is exposed by name. Expressions in holes fail silently, so
  the language makes them unwritable-by-rule rather than debuggable-by-luck. [L,F]
- Control flow is two host elements (`for`/`if`) that REQUIRE streaming hints (placeholder count /
  placeholder value) so partially-streamed documents still render sensibly. [P,F]
- Child composition via explicit import elements with mandatory size hints (placeholder + min-size
  while streaming). Never bare capitalized component tags. [F]

Styling invariants:
- **Inline styles only.** No stylesheets, no classes, no design-token files — even for decks
  (repeat literals per slide). Rationale: class-based CSS delays first paint until rules AND markup
  have streamed; inline styles paint immediately. The invariant serves streaming, not aesthetics. [P]
- The only legal style/script location is a head-equivalent block at template top (fonts,
  keyframes, body resets, bundles). A script lower in the body doesn't run until the stream
  reaches it — so it's banned, not discouraged. [F]

Editability-by-construction (the artifact is designed for the HUMAN's direct-manipulation loop):
- User-editable content must be **static markup**, not script-generated DOM. Static elements can
  be retyped in place by the user; script-rendered content forces every tweak back through chat. [F]
- Each piece of text lives in its own leaf element; repeated structure is written out (three
  literal bullets, not one bullet rendered from an array). The repetition is the point — it lets
  the user edit bullet two without touching bullet one.
- Anything rendered through imperative createElement is opaque to the editor; "user can't edit X"
  is diagnosed as "X is an imperative subtree — convert to template markup."

## 3. Specialized mutation tools (tool semantics match artifact structure)

- `dc_write` — write/rewrite a DC from the three authored pieces; host assembles the file.
  Template **streams into the live preview as it's written**.
- `dc_html_str_replace` — exact-string edit of the template ONLY; streams into the preview.
- `dc_js_str_replace` — edit the logic class ONLY; **hot-reloads in place, state preserved, no
  remount**. Explicitly framed as "iterate with small edits rather than rewriting the file". [L]
- `dc_set_props` — replace the props JSON only.
- Generic `write_file` is **forbidden for artifact content** (allowed only for data/helper files).
  The blunt tool is removed from the surface where it causes thrash, not deprecated-by-advice. [L]
- Multi-edit batching: multiple edits to one file go in ONE atomic call (all-or-nothing);
  parallel file writes are emitted as parallel tool calls in one turn — "do not
  write-then-check-then-write." [P,L]

Anti-pattern list is explicit, enumerated, and each entry carries its failure reason (nested
documents, script tags in body, JS in holes, style-holes that delay paint, premature
componentization, missing size hints). Prompts teach the *why* so the rule generalizes.

## 4. Starter components (the kit catalog)

Host-shipped, copy-into-project scaffolds; the model composes them and NEVER hand-rolls their
domain:
- **deck_stage** — slide-deck shell: scaling, keyboard/tap nav, thumbnail rail with drag-reorder +
  context menu, speaker-notes protocol, print-to-PDF one-page-per-slide, skip-slide, live
  thumbnails. ~1500 lines of hardened web component the model gets for free per deck.
- **device frames** — iOS / Android / macOS window / browser window chrome.
- **animations** — timeline engine (Stage/Sprite/scrubber/easing/persistence).
- **tweaks panel** — full host-protocol tweak shell with ready-made controls.
- **image-slot** — a *designed affordance for missing content*: a drag-and-drop placeholder the
  USER fills, with shape/fit/persist. Instead of the model faking an image (SVG slop) it places an
  honest, functional placeholder. This is the anti-false-affordance move in its purest form. [F]
- **metrics-overlay** — instrumentation overlay (further pattern: overlay reads a snapshot file,
  posts ONE typed message to the host to request recompute; host builds the real query).

Copy semantics: the tool copies the file INTO the project and returns usage notes. Assets from
design systems are copied, not referenced cross-project. Projects stay self-contained.

Precedence rule: bound design-system templates > starter components > hand-rolling. A funnel.

## 5. Skills as recipes (prompt pack)

Domain recipes (deck, doc, PDF, standalone-HTML, PPTX, design-system creation, handoff...) are
loaded on demand (`read_skill_prompt`) rather than resident. Each skill carries:
- The **structure** that makes output export/print cleanly (e.g. the doc skill's LOAD-BEARING
  print-CSS template with per-rule comments explaining what breaks if changed).
- Domain role reframing ("you are a presentation designer... boardroom clarity, not a website").
- Hard numeric floors (min font sizes at 1920×1080; validator enforces ≥24px — a machine-checked
  content rule, not advice). [F]
- Named self-failure modes: "avoid Claude-isms" (verdict-titles, 'It's not X. It's Y.'),
  web-density reflex on slides ("that urge is the web-design reflex. Resist it."), data-slop.
  The prompt teaches the model to recognize its own house style as a defect signature.
- Anti-slop content discipline: no filler, no padding, ask before adding material, placeholder
  over fake imagery, 1000 no's for every yes.

## 6. Verification & finalizer contract

The single most important loop-killer. ONE call ends every piece of work:

`ready_for_verification({path})` =
  (a) opens the file in the user's view,
  (b) waits for load, returns **real console errors + load diagnostics**,
  (c) if clean → forks a **background verifier subagent** with its own iframe/context
      (screenshots, layout probes, JS probing),
  (d) the verifier is **silent on pass** and wakes the main agent only on `needs_work` with a
      specific, actionable description.

Discipline around it:
- If errors come back → fix → call it again. The user must always land on a non-crashing view.
- **Do NOT self-verify before calling it; do NOT proactively screenshot your own work.** The
  verifier catches issues without cluttering the builder's context or blocking the user. [L]
- Don't wait for the verifier — end the turn; summary text goes in the SAME message as the call. [P]
- Trivial changes may skip the verifier fork (but never the load check).
- The verifier has a typed exit: `verification_feedback({verdict, description})`, one call,
  terminate. "needs_work ONLY for real, actionable problems — not nitpicks." [L]

Separation of contexts is the design: builder context stays clean of screenshots/probing;
verification truth comes from a separate context with its own eyes. (= Disco's separate-verifier
thesis, plus the wake-only-on-fail economy.)

## 7. Context management (snip) — the anti-stall mechanism

- Every user message carries an id tag. The model registers **deferred snips** (ranges of resolved
  work) as it goes; the host executes them together only when context pressure builds. [P]
- Registration is cheap and non-destructive; the instruction is to register aggressively and
  early, right after each chunk resolves (resolved explorations, superseded drafts, consumed tool
  output).
- Silent by default. The host, not the model, decides when pressure demands execution.
- Maps directly onto Disco's CXT-3 agent-driven deferred snip + CondensationEvent tombstones —
  we built the substrate; the playbook confirms the policy (agent marks, host executes on
  pressure) and gives the prompt-side habit language. Long-context actionless stall (the dominant
  M3 real-build failure) is attacked HERE. [P]

## 8. Structured intake (questions_v2)

- A typed question form (radio/checkbox/SVG-visual options/slider/file/freeform), streamed, used
  BEFORE building anything new/ambiguous — with mandated always-present options ("Explore a few
  options", "Decide for me", "Other").
- Calibration examples of when NOT to ask (enough info provided → build).
- Design-specific mandate: always confirm the starting point / design system ("starting a design
  without context always leads to bad design"); always ask about variations and what they should
  explore.
- After calling, END THE TURN (it doesn't return an answer inline). No spin-waiting. [P]

## 9. Delivery & export (host-owned single-call, ground-truth flags)

- Every export path is ONE host-owned call: PPTX (`gen_pptx` does capture+fonts+generation+
  download), PDF (print-CSS recipe + `open_for_print`), standalone HTML (deterministic bundler),
  Canva (bundle + short-lived public URL + import), download cards.
- Exports return **validation flags** (duplicate slides, size mismatch, font-swap failure, image
  decode failure) — capture-side ground truth the model must read and judge ("expected for THIS
  deck?") instead of assuming success. Flags are internal diagnostics; the user gets plain
  language, never flag identifiers. [F]
- Preview ≠ delivery ≠ download are distinct tools with distinct rules; the "reading a file does
  NOT show it to the user" distinction is explicit.

## 10. Semantic direct manipulation (the P8 shape)

- **mentioned-element blocks:** when the user clicks/comments/drags an element in the preview, the
  model receives the React component chain + DOM ancestry + a transient runtime handle — enough to
  map a visual gesture to a source edit.
- **Comment anchors:** `data-comment-anchor` attrs pin review comments to elements; edit rules
  require carrying the attr to the semantic equivalent across restructures.
- **Screen labels:** `data-screen-label` on slides/screens so comments arrive pre-localized
  ("which slide is this about"). Host stamps these automatically in deck_stage.
- **Edit-override block:** user's direct style edits live in a dedicated `!important` block; the
  model must edit/remove those rules rather than fight them with inline styles.
- Small-change discipline: "change ONLY that... don't redesign parts you weren't asked to touch;
  prefer str_replace over rewriting; SUGGEST broader changes rather than applying them." [L]
- Versioning by copy: significant revisions copy the file and edit the copy (preserve the old
  version). Cheap, model-driven, no VCS machinery.

## 11. Rails in the deck/doc domain (worth copying wholesale)

- Type scale + spacing as CSS custom properties declared BEFORE any slide is written — commits the
  model to projection-appropriate sizing ("if the values don't feel generous, they aren't"),
  machine-validated floor (≥24px).
- Title-sequence planning: write all titles first, one grammatical style, read them back as a
  table of contents.
- Slide markup rules that keep the direct-edit path alive (leaf elements, written-out repetition).
- 0-index vs human-index: "slide 5" means the 5th slide, never array[4].
- Verifier told which web-instincts are NOT defects on slides (open bottom third is composition,
  not a bug) — verifier calibration, not just builder calibration. [F,L]

---

## 12. Mapping to Disco / disclaude phases

| Playbook mechanism | Disclaude phase | Status vs P0–P7 as built |
|---|---|---|
| DC artifact contract | P2 Contract Runtime | Contract runtime exists; needs the single-mandated-format + host-assembles-document semantics |
| Weak template language + streaming hints | P2/P4 | Not built — ours is generic file mutation |
| dc_* mutation tools, write_file ban on artifacts | P4 Specialized Mutation Tools | TOOL-1 AppKit set exists; needs streaming edit + hot-reload semantics + blunt-tool removal |
| Skills/recipes on demand | P3 WorkflowPromptPack | WPP built; needs per-domain recipes w/ load-bearing structure + self-failure-mode language |
| ready_for_verification finalizer | P5 Delivery + P6 Finalizers | Built (host-owned finalizer); needs the one-call bundle + silent-on-pass verifier economy |
| Background verifier subagent, typed verdict | P1 Harness / P6 | Oracles exist headless; the builder/verifier context split + wake-on-fail is the gap |
| Starter component catalog | P7 Kits | P7 committed (code-gate-pending); compare against deck_stage-class depth |
| snip deferred compaction | P0 Context Runtime | CXT-3 substrate built; adopt register-aggressively policy + host-pressure execution |
| questions_v2 intake | (new, small) | Not built |
| mentioned-element / comment anchors / edit-overrides | P8 Semantic Direct Manipulation | Not started — this doc is the P8 spec seed |
| Export validation flags | P10 Export/Handoff | Partially (P10b export smoke); add capture-side flags the model must judge |
| Versioning-by-copy, small-change discipline | P9 TweakSpec / prompts | Prompt-side, cheap |

## 13. Failure-mode kill matrix (what we must be able to demonstrate)

- **[P] Pauses:** streaming-first artifact + parallel-writes-one-turn + end-turn-don't-wait +
  aggressive deferred snip + intake-form-then-end-turn. Metric: zero actionless stalls in soak.
- **[L] Loops:** small-edit tools with hot-reload + write_file ban on artifacts + no-self-verify
  rule + silent-on-pass verifier + fix-and-recall-finalizer as the ONLY retry shape + small-change
  discipline. Metric: zero rewrite-thrash / verify-spin in soak.
- **[F] False affordances:** host-assembled documents + load-check with real console errors +
  separate-context verifier with screenshots + machine-validated content floors + export
  validation flags + image-slot-style honest placeholders + static-markup editability rules.
  Metric: zero looks-done-but-broken deliverables in adversarial review of soak artifacts.
