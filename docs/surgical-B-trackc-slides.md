# Surgical Plan — Track C: Slides + Artifacts pipeline (build-ready)

**Date:** 2026-06-18
**Status:** Standalone surgical plan. Verified against CURRENT code; every `file:line` re-located.
**Scope:** Track C only (slides + artifacts + image-gen + render/preview fixes + experiment + `artifact_mode`).
The in-browser editor + element→agent substrate (decisions doc §4) is **Phase 5 — NOT in this plan**.
**Source:** `docs/disco-direction-and-decisions-6-17-26.md` §3 (C1–C8), re-verified.
Companion: `docs/dylans-runthru-6-17-26-v2.md` (§H + Wave-3 A/B/C decision), `source-accuracy-testing.md`.

> **LINE-NUMBER DISCIPLINE.** Every `file:line` below was re-located against the working tree on
> 2026-06-18 and corrected where the §3 spec had drifted (corrections called out inline as
> `was §3:NNN → now :MMM`). They will drift again — treat the **symbol + behavior** as authoritative,
> the line as a hint. Use Serena `find_symbol` before editing.

---

## 0. Current-state verification (what the plan builds on)

| Asset | §3 claim | Verified state (2026-06-18) |
|---|---|---|
| **§1 brand engine** | "DONE, lives at `core/brand/`" | **CONFIRMED DONE.** `packages/core/src/disco/core/brand/` = `__init__.py`, `tokens.py`, `css.py`, `mark.py`, `fonts/`. Public surface exactly: `Theme, THEMES, resolve_theme, font_face_css, theme_css_vars, print_skeleton_css, definition_mark_html, wordmark_html`. ⚠ `resolve_theme(name, mode="light")` is a **two-arg** signature — §3 C1's `resolve_theme(authored.theme)` pseudocode must split `"disco-light"`→`("disco","light")`. |
| **`chart_svg.py`** | "just added, reusable for slide charts" | **CONFIRMED EXISTS** at `packages/agent-server/src/disco/agent_server/chart_svg.py`. Public: `Palette`, `palette_from_theme(theme)`, `render_chart_svg(spec, pal)->str\|None`, `render_chart_table(spec)->str`; renderers bar/line/pie/scatter (`_render_*`). ⚠ **LAYERING TRAP (new finding):** it lives in **agent-server (a TOP layer)**. The deck renderer (C3/C8) lives in **tools (lower)**. `tools → agent_server` is an **illegal upward import** (only the one whitelisted audio edge exists). **C8 cannot import `chart_svg` from the tools deck renderer.** See C8 for the resolution (move down to `core`, or don't reuse it). |
| **`artifact_mode` (C6)** | "DEFINED in `_common.py`, NOT wired" | **CONFIRMED.** Field present: `CreateConversationBody.artifact_mode: bool = False` at `_common.py:100`. **Consumed NOWHERE** — grep finds only the definition + comment. No `_artifact_mode` / `set_artifact_mode` / `artifact_scope` anywhere in `runtime.py` or `registry.py`. C6 = field done, all wiring pending. |
| **`ImageBackend` seam (C7)** | "Protocol + injected ctor exist" | **CONFIRMED.** `ImageBackend` Protocol `image_gen.py:136-158`; `_PILProceduralBackend` `:170`; injected ctor `ImageGenTool.__init__(backend=None)` `:286-290`. ⚠ Registration line: `image_gen.py:99` was §3's claim but `builtin/__init__.py:99` is **`SlidesTool()`** — `ImageGenTool()` is registered at **`builtin/__init__.py:102`**. |
| **`slides.py`** | "Marp-only, image-per-slide PPTX" | **CONFIRMED.** Single arg `markdown: str` (`slides.py:29`); `_split_slides` on `\n---\n` (`:207`); `_basic_md_to_html` (`:107`) or marp CLI; PPTX = `marp --pptx` image-per-slide (`:291`, note strings `:9,:312-313,:406-410`). Structured = `{filename,base_name,format,slide_count,renderer}` (`:421-427`). **Reusable:** the sandbox-jailed marp runner (`_marp_render_in_sandbox` `:280`, `_marp_available` `:270`) + `_fallback_html` (`:218`) + `_split_slides` — keep ALL as the C2 fallback path. |
| **K1 (`_snip_args` guard)** | "GATES Track C; done elsewhere" | ⚠ **PARTIAL — must confirm before any Track-C path ships.** The marker renderer `_snip_args` (`events.py:300`), the structural detector `_ELISION_MARKER_RE` (`events.py:297`), and the pure guard helper **`find_elided_arg_markers(arguments)` (`events.py:312-322`)** all EXIST. **But the helper is NOT yet wired into the tool executor** — grep finds zero call-sites in `packages/tools/` or the agent-server dispatch. **K1's detection primitives are done; the execution-guard call-site (reject/rehydrate before dispatch) is NOT.** This is a Track-A deliverable; verify it is green before merging C1–C8. |

**`_ARTIFACT_TYPES`** (`_common.py:48-60`): `.pptx` and `.html` already allowlisted → C3 outputs are download-reachable with no allowlist change. Currently every type served **`attachment`** (the deliberate no-inline posture, `:43-47`) — C5 adds the one narrow exception.

---

## C4 — constrained-vs-free experiment harness  **(GATE; decision-independent; runs FIRST after K1)**

- **ID · approach.** Standalone offline harness that empirically picks the AUTHORING emit-strategy and **freezes the `AuthoredSlide` field set** before C1-Layer1 / C2 prompts are written. This is the answer to Dylan's warning that *"we've previously solved issues by making them LESS deterministic; not sure more-deterministic (constrained schema) is viable."* The verdict, not an assumption, decides it.
- **Files (current, verified).** New tree only: `harness/slides_experiment/` (repo already has top-level `harness/`). Reuses C1's `_fit_text` for the overflow scorer → C1's lowering skeleton must exist first. Output: **`docs/slides-experiment-verdict.md`**.
- **The change.** Matrix = **15–20 prompts** (simple / data-heavy / image-heavy / long-content overflow-stress) **× 3 emit strategies** {free-form prose-deck; rigid grammar-constrained JSON; loose-hybrid instructed-JSON (the proposed default)} **× 2 models** {`gpt-oss-120b` via OpenRouter-free; weakest local tier}. Three scorers: (1) **overflow rate** — deterministic, reuse C1 `_fit_text`, count font-floor hits + continuation splits; (2) **LLM-judge content** — rubric judge (gpt-oss-120b in dev), using the `source-accuracy-testing.md` *judge-not-reranker* discipline; (3) **blind human visual** — render each deck, present unlabeled, rank (Firefox screenshots). Verdict picks the strategy maximizing judged content quality **without collapsing weak-model success rate**, and records the FROZEN `AuthoredSlide` fields.
- **New files.** `harness/slides_experiment/{prompts/*.json, strategies.py, scorers.py, run.py}` + committed prompt fixtures; `docs/slides-experiment-verdict.md` (the deliverable).
- **Tests.** Overflow scorer unit-tested against known-overflow fixtures; runner asserts every cell produced a parseable deck OR recorded an explicit failure (no silent gaps).
- **Acceptance.** `docs/slides-experiment-verdict.md` exists, NAMES the frozen authoring schema, and records per-cell scores + blind ranks. Until it lands, C1-Layer1 + C2 prompts are PROVISIONAL.
- **Risk.** None to production (fully offline). Schedule risk only.
- **Dependencies.** K1 (gate); C1 lowering skeleton (overflow scorer); real FREE-model driver access (no cassettes — real-only per the verification protocol).

---

## C1 — `_deck_schema.py`: two-layer schema + deterministic lowering  **[SCHEMA-PENDING-C4-VERDICT]**

- **ID · approach.** A new pure-data module holding BOTH Pydantic layers + a deterministic `lower_deck()`. Layer 1 (authoring) is what the LLM emits — loose/semantic, NO coordinates. Layer 2 (deck) is precise/positional (PPTist-shaped) and is what the renderer (C3) + the future editor consume. **Build Layer 2 + the lowering skeleton NOW; finalize Layer-1 fields AFTER the C4 verdict.**
- **Files (current, verified).** New file `packages/tools/src/disco/tools/builtin/_deck_schema.py`. Imports **downward** to `disco.core.brand.resolve_theme` (legal: `core ← tools`). No cross-package upward import. Consumed by `slides.py` (C2/C3) and `harness/slides_experiment/` (C4).
- **The change (the shape; finalize Layer-1 after C4).**
  - **Layer 1 — authoring (LLM emits, instructed JSON, NOT grammar-constrained):** `AuthoredDeck{title, theme: Literal["disco-light","disco-dark","neutral"], slides: list[AuthoredSlide]}`; `AuthoredSlide{type:str, title:str, body:list[str]=[], layout_hint: LayoutHint|None, image_prompt:str|None, notes:str|None, chart: ChartSpec|None, table: TableSpec|None}`. ⚠ The `image_prompt`/`layout_hint`/`notes` set is **C4-gated** — keep the whole model in this one file so a schema change is one edit.
  - **Layer 2 — deck (renderer + editor consume, precise/positional):** `Element{id, kind:Literal["text","image","shape","line","chart","table","latex"], left/top/width/height:float (EMU), rotate, lock, text, runs:list[TextRun]|None, src, align, role}`; `Slide{id, type, layout:LayoutHint (resolved, never None), elements:list[Element], background, notes}`; `Deck{id, title, theme:ThemeTokens (from core/brand), size=(12192000,6858000) 16:9 EMU, slides}`.
  - **`lower_deck(authored, theme) -> Deck`** — PURE; owns overflow/fonts/16:9: (1) resolve theme via `core.brand.resolve_theme(name, mode)` — **split `"disco-light"`→`("disco","light")`** (two-arg signature, verified); (2) `_infer_layout(slide)` total/deterministic on field presence (`image_prompt`→`image_right`; title-only→`section_header`; paired body→`two_column`/`comparison`; `chart|table`→`metrics`); (3) per-layout pure `layout_<name>(slide, theme, canvas) -> list[Element]` returning absolutely-positioned EMU boxes (≤200 LOC each — arch budget); (4) `_fit_text(runs, box, theme)` measures, steps font down within `[min,max]` per role, splits `body` to a `type="<type>_cont"` continuation slide on overflow.
- **New files.** `_deck_schema.py` (the only one).
- **Tests.** `lower_deck` deterministic (same input → byte-identical `Deck`); every `layout_*` keeps elements in-canvas; a 30-bullet slide splits; `_infer_layout` total; theme tokens = exact `core/brand` values (no invented colors).
- **Acceptance (no LLM in loop).** A hand-written `AuthoredDeck` fixture → a `Deck` of positioned, typed, in-bounds, theme-resolved elements.
- **Risk.** LOW-MED (pure data + math; text measurement is the subtlety — start conservative, refine against real PPTX in C3).
- **Dependencies.** K1 (gate); §1 `core/brand` (DONE). **Authoring-field finalization ← C4 verdict.**

---

## C2 — staged generation pipeline + tier-aware prompt  **[SCHEMA-PENDING-C4-VERDICT]**

- **ID · approach.** Replace "model authors raw Marp markdown" with a staged generator that emits the C1 authoring schema, then deterministically lowers+renders. Tier-aware framing via the EXISTING `ctx.assist` signal (threaded at `runtime.py:1139` into the executor; consumed in tools). Keep the Marp path as the defensive fallback — never hard-fail.
- **Files (current, verified).** `slides.py` (`SlidesTool.run` at `:322` — **was §3:322, confirmed**) gains a `deck` branch beside the kept markdown path; spill into a sibling `_slides_pipeline.py` if `slides.py` nears the size cap. `ctx.assist` arrives via `DefaultToolExecutor(..., assist=self._effective_assist(conversation_id))` (`runtime.py:1139`).
- **The change.** Pipeline: `outline` (titles only → `AuthoredDeck.slides[].{type,title}`) → `pick` (`_infer_layout`/honor hint) → `fill` (LLM fills `body[]/image_prompt/notes`, instructed JSON) → `assets` (per `image_prompt` call `image_generate`, C7) → `render` (`lower_deck` → C3). **Defensive parse:** JSON failure → ONE retry with a "return ONLY valid JSON" reminder → second failure → fall back to the kept Marp path (`_render_with_marp`/`_fallback_html`). **Tier prompt keyed on `ctx.assist`:** capable → compact schema + "≤6 bullets/slide"; weak → a FULL worked-example `AuthoredDeck` JSON + "copy this structure" (the §3 quality finding: constraining open-weight emit costs 3–30 pts; an 8B hit 0% under rigid JSON → instructed JSON, never constrained decoding). Add `deck: AuthoredDeck|None` + `mode:Literal["deck","markdown"]="deck"` to `SlidesGenerateArgs` (`slides.py:26`); KEEP `markdown` for back-compat with the `slides?` activity render (`buildTrace.ts:51-57,145-150`). Keep `structured` a superset of `{filename,base_name,format,slide_count,renderer}` so the frontend keeps working.
- **New files.** Optional `_slides_pipeline.py` (size-cap relief); prompt constants (adapt Presenton's `generate_presentation_outlines.py`/`generate_slide_content.py`, Apache-2.0, **restyle — transcribe nothing**).
- **Tests.** Goal → valid `AuthoredDeck` (mocked LLM) → renders; malformed JSON → one retry → Marp fallback, no crash; weak tier gets the worked example; `image_prompt` slides invoke `image_generate` once each; `structured.slides[]` present.
- **Acceptance.** `slides_generate(goal=...)` on the REAL dev driver (gpt-oss-120b-free) → a fitted multi-slide deck end-to-end with images. Firefox screenshot.
- **Risk.** MED (LLM-output parsing is the fragile seam; Marp fallback + single-retry are the net).
- **Dependencies.** C1, C7, **C4 verdict**; K1.

---

## C3 — `_pptx_render.py`: native EDITABLE PPTX + PDF(LibreOffice) + 16:9 brand HTML  **(decision-independent)**

- **ID · approach.** New renderer turning the C1 `Deck` into a **native editable .pptx via python-pptx (BSD)** — REAL text boxes/shapes/images, NOT image-per-slide — plus PDF via LibreOffice headless and a self-contained 16:9 brand HTML for the Artifacts pane. This is the spec's central deliverable; it replaces the image-per-slide weakness.
- **Files (current, verified).** New `packages/tools/src/disco/tools/builtin/_pptx_render.py`; `slides.py` render branch calls it (the marp branch `_render_with_marp` `:372` stays as fallback). PDF runs through `ctx.sandbox.exec_shell` (same jailing rationale as the marp comment `slides.py:261-267`). `.pptx`/`.html` already in `_ARTIFACT_TYPES` (`_common.py:50,52`) → no allowlist change. python-pptx is **NOT** currently a dep (confirmed: absent from `packages/tools/pyproject.toml`) — add it. LibreOffice/`soffice` is **NOT** in `deploy/sandbox/Dockerfile` (confirmed absent) — add it.
- **The change.** **PPTX:** build a python-pptx `Presentation` at 16:9 EMU; per `Element` add the real object — `text`→`add_textbox` with runs (font from `core/brand/fonts/` OFL set, size from C1 `_fit_text`, color = theme token, align), `image`→`add_picture`, shape/line/accent-rule→`add_shape`/connector with the accent token, notes→`notes_slide`. One `render_layout_<name>(slide_obj, slide_model, theme)` per layout, each ≤200 LOC (arch budget — do NOT game the allowlist; `_pptx_render.py` is THE size-risk file). **PDF:** write the .pptx, then `soffice --headless --convert-to pdf --outdir . deck.pptx` via `ctx.sandbox.exec_shell`; absent/timeout → clean failure mirroring the marp-absent branch (`slides.py:359-368`). **HTML:** one absolutely-positioned `<section class="slide">` per `Slide`, §1 brand CSS + OFL `@font-face` inlined, keyboard nav (←/→) inline — this is the file C5's `?inline=true` route previews. All output via `ctx.sandbox.write_file(path, bytes)` — **.pptx is BINARY: pass raw bytes, never `.encode()`.**
- **New files.** `_pptx_render.py` (`render_pptx(deck)->bytes`, `render_html(deck)->str`, per-layout fns, `convert_to_pdf(ctx,name)->(ok,err)`). Edits: `packages/tools/pyproject.toml` (+`python-pptx`); `deploy/sandbox/Dockerfile` (+LibreOffice). Delete the false notes at `slides.py:9,:312-313,:406-410` ("image-based / editable PPTX is v2"). `structured.renderer` reports `"pptx-native"`/`"libreoffice"`/`"html-brand"`.
- **Tests.** Render a `Deck` fixture → open the .pptx with python-pptx, assert text frames hold real titles/bullets (NOT one picture); HTML has one positioned section/slide + inlined fonts + 16:9; PDF ok with soffice present, clean failure without; binary round-trip unchanged.
- **Acceptance (VISUAL EVIDENCE MANDATORY).** Agent → 5-slide deck → `.pptx` **opens AND is text-editable in LibreOffice/PowerPoint (real text boxes, not images)** + `.pdf` renders + 16:9 `.html` renders inline and keyboard-navigates. Firefox screenshot + the OPENED editable .pptx, SendUserFile.
- **Risk.** MED (python-pptx geometry + LibreOffice availability; marp/markdown fallback bounds it). Size-risk for arch-budget — keep each layout a separate function.
- **Dependencies.** C1; §1 `core/brand` fonts (DONE); K1; sandbox image (LibreOffice).

---

## C5 — render/preview fixes (deriveSrcDoc null · "0 B" · `?inline=true`)  **(INDEPENDENT — can land FIRST)**

- **ID · approach.** Three CONFIRMED bugs blocking ANY server-side HTML deck from previewing; disjoint files from C1–C3, so this can land first and fix even the existing Marp-HTML blank frame.
- **Files (current, verified) + the three bugs.**
  1. **Blank iframe.** `deriveSrcDoc` (`buildTrace.ts:370`) returns `entry.content`. **Verified:** for server-side artifacts `deriveFiles` sets `content=""` (`buildTrace.ts:347` slides/sheets, `:353` deliverables); so `html = entry.content = ""` and the function **returns `""`, not `null`**. Consumers guard on `srcDoc != null` (`PreviewPane.tsx:120,122,164,215,244`) → an iframe with `srcDoc=""` → blank white frame instead of the live-server/placeholder fallback. *(Note: `deriveSrcDoc`'s return type is already `string | null` (`:370`) and it returns `null` for the no-entry cases — the bug is specifically the empty-content `.html` path returning `""`.)*
  2. **"0 B".** `FilesPane.tsx:76` renders `{file.bytes} B`; `file.bytes = TextEncoder().encode(content).length = 0` for the `content=""` artifacts (`buildTrace.ts:359`) → misleading "0 B" on real files (the `(empty)` body text is at `FilesPane.tsx:79`).
  3. **No inline render.** `artifact_file` (`files.py:165`) serves EVERY type as **`attachment`** (`files.py:208`, by design `_common.py:43-47`) → a server-side brand HTML deck can never preview.
- **The change.** (1) `deriveSrcDoc`: after picking `entry`, `if (!entry || !entry.content) return null;`. (2) `FilesPane.tsx:76`: show size only when known — `{file.content ? `${file.bytes} B` : "server-side"}` (consistent with the `(empty)` text at `:79`). (3) `artifact_file`: add `inline: bool = False`; when true REQUIRE `ext==".html"` (else 404; keep the declared-artifact jail `:181`, traversal-normalize `:196-197`, 50 MB cap `:201`), replace `attachment` (`:208`) with `inline`, KEEP `X-Content-Type-Options: nosniff` (`:209`), ADD `Content-Security-Policy: sandbox allow-scripts; default-src 'none'; style-src 'unsafe-inline'; img-src 'self' data:; font-src 'self' data:` + `frame-ancestors 'self'`. (4) `PreviewPane.tsx`: when `srcDoc==null` but a declared `.html` artifact exists, render an iframe `src=.../artifacts/<path>?inline=true` with `sandbox="allow-scripts"` and **NO `allow-same-origin`** — distinct from the same-origin live-proxy iframe (`:203-207`, which keeps `allow-same-origin allow-forms allow-popups` for dev-server assets). Keep the untrusted-run `sandbox=""` (`:244`).
- **New files.** None. Edits: `buildTrace.ts` (`deriveSrcDoc`), `FilesPane.tsx:76`, `files.py` (`artifact_file`), `PreviewPane.tsx`. Tests: `buildTrace.test.ts`/`srcdoc.test.ts` + `ExecutionCanvas.preview.test.tsx`.
- **Tests.** `deriveSrcDoc` → `null` for content-empty `.html`, real HTML for inline-content; no "0 B"; route 404s `?inline=true` for non-HTML, serves HTML with `inline`+`nosniff`+CSP and no `attachment`; iframe has `allow-scripts` WITHOUT `allow-same-origin`.
- **Acceptance (VISUAL EVIDENCE MANDATORY).** A server-side 16:9 HTML deck renders inline in the Artifacts/Preview pane — no blank frame, no "0 B"; the inline route refuses non-HTML and never sets `allow-same-origin`. Firefox screenshot.
- **Risk.** LOW-MED — inline HTML is the sensitive surface; HTML-only allowlist + sandbox-without-same-origin + CSP + declared-artifact jail are the controls. **Do NOT broaden the inline allowlist beyond `.html`.**
- **Dependencies.** K1. Independent of C1–C3 (lands first to fix the existing Marp-HTML blank frame); best paired with C3.

---

## C6 — `artifact_mode` flag wiring (NO surface split)  **(INDEPENDENT)**

- **ID · approach.** A per-conversation flag that branches the EXISTING build-like machinery (`_compose_build_loop`) to a low-friction artifact path: `NeverConfirm` gate + `OperatingMode.INTERACTIVE` (no plan-approval) + a narrowed `artifact_scope()` (NO shell/browser/plan-gate). Plumbed exactly like the existing `depth`/`assist`/`autonomous` overrides. The #33 decision: a FLAG, not a new surface. **The body field is already present (`_common.py:100`) but consumed nowhere — this item wires it.**
- **Files (current, verified — §3 line refs CORRECTED).**
  - `_compose_build_loop` is at **`runtime.py:1091`** (was §3:1070). Inside it: `agent_scope(model_caps=...)` at **`:1129`** (was §3:1094); the gate **`BlastRadiusConfirm()`** at **`:1194`** (was §3:1162); **`OperatingMode.PLANNING`** at **`:1205`** (was §3:1173); `planning_tools=frozenset({...})` at **`:1206-1208`**; `autonomous=self._effective_autonomous(...)` / `assist=self._effective_assist(...)` at `:1213-1214`.
  - Mirror plumbing: `_effective_autonomous` `runtime.py:876`, `_effective_assist` `:895`, `set_depth` **`:1227`** (was §3:1195), `_BUILD_LIKE_SURFACES` **`:696`** (was §3:690).
  - Body field DONE: `CreateConversationBody.artifact_mode: bool = False` `_common.py:100`.
  - Scopes: `agent_scope()` `registry.py:119`, `research_scope()` `:115`, `AGENT_TOOLS` **`registry.py:63-105`** (was §3:61-99). No `artifact_scope`/`ARTIFACT_TOOLS` yet.
- **The change.** (1) `registry.py`: `ARTIFACT_TOOLS = frozenset({"file_read","file_write","file_append","file_edit","file_replace_lines","file_insert_lines","file_list","search","extract","sheet_generate","slides_generate","image_generate","audio_overview","think"})` — a **strict subset of `AGENT_TOOLS`**, with NO `shell*`/`browser`/`deploy_preview`/`server_status`/`submit_plan`/`plan_step`/`delegate_explore`/`code_exec`/`file_str_replace` (⚠ §3's proposed set omitted the line-edit tools `file_replace_lines`/`file_insert_lines` — INCLUDE them so artifacts stay editable) + `def artifact_scope() -> ToolScope`. (2) runtime state mirroring depth: `self._artifact_mode: dict[str,bool]` + `set_artifact_mode(cid,on)` + `_effective_artifact_mode(cid)`. (3) Branch `_compose_build_loop` when on: `artifact_scope()` instead of `agent_scope(...)` (`:1129`), `NeverConfirm()` instead of `BlastRadiusConfirm()` (`:1194`), `OperatingMode.INTERACTIVE` instead of `PLANNING` (`:1205`), drop/relax `planning_tools` (`:1206`); everything else identical. (4) Call `set_artifact_mode` in the create-conversation handler where `set_depth` is called, reading the (already-present) body field. (5) Agent-surface toggle (mirror the depth picker) + TS field mirror.
- **New files.** None (edits to `registry.py`, `runtime.py`, the create-conversation route handler, frontend toggle + TS type).
- **Tests.** `artifact_mode=True`: executor scope == `ARTIFACT_TOOLS` (NO shell/browser), gate == `NeverConfirm`, mode == `INTERACTIVE` (assert via the `_compose_build_loop` test pattern in `test_sandbox_config.py`); junk value 422s at the edge (already a `bool` field → Pydantic enforces); OFF → byte-identical loop. Frontend: toggle sets the field; vitest.
- **Acceptance.** An Agent-surface conversation with `artifact_mode` runs slides/sheet/image with NO plan-approval gate and NO shell/browser tools, identical machinery otherwise. Off = unchanged.
- **Risk.** LOW-MED (loop composition; branch is small/additive; `NeverConfirm`/`INTERACTIVE` already exist). **Guard:** artifact mode must NOT silently grant shell/browser — the `ARTIFACT_TOOLS ⊂ AGENT_TOOLS` intersection is the boundary.
- **Dependencies.** K1. Independent of C1–C5, C7.

---

## C7 — image-gen backends into the existing `ImageBackend` seam  **(INDEPENDENT; overlaps product idea #1)**

- **ID · approach.** Concrete backends behind the EXISTING Protocol + a `select_image_backend()` factory choosing by configured secrets, defaulting keyless. NO change to the tool's `run()` (the point of the seam). **Overlap note:** this is also the standalone image-gen-as-tool product feature — spec'd here as the seam; the Settings CRUD for provider keys reuses the existing `/api/secrets` store (no new secrets surface).
- **Files (current, verified — line CORRECTED).** `image_gen.py`: Protocol `:136-158`, `_PILProceduralBackend` `:170`, injected ctor `:286-290`, binary `isinstance(bytes)`/magic-byte guards `:328+`/`:370-389` — all unchanged. Registration: change **`builtin/__init__.py:102`** (`ImageGenTool()`) — ⚠ NOT `:99` (that's `SlidesTool()`) — to `ImageGenTool(backend=select_image_backend())`. Secrets via `app-server/.../routes/secrets.py` (`/api/secrets`, encrypted at rest).
- **The change.** Backends (each speaks the Protocol, returns raw `bytes`): keep `_PILProceduralBackend` (keyless fallback); add `_ComfyUIBackend` (keyless/self-host POST+poll+fetch PNG); `_OpenAIImageBackend`/`_GeminiImageBackend` (paid; key from the secrets store, **NEVER hardcode/log**); optional `_PexelsBackend`/`_PixabayBackend` (paid stock fetch). `select_image_backend(secrets, config)` prefers an explicit provider, else ComfyUI if a host is set, else procedural — **paid default-OFF** (no key → never selected). Binary safety preserved by the tool's existing guards; new backends MUST NOT `.encode()`. C2 wires `image_prompt` per slide → `Element(kind="image")`.
- **New files.** None (backends + factory inside `image_gen.py`; lazy-import SDKs/`httpx` mirroring the PIL lazy-import). Edits: `builtin/__init__.py:102`; Settings UI for provider keys (reuse `/api/secrets` CRUD).
- **Tests.** Factory selects keyless with no secret, the configured provider with one, NEVER paid without a key; each backend returns bytes passing the magic-byte guard; a stub round-trips through `run()`; `image_prompt` slides get an `Element(kind="image")`. No real paid calls in unit tests.
- **Acceptance.** With a key, `image_generate` produces a real generative image; without, the keyless default still produces a valid PNG; a deck with `image_prompt` slides renders real images. Firefox screenshot.
- **Risk.** LOW-MED — secret handling is sensitive (existing encrypted store + per-request overlay; never persist/log, per CLAUDE.md). Paid default-OFF.
- **Dependencies.** K1. Independent except C2 consumes it. Existing `/api/secrets` CRUD.

---

## C8 — data-report layouts (charts/tables)  **(FAST-FOLLOW on C1–C3; decision-independent)**

- **ID · approach.** Fill the C1 `ChartSpec`/`TableSpec` stubs, add `chart`/`table`/`metrics` layouts, render charts in all three formats. Per the locked "Chart.js-not-Vega" decision: HTML via inlined Chart.js (MIT, bundled local, NO CDN), PPTX via python-pptx native charts, PDF via the LibreOffice conversion. Rides C1→C2→C3, no new pipeline.
- **Files (current, verified) + the chart_svg layering finding.** `_deck_schema.py` (fill stubs + chart layouts); `_pptx_render.py` (chart/table renderers); C2 prompt (allow `chart`/`table` payloads). ⚠ **`chart_svg.py` reuse is BLOCKED by layering.** `packages/agent-server/.../chart_svg.py` (`render_chart_svg(spec,pal)`, `render_chart_table(spec)`, `palette_from_theme`) is genuinely reusable code, but it sits in **agent-server (top layer)** while the deck renderer is in **tools (lower)** — `tools → agent_server` is an illegal upward import (lint-imports gate). **Resolution options:** (A) **MOVE `chart_svg.py` down to `core/` (e.g. `core/charts/`)** so BOTH the agent-server report path AND the tools deck renderer import it downward — preferred, makes it a true shared asset like `core/brand`; (B) reimplement the SVG charting inside tools (duplication — avoid); (C) for the deck path use Chart.js (HTML) + python-pptx native charts (PPTX) and DON'T reuse `chart_svg` at all (works, but loses the shared SVG renderer for static/PDF chart fallback). **Recommendation: (A)** — relocate `chart_svg` to `core` as part of C8, then both consumers share it; re-run the arch-diagram + lint-imports gates after the move.
- **The change.** `ChartSpec = {kind:"bar"|"line"|"pie"|"scatter"|…, labels:[…], series:[{name,data:[…]}], title?}` (align fields with `chart_svg`'s `spec` shape if reusing it). Lowering places a `chart`/`table` `Element`. HTML: `<canvas>` + inlined Chart.js config (brand token colors) — or inlined `render_chart_svg` output if going route (A). PPTX: `slide.shapes.add_chart(...)` with `CategoryChartData` (native editable), image-fallback for unsupported types. Tables: `add_table` (PPTX) / `render_chart_table` (HTML).
- **New files.** Possibly `core/charts/` (relocated `chart_svg`) if route (A); Chart.js vendored locally (MIT). Edits: `_deck_schema.py`, `_pptx_render.py`, C2 prompts, + ≥1 data-heavy prompt added to C4's set.
- **Tests.** A `ChartSpec` renders a `<canvas>`+config in HTML and a native chart in the .pptx (chart part exists); a `TableSpec` renders a real table; brand colors applied; (route A) `chart_svg` import is downward-only (lint-imports passes).
- **Acceptance.** A data deck with a bar chart + metrics slide renders editable in PPTX, interactive in HTML. Firefox screenshot.
- **Risk.** LOW-MED (python-pptx native charts have type gaps → image-fallback; the `chart_svg` move touches cross-package imports → re-run arch gates). Fast-follow — does not block C1–C3.
- **Dependencies.** C1, C2, C3; K1. If reusing `chart_svg`: the relocation to `core`.

---

## Ordered build sequence

```
K1  (Track A — _snip_args EXECUTION-GUARD wiring; detector primitives done, call-site NOT)
      └─ HARD GATE — confirm green before ANY Track-C item ships.
        │
        ├─ C4  experiment harness ──── starts immediately after K1; PARALLEL-OK
        │        └─ docs/slides-experiment-verdict.md  (FREEZES the AuthoredSlide field set)
        │
        ├─ C5  render/preview fixes ── INDEPENDENT, can land FIRST (fixes existing blank frame)
        ├─ C6  artifact_mode wiring ── INDEPENDENT (body field already present)
        └─ C7  image-gen backends ──── INDEPENDENT (own seam)
                 (C5/C6/C7 touch disjoint files → 3 parallel workers once K1 lands)
        ▼
  C1  _deck_schema.py  — Layer-2 + lowering skeleton NOW; Layer-1 fields after C4 verdict
        ▼
  C2  generation pipeline + tier prompt  (consumes C1, C7; gated by C4)
        ▼
  C3  _pptx_render.py  — native editable PPTX + PDF(LibreOffice) + 16:9 brand HTML
        ▼
  C8  data-report layouts (charts/tables)  — FAST-FOLLOW; reuses C1–C3 machinery
```

- **Critical chain:** C1 → C2 → C3 (each consumes the prior). C4 gates C1-Layer1/C2's authoring fields.
- **Parallelizable after K1:** C4, C5, C6, C7 (disjoint files; separate workers).
- **Fast-follow:** C8 (after C1–C3; +the `chart_svg`→`core` relocation if reusing it).
- **Fitness gates per Python item:** `.venv/bin/python3 -m pytest -m "not integration"` (exit code is truth) · `uv run basedpyright` (0) · `uv run lint-imports` · `uv run python scripts/check_arch_budget.py` (no class >800 / func >200 LOC — keep each `layout_*`/`render_layout_*` a separate ≤200-LOC fn) · `uv run python scripts/gen_arch_diagram.py --check` (re-run without `--check` after C8's cross-package move). Frontend (C5/C6): `npm run typecheck:build` + `npx vitest run` + `npx vite build`. **Visual evidence MANDATORY** on every UI/artifact item — real **Firefox** screenshot in the running app + SendUserFile (green vitest ≠ evidence; for C3 the opened editable .pptx is part of the evidence).

---

## The open decision, restated

**Slides architecture A / B / C is UNDECIDED** (decisions doc §3 / runthru-v2 §H, Wave 3). Dylan's warning stands: *"we've previously solved issues by making them LESS deterministic; not sure more-deterministic (constrained schema) is the viable solution here — investigate."*

- **A** = Presenton FastAPI sidecar (markdown-driven, weaker schema).
- **B** = lean in-repo python-pptx typed-schema tool (this plan's C1–C3).
- **C** = hybrid (B for slides + sidecar for images/reports).

**This plan does NOT pre-decide A/B/C.** It makes the decision-INDEPENDENT items shippable now (C3, C5, C6, C7, C8) and treats the schema-bound items (C1 Layer-1 authoring fields, C2 prompts) as **SCHEMA-PENDING-C4-VERDICT** — the shape and seams are specified, but the exact `AuthoredSlide` field set is frozen ONLY by the C4 experiment (`docs/slides-experiment-verdict.md`), which runs first and decides constrained-vs-free empirically rather than by assumption. C4 is the gate that converts Dylan's open question into evidence.
