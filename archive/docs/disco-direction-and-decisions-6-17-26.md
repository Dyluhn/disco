# Disco — Direction & Decisions (Source of Truth for Surgical Planning)

**Date:** 2026-06-17
**Status:** Decisions LOCKED except the build-harness *depth* call (pending 2 more OSS research rounds).
**Purpose:** This is the document we build directly off of. It captures every decision made,
the rationale, and the exact OSS + in-repo references each section needs. It supersedes the
ad-hoc plan in `docs/dylans-runthru-6-17-26-v2.md §6` (which it absorbs); the verbatim issue
list and Dylan's confirmations remain authoritative in that file's §1/§4.

**Companion docs:**
- `docs/dylans-runthru-6-17-26-v2.md` — verbatim runthrough issues (§1), Dylan's confirmations + nuances (§4).
- `docs/build-harness-review-6-17-26.md` — the 2-reviewer unbiased build-loop root-cause consensus (§7).
- `docs/runthru-v2-research-digest.md` — R1–R5 research lane distillations.
- `docs/evidence/macos-build-trace-6-17-26.txt` — the 374-event golden trace driving the metric table.
- `docs/evidence/brand-mockup-*.png` — the rendered brand mockups (light/dark/definition mark).
- `source-accuracy-testing.md` — grounding-grader fairness baseline + iterative-mode verdict.

---

## 0. The three-track shape

```
TRACK A — BUILD HARNESS   (the breaking issue; engine risk; highest priority)
TRACK B — DEEP RESEARCH   (fully parallel; low risk; 2nd worker)
TRACK C — ARTIFACTS/SLIDES/EDITOR (gated: ships only after K1 + Track-A Phase A)
```

**One hard cross-track rule:** `K1` (the `_snip_args` execution-guard) must merge before ANY
Track-C artifact path ships — the elision bug corrupts every new artifact identically (it is
what stamped `<N chars elided>` onto a slide). K1 is small; it goes first.

Build/agent share machinery: `_BUILD_LIKE_SURFACES = {"build","agent"}` in `runtime.py`; both
use `BuildAgent` + `_compose_build_loop`. Research/deep_research use `ResearchAgent`.

---

## 1. DECISION — Brand theme engine (surgical)

**Decision (LOCKED).** ONE token-based **theme engine** that BOTH the report exporter (`report_export.py`)
AND the deck/artifact renderer (Track C, §3) consume. Disco-brand is the **default** theme (light + dark);
users can pick alternates incl. a **neutral/plain** theme (system fonts, no branding). "Shared vs distinct
identity" is a per-export SETTING, not a codebase fork. Type: Fraunces (display), Schibsted Grotesk
(UI/labels), Newsreader (body) — all OFL. The accent is rationed to ONE job (cover rule, section numbers,
`[[id]]` chips, slide bullets, page numerals, wordmark dot — never decorative). The `disco` Latin mark
(*disco/discere/didicī* = "I learn") is a reusable element on export covers + artifacts + in-app.

**Evidence (Dylan signed off, "absolutely excellent"):** `docs/evidence/brand-mockup-report-cover.png`,
`-cover-dark.png`, `-body-dark.png`, `-slide-dark.png`, `-disco-definition.png`, `-disco-definition-dark.png`;
source under `docs/evidence/_*.html`, `_brand_common.css`, `_brand_dark.css`, `_brandfonts/`. **These are the
token + mark source-of-truth — port them into shippable modules, do not re-derive.**

This is the build spec for the shared, token-based engine. It makes the LOCKED decision actionable.

> **CAUTION on line numbers:** as-of-research (2026-06-17). Re-locate symbols before editing (Serena
> `find_symbol` / by name); symbol + behavior is authoritative, the line number is a hint.

### 1.0 Invariants for the whole engine
- **One engine, two consumers, no fork.** A single token+font+mark module is imported by `report_export.py`
  (agent-server) and the deck renderer (tools). "Shared vs distinct" is a per-export *theme selection*, never
  a code path. Both consumers render from the SAME `Theme` object.
- **Layering (enforced — `core ← retrieval ← tools ← {agent_server | app_server}`):** the engine must sit in
  the LOWEST package both consumers can import downward = **`core`**. Home: **`packages/core/src/disco/core/brand/`**.
  *This is an intentional deviation-with-rationale from the earlier "bundle TTFs under agent-server/.../fonts/"
  prose: because the engine is genuinely SHARED (agent-server report export AND tools slide render), the bundle
  MUST live in `core/brand/` so `tools` reaches it without an illegal upward import. Not a contradiction of the
  decision — a correction of the placement.* `report_export.py` keeps doing the WeasyPrint call; it just sources
  CSS/fonts from `core.brand`.
- **Tokens are the contract.** Components/CSS consume named tokens, never raw values (mirrors the design-system
  header `frontend/src/styles/theme.css:1-13`). Canonical values = the signed-off sRGB hex in
  `docs/evidence/_brand_common.css:14-22` (light) + `_brand_dark.css:15-23` (dark), themselves converted from the
  oklch in `theme.css:53-91`. Do NOT re-invent values.
- **OFF / neutral parity:** the neutral theme must produce a generic unbranded deliverable (system fonts,
  ink-on-white, no accent, no mark). It is a first-class registered theme, not a special case.
- **Fitness gates:** the four Python gates — moving the bundle into `core` CHANGES cross-package imports, so
  regenerate the diagram + re-check layering. Frontend mark component: `typecheck:build` + `vitest` + `vite build`.
  Export changes are UI-adjacent → real visual evidence (render a PDF + a deck, Firefox screenshot, SendUserFile).

### 1.1 The shared brand package (the engine's home + public surface)
- **Problem:** no theme/token concept in the export path. `report_export.py:96-154` (`_markdown_to_html`)
  hardcodes a Helvetica-Neue/`#1a1a1a`/`#0b5cad` stylesheet inline (`:130-149`); slides (`slides.py:1-10`) is
  Marp-only with its own theming. Two renderers, zero shared identity.
- **Solution:** one Python package `packages/core/src/disco/core/brand/` exposing `Theme` + registry + CSS
  emitters + the `disco` mark partial, consumed by both renderers.
- **Where:** NEW `core/brand/`: `__init__.py` (re-exports `Theme`, `THEMES`, `resolve_theme`, `font_face_css`,
  `theme_css_vars`, `print_skeleton_css`, `definition_mark_html`, `wordmark_html`); `tokens.py` (`Theme` + 3
  themes); `css.py` (`font_face_css`, `theme_css_vars`, `print_skeleton_css`); `mark.py` (`definition_mark_html`,
  `wordmark_html`); `fonts/` (OFL TTFs + `LICENSE-OFL.txt`).
- **How:** consumers call `resolve_theme(name, mode)` → `Theme`, then assemble
  `font_face_css() + theme_css_vars(theme) + print_skeleton_css()` into one `<style>`/WeasyPrint `CSS`. The deck
  renderer reuses the same `Theme` for element styling (§3).
- **What needs doing:** create the package; wire `report_export.py` (§1.5); the deck renderer (§3) consumes this
  exact surface; regenerate the arch diagram.
- **Tests:** `import disco.core.brand` resolves; `resolve_theme("disco","light")` = the exact hex from
  `_brand_common.css:14-22`; `..."dark"` = `_brand_dark.css:15-23`; `"neutral"` carries no accent/brand font;
  unknown name raises (caller → 400).
- **Acceptance:** both `report_export.py` and a deck-render smoke test import and render from the same `Theme`
  with no duplicated value literals.
- **Risk:** LOW (new leaf package; no behavior change until wired). **Dependencies:** none. Precedes §1.5 + §3.
- **Reference:** `docs/evidence/_brand_common.css`, `_brand_dark.css`; the layering rule in `CLAUDE.md`.

### 1.2 The canonical token schema (light + dark + neutral)
- **Problem:** tokens live only as CSS custom properties in `theme.css` (frontend) + duplicated hex in the
  evidence CSS — neither importable by a Python renderer.
- **Solution:** a typed `Theme` dataclass whose fields are the union of theme.css semantic tokens, populated from
  the signed-off evidence hex.
- **Where:** `core/brand/tokens.py`.
- **How:** `@dataclass(frozen=True) class Theme` with fields `name, mode, bg, surface_1, surface_2, hairline,
  hairline_strong, text, text_muted, text_faint, accent, link, verify_supported, verify_weak, verify_unsupported,
  warn, font_display, font_ui, font_reading, font_mono, branded:bool`. Populate from the AUTHORITATIVE sources:
  **DISCO_LIGHT** ← `_brand_common.css:14-22` (bg `#fcfcfa`, surface-1 `#f7f7f4`, surface-2 `#f1f0ed`, hairline
  `#dfdedb`, hairline-strong `#cbcac7`, text `#1a1813`, muted `#5a5853`, faint `#878682`, accent `#4077a3`, link
  `#39688e`); **DISCO_DARK** ← `_brand_dark.css:15-23` (bg `#0e0f12`, surface-1 `#16171a`, surface-2 `#1e2124`,
  hairline `#303337`, hairline-strong `#4a4d53`, text `#e5e8ec`, muted `#9b9fa3`, faint `#727579`, **accent
  `#79c0f1`** brighter-shift, link `#74b3de`); **verify_*/warn** ← derive from `theme.css:68-71` (light) /
  `:87-90` (dark) oklch through the SAME oklch→sRGB pipeline that produced the mockups (store hex; must match the
  rendered grounding chips); **NEUTRAL_LIGHT/DARK** = desaturated grey ladder, `accent=link=text_muted` (no
  chroma), system font stacks, `branded=False`. `THEMES: dict[(name,mode), Theme]`; `resolve_theme(name,
  mode="light")` default `name="disco"`, falling back to light if no dark variant.
- **What needs doing:** transcribe the hex; build the registry; expose `resolve_theme`.
- **Tests:** every field non-empty for the four Disco entries; neutral has `branded=False` + no accent chroma; a
  light/dark round-trip pulls the documented hex byte-for-byte; a snapshot test pins each theme's full token set
  so an accidental drift fails CI.
- **Risk:** LOW. **Dependencies:** §1.1. **Reference:** `theme.css:53-91`; `_brand_common.css:14-22`; `_brand_dark.css:15-23`.

### 1.3 OFL font bundle + WeasyPrint FontConfiguration wiring
- **Problem:** `serialize_pdf` (`report_export.py:160-192`) calls `weasyprint.HTML(...).write_pdf()` with NO
  `FontConfiguration` and NO bundled fonts → the brand fonts silently fall back to Helvetica.
- **Solution:** bundle the OFL TTFs in `core/brand/fonts/`, emit `@font-face` at their absolute paths, load via
  `FontConfiguration`.
- **Where:** `core/brand/fonts/` (binaries) + `core/brand/css.py` (`font_face_css`) + `report_export.py:160-192`
  (`serialize_pdf`).
- **How:** copy the families' OFL TTFs from `docs/evidence/_brandfonts/` (referenced by `_brand_common.css:3-12`):
  Fraunces (+Italic), Schibsted Grotesk, Newsreader (+Italic), plus JetBrains Mono (the 4th role, `theme.css:30`,
  code blocks); include `LICENSE-OFL.txt`. `font_face_css()` resolves the dir via
  `importlib.resources.files("disco.core.brand") / "fonts"` and emits `@font-face` with
  `src: url(file://<abs>/Fraunces.ttf) format('truetype')` + the weight RANGES from `_brand_common.css:3-12`
  (Fraunces `100 900`, Schibsted `300 900`, Newsreader `200 800` + italics); `file://` absolute so WeasyPrint
  resolves without `base_url`. In `serialize_pdf` switch to:
  ```python
  from weasyprint.text.fonts import FontConfiguration
  font_config = FontConfiguration()
  css = weasyprint.CSS(string=brand_css, font_config=font_config)
  weasyprint.HTML(string=doc_html).write_pdf(stylesheets=[css], font_config=font_config)
  ```
  Keep `assert pdf is not None` + the `RuntimeError` wrap (`:189-192`) + the lazy import (`_lazy_import_weasyprint`, `:160-164`).
- **What needs doing:** add the TTFs; write `font_face_css`; rewire `serialize_pdf`.
- **Tests:** rendered PDF embeds Fraunces/Newsreader (assert font names in the PDF font table or pixel-diff the
  cover title vs Helvetica); `pdf_available()` (`:274-280`) still reflects the lazy import; absent-weasyprint path
  still skips cleanly. **Acceptance:** a PDF cover renders in the bundled display/reading fonts (Firefox screenshot).
- **Risk:** MED (WeasyPrint font loading is finicky on missing pango/cairo). **Dependencies:** §1.2.
- **Reference:** `_brand_common.css:3-12`; WeasyPrint `FontConfiguration` docs.

### 1.4 The print/render skeleton CSS + the WeasyPrint drop-cap constraint
- **Problem:** the current export CSS (`report_export.py:130-149`) is flat — no cover, header/footer, numbered
  sections, chips, or appendix. The §1 brand structure is unrealized.
- **Solution:** `print_skeleton_css()` emitting the brand layout via token `var()`s only, reusing the proven
  mockup classes (`.kicker`, `.wordmark`, `.chip`, `_brand_common.css:25-33`).
- **Where:** `core/brand/css.py` (`print_skeleton_css`, `theme_css_vars`).
- **How:** `theme_css_vars(theme)` emits `:root{ --bg:…; --accent:…; --display:…; … }` straight from `Theme`
  (same var names as the evidence CSS so the mockup rules drop in unchanged). `print_skeleton_css()` provides:
  `@page` size/margins + running header/footer (wordmark + page number); the cover block (Fraunces title via
  `--display`, Newsreader-italic subtitle via `--reading`, an `--accent` rule, the metadata row: compiled /
  author / sources / grounding); numbered section headers (`.section-no` Schibsted Grotesk + `--accent`,
  `02 — Title`); inline `.chip` citation styling reused verbatim from `_brand_common.css:29-33`; follow-up Q&A
  on its own page (`break-before: page`); sources appendix (2-col when >30 via a guarded class; TOC when >10);
  page-break hygiene (`break-inside: avoid` on chips/metrics). **Accent rationing:** when `theme.branded` is
  False, set accent = text (no chroma); when branded, only USE `--accent` in the rationed selectors. **WeasyPrint
  drop-cap bug (LOCKED constraint):** do NOT use `p::first-letter { float: left }` — WeasyPrint asserts on a
  floated `::first-letter`. Instead emit an inline `<span class="dropcap">` glyph (accent-colored, `--display`,
  manually sized/line-boxed) produced in `_build_pdf_html` (§1.5), styled non-floated.
- **What needs doing:** author the skeleton; port `.kicker/.wordmark/.chip/.defmark` into reusable emitters;
  implement the dropcap-as-inline-glyph.
- **Tests:** a >10-section report emits a TOC; a >30-source report emits 2-col sources; rendered HTML contains NO
  `::first-letter{float}`; follow-up starts on a fresh page; neutral output contains no accent hex.
- **Acceptance:** PDF matches the signed-off `brand-mockup-report-cover.png` layout (visual diff).
- **Risk:** MED (print CSS + WeasyPrint quirks). **Dependencies:** §1.2, §1.3.
- **Reference:** `_brand_common.css:25-33`; the §1 "Rendering constraint learned" note; mockup PNGs.

### 1.5 Wiring the engine into `report_export.py` + the per-export theme SETTING
- **Problem:** exports flatten markdown (`_markdown_to_html(serialize_markdown(...))`, `report_export.py:182`)
  with `nl2br` (`:120`) + a hardcoded stylesheet — no structured rendering, no theme selection, no light/dark or
  Disco/neutral choice. No theme field on any request (`CreateConversationBody`, `_common.py:74-91`, has none).
- **Solution:** a structured `_build_pdf_html(report, follow_ups, theme)` rendering from the STRUCTURED
  `ReportEvent` model + consuming the brand engine; thread a `theme`/`mode` setting from the export request down.
  **This is the SAME `_build_pdf_html` DR-2/§11.3 calls for — §1.5 is its theme half; build them as one change,
  sequenced after DR-1's `report_export.py` edits.**
- **Where:** `report_export.py` (`_build_pdf_html` NEW, `serialize_pdf:167-192`, `export_report:242-271`); the
  export route; optionally `CreateConversationBody` (`_common.py:74-91`) for a conversation default.
- **How:** NEW `_build_pdf_html(report, follow_ups, theme: Theme) -> str` iterates `report.sections`
  (titles, `s.markdown`, `s.disputed_notes`), `report.passages`, `report.summary`, `report.bounded_by`, and
  `follow_ups` — building real `<section>`/`<h2 class="section-no">`/`.chip`/appendix markup (NOT a flattened
  string; wrap section bodies through the `markdown` lib for inline formatting but emit the STRUCTURE ourselves).
  **Drop `nl2br`.** Inject the cover dropcap as an inline `<span class="dropcap">` glyph (§1.4). Prepend
  `font_face_css() + theme_css_vars(theme) + print_skeleton_css()`. Render the `disco` mark on the cover via
  `definition_mark_html("colophon")` (§1.6) when `theme.branded`. `serialize_pdf(report, follow_ups,
  theme="disco", mode="light")`: `resolve_theme(theme, mode)` → `_build_pdf_html` → WeasyPrint with
  `FontConfiguration` (§1.3). The MD path (`serialize_markdown:28-90`) is UNCHANGED (byte-parity). `export_report`
  (`:242-271`) forwards `theme`/`mode`; `md` ignores them; DOCX (§11.3) gets a themed `reference.docx`. **SETTING
  flow:** the export endpoint accepts `?theme=disco|neutral&mode=light|dark` (default `disco`/`light`) →
  `export_report(..., theme, mode)`; for decks the same selection rides as a slide-tool arg (§3) and/or a
  conversation default `brand_theme: str | None` on `CreateConversationBody`. "Shared vs distinct" = the user
  picking the same theme (default) or `neutral` per export. **Back-compat:** no `theme` ⇒ `disco`/`light` branded
  default; MD stays byte-identical.
- **What needs doing:** write `_build_pdf_html`; thread `theme`/`mode` through `serialize_pdf` + `export_report` +
  the export route; add the optional `CreateConversationBody.brand_theme`; update the export control/UI to offer
  the theme+mode choice (no false affordance — only offer themes that resolve).
- **Tests:** PDF renders structured sections + chips + cover + appendix with brand fonts; `theme="neutral"` →
  unbranded; `mode="dark"` → dark tokens; MD byte-identical (the existing parity test passes); unknown theme → 400.
- **Acceptance:** Firefox screenshots of (a) Disco-light, (b) Disco-dark, (c) neutral PDFs of the same report.
- **Risk:** MED (rendering rewrite; the MD byte-parity guard de-risks). **Dependencies:** §1.1-1.4; coordinates
  with DR-2 (§11.3, same file) — sequence after DR-1's `report_export.py` edits.
- **Reference:** `report_export.py:96-154, :167-192, :242-271`; `_common.py:74-91`.

### 1.6 The `disco` definition mark — reusable component (React in-app + HTML/CSS export partial)
- **Problem:** the `disco`/`discere`/`didicī` mark exists only as a one-off mockup (`docs/evidence/_definition.html`).
  It must be reusable on export covers + artifacts + in-app, at three scales (masthead/colophon/footer).
- **Solution:** two parallel implementations of ONE design — a server/export HTML+CSS partial in `core.brand` and a
  React component in-app — both driven by the same tokens + the signed-off `.defmark` structure.
- **Where:** `core/brand/mark.py` (export) + `frontend/src/components/brand/DefinitionMark.tsx` + `Wordmark.tsx` (in-app).
- **How:** `definition_mark_html(scale: Literal["masthead","colophon","footer"]) -> str` emits the `.defmark`
  markup equivalent to `_definition.html:53-60` (`.l1` headword `disco` in `--display`; `.pos` `Latin · verb`
  small-caps faint with the accent `·`; `.ipa` `/ˈdɪs.koː/`; `.rule` hairline with the accent `<i>` tick; `.gloss`
  "I learn; I become acquainted with." in Newsreader-italic; `.root` `from discere — to learn`). The `.defmark` CSS
  (`_definition.html:21-45`) moves into `print_skeleton_css()` so it themes off the tokens; the scales map to the
  `.s-lg/.s-md/.s-sm` size drivers (`_definition.html:43-45`). `wordmark_html()` emits `Disco<span class="dot">.</span>`
  (`_definition.html:90-92`) for the footer. In-app: `DefinitionMark.tsx` renders the identical structure as JSX with
  Tailwind bound to the existing `--accent`/`--display` props (already in `theme.css`), `scale` prop. Both honor
  `branded` (neutral suppresses the mark — it IS the brand).
- **What needs doing:** port the markup/CSS into `mark.py`; build the two React components; place the colophon mark
  on the export cover (§1.5) + a masthead in Settings/About.
- **Tests:** `definition_mark_html("colophon")` contains headword + gloss + root + accent tick; renders inside a
  WeasyPrint PDF without layout error; the React component snapshot matches at all three scales; neutral omits it.
- **Acceptance:** the export cover + the in-app masthead both match `brand-mockup-disco-definition.png`/`-dark.png`.
- **Risk:** LOW. **Dependencies:** §1.1-1.4. **Reference:** `docs/evidence/_definition.html` (the spec);
  `brand-mockup-disco-definition*.png`.

---

## 2. DECISION — Vision-for-verify: universal auto-enable (surgical)

**Decision (LOCKED):** DERIVE vision capability per-model at runtime; no user toggle for the providers we
ship. The build agent auto-enables screenshot→vision verification iff the verifying model (or its
configured `vision_escalation_model`) is vision-capable. Keep a per-model manual override pin and an
*optional, default-off* test-image probe for exotic self-host backends. This is the source of W6's
"vision auto-enable" line in §10; build it as the V-items below. Research provenance: §5b + the vision
research report (all claims CONFIRMED against provider docs + our code).

> **CAUTION on line numbers:** as-of-research (2026-06-17). Re-locate symbols (Serena `find_symbol`) and
> verify behavior before editing; the symbol + behavior is authoritative, the line number is a hint.

**Why (CONFIRMED problem):** today local-driver vision is gated purely on the `DISCO_DRIVER_VISION` env
var — a hand-guess that ignores whether an mmproj is actually loaded (`config.py:251-254`
`driver_caps`); screenshots are captured only when that env=="1" (`browser.py:194`), so a vision-capable
cloud driver still never gets a screenshot, and the prompt's "a build you have not seen render is not
finished" mandate (`prompts.py:212-214`) then drives an unbounded verify-loop. There is NO cross-vendor
capability standard — llama.cpp and OpenRouter advertise in different shapes; OpenAI/Anthropic don't
advertise at all — so detection must be provider-shaped.

**Detection matrix (CONFIRMED against provider docs):**
| Provider | Advertises? | Mechanism (exact) |
|---|---|---|
| **llama.cpp** | YES, runtime-authoritative | `GET {base_url − /v1}/props` → `modalities.vision` bool. Flips true with `--mmproj`. Reflects whether the projector is loaded NOW — strictly better than the env guess. (`/props` is at server root, NOT under `/v1`.) |
| **OpenRouter** | YES, metadata | `GET /api/v1/models` → match `data[].id` to `entry.model_id`; vision = `"image" in data[].architecture.input_modalities`. Check *input* (output-`image` = an image-GEN model). |
| **OpenAI / generic OpenAI-compat** | NO | `/v1/models` = id/object/created/owned_by only. Static name→vision table; unknown ⇒ `false` (fail-safe). |
| **Anthropic** | NO per-model flag (unnecessary) | All Claude ≥ v3 are vision-capable → family rule. Reuse `openai_provider.py:66 _is_anthropic()`. |

### V0 — invariants
Layering: `config.py` deliberately avoids `httpx` (see its module docstring) — the network probe lives in
`wiring.py`; `config.py` only consumes the resolved result. Fail-soft always: a probe network error must
NEVER crash wiring — fall back to the static table / existing declared caps. Memoize per endpoint for the
process lifetime; re-probe on provider rebuild (same cadence `apply_runtime_capabilities` runs today).

### V1 — capability data model + manual override pin
- **Problem:** no per-model way to pin vision on/off; capabilities are hand-declared only.
- **Where:** `core/llm/types.py:44` (`Requirement.VISION` — already exists; the token everything keys
  on). `core/llm/config.py:31-49` (`ModelEntry`, `capabilities: frozenset[Requirement]`).
- **How:** add `vision: bool | None = None` to `ModelEntry` (the override pin). `None` ⇒ derive;
  `True/False` ⇒ skip probing, force the cap. Keep `Requirement.VISION` as the resolved-capability token.
- **Tests:** a pinned `vision=False` model never gets VISION even if the probe says yes; `vision=True`
  forces it without a probe. **Risk:** trivial.

### V2 — runtime modality probe (the network half)
- **Problem:** we never query the providers that DO advertise.
- **Where:** `wiring.py:20-48 build_providers` — already iterates endpoints with `httpx` available and
  computes an advisory capability union (`:34-36`).
- **How:** NEW helper `async probe_vision(endpoint, entry, client) -> bool | None`:
  - OpenRouter (provider == "openrouter" or host == openrouter.ai): `GET /api/v1/models`, find the slug,
    return `"image" in architecture.input_modalities`.
  - llama.cpp / self-host (local/LAN base_url or a successful `/props`): `GET {base_url − /v1}/props`,
    return `modalities.vision`.
  - else return `None` (defer to the table). Wrap in try/except → `None` on any network error. Memoize
    per `(endpoint.base_url)` in a process-lifetime dict.
- **Tests:** stub a llama.cpp `/props` with `modalities.vision=true` → True; OpenRouter models JSON with
  `input_modalities=["text","image"]` → True; network error → None (no raise). **Risk:** MED (network
  I/O; must be fail-soft + timeout-bounded).

### V3 — static capability table (the no-advertisement half)
- **Problem:** OpenAI/Anthropic don't advertise; unknown self-host has no `/props`.
- **Where:** NEW small module `core/llm/vision_table.py` (pure, no httpx → importable from `config.py`).
- **How:** `def table_vision(model_id, family) -> bool`: Anthropic family rule (`_is_anthropic`-style →
  True); OpenAI/Gemini known-vision name patterns (gpt-4o/4.1/4.5, o3/o4, gpt-5*, gemini-* = True;
  embeddings/whisper/tts/rerank = False); unknown ⇒ **False** (fail-safe — never claim unproven vision).
- **Tests:** `claude-*` → True; `gpt-4o` → True; `text-embedding-3` → False; `mystery-7b` → False.
  **Risk:** LOW (keep the table small + documented; it's the only hand-maintained piece).

### V4 — overlay TRUE caps (replace the env guess)
- **Problem:** `apply_runtime_capabilities()` (`config.py:363-389`) overlays only the `DRIVER_VISION` env
  onto `driver-local`.
- **Where:** `config.py:363-389` + the `wiring.py` call-site.
- **How:** generalize to ALL entries: for each `ModelEntry`, resolve vision via the probe order — (1)
  `entry.vision` pin → (2) V2 probe result (passed in from `wiring.py`) → (3) V3 table → set/clear
  `Requirement.VISION` in `capabilities`. Do the HTTP in `wiring.py` (V2), pass results into this overlay
  (keeps `config.py` httpx-free). Drop the bare `DRIVER_VISION` env path (keep the env as a manual
  override pin alias for back-compat + one deprecation log).
- **Tests:** a llama.cpp endpoint with mmproj loaded → `driver-local` gains VISION without the env var; a
  vision-capable OpenRouter model gains VISION; an OpenAI embedding model never does. **Risk:** MED
  (touches the capability resolution all routing keys on — cover with the V2/V3 unit tests).

### V5 — the manual→auto flip (the single highest-leverage change)
- **Problem:** `browser.py:194` captures a screenshot only when `disco_env("DRIVER_VISION")=="1"`, so the
  escalation path (`vision_escalation_model`) never triggers a capture.
- **Where:** `packages/tools/src/disco/tools/builtin/browser.py:194`.
- **How:** change the gate to capture when **the resolved verifier has `Requirement.VISION` OR a
  `vision_escalation_model` is configured** (because `routing.py:253-301` already forwards the image to
  that escalation target). This one change flips screenshot→vision from manual to automatic.
- **Tests:** vision-capable driver → screenshot captured; non-vision driver + escalation model set →
  captured (routed to escalation); non-vision driver + no escalation → not captured. **Risk:** LOW
  (routing already does the right thing once caps are truthful — `routing.py` needs NO change).

### V6 — optional test-image probe (default OFF; last resort only)
- **Problem:** exotic self-host (vLLM/LM Studio) exposes neither `/props` nor a known name.
- **Where:** `wiring.py` (after V2 returns None) behind a default-off flag (e.g. `DISCO_VISION_PROBE`).
- **How:** send a 1×1 (or small solid) PNG + "what color is this? one word"; classify a
  `400`/`unsupported content`/`invalid image` error → no-vision, a coherent answer → vision; cache the
  verdict. Default OFF (an accept-and-ignore server false-positives it; consistent with
  [[feedback-gate-weak-model-assists]]). The manual pin (V1) always trumps it.
- **Tests:** with the flag off, never sent; with it on, a 400 → False, an answer → True. **Risk:** LOW
  (gated off by default; one real call when on).

### V7 — prompt wiring (ties to W6)
- **Where:** `core/llm/prompts.py:493-505` (vision bullet, injected only when caps include VISION) +
  `prompts.py:212-214` (the "unseen build is not finished" mandate).
- **How:** keep the vision bullet caps-gated; make the visual-verify mandate vision-AWARE — only demand
  "see it render" when a vision-capable verifier exists; otherwise the no-test finish-gate (W6: build
  exit 0 ∧ lint clean) stands without the unbounded visual loop. (This is the prompt half of W6's
  finish-gate; build them together.)
- **Tests:** non-vision driver build → no "must see it render" pressure; vision driver build → one
  screenshot/vision check through the bounded gate. **Risk:** LOW.

**Sequencing:** V1 → V3 → V2 → V4 → V5 (V5 is the user-visible flip; needs V1–V4's truthful caps) →
V7 (with W6) → V6 (optional, anytime). Lands as part of Track A W6 but is independently testable.

**Reference materials:** llama.cpp server README (`/props`, `modalities`) + `docs/multimodal.md`;
OpenRouter "List models" (`architecture.input_modalities`) + Multimodal docs; OpenAI "List models" (+
the open feature request to expose capabilities); Anthropic Models overview + Vision docs. In-repo:
`openai_provider.py:66 _is_anthropic`, `config.py` `vision_escalation_model` (`:198, :355-360`, default
`or-gemini-3-flash`), `routing.py:253-301` vision guard.

---

## 3. DECISION — Slides + Artifacts + Track-C build spec (surgical)

This REPLACES the decision-level §3 and folds in the §7 phase detail. It is the §10-grade build spec for Track C (slides + artifacts + image-gen + the experiment + render/preview fixes + the `artifact_mode` flag). The in-browser editor (§4) is **not** in scope here — it is Phase 5 and gets its own spec once the deck schema below ships. Decisions are LOCKED (§9 ledger); the **authoring schema is FROZEN only after the #34 experiment (C4)** — every item that depends on the authoring schema's exact field set is flagged **[experiment-gated]** and must not be coded against a guessed schema before C4 returns `docs/slides-experiment-verdict.md`.

> **CAUTION on line numbers:** every `file:line` below is as-of-research (2026-06-17) and WILL drift. Before editing, re-locate the symbol with Serena (`find_symbol`, e.g. `SlidesTool/run`, `deriveSrcDoc`) or by name and confirm the surrounding code still matches the description. Treat the **symbol + described behavior** as authoritative, the line number as a hint.

### 3.0 Invariants & sequencing for the whole track

- **K1 GATES everything here.** K1 (§10.3, the `_snip_args` execution-guard, `core/events.py:287-294`) must merge before ANY new artifact path ships. The elision marker is what stamped `<N chars elided>` onto a slide; it corrupts every new artifact identically. No Track-C item lands until K1 is green. This is the one hard cross-track dependency.
- **Two-layer discipline is non-negotiable.** The LLM emits the LOOSE authoring schema only; it NEVER emits pixel coordinates. The deterministic *lowering* owns overflow/fonts/16:9. Constraining open-weight generation costs 3–30 pts quality (an 8B model hit 0% under rigid JSON) — so the authoring emit is **instructed JSON, NOT grammar-constrained**.
- **Artifacts are a flag, not a surface.** Per the #33 answer there is NO new surface. `artifact_mode` is a flag on the EXISTING build-like machinery (`_compose_build_loop`, `runtime.py:1070`). Build + agent share machinery (`_BUILD_LIKE_SURFACES={"build","agent"}`, `runtime.py:690`); every change must hold for both.
- **Image-gen fills the EXISTING seam.** The `ImageBackend` Protocol (`image_gen.py:136-158`) + injected-backend constructor (`image_gen.py:286-290`) + one-line registration (`builtin/__init__.py:99`) already exist. We harvest Presenton's provider-pluggable service INTO this seam — we do not build a parallel one.
- **Licenses (clean-room, transcribe nothing):** Presenton Apache-2.0 (pattern/prompts/layout structures), python-pptx BSD (real dep), pptxtojson MIT (import), Chart.js MIT. **PPTist AGPL-3.0 = MODEL/SCHEMA BLUEPRINT ONLY** — study `src/types/slides.ts` for the deck data shape, vendor NO code (collides with Disco's commercial/self-host posture + the license-audit discipline). Reimplement clean-room as with Track A.
- **Brand consumption:** the deck renderer consumes the SAME `core/brand` theme engine the report exporter does (§1) — light/dark/neutral `Theme` tokens + OFL fonts from `core/brand/fonts/`. Do NOT invent colors or re-bundle fonts.
- **Fitness gates per item (all must pass before "done"):** `.venv/bin/python3 -m pytest -m "not integration"` (exit code is truth) · `uv run basedpyright` (0) · `uv run lint-imports` · `uv run python scripts/check_arch_budget.py` (no class >800 / func >200 LOC; do NOT game the allowlist — `_pptx_render.py` is the size-risk item, keep each renderer a separate ≤200-LOC function) · `uv run python scripts/gen_arch_diagram.py --check`. Frontend: `npm run typecheck:build` + `npx vitest run` + `npx vite build`. **Visual evidence MANDATORY** for every UI/artifact item: a real **Firefox** (not Chromium — host can't rasterize oklch/text) screenshot of the rendered deck/preview in the running app + SendUserFile. Green vitest ≠ evidence.
- **Sequencing (maps onto §7 Phase 0–4; Phase 5 editor = §4):**
  ```
  K1 (Track A §10.3) ──────────── GATES all of Track C
        │
  C4  #34 experiment harness  ── PARALLEL-OK, starts immediately after K1; FREEZES the authoring schema
        │   └─ docs/slides-experiment-verdict.md
        ▼
  C1  _deck_schema.py (two models + lowering)   [authoring half experiment-gated by C4]
        ▼
  C2  generation pipeline + model-tier prompt    [experiment-gated]
        ▼
  C3  _pptx_render.py (PPTX) + PDF(LibreOffice) + 16:9 HTML
  C5  render/preview fixes (deriveSrcDoc/0 B/?inline route)   ── INDEPENDENT of C1–C3, can land first
  C6  artifact_mode flag (runtime + body + Agent toggle)      ── INDEPENDENT, can land in parallel
  C7  image-gen backends into ImageBackend seam              ── INDEPENDENT (own seam)
        ▼
  C8  data-report layouts (Chart.js)            ── FAST-FOLLOW on the C1–C3 pipeline
  ```
  C5/C6/C7 touch disjoint files from C1–C3 and from each other → parallelizable on separate workers once K1 lands. C1→C2→C3 is a single sequential chain (each consumes the prior). C8 is a fast-follow that reuses C1–C3 machinery. The §4 in-browser editor + element→agent substrate is the deferred Phase 5 (binds to the C1 deck schema; do not start its schema-bound parts until C4 lands).

### 3.1 C1 — `_deck_schema.py`: the two-layer schema + deterministic lowering  **[authoring half experiment-gated]**

**Problem (root cause, CONFIRMED in code).** `slides.py` has NO data model — it is Marp-markdown-only end to end: the single arg is a raw `markdown: str` (`slides.py:29-36`); rendering is `_split_slides()` on `\n---\n` (`slides.py:207-215`) → `_basic_md_to_html` (`slides.py:107-204`) or marp CLI. There is no positional model, no overflow control, no element typing, and the only "structured" output is `{filename, base_name, format, slide_count, renderer}` (`slides.py:421-427`). The frontend already half-anticipates a deck shape (`deriveFiles`/activity `slides?: {filename,format,slide_count,slides:[{title,content}]}`, `buildTrace.ts:51-57, 145-151`) but nothing produces it. There is no schema for an editor to bind to.

**Solution.** A NEW module `packages/tools/src/disco/tools/builtin/_deck_schema.py` holding BOTH Pydantic layers + a pure deterministic `lower_deck()`. The authoring layer is what the LLM emits (loose/semantic); the deck layer is what the renderer (C3) and the future editor (§4) consume (precise/positional, PPTist-shaped).

**Where.** New file `packages/tools/src/disco/tools/builtin/_deck_schema.py`. Consumed by `slides.py` (C2/C3) and the experiment harness (C4). No cross-package import — stays inside `tools` (reaches `core/brand` downward for `Theme`).

**How (the two models + the lowering).** [field set of `AuthoredSlide` is **experiment-gated** — C4 may add/drop `layout_hint`/`image_prompt`/`notes`; keep the model in one file so a schema change is one edit].

```python
# ---- LAYER 1: authoring schema (LLM emits; LOOSE + semantic) ----
LayoutHint = Literal["title","title_content","two_column","image_left",
                     "image_right","section_header","quote","metrics","comparison"]
class AuthoredSlide(BaseModel):
    type: str                      # free-text semantic role ("cover","agenda","detail"…)
    title: str
    body: list[str] = []           # bullets/paragraphs, semantic — NO coordinates
    layout_hint: LayoutHint | None = None    # advisory; lowering picks final layout
    image_prompt: str | None = None          # → C7 image-gen (Slide.image_prompt→image)
    notes: str | None = None                 # speaker notes
    chart: ChartSpec | None = None           # C8 data-report extension
    table: TableSpec | None = None
class AuthoredDeck(BaseModel):
    title: str
    theme: Literal["disco-light","disco-dark","neutral"] = "disco-light"
    slides: list[AuthoredSlide]
# ---- LAYER 2: deck schema (renderer + editor consume; PRECISE + positional, PPTist-shaped) ----
class Element(BaseModel):
    id: str
    kind: Literal["text","image","shape","line","chart","table","latex"]
    left: float; top: float; width: float; height: float   # EMU canonical units
    rotate: float = 0.0; lock: bool = False
    text: str | None = None
    runs: list[TextRun] | None = None      # styled spans (font/size/weight/color token)
    src: str | None = None                 # image → workspace-relative path
    align: Literal["left","center","right"] | None = None
    role: str | None = None                # "title"|"kicker"|"bullet"|"page_no"|"accent_rule"
class Slide(BaseModel):
    id: str; type: str
    layout: LayoutHint                     # the RESOLVED layout (never None)
    elements: list[Element]
    background: Background; notes: str | None = None
class Deck(BaseModel):
    id: str; title: str
    theme: ThemeTokens                     # resolved from core/brand (§1)
    size: tuple[int,int] = (12192000, 6858000)   # 16:9 EMU (13.333"×7.5")
    slides: list[Slide]
```

`lower_deck(authored, theme) -> Deck` is PURE/deterministic and OWNS overflow/fonts/16:9:
1. **Resolve theme** via `core/brand.resolve_theme(authored.theme)` (§1) — the SAME tokens the report exporter uses; never invent colors.
2. **Resolve layout:** `slide.layout = authored.layout_hint or _infer_layout(slide)` (total, deterministic — keys off field presence: `image_prompt`→`image_right`; title-only→`section_header`; paired body→`comparison`/`two_column`; `chart|table`→`metrics`).
3. **Place elements:** each layout is a pure `layout_<name>(slide, theme, canvas) -> list[Element]` returning absolutely-positioned EMU boxes (one per role). The 6–8 ported Presenton structures become concrete coordinate templates here.
4. **Overflow control** (the value the markdown path never had): `_fit_text(runs, box, theme)` measures, steps font size down within `[min,max]` per role, and splits `body` across a continuation slide (`type="<type>_cont"`) if still overflowing.

**What needs to be done (ordered):** (1) write the models + `ChartSpec/TableSpec` stubs (C8 fills); (2) implement `_infer_layout`, the 6–8 `layout_*` functions, `_fit_text`, `lower_deck` (each `layout_*` a separate ≤200-LOC function); (3) consume `core/brand.resolve_theme`; (4) unit-test lowering as a pure function; (5) **hold the `AuthoredSlide` field set behind C4** — land Layer 2 + lowering skeleton first, finalize Layer 1 fields after the verdict.

**Tests.** `lower_deck` deterministic (same input → byte-identical `Deck`); every `layout_*` keeps elements in-canvas; a 30-bullet slide splits; `_infer_layout` total; theme resolution = exact `core/brand` tokens.
**Acceptance.** A hand-written `AuthoredDeck` fixture → a `Deck` with positioned, typed, in-bounds, theme-resolved elements, NO LLM in the loop.
**Risk.** LOW-MED (pure data+math; text measurement is the only subtlety — start conservative, refine against real PPTX in C3).
**Dependencies.** K1 (gate); §1 `core/brand`. Authoring-field finalization ← C4.
**Reference.** Presenton (Apache-2.0) `models/presentation_*_model.py` + `presentation-templates/general/` (port STRUCTURE, restyle in §1). PPTist (AGPL — MODEL ONLY) `src/types/slides.ts`. python-pptx EMU docs.

### 3.2 C2 — generation pipeline + model-tier-aware prompt  **[experiment-gated]**

**Problem (CONFIRMED).** The model authors raw Marp markdown directly (`SlidesGenerateArgs.markdown`, `slides.py:29`); no outline→layout→fill pipeline, no asset step, one prompt regardless of tier → weak local models produce overflowing, unstructured decks.

**Solution.** A staged generator emitting the C1 authoring schema (instructed JSON), then deterministic lower+render. Tier-aware framing: one-pass loose for `gpt-oss-120b`; rigid worked-example for the weakest local tier (the `ctx.assist` signal already exists, `files.py:243,269`, threaded at `runtime.py:1106,1182`).

**Where.** `slides.py` (`SlidesTool.run`, `slides.py:322`) gains a `deck` path beside the kept markdown fallback; a sibling `_slides_pipeline.py` if `slides.py` nears the size cap. Prompts as module constants (adapt Presenton's, restyled).

**How.** Pipeline: `outline → AuthoredDeck.slides[].{type,title}` (titles only) → `pick` (`_infer_layout`/honor hint) → `fill` (LLM fills `body[]/image_prompt/notes`, instructed JSON) → `assets` (per `image_prompt` call `image_generate`, C7) → `render` (`lower_deck` → C3). Defensive parse: on JSON failure retry once with a "return ONLY valid JSON" reminder; second failure → fall back to the kept Marp path (never hard-fail). Tier prompt keyed on `ctx.assist`: capable → compact schema + "≤6 bullets/slide"; weak → a FULL worked-example `AuthoredDeck` JSON + "copy this structure". Add `deck: AuthoredDeck | None` + `mode: Literal["deck","markdown"]="deck"` to `SlidesGenerateArgs` (keep `markdown` for back-compat with the `slides?` activity rendering at `buildTrace.ts:177-192`).

**What needs to be done.** (1) add `deck`/`mode` fields; (2) write outline+fill prompt constants (capable+weak; adapt Presenton's `generate_presentation_outlines.py`/`generate_slide_content.py`, restyle); (3) orchestrate in `SlidesTool.run` (branch on `mode`; defensive parse+retry+Marp fallback); (4) wire `image_prompt`→`image_generate`→`Element(kind="image")`; (5) thread `ctx.assist`; (6) keep `structured` superset-compatible with `buildTrace.ts:51-57`.

**Tests.** Goal → valid `AuthoredDeck` (mocked LLM) → renders; malformed JSON → one retry → Marp fallback, no crash; weak tier gets the worked example; `image_prompt` slides invoke `image_generate` once each; `structured.slides[]` present.
**Acceptance.** `slides_generate(goal=...)` on the real dev driver (gpt-oss-120b-free) → a fitted multi-slide deck end-to-end with images.
**Risk.** MED (LLM-output parsing is the fragile seam; Marp fallback + single-retry are the net). **[experiment-gated by C4]**.
**Dependencies.** C1, C7, C4. K1.
**Reference.** Presenton `utils/llm_calls/generate_slide_content.py`, `generate_presentation_outlines.py` (Apache-2.0). Instructed JSON, NOT constrained decoding (the §3 quality finding).

### 3.3 C3 — `_pptx_render.py`: native EDITABLE PPTX + PDF + self-contained 16:9 HTML

**Problem (CONFIRMED — the fatal weakness we replace).** The current PPTX is image-per-slide: `_render_with_marp` runs `marp --pptx` (`slides.py:291-298`) and the tool documents "PPTX output is image-based slides … Editable PPTX is v2" (`slides.py:9,313,406-410`). An image-per-slide PPTX is NOT editable (text isn't text). The HTML fallback (`_fallback_html`, `slides.py:218-258`) is a generic `<section>` dump with a minimal grey theme (`_MARP_MINIMAL_THEME`, `slides.py:55-81`) — no brand, no 16:9, no layouts.

**Solution.** NEW `packages/tools/src/disco/tools/builtin/_pptx_render.py` rendering the C1 `Deck` to a **native editable .pptx via python-pptx (BSD)** — REAL text boxes/shapes/images — using the 6–8 layouts (Presenton structures restyled to Disco brand §1, light+dark). PDF via **LibreOffice headless in the sandbox**. Self-contained 16:9 HTML (brand CSS inlined) for the Artifacts pane.

**Where.** New `_pptx_render.py`; `slides.py` render branch calls it. PDF runs through `ctx.sandbox.exec_shell` (same jailing rationale as the marp comment, `slides.py:261-267`). `.pptx` is already in the download allowlist (`_ARTIFACT_TYPES`, `_common.py:50`).

**How.** PPTX: build a python-pptx `Presentation` at 16:9 EMU; per `Element` add the real object — `text`→`add_textbox` with runs (font from `core/brand` OFL set, size from C1 `_fit_text`, color = theme token, align), `image`→`add_picture`, shape/line/accent-rule→`add_shape`/connector with the accent token, notes→`notes_slide`. One `render_layout_<name>(slide_obj, slide_model, theme)` per layout, each ≤200 LOC. PDF: write the .pptx, then `soffice --headless --convert-to pdf --outdir . deck.pptx` via `ctx.sandbox.exec_shell` (add LibreOffice to `deploy/sandbox/Dockerfile` if absent, mirroring the marp layer); on absent/timeout return a clean failure like the marp-absent branch (`slides.py:359-368`). HTML: one absolutely-positioned `<section class="slide">` per `Slide`, §1 brand CSS + OFL `@font-face` inlined, keyboard nav (←/→) inline — the file C5's `?inline=true` route previews. All output via `ctx.sandbox.write_file(path, bytes)` — `.pptx` is BINARY, pass raw bytes, never `.encode()`.

**What needs to be done.** (1) add `python-pptx` to `packages/tools/pyproject.toml` (NOT currently a dep — confirmed), lazy-import inside the renderer; (2) reuse the `core/brand/fonts/` OFL TTFs (§1) — do not re-bundle; (3) write `render_pptx(deck)->bytes`, `render_html(deck)->str`, per-layout fns, `convert_to_pdf(ctx, name)->(ok,err)`; (4) rewire `slides.py` render branch (deck mode → lower → render; keep marp fallback); delete the false "image-based/editable PPTX is v2" notes (`slides.py:9,313,406-410`); (5) `structured.renderer` reports `"pptx-native"`/`"libreoffice"`/`"html-brand"`; (6) add LibreOffice to the sandbox image if missing.

**Tests.** Render a `Deck` fixture → open the .pptx with python-pptx, assert text frames contain real titles/bullets (NOT one picture); HTML has one positioned section/slide + inlined fonts + 16:9; PDF returns ok with soffice present, clean failure without; binary round-trip unchanged.
**Acceptance.** Agent → 5-slide deck → `.pptx` opens AND is text-editable in LibreOffice/PowerPoint + `.pdf` renders + 16:9 `.html` renders and keyboard-navigates. Firefox screenshot + the opened editable pptx, SendUserFile.
**Risk.** MED (python-pptx geometry + LibreOffice availability; marp/markdown fallback bounds it). Size-risk for arch-budget — keep each layout a separate function.
**Dependencies.** C1; §1 `core/brand` fonts; K1; sandbox image (LibreOffice).
**Reference.** python-pptx (BSD) docs. Presenton (Apache-2.0) `presentation-templates/general/`. pptxtojson (MIT) for the eventual PPTX *import* (note for §4 Phase 5). §1 brand tokens + OFL fonts.

### 3.4 C4 — the #34 constrained-vs-free experiment harness  **(GATE; freezes the authoring schema)**

**Problem.** The two-layer decision rests on an empirical claim (constraining open-weight generation costs 3–30 pts; rigid JSON broke an 8B model at 0%). The exact AUTHORING schema must be chosen by evidence before C1 Layer-1 + C2 prompts freeze.

**Solution.** A standalone `harness/slides_experiment/` running 15–20 prompts × 3 emit strategies × 2 models, scoring each deck, writing a verdict. Parallel-OK: starts the moment K1 lands, runs while C5/C6/C7 proceed.

**Where.** New `harness/slides_experiment/` (repo already has a top-level `harness/`). Output: `docs/slides-experiment-verdict.md`.

**How.** Matrix: 15–20 deck prompts (simple / data-heavy / image-heavy / long-content overflow stress) × `{free-form, rigid (grammar-constrained JSON), loose-hybrid (instructed JSON, the proposed default)}` × `{gpt-oss-120b (or-free), weakest local}`. Three scores: (1) **overflow rate** — deterministic, reuse C1 `_fit_text`, count font-floor/continuation splits; (2) **LLM-judge content** — a rubric judge (gpt-oss-120b in dev) using the `source-accuracy-testing.md` judge-not-reranker discipline; (3) **blind human visual** — render, present unlabeled, rank (Firefox screenshots). Verdict picks the strategy maximizing content quality without collapsing weak-model success; records the FROZEN `AuthoredSlide` field set in the verdict doc; C1/C2 code against it.

**What needs to be done.** (1) build the prompt set (committed fixtures); (2) the three emit strategies against the REAL driver (no cassettes, FREE models in dev); (3) the scorers (overflow reuses C1 `_fit_text`); (4) run the matrix + blind ranks + write the verdict.
**Tests.** Overflow scorer unit-tested against known-overflow fixtures; the runner asserts every cell produced a parseable result or recorded failure.
**Acceptance.** `docs/slides-experiment-verdict.md` exists, names the frozen authoring schema; C1/C2 reference it. Until then C1-Layer1/C2 prompts are PROVISIONAL.
**Risk.** None to production (offline). Schedule risk only.
**Dependencies.** K1; C1 lowering skeleton (for the overflow scorer); real driver access.
**Reference.** `source-accuracy-testing.md`; the §8/§11 verification protocol (real-only, FREE models in dev).

### 3.5 C5 — render/preview fixes (deriveSrcDoc null, "0 B", `?inline=true` route)  **(INDEPENDENT — can land first)**

**Problem (three CONFIRMED bugs).** (1) **Blank iframe** — `deriveSrcDoc` (`buildTrace.ts:370-401`) returns `entry.content`, which is `""` for server-side artifacts (`deriveFiles` sets `content=""` at `buildTrace.ts:345,347,353`); it returns `""` NOT `null`, and consumers guard on `srcDoc != null` (`PreviewPane.tsx:122,164,215`; `AgentCanvas.tsx:209`; `ExecutionCanvas.tsx:56,82`) → an iframe with `srcDoc=""` → blank white frame instead of the live-server/placeholder fallback. (2) **"0 B"** — `FilesPane.tsx:76` renders `{file.bytes} B`; `file.bytes = TextEncoder().encode(content).length` = `0` for the `content=""` server-side artifacts (`buildTrace.ts:359`) → a misleading "0 B" on real files. (3) **No inline render** — the artifact route serves EVERY type as `attachment` (`files.py:208`, by design `_common.py:43-47`), so a server-side brand HTML deck can never preview.

**Solution.** (1) `deriveSrcDoc` returns `null` when no inlinable content; (2) suppress "0 B" for content-unknown files; (3) a NARROW `?inline=true` branch on the artifact route — HTML-only allowlist, no `attachment`, framing-safe headers — pointed at by the preview iframe with `sandbox="allow-scripts"` and crucially **NO `allow-same-origin`** (so a malicious deck can't reach this instance's open-CORS APIs).

**Where.** `frontend/src/lib/buildTrace.ts` (`deriveSrcDoc`, `deriveFiles`); `frontend/src/components/build/canvas/FilesPane.tsx:76`; `PreviewPane.tsx` (the artifact iframe `:238-246` — this is the "ArtifactsPane" the brief means); `packages/agent-server/src/disco/agent_server/routes/files.py` (`artifact_file`, `:164-212`) + `routes/_common.py` (`_ARTIFACT_TYPES`, `:48-60`).

**How.** (1) `deriveSrcDoc`: after picking `entry`, `if (!entry || !entry.content) return null;`. (2) `FilesPane.tsx:76`: render size only when known (`{file.content ? `${file.bytes} B` : "server-side"}`) or a `bytes:-1` sentinel consistent with the `(empty)` text at `:79`. (3) `artifact_file`: add `inline: bool = False`; when true REQUIRE `ext==".html"` (else 404; keep the declared-artifact jail `:181`, traversal `:172-174`, 50 MB cap `:201`), replace `attachment` (`:208`) with `inline`, keep `X-Content-Type-Options: nosniff`, ADD `Content-Security-Policy: sandbox allow-scripts; default-src 'none'; style-src 'unsafe-inline'; img-src 'self' data:; font-src 'self' data:` + `frame-ancestors 'self'`. (4) `PreviewPane.tsx`: when `srcDoc==null` but a declared `.html` artifact exists, render an iframe `src=.../artifacts/<path>?inline=true` with `sandbox="allow-scripts"` (NO `allow-same-origin`) — distinct from the same-origin live-preview iframe (`:203-209`, which keeps `allow-same-origin` for dev-server assets). Keep the untrusted-run `sandbox=""` (`:244`).

**What needs to be done.** edit `deriveSrcDoc` + a `deriveFiles` sentinel; edit `FilesPane.tsx:76`; add the `inline` branch + headers to `artifact_file`; add the inline-artifact iframe branch to `PreviewPane.tsx`; update `buildTrace.test.ts`/`srcdoc.test.ts` + `ExecutionCanvas.preview.test.tsx`.
**Tests.** `deriveSrcDoc` → `null` for content-empty `.html`, real HTML for inline-content; no "0 B"; route 404s `?inline=true` non-HTML, serves HTML with `inline`+`nosniff`+CSP, no `attachment`; iframe has `allow-scripts` without `allow-same-origin`. Visual: Firefox screenshot of a generated brand HTML deck rendering in the pane (not blank).
**Acceptance.** A server-side 16:9 HTML deck renders inline in the Artifacts/Preview pane; no blank frame, no "0 B"; the inline route refuses non-HTML and never sets `allow-same-origin`.
**Risk.** LOW-MED — inline HTML rendering is the sensitive surface; HTML-only allowlist + sandbox-without-same-origin + CSP + declared-artifact jail are the controls. Do NOT broaden the inline allowlist beyond `.html`.
**Dependencies.** K1. Independent of C1–C3 (can land first to fix the existing Marp-HTML blank frame). Best with C3.
**Reference.** Same-origin iframe pattern (`PreviewPane.tsx:203-209`); the attachment-only rationale (`_common.py:43-47`) — this is the deliberate narrow exception.

### 3.6 C6 — the `artifact_mode` flag (NO surface split)  **(INDEPENDENT)**

**Problem (CONFIRMED).** No low-friction "just make me an artifact" mode. `_compose_build_loop` (`runtime.py:1070`) ALWAYS composes the full build gate: `BlastRadiusConfirm()` (`:1162`), `OperatingMode.PLANNING` with plan→approve (`:1173`), the full `agent_scope()` toolset (`:1094`). For artifact authoring, the plan-gate + shell/browser is friction with no benefit. The #33 decision: a FLAG, NOT a new surface.

**Solution.** A per-conversation `artifact_mode` flag that branches `_compose_build_loop` to `NeverConfirm` (Research's gate, contrasted at `runtime.py:1075`), `OperatingMode.INTERACTIVE` (no plan-approval), and a narrowed `artifact_scope()` (slides/sheet/image/file/search; NO shell/browser/plan-gate). Plumbed exactly like the existing `depth_tier`/`assist`/`autonomous` overrides.

**Where.** `runtime.py` (`_compose_build_loop` `:1070-1183`; add `_artifact_mode` state + `set_artifact_mode` mirroring `set_depth` `:1195`); `routes/_common.py` (`CreateConversationBody` `:74-92`, add the field by `depth_tier` `:91`); `registry.py` (new `artifact_scope()` next to `agent_scope()` `:106`); the Agent surface UI (a toggle mirroring the depth picker).

**How.** (1) `registry.py`: `ARTIFACT_TOOLS = frozenset({"file_read","file_write","file_append","file_edit","file_list","search","extract","sheet_generate","slides_generate","image_generate","audio_overview","think"})` (strict subset of `AGENT_TOOLS` `:61-99`, NO `shell*`/`browser`/`deploy_preview`/`submit_plan`/`plan_step`/`delegate_explore`) + `def artifact_scope() -> ToolScope`. (2) runtime state like depth: `self._artifact_mode: dict[str,bool]` + `set_artifact_mode(cid,on)` + `_effective_artifact_mode(cid)` (mirror `_effective_assist`/`_effective_autonomous` `:1106,1181`). (3) branch `_compose_build_loop` when on: `artifact_scope()` instead of `agent_scope()` (`:1094`), `NeverConfirm()` instead of `BlastRadiusConfirm()` (`:1162`), `INTERACTIVE` instead of `PLANNING` (`:1173`), drop/relax `planning_tools` (`:1174`); everything else identical. (4) `CreateConversationBody.artifact_mode: bool = False` (`_common.py:82-91`) + call `set_artifact_mode` where `set_depth` is called. (5) Agent-surface toggle + TS field mirror.

**What needs to be done.** (1) `artifact_scope()`+`ARTIFACT_TOOLS`; (2) runtime state+setter+reader; (3) the `_compose_build_loop` branch; (4) the body field + handler wiring; (5) the toggle + TS mirror; (6) tests.
**Tests.** With `artifact_mode=True`: the loop's executor scope is `ARTIFACT_TOOLS` (no shell/browser), gate `NeverConfirm`, mode `INTERACTIVE` (assert via the `_compose_build_loop` test pattern, `test_sandbox_config.py:276-292`); junk value 422s at the edge; OFF → byte-identical. Frontend: the toggle sets the field; vitest.
**Acceptance.** An Agent-surface conversation with `artifact_mode` runs slides/sheet/image with no plan-approval gate and no shell/browser tools, identical machinery otherwise. Off = unchanged.
**Risk.** LOW-MED (loop composition; the branch is small/additive; `NeverConfirm`/`INTERACTIVE` already exist). Guard: artifact mode must NOT silently grant shell/browser — the scope intersection is the boundary (`registry.py:45-51`).
**Dependencies.** K1. Independent of C1–C5.
**Reference.** `set_depth`/`depth_tier` plumbing (`runtime.py:1195`, `_common.py:91`); `agent_scope()`/`research_scope()` (`registry.py:102-107`); `NeverConfirm`/`OperatingMode` (Research loop composition).

### 3.7 C7 — image-gen backends into the existing `ImageBackend` seam  **(INDEPENDENT; product idea #1)**

**Problem (CONFIRMED).** The seam exists but only the keyless procedural default is wired. `image_gen.py` ships the `ImageBackend` Protocol (`:136-158`), `_PILProceduralBackend` (`:170-250`), and an injected-backend constructor (`:286-290`) — but the live diffusers wire is "deferred" (`:11-14,183-186`) and registration is the unconditional `ImageGenTool()` (`builtin/__init__.py:99`). No real generative backend, no provider selection.

**Solution.** Concrete backends behind the existing Protocol + a `select_image_backend()` factory choosing by configured secrets, defaulting keyless. NO change to the tool's `run()` (the point of the seam, `:138-140`). Wire `Slide.image_prompt → image_generate → Element(kind="image")` (C2).

**Where.** `image_gen.py` (new backends + factory; Protocol/tool unchanged); `builtin/__init__.py:99` (→ `ImageGenTool(backend=select_image_backend())`); secrets via the existing `/api/secrets` store (`app-server/.../routes/secrets.py:36-55`, encrypted at rest).

**How.** Backends (each speaks the Protocol, returns raw `bytes`): keep `_PILProceduralBackend` (keyless fallback); `_ComfyUIBackend` (keyless/self-host POST+poll+fetch PNG); `_OpenAIImageBackend`/`_GeminiImageBackend` (paid; key from the secrets store, NEVER hardcode/log); `_PexelsBackend`/`_PixabayBackend` (paid stock fetch). `select_image_backend(secrets, config)` prefers an explicit provider, else ComfyUI if a host is set, else procedural — paid default-OFF (no key → never selected). Binary safety preserved by the tool's existing `isinstance(bytes)` (`:331`), magic-byte (`:370-389`), and `write_file` (`:362`); new backends MUST NOT `.encode()`. C2 wires `image_prompt` per slide.

**What needs to be done.** (1) backends (lazy-import SDKs/`httpx`, mirroring PIL lazy-import `:205`); (2) `select_image_backend` reading the secrets store; (3) change `builtin/__init__.py:99`; (4) wire `image_prompt` in C2; (5) Settings UI for provider keys (reuse the `/api/secrets` CRUD — no new secrets surface); (6) tests with a stub backend.
**Tests.** Factory selects keyless with no secret, the configured provider with one, never paid without a key; each backend returns bytes passing the magic-byte guard; a stub round-trips through `run()`; `image_prompt` slides get an `Element(kind="image")`. No real paid calls in unit tests.
**Acceptance.** With a key, `image_generate` produces a real generative image; without, the keyless default still produces a valid PNG; a deck with `image_prompt` slides renders real images.
**Risk.** LOW-MED — secret handling is sensitive (existing encrypted store + per-request overlay, never persist/log, per CLAUDE.md). Paid default-OFF.
**Dependencies.** K1. Independent except C2 consumes it. Existing `/api/secrets` CRUD.
**Reference.** Presenton (Apache-2.0) `services/image_generation_service.py`. The existing Protocol + `_PILProceduralBackend` (`image_gen.py:136-250`). `/api/secrets` route. Universal-providers tier discipline (keyless default, paid via secrets).

### 3.8 C8 — data-report layouts (Chart.js) — same-pipeline fast-follow

**Problem.** Decks carry no charts/metrics/tables; the §3 decision wants data decks + data reports in the SAME machinery (Chart.js, per the locked "Chart.js-not-Vega" decision).
**Solution.** Fill the C1 `ChartSpec`/`TableSpec` stubs, add `chart`/`table`/`metrics` layouts, render: HTML via inlined Chart.js (MIT, bundled local, no CDN), PPTX via python-pptx native charts (or a rendered chart image fallback), PDF via the LibreOffice conversion. Rides C1→C2→C3, no new pipeline.
**Where.** `_deck_schema.py` (fill stubs + chart layouts); `_pptx_render.py` (chart/table renderers); C2 prompt (allow `chart`/`table` payloads).
**How.** `ChartSpec = {kind:"bar"|"line"|"pie"|…, labels:[…], series:[{name,data:[…]}], title?}`. Lowering places a `chart`/`table` `Element`. HTML: `<canvas>` + inlined Chart.js config (brand token colors). PPTX: `slide.shapes.add_chart(...)` with `CategoryChartData` (native editable), image-fallback for unsupported types. Tables: `add_table`.
**What needs to be done.** (1) finalize `ChartSpec`/`TableSpec`; (2) chart/table layouts + table `_fit_text`; (3) HTML Chart.js renderer (bundle local); (4) PPTX native chart/table renderers; (5) extend C2 prompts + add ≥1 data-heavy prompt to C4's set.
**Tests.** A `ChartSpec` renders a `<canvas>`+config in HTML and a native chart in the .pptx (chart part exists); a `TableSpec` renders a real table; brand colors applied.
**Acceptance.** A data deck with a bar chart + metrics slide renders editable in PPTX, interactive in HTML; Firefox screenshot.
**Risk.** LOW-MED (python-pptx native charts have type gaps; image-fallback bounds it). Fast-follow — does not block C1–C3.
**Dependencies.** C1, C2, C3. K1.
**Reference.** Chart.js (MIT) bundled local. python-pptx chart docs (BSD). Presenton data-report layouts (Apache-2.0).

### 3.9 Track-C acceptance (supersedes the §7 acceptance)

After K1 + C1–C7 (C8 fast-follow): an Agent-surface conversation in `artifact_mode` → "make me a 5-slide deck on X" → a native **editable** `.pptx` (real text in PowerPoint/LibreOffice — NOT image-per-slide) + a `.pdf` (LibreOffice) + a self-contained 16:9 `.html` that renders inline in the Artifacts/Preview pane (`?inline=true`, `sandbox=allow-scripts` no-same-origin) AND keyboard-navigates; image slides carry real generated images; no blank frame, no "0 B". **Visual evidence MANDATORY:** Firefox screenshots of the rendered deck in-app + the opened editable .pptx, SendUserFile. The in-browser editor + element→agent substrate (§4) is the deferred Phase 5, specced at §4 and built once this deck schema is stable.

---

## 4. DECISION — In-browser editor + element-to-agent substrate (surgical)

This is the build spec for Dylan's "Manus highlight-mode" + the fresh React deck editor over the positional deck schema (§3). It makes the §4 LOCKED architecture actionable: ONE shared substrate (selection overlay + postMessage bridge + selection envelope + agent edit-loop) with TWO pluggable resolvers (`DeckResolver`, `SourceResolver`) serving BOTH the slide editor AND the app/site builder. This is Track C **Phase 5** (the deferred §7 phase).

> **CAUTION on line numbers:** every `file:line` below is as-of-research (2026-06-17). Re-locate symbols before editing (Serena `find_symbol` / by name). Symbol + behavior is authoritative, the line number is a hint.

### 4.0 Invariants + the build/buy boundary
- **One substrate, two resolvers, two surfaces.** Build it for SLIDES first (`DeckResolver` over the deck JSON); the app/site builder inherits it by swapping in `SourceResolver`. Everything except the resolver is identical: overlay UI, bridge transport, envelope, agent edit-loop.
- **Gating (HARD):** this whole section ships only after **K1** (`_snip_args` guard, §10.3) merges AND after the **#34 experiment freezes the AUTHORING schema** (`docs/slides-experiment-verdict.md`, §3 C4). Every part touching `slide.elements[i]` shape is **schema-frozen-by-#34** (flagged below); the substrate plumbing (overlay/bridge/envelope) is schema-INDEPENDENT and can be built in parallel.
- **License discipline (clean-room, per the §3 PPTist call):** **Onlook (Apache-2.0)** is the blueprint for `data-oid` build-time tagging + DOM→source resolution — reimplement clean-room (transcribe no upstream code). **bolt.diy (MIT)** is the reference for selection-as-chat-context + structured action-apply (`ActionRunner`). **PPTist (AGPL-3.0)** is MODEL/SCHEMA-CONCEPT ONLY — do NOT vendor; the editor is built FRESH in React.
- **No false affordances:** the editor and highlight-mode must not ship visually-usable controls that aren't wired (CLAUDE.md). Each milestone wires end-to-end or is flagged non-functional.
- **CONFIRMED-from-code baseline:** there is currently **zero `postMessage` usage anywhere in the frontend** (grep clean) — the bridge is entirely net-new. The preview iframes the overlay attaches to DO exist and are CONFIRMED: `PreviewPane.tsx` (live proxy iframe `:203-209`; srcdoc iframe `:238-246`) and `AgentCanvas.tsx` `ArtifactsPane` (srcdoc iframe `:217-224`). `deriveSrcDoc` (`buildTrace.ts:370-401`) is what we control to inject the in-frame agent for srcdoc renders.
- **Fitness gates:** frontend — `npm run typecheck:build` + `npx vitest run` + `npx vite build`; Python (deck patch tool / tag pass) — the four gates. Visual evidence MANDATORY: a Firefox screenshot of an element selected → edited → re-rendered in the real app, SendUserFile.

### 4.1 The selection overlay (hover-highlight · click-select · walk-up-to-parent)
- **Problem (to-be-built):** no way to point at a rendered element in a preview iframe and act on it; the iframes (`PreviewPane.tsx:238-246`, `AgentCanvas.tsx:217-224`) are inert render targets.
- **Solution:** an in-frame "selection agent" script (Onlook-style) that, when armed, draws a hover outline + click-selection box and reports geometry + tag to the host; the host renders the selection chrome + edit affordance. Walk-up-to-parent re-targets up the ancestor chain (or via a layers list).
- **Where:** NEW `frontend/src/components/build/canvas/SelectionOverlay.tsx` (host side) + NEW `frontend/src/lib/selectionAgent.ts` (in-frame, injected) + a hook `frontend/src/hooks/useElementSelect.ts`. Attaches in `PreviewPane.tsx` and `ArtifactsPane`.
- **How:** **In-frame agent:** injected into the previewed document; on `mousemove` → `elementFromPoint` → hover ring; on armed `click` → `preventDefault`, capture `getBoundingClientRect()` + the nearest tagged ancestor's resolver attr (`data-element-id` decks / `data-oid` source, §4.3) + a human label → `postMessage` a `disco:selection` envelope to the parent (§4.2). Walk-up = re-resolve to `parentElement` (skip untagged wrappers) + re-emit. For **srcdoc** decks/apps inject this script in `deriveSrcDoc` (`buildTrace.ts:370-401`) before returning HTML; for the **live-proxy** iframe serve it via the preview proxy (§4.3) and inject a `<script>` tag in the proxied HTML (`preview.py:33-61`). **Host overlay:** a positioned layer over the iframe mirroring the rect from the in-frame agent (cross-frame can't read the DOM directly), rendering the selection box, a "↑ parent"/layers control, and the inline edit-instruction affordance. Arm/disarm via a "Select element" toggle. **Sandbox:** the srcdoc iframe is `sandbox="allow-scripts"` (`AgentCanvas.tsx:222`, `PreviewPane.tsx:244`) → injected scripts run; the `untrusted` (shared/imported) path keeps the empty sandbox and MUST disable highlight-mode (honest disabled state — no false affordance).
- **What needs doing:** build the in-frame agent, the host overlay, the arm toggle; inject the agent in both render paths; gate off for `untrusted`.
- **Tests (vitest + Playwright):** arming shows a hover ring; clicking emits one `disco:selection`; walk-up re-targets to parent; untrusted run hides the control.
- **Acceptance:** Firefox screenshot of a slide element highlighted in the Artifacts iframe, SendUserFile.
- **Risk:** MED (cross-frame coordinate sync; Firefox-only host constraint). **Dependencies:** §4.2. Schema-INDEPENDENT.
- **Reference:** Onlook overlay (Apache-2.0); `PreviewPane.tsx:238-246`, `AgentCanvas.tsx:217-224`, `buildTrace.ts:370-401`.

### 4.2 The postMessage bridge protocol + the selection envelope
- **Problem (to-be-built):** no host↔iframe transport exists (zero `postMessage` in the tree). The agent edit-loop needs a stable, typed contract independent of the active resolver.
- **Solution:** a small versioned postMessage protocol with an origin/nonce guard + the canonical **selection envelope** `{selection_ref, human_label, screenshot_crop?, edit_instruction}` — identical for decks and apps.
- **Where:** NEW `frontend/src/lib/selectionBridge.ts` (host transport + types) + matching handling in `selectionAgent.ts` (§4.1); envelope type mirrored to the agent-edit tool input (Python, §4.4).
- **How — messages (all carry `{v:1, channel:"disco-select", nonce}`):** host→iframe `disco:overlay:arm`/`disco:overlay:disarm`/`disco:overlay:walkup`; iframe→host `disco:hover {rect}`; iframe→host `disco:selection` (the ENVELOPE):
  ```ts
  interface SelectionEnvelope {
    selection_ref: DeckRef | SourceRef;   // resolver-tagged target (§4.3)
    human_label: string;                   // e.g. "Title text — 'Q3 Revenue'"
    rect: DOMRect;                         // host overlay placement
    screenshot_crop?: string;              // dataURL crop for vision (only if §2 vision present)
    edit_instruction?: string;             // filled when the user submits an edit
  }
  type DeckRef   = { kind:"deck";   slide_id:string; element_id:string };
  type SourceRef = { kind:"source"; oid:string; file:string; line:number };
  ```
  host→agent `disco:apply` posts the completed envelope into the agent loop (§4.4). **Guards:** validate `event.origin` against the known preview origin, check the `nonce` handed in at arm time, drop malformed. `screenshot_crop` captured host-side (canvas crop of the rect) only when a vision-capable verifier exists (§2).
- **What needs doing:** implement the bridge + types; wire arm/disarm/walkup/selection/apply; origin+nonce validation.
- **Tests:** malformed/origin-mismatched messages dropped; a valid selection round-trips host→iframe→host; walk-up yields a parent ref; the envelope serializes to the Python tool-input shape.
- **Acceptance:** an end-to-end select→edit-instruction→apply round-trip logged in dev.
- **Risk:** MED (security-sensitive — origin/nonce must be correct). **Dependencies:** none (defines the contract). Schema-INDEPENDENT.
- **Reference:** bolt.diy selection-as-context + structured apply (MIT); Onlook DOM→tag resolution (Apache-2.0).

### 4.3 The two resolvers + their data-attribute tagging mechanisms
- **Problem:** a clicked DOM node must resolve to something the agent can edit — for decks a JSON element in `slide.elements[]`; for apps a `file:line`. The tagging that enables this doesn't exist yet.
- **Solution:** two pluggable resolvers behind one interface; each defined by the data-attribute the renderer/build stamps + the resolution it performs.
- **Where:** `frontend/src/lib/resolvers/deckResolver.ts` + `sourceResolver.ts` (host) + the tagging emitters: deck renderer (§3 C3) for `data-element-id`; a build-time source tag pass (sandbox/preview path) for `data-oid`.
- **How:** `interface Resolver { resolveFromTag(tag, slideOrFile): DeckRef | SourceRef | null }`. **`DeckResolver` (slides — CONFIRMED design, schema-frozen-by-#34):** the deterministic deck renderer (§3 C3) stamps `data-element-id={el.id}` + `data-slide-id={slide.id}` on every rendered box; the agent reads `data-element-id`; resolver returns `{kind:"deck", slide_id, element_id}`; the agent edits DATA (a JSON patch on `slide.elements[i]`, §4.4) and the deterministic renderer re-renders. ⚠ the `el.id`/`elements[]` shape is **frozen by #34** — do NOT finalize the ref fields until C4 lands. **`SourceResolver` (app builder — Onlook-style, to-be-built):** a build-time tag pass stamps `data-oid="<file>:<line>:<col>"` on every JSX/HTML element (a Babel plugin, clean-room of Onlook's, for JSX; a server-side HTML tag pass at preview-serve time for plain-HTML apps — extend `preview.py:33-61`/`preview_service.py`); the agent reads `data-oid`; resolver returns `{kind:"source", oid, file, line}`; the agent edits SOURCE at that `file:line` (§4.4) and HMR/preview re-renders. **Resolver selection** by surface — decks→`DeckResolver`, app-builder preview→`SourceResolver`; the substrate is otherwise identical.
- **What needs doing:** (deck) add `data-element-id`/`data-slide-id` emission to the C3 renderer + `deckResolver.ts`; (source) build the `data-oid` Babel pass + server HTML tag pass + `sourceResolver.ts`. **Strip `data-oid`/`data-element-id` from any EXPORTED artifact** (editor-only) so downloads stay clean.
- **Tests:** a clicked deck element resolves to the right `slide_id`/`element_id`; a clicked app element to the right `file:line`; tags absent from exported `.html`/`.pptx`; the resolver swap changes only the ref kind.
- **Acceptance:** the same overlay+bridge drives a deck edit AND a source edit by swapping only the resolver (demoed).
- **Risk:** MED-HIGH (the source tag pass + AST mapping is the genuinely-new, fiddly part; off the slides critical path). **Dependencies:** `DeckResolver` ⟂ schema-frozen-by-#34; `SourceResolver` ← the app-builder preview (already has a live iframe).
- **Reference:** Onlook `data-oid` Babel pass + DOM→AST→file:line (Apache-2.0, clean-room); PPTist `src/types/slides.ts` (AGPL — concept only); `preview.py:33-61`.

### 4.4 The agent edit-loop: envelope → structured patch → re-render
- **Problem (to-be-built):** the agent has no path to receive "the user highlighted X, do Y" and apply a SCOPED edit; today edits are whole-file tool calls with no element targeting.
- **Solution:** the host posts the completed envelope into the loop; the agent applies a STRUCTURED patch chosen by ref kind — a JSON patch for decks, a source edit for apps — then triggers a re-render.
- **Where:** host `disco:apply` handler → a new steer/tool input; agent-server tool layer: NEW `deck_patch` tool for decks; reuse `file_edit`/`file_str_replace` (W3/W4, §10.7-10.8) for source edits.
- **How:** the envelope arrives as scoped context (`human_label` + `selection_ref` + `edit_instruction` + `screenshot_crop` as vision evidence when present, §2) — bolt.diy's "selection-as-chat-context" (MIT): the model gets TARGETED context, not the whole artifact. **Deck path (schema-frozen-by-#34):** NEW `deck_patch` tool takes `{slide_id, element_id, patch}`, applies an RFC-6902 JSON Patch to `slide.elements[i]` in the stored deck JSON, validates the patched element against the deck schema before commit (revert on invalid), then re-lowers + re-renders deterministically (§3) — no model re-generation of unchanged slides. Args go through the K1 `_snip_args` guard (§10.3). **Source path:** resolve `file:line` → a scoped `file_edit`/`file_str_replace` (the W4 anchored-edit tools) → save → HMR/preview reload. **Apply-and-verify:** after the patch the host bumps `reloadKey` (`PreviewPane.tsx:55`) so the user sees the re-render; optional follow-up screenshot for vision verification.
- **What needs doing:** build `deck_patch` + schema validation + re-lower trigger; wire the host apply handler to the loop; reuse source-edit tools for the app path; ensure the K1 guard covers the new tool args.
- **Tests:** an envelope edit on a deck title → `deck_patch` mutates only that element, re-renders, other slides byte-stable; an invalid patch is rejected+reverted; a source edit lands at the right `file:line` and HMR reloads; the K1 guard rejects an elision-marker-bearing patch arg.
- **Acceptance:** highlight a slide element → "make this the headline, bigger" → it changes in the re-rendered deck (Firefox screenshot, SendUserFile).
- **Risk:** MED (deck patch + revert); the source path reuses hardened W3/W4 tools. **Dependencies:** K1 (gates Track C); `deck_patch` schema-frozen-by-#34; reuses W3/W4.
- **Reference:** bolt.diy `ActionRunner`/selection-context (MIT, clean-room); §10.3 K1; §10.7-10.8 W3/W4.

### 4.5 The fresh React deck editor over the positional deck schema
- **Problem (to-be-built):** the only deck UI today is `SlidesBlock.tsx` — a read-only prev/next previewer (`:28-157`) with no editing; inline rendering is explicitly deferred there (`:14-26`). No editor exists.
- **Solution:** a fresh React editor binding to the positional deck schema (§3) — a canvas of absolutely-positioned element boxes from `slide.elements[]`, with direct-manipulation editing that writes back through the SAME envelope/patch path as highlight-mode (§4.4), so manual and agent edits share one mutation route.
- **Where:** NEW `frontend/src/components/build/editor/DeckEditor.tsx` + `SlideCanvas.tsx` + `ElementBox.tsx` + `LayersPanel.tsx`; mounts in the Artifacts/Build surface; replaces/extends `SlidesBlock.tsx` for editable decks.
- **How (schema-frozen-by-#34):** render each `Slide` as a 16:9 canvas; each `Element` as an absolutely-positioned `ElementBox` (subtypes per §3). Stamp `data-element-id`/`data-slide-id` so the SAME selection overlay (§4.1) works inside the editor. Direct manipulation (drag/resize/rotate/edit-text) emits an RFC-6902 JSON Patch on `slide.elements[i]` — the SAME shape `deck_patch` uses (§4.4) — so the deck JSON is the single source of truth and agent+human edit through one channel; the deterministic renderer means editor and export render the SAME JSON. `LayersPanel` is the non-spatial walk-up/selection list. PPTist is the MODEL reference only (AGPL) — code written fresh.
- **What needs doing:** build the canvas/element/layers components; bind to the deck schema; route all mutations through the JSON-patch path; integrate the selection overlay; export through the existing artifact/download route (`AgentCanvas.tsx:165-191`).
- **Tests:** loading a deck JSON renders elements positioned; dragging emits a patch and persists; editor + export render byte-identical layout; selecting in the editor opens the same highlight-mode affordance.
- **Acceptance:** create a deck via the agent → open in the editor → move/edit an element → export an editable `.pptx` that opens in PowerPoint/LibreOffice (Firefox screenshot + the file) — the §7 Phase-5 acceptance.
- **Risk:** MED-HIGH (a real editor is substantial; bounded by reusing the substrate + deterministic renderer). **Dependencies:** schema-frozen-by-#34; K1; §4.1-4.4.
- **Reference:** PPTist `src/types/slides.ts` (AGPL — model only); §3 two-layer schema; `SlidesBlock.tsx:14-26` (the deferred-inline-render note this supersedes).

### 4.6 Reusing the substrate for the app/site builder (second surface, near-free)
- **Problem:** the app/site builder needs the same "highlight → tell the agent → it edits"; building it separately doubles the work.
- **Solution:** the app builder reuses §4.1-4.4 UNCHANGED and plugs in `SourceResolver` (§4.3) — it already has a live-preview iframe (`PreviewPane.tsx`), so it inherits highlight-mode for the cost of the tag pass + resolver.
- **Where:** the build-surface preview (`PreviewPane.tsx`, live proxy iframe `:203-209`); the `data-oid` tag pass + `sourceResolver.ts` (§4.3); the source-edit path (§4.4).
- **How:** arm the SAME `SelectionOverlay` over the build-surface live iframe; the in-frame agent reads `data-oid` (stamped by the build-time pass); `SourceResolver` → `{kind:"source", file, line}` → the agent edits source via `file_edit`/`file_str_replace` → HMR/preview reload. No new overlay, bridge, or envelope.
- **What needs doing:** mount the overlay on the build-surface preview; ensure the `data-oid` pass runs in the build/preview pipeline; select `SourceResolver` for that surface.
- **Tests:** highlight a button in the running app preview → "make it primary blue" → the source at the resolved `file:line` changes → preview HMR-reloads.
- **Acceptance:** a live-app element edited via highlight-mode, screenshot before/after, SendUserFile.
- **Risk:** MED (depends on the source tag pass, §4.3 — the riskiest sub-part). **Dependencies:** §4.1-4.4 (built for slides first); §4.3 `SourceResolver` + tag pass. Schema-INDEPENDENT.
- **Reference:** Onlook (Apache-2.0); bolt.diy (MIT); `PreviewPane.tsx:203-209`, `preview.py:33-61`.

---

## 5. DECISION — Build harness root cause (RESOLVED — revision in §5b, execution plan in §10)

**The breaking issue.** Root cause (2 unbiased Opus reviewers, `build-harness-review-6-17-26.md §7`,
CONFIRMED): the harness *manufactures* "re-read this file" signals + every brake is blind to read-loops.
NOT `_snip_args` (the macOS trace has ZERO elision markers; that bug corrupts the SLIDES path only).

**Locked sub-decisions:**
- **K1 — `_snip_args` execution-guard** (`core/events.py:287-294` + tool executor + `files.py`): reject/
  rehydrate any tool arg containing the elision marker before dispatch; reword marker to point to the
  in-context snapshot (never "use file_read"). GATES Track C. *Small, MED risk.*
- **Phase 0 — regression harness** (`core/tests/loop/test_build_loop_regression.py` + the golden trace):
  encode the acceptance metric table so loop-death is provable WITHOUT a live model.
  - **Acceptance metric table (macOS-clone):** file_read 82→**<20**; max reads/path **20**→**≤3** (measured from the golden trace by the Phase-0 harness; windows.js);
    "haven't-seen/truncated" thoughts 91→**<10**; browser 30s timeouts ≥4→**0**; false plan "NOT met"
    many→**0**; events ~374(noop)→**<150 via finish()**; preview text/503→**CSS-bearing 200**.
- **Phase A — 6 parallel quick-wins (stop the bleed; A1 shares a file with B1):**
  - A1 snapshot: budget-by-chars not 8-file count; never evict deliverable files; stop "re-read me"
    markers; align the 6000/7000/8000 caps so an ~8KB file is "fully seen". (`view_render.py:41-43`,
    `files.py:23`, `view.py:386-388`)
  - A2 read-aware breaker: new `repeated_read_no_edit` (hash tool+args, NOT thought; file-specific to
    dodge the OpenHands #5355 wait-loop FP). (`stuck.py:334`, `equality.py:36-40`, `turn_control.py`, `engine.py`)
  - A3 un-gate F9 read-dedup for all tiers. (`dedup.py:9-15`)
  - A4 C18 done-condition: resolve `file_exists` via the sandbox FS not agent-server CWD (the
    `None workspace_path` bug). (`plan_conditions.py:163-197`, `finish.py`, `base.py:97-118`,
    `process.py:41`)
  - A5 browser: text/CSS-selector clicks (not only `[data-pmx-index]`) + short timeout.
    (`browser.py`, `_browser_daemon.py:179-204`)
  - A6 preview: serve the subdirectory app (detect index.html dir) + keep alive after finish.
    (`session.py:420-422`, `preview_service.py`, `preview.py:46-51`)
  - A7 prompts: anti-monolith + single-verify-pass + vision-aware verify mandate. (`prompts.py:208-214`)
- **Phase B — durable redesign (the HYBRID; behind a flag, A/B vs A1):**
  - B1 **hybrid context** = full content for the active working set + a **structural map** for
    everything else (omitted file → "here's its shape", never "go re-read it") + **diff-since-last-step**
    (the novel piece; no surveyed framework had it). New `workspace_map.py` + `view_render.py`.
  - B2 architectural finish-gate (OpenHands pattern; one "build→verify once→back to build" check). (`finish.py`)
  - B3 vision-for-verify (§2). (`config.py`, `wiring.py`, routing, `browser.py`)

**THE OPEN DECISION (Dylan wants 2 more OSS research rounds before committing):**
*Phase-A-now-then-Phase-B-hybrid* (recommended: fast relief, low risk, only A1 throwaway, A/B the
hybrid) **vs** *jump straight to the structural-map/hybrid* (no throwaway A1 but highest engine risk,
longest time-to-relief, no safety net, hard to verify incrementally).

**Aider-vs-ours breakdown (already delivered, recorded for the planning):**
- OURS = full-content snapshot (≤8 files, 6000/16000 char caps, re-read from disk each turn, kept out
  of condensation). O(project size) → caps → manufactured re-read pressure → cliff at 13 files.
- AIDER = structural repo-map: tree-sitter tags (defs+refs) → reference graph → **PageRank**
  (personalized to chat/recent files) → ranked **signatures** (no bodies) → ~1k-token budget (binary
  search) → disk-cached by mtime (`.aider.tags.cache`); full content added **on demand** and KEPT.
  ~Constant size regardless of repo size; never says "you haven't seen this".
- Mismatch to note: Aider's map is tuned for EDIT-heavy navigation of LARGE existing repos; our build
  agent is WRITE-heavy creating SMALL projects → half-written files may not tree-sitter-parse, so the
  freshest files drop out of a pure map. → argues for the HYBRID, not pure-map.
- **OSS prior art (`runthru-v2-research-digest.md R3`):** Aider (repo-map), SWE-agent (state blob +
  last-5 obs keeping model reasoning + 100-line window + $-budget kill; abandoned semantic loop
  detection as FP-prone), OpenHands (event stream + 3-tier condensation + StuckDetector 5 patterns +
  architectural finish-gate after pytest = gold standard), Cline (`[DUPLICATE FILE READ]` read-aware
  dedup for everyone + "CRITICAL FILE STATE ALERT" on external change).

### 5b. Round-1+2 OSS research outcome — code-grounded revision (CONFIRMED 2026-06-17)

Two research rounds (3 parallel agents each) against the most *successful* OSS agents (ranked by
SWE-bench Verified: OpenHands 68%, Cline ~60%, SWE-agent 43%, Aider 31%) — winner survey + code-level
mechanism extraction from the actual source. **Result: the winning architecture is ~80–90% already
present in Disco. The A-vs-B framing dissolves — there is no risky tree-sitter rewrite on the critical
path.** Do ONE consolidated surgical pass over code we mostly own; DEFER the only genuinely-new/risky
piece (tree-sitter structural map) behind a project-size gate.

**Convergent best-practice (CONFIRMED across winners):** the top agents do NOT push files in via a
standing snapshot — they let the model PULL on demand, govern pulls with a file-state tracker, dedup/
condense pulled content, break loops with a THOUGHT-EXCLUDED args-only hash, and finish via a BOUNDED
verify-once gate. Aider's repo-map is a large-EXISTING-repo navigation tool (31%, different job); for
write-heavy from-scratch builds there is almost nothing to map.

**Corrections Round 2 forced (CONFIRMED from source):**
- OpenHands `StuckDetector._event_eq` INCLUDES `thought` — the SAME bug we have; the authority for
  excluding thought is Roo/Cline's `ToolRepetitionDetector` (hashes `{name,params}` only). [Roo Apache-2.0]
- OpenHands-ACI does NOT auto-revert on lint — it lints+reports; **SWE-agent** is the auto-reverter
  (`tools/windowed_edit_linting/bin/edit`, the flow that doubled its SWE-bench). [OpenHands/SWE-agent MIT]
- Disco's finish-verify is ALREADY bounded (cap-3 in 4 places: `finish.py:50`, `:59`, `:720`, +noop);
  the work is CONSOLIDATION, not de-looping. The real net-new gap is no write-time syntax gate.

**REVISED Track A — one consolidated surgical pass (supersedes the Phase-A/Phase-B split above):**

- **K1 — `_snip_args` execution-guard** (`events.py:287-294`). Unchanged. GATES Track C. Small.
- **Phase 0 — regression harness** (`test_build_loop_regression.py` + golden trace). Unchanged. The
  acceptance metric table stands (file_read 82→<20, timeouts ≥4→0, events 374→<150 via finish()).
- **W1 — thought-excluded read-aware breaker** *(mostly already built; ~surgical)*:
  - `equality.py:29` `event_content_eq` — add `ignore_thought: bool = False`; at `:38` gate the
    `a.thought==b.thought` term behind it. Pass `ignore_thought=True` at the StuckDetector call-sites
    (`stuck.py:279/290/316/318-320` + `:384` no-progress dedup); leave idempotency/dedup callers default.
  - `stuck.py` — add `_WAIT_POLL_TOOLS = {sleep,wait,server_status,poll,browser_wait,job_status,
    deploy_status}`; exempt them from repeat patterns 1/4 (NOT action→error). (OpenHands #5355 FP.)
  - `stuck.py:61` raise `repeat_action_observation` 3→4 (OpenHands value); add a Roo-style pure-
    consecutive-repeat pattern (`_pure_repeat`, OR into `evaluate()` at `:170`).
  - Disposition layer `turn_control.py:286 gate_stuck` already escalates-then-halts — NO change.
  - *Canonical args:* hash `(tool_name, canonical_args)` — sort keys, collapse prose-key whitespace,
    normalize path separators, but PRESERVE genuinely different paths/commands/line-ranges.
- **W2 — stale-aware snapshot + read-dedup-for-all** *(subsumes old A1 + A3; refactor of code we own)*:
  - NEW `loop/file_state.py` `FileStateTracker`: per-path `(last_seen_seq, sha256, mtime)`; agent
    writes pre-register the new sha (self-write ≠ stale); `stale_paths(sbx, working_set)` compares disk
    sha to last-seen; `file_state_notice(stale)` emits a NAMED "these files changed on disk, re-read
    before editing" block ONLY when non-empty (silent otherwise). Content-sha NOT chokidar (sandbox-
    robust; stronger than mtime). Working-set source = `messages.py:42-70 _workspace_paths_from_events`.
  - `view_render.py:61-236 workspace_snapshot_message` — emit FULL body only for (stale ∪ mutated ∪
    never-shown); collapse the unchanged rest to a one-line "current, see above" pointer. Keep
    disk-fresh re-read + post-history placement (correct; matches Aider `chat_chunks.py`).
  - `view_render.py:193-199` — REPLACE the "call file_read … do NOT call file_write" oversize marker
    (the loop generator) with a windowed-view directive pointing at `file_read(offset,limit)` (which
    ALREADY exists, `files.py:112-117`) + `file_edit` anchor. Stop forbidding file_write.
  - `dedup.py:9-15` — ungate the RENDER-TIME history body-collapse (`collapse_superseded_reads`: rewrite
    every earlier file_read of a path to a "[superseded — current shown later]" notice) for ALL tiers.
    Keep F9's EXECUTION-time short-circuit assist-gated.
- **W3 — syntax-gate every write + auto-revert** *(net-new; the highest-value new mechanism; clobber fix
  at the write seam, complements [[disco-filestate-fix]] A8)*:
  - NEW in `files.py`: `_syntax_errors(path,text)` (`.py`→compile, `.json`→json.loads, html/css/js/ts→
    tree-sitter ERROR nodes; unsupported→[]) + `_gated_write(ctx,path,new_bytes,old_text)`: apply →
    check NEWLY-INTRODUCED errors (diff-filter vs pre-edit, like OH `linter.py`/SWE-agent
    `format_flake8_output`) → on new error AUTO-REVERT to old bytes + return failure "kept old content;
    DO NOT re-run the same failed edit".
  - Route all 5 writers through it: `file_write:267-273`, `file_edit:445-448`, `file_replace_lines:513`,
    `file_insert_lines:563`, `file_append:305` (old text already in scope at each via the F3 read).
- **W4 — capability-gated edit format** *(uses existing `ctx.assist` seam)*:
  - Add `Requirement.ANCHORED_EDIT` (`types.py:48`); set in `ModelEntry.capabilities` only for models
    that benchmark well on diff edits (default-off ⇒ unknown/weak → whole-file, mirrors Aider `models.py`
    `edit_format="whole"` default). Weak tier → whole-file `file_write` + W3 gate (Aider Qwen3: whole =
    higher pass + ~100% well-formed for weak open models); capable → NEW `file_str_replace` (OH unique-
    match + whitespace-retry), withheld from weak tier via existing capability tool-withholding.
- **W5 — C18 done-condition + unified finish-gate** *(consolidation)*:
  - C18: `plan_conditions.py:163-197` resolve `file_exists` against the real sandbox FS, not agent-server
    CWD (the `None workspace_path` bug; `base.py:97-118`/`process.py:41`). "Done" = ONE command in the
    resolved workspace cwd, never a model-asserted checkbox.
  - Consolidate the four cap-3 gates into one verify-once envelope; cap the uncapped
    `gate_execution_nudge` (`finish.py:658`) at 3 for parity.
- **W6 — browser + preview + prompts + vision** *(former A5/A6/A7/B3)*:
  - Browser: text/CSS-selector clicks not only `[data-pmx-index]` + short timeout (`browser.py`,
    `_browser_daemon.py:179-204`). Preview: serve the subdir app + keep alive after finish
    (`session.py:420-422`, `preview.py:46-51`). Prompts: anti-monolith + single-verify-pass (`prompts.py`).
  - No-test UI finish-gate = build exit 0 ∧ lint clean ∧ ONE Firefox screenshot through the bounded gate;
    extend `gate_browser_verify` (`finish.py:704-757`) to capture the screenshot as vision evidence,
    satisfying the `prompts.py:212-214` "unseen build is not finished" mandate with real evidence.
  - Vision auto-enable per §2 (runtime capability detection).
- **DEFERRED — D1 tree-sitter structural map** *(the ONLY genuinely-new/risky piece; NOT on the critical
  path)*: NEW `loop/repo_map.py`, default-OFF, engages only past `_REPO_MAP_FILE_THRESHOLD=40` files;
  signatures-only, EXCLUDES the working set, binary-search to ~1024 tokens, `.disco/tags.cache` mtime
  cache (Aider `repomap.py` pattern). Schedule AFTER W1–W6 ship and only if real project sizes justify it.

**Risk verdict:** W1/W2/W5 are refactors of code we own (LOW-MED). W3/W4 are net-new but small and
self-contained (MED, lint-gate is high-value). W6 is mechanical (LOW). The deferred tree-sitter map is
the only HIGH-risk item and it is off the critical path. Net: Track A is far lower-risk than the
original "durable redesign" framing implied.

**License:** every source studied (Roo/Cline/Aider Apache-2.0; OpenHands/SWE-agent MIT) is permissive;
we REIMPLEMENT CLEAN-ROOM (describe behavior, transcribe no upstream code) — consistent with the
PPTist-model-only call (§3).

> **STATUS:** research COMPLETE, both rounds delivered. Awaiting Dylan's ratification of the revised
> single-pass Track A (W1–W6 now, tree-sitter map deferred) before execution begins.

---

## 6. DECISION — Deep Research polish (Track B) — SUPERSEDED by §11

The decision-level summary that was here is now fully expanded into the surgical build spec at **§11
(TRACK B — DEEP RESEARCH EXECUTION PLAN)**: DR-1 (D1 citation, C1 TTS normalizer, C2 acronyms, B1–B4
export/audio), DR-2 (branded export template), DR-3 (recency toggle), DR-4 (attach files), and the
keep-searching wave — each with Problem/Solution/Where/How/Tests/Acceptance/Risk. **§11 is authoritative
for Track B.** This stub is retained only so section numbering and inbound references stay stable.

---

## 7. Track C assembly — SUPERSEDED by §3 + §4

The phase-level assembly that was here is now fully expanded into surgical build specs:
- **Slides + artifacts + image-gen** (former Phases 0–4) → **§3** (items C1–C8, each with
  Problem/Solution/Where/How/Tests/Acceptance/Risk; sequencing + the K1 gate in §3.0; acceptance in §3.9).
- **In-browser editor + element→agent substrate** (former Phase 5) → **§4** (items 4.1–4.6).

**§3 + §4 are authoritative for Track C.** This stub is retained only so section numbering and inbound
references stay stable. (§12 below is the higher-level "Track C is gated" pointer, kept for the same reason.)

---

## 8. Fitness gates & verification discipline (every change obeys these)

- `.venv/bin/python3 -m pytest -m "not integration"` (NOT `uv run pytest`); exit code is truth.
- `uv run basedpyright` (0 errors), `uv run lint-imports`, `uv run python scripts/check_arch_budget.py`
  (no class >800 / func >200 LOC outside the whitelist — do NOT game the allowlist), `gen_arch_diagram.py --check`.
- Frontend: `npm run typecheck:build` + `npx vitest run` + `npx vite build`.
- **Visual evidence MANDATORY** for UI: Firefox/Playwright screenshot in the running app + SendUserFile.
  (Headless Firefox CANNOT render backdrop-filter — measure pixels for glass effects.) Green vitest ≠ evidence.
- Real-sample harnesses from VERBATIM captured samples; real results only for verification; no cassettes,
  no hardcoded stand-ins, no cheap workarounds. Preserve originals. Prove before concluding.
- Secrets: never echo values; 0600 at `~/.config/disco/`; OpenRouter key only for the 120b-free driver;
  never drop CF Access.

---

## 9. Decision ledger (one-line status)

| # | Decision | Status |
|---|---|---|
| Brand | shared theme engine, Disco default (light+dark), user-selectable, `disco` Latin mark | **LOCKED** ✓ |
| Vision | universal auto-enable via runtime capability detection | **LOCKED** ✓ |
| Slides | build-our-own, harvest all 4, two-layer schema, python-pptx, PPTist model-only | **LOCKED** ✓ |
| Editor | React editor + Onlook-style shared substrate (2 resolvers), decks + app builder | **LOCKED** ✓ |
| Deep Research | DR-1..4 as specified | **LOCKED** ✓ |
| Build-harness depth | RESOLVED by research → ONE surgical pass W1–W6 (code we own), tree-sitter map DEFERRED behind 40-file gate | **PENDING RATIFICATION** (§5b) |
| Slides de-risk | gate authoring schema on #34 experiment (no Presenton sidecar) | **LOCKED** ✓ |
| Track A exec plan | revised single-pass W1–W6, tree-sitter deferred | **RATIFIED** ✓ (§10) |

---

## 10. TRACK A — BUILD HARNESS EXECUTION PLAN (RATIFIED 2026-06-17)

This is the surgical build spec for the build/agent loop fix. It is derived from 2 OSS research
rounds (§5b) with code-level extraction from the most successful agents (OpenHands, Cline, SWE-agent,
Aider, Roo). **The winning architecture is ~80–90% already in Disco; this is a consolidated refactor
of code we own + 2 small net-new mechanisms, NOT a rewrite.** Reimplement clean-room (no upstream code
transcribed; all sources are MIT/Apache).

> **CAUTION on line numbers:** every `file:line` below is as-of-research (2026-06-17). Line numbers
> drift. Before editing, re-locate the symbol with Serena (`find_symbol`) or by name, and verify the
> surrounding code still matches the description. Treat the SYMBOL + described behavior as authoritative,
> the line number as a hint.

### 10.0 Invariants for the whole track
- **Surfaces:** build + agent share machinery (`_BUILD_LIKE_SURFACES={"build","agent"}` in `runtime.py`;
  both use `BuildAgent` + `_compose_build_loop`). Every change must hold for BOTH.
- **Event-sourcing:** state is a projection of an append-only log; never mutate state in place. The
  loop turn body runs under `self._lock`; extracted helpers must NOT re-acquire it.
- **The assist kit is preserved:** existing weak-model affordances (F-flags, per-file spiral, forgiving
  replace) stay; we UN-gate only what should be universal (render-time read-dedup) and ADD gates where
  capability-appropriate (edit format). Per [[feedback-gate-weak-model-assists]], anything that makes a
  capable model worse stays behind the `assist` gate.
- **Fitness gates per wave (all must pass before a wave is "done"):**
  `.venv/bin/python3 -m pytest -m "not integration"` (exit code is truth) · `uv run basedpyright` (0) ·
  `uv run lint-imports` · `uv run python scripts/check_arch_budget.py` (no god-objects; do NOT game the
  allowlist) · `uv run python scripts/gen_arch_diagram.py --check`. UI-touching changes additionally
  need a real Firefox screenshot in the running app.
- **Branch discipline:** one branch per wave; commit only when the user asks; never commit to default.

### 10.1 Acceptance metric table (the frozen target — Phase 0 encodes it)
Golden conversation: `conv_6483d49d30f045699016b2946ca34523` ("build a simple macosx clone"), trace at
`docs/evidence/macos-build-trace-6-17-26.txt`. The fix is PROVEN when a replay/regression run hits:

| Metric | Before | Target |
|---|---|---|
| `file_read` count | 82 | **< 20** |
| max reads of any single path | 15 | **≤ 3** |
| "haven't-seen / truncated / re-read" thoughts | 91 | **< 10** |
| browser 30s click timeouts | ≥ 4 | **0** |
| false plan "NOT met" (C18) | many | **0** |
| total events | ~374 (noop death) | **< 150, terminated via `finish()`** |
| preview | text-only / 503 | **CSS-bearing HTTP 200** |

### 10.2 Sequencing & parallelization
```
K1 ─┐
     ├─ Phase 0 (regression harness; encodes 10.1)  ── must land before W1–W6 can be "proven"
W?  ─┘
        W1 (breaker)         ┐  W1,W2,W5 touch DIFFERENT files → parallelizable on 3 workers
        W2 (snapshot+dedup)  ┤  W3,W4 both touch files.py → SAME worker, W3 before W4
        W5 (C18+finish)      ┘
        W3 (write-gate) → W4 (edit-format)     [files.py, sequential]
        W6 (browser/preview/prompts/vision)    [independent; parallelizable]
   ── then live acceptance on disco-live ──
   DEFERRED: D1 tree-sitter map (only if real builds exceed ~40 files)
```
K1 also GATES Track C (artifacts) — it must merge before any new artifact path ships.
Track B (Deep Research, §6) runs fully parallel on a separate worker — no file collisions with Track A.

---

### 10.3 K1 — `_snip_args` execution-guard
**Why:** `_snip_args` (`packages/core/src/disco/core/events.py:287-294`, called ~:251) is UNGATED and
renders any tool arg >1500 chars as `"<N chars elided — already applied; use file_read for the
content>"`. A weak model COPIES that marker string into a NEW `file_write`/`slides_generate` arg →
executes → writes the 72-byte placeholder over real content (DATA LOSS) and the elision self-perpetuates.
This corrupts the SLIDES/artifact path (it is NOT the build-loop driver — that was the demoted keystone).
**Change:**
1. `events.py` — define the marker as a single regex constant (e.g. `_ELISION_MARKER_RE`) so the shaper
   and the guard share one source of truth.
2. NEW execution-time guard in the tool dispatch path (where args are finalized before the tool runs —
   the executor/`_execute_and_observe` seam): if any string arg CONTAINS the elision marker, REJECT the
   call with a tool error: "Argument for `{k}` contains an internal elision placeholder, not real
   content. Use the CURRENT WORKSPACE block (or `file_read`) to get the actual content, then resend the
   full argument." Do NOT execute. Optionally rehydrate from the live snapshot if the path is known.
3. Reword the shaper's marker so it points at the in-context snapshot, never "use file_read" (which
   invites the read-loop): `"<{n} chars — full content is in the CURRENT WORKSPACE block below>"`.
**Tests:** `packages/tools` (or core) unit — an arg carrying the marker is rejected, never written;
a legit large arg passes; round-trip a slides_generate with a >1500-char body.
**Risk:** small/MED (touches dispatch). **Files:** `events.py`, the executor/dispatch module, `files.py`.

### 10.4 Phase 0 — regression harness (encodes 10.1)
**Why:** make loop-death provable WITHOUT a live model so every wave is measurable.
**Build:** `packages/core/tests/loop/test_build_loop_regression.py` + fixtures derived from the golden
trace. Two layers:
1. **Pure-function metric assertions** over a synthesized event sequence: feed the loop's
   view-render/stuck/dedup/finish pure functions a constructed 13-file build history and assert the
   10.1 metrics (e.g. a file already shown fresh is NOT re-marked "re-read"; a stale file IS named; the
   breaker fires on a thought-varied read-loop; finish() terminates < 150 events).
2. **(Optional) golden replay:** parse `macos-build-trace-6-17-26.txt` into events and assert the new
   pure-function pipeline would not have emitted the re-read pressure that produced the 82 reads.
**Acceptance:** the harness FAILS on today's code (proving it captures the bug) and is the gate each
W-wave must turn green. **Risk:** none. **Files:** new test + a small fixture loader.

---

### 10.5 W1 — thought-excluded, wait-exempt read-aware breaker  *(~80% already built)*
**Root cause (CONFIRMED in our code):** `equality.py:~38` `event_content_eq` compares
`a.thought == b.thought` — we independently re-derived the exact OpenHands `_event_eq` bug. A model that
paraphrases its reasoning each turn while emitting the identical tool call defeats stuck patterns 1/2/4
and the no-progress dedup. Plus there is no wait/poll exemption → a legit "poll until server up" loop
trips pattern-1 (OpenHands #5355 false-positive class).
**Already present (do NOT rebuild):** 20-event window (`stuck.py:~65` `scan_window`, sliced in
`engine.py:~703`), last-user-message reset (`stuck.py:~109`), patterns 1–4 (`stuck.py:~272/283/294/307`),
per-file spiral (`:~191`), semantic no-progress (`:~334`), escalate-then-halt (`turn_control.py:~286`
`gate_stuck`). The disposition layer needs NO change.
**Changes:**
1. `equality.py` `event_content_eq` — add keyword `ignore_thought: bool = False`; gate the
   `a.thought == b.thought` term behind it:
   `return ((ignore_thought or a.thought==b.thought) and a.tool_call.tool_name==b.tool_call.tool_name and a.tool_call.arguments==b.tool_call.arguments)`.
   Pass `ignore_thought=True` ONLY at the StuckDetector call-sites (`stuck.py:~279/290/316/318-320` and
   the no-progress distinct-edit dedup `:~384`). Leave idempotency/dedup callers on the default — verify
   `dedup.py` callers first.
2. `stuck.py` (~near :54) — add `_WAIT_POLL_TOOLS = frozenset({"sleep","wait","server_status","poll","browser_wait","job_status","deploy_status"})`.
   In patterns 1 (`_repeated_action_observation` :~272) and 4 (`_alternating` :~307), filter out
   `ActionEvent`s whose `tool_call.tool_name in _WAIT_POLL_TOOLS` before building the pair/alt stream.
   Do NOT filter in pattern 2 (`_repeated_action_error` :~283) — a perpetually-erroring poll IS stuck.
3. `stuck.py:~61` — raise `repeat_action_observation` 3→4 (OpenHands value; fewer FPs on one legit retry).
4. NEW `_pure_repeat` method: over `[e for e in window if isinstance(e, ActionEvent) and tool_name not in _WAIT_POLL_TOOLS]`,
   compute the trailing run where `event_content_eq(x, last, ignore_thought=True)`; fire at ≥ threshold.
   OR it into `evaluate()` (`stuck.py:~170`). (Catches back-to-back identical actions with no paired obs.)
5. **Canonical-args for hashing** (used wherever args are compared/hashed for repetition): sort keys;
   drop volatile keys (`call_id`,`request_id`,`idempotency_key`,`timestamp`,`nonce`,`cursor`); collapse
   runs of whitespace ONLY in prose-type keys; normalize path separators/`./`/trailing-slash for
   path-type keys; leave everything else EXACT. A different path/command/line-range MUST remain different.
**Tests:** thought-varied identical reads → fires; legit poll loop → does NOT fire; A-B-A-B varied-thought
→ fires; editing file A,B,C in sequence → does NOT fire.
**Risk:** LOW (pure-function edits in `stuck.py`/`equality.py`; no engine rewiring).

### 10.6 W2 — stale-aware snapshot + read-dedup-for-all  *(subsumes old A1+A3; refactor of our code)*
**Root cause (CONFIRMED):** `view_render.py:61-236 workspace_snapshot_message` re-reads & re-dumps the
FULL content of every tracked file EVERY turn (caps `_WS_MAX_FILES=8`/`_WS_PER_FILE_CHARS=6000`/
`_WS_TOTAL_CHARS=16000` at :41-43); files > caps get a marker (:193-199) that says "call file_read … do
NOT call file_write" — the literal loop generator. `windows.js` (8047B) > both the 6000 snapshot cap and
the 7000 `file_read` budget (`files.py:23`) → never "fully seen" → re-read 20× (Phase-0 measured). Aider VALIDATES our
post-history fresh-read placement (`chat_chunks.py`); we just over-apply it.
**Changes:**
1. NEW `packages/core/src/disco/core/loop/file_state.py` — `FileStateTracker`:
   - per-path `FileSnap(last_seen_seq:int, sha:str, mtime:float|None)`.
   - `record_agent_io(path, content, seq, mtime)` — called after a successful file_read OR
     file_write/edit/append; an agent WRITE pre-registers the new sha (so a self-write is never "stale").
   - `async stale_paths(sbx, working_set) -> list[str]` — for each working-set path, read current disk
     content (timeout `_WS_READ_TIMEOUT_S`, swallow FileNotFound/Timeout/Permission) and compare sha to
     last-seen; return the differing paths. **Content-sha, NOT a chokidar watcher** — sandbox-robust and
     stronger than mtime (no false positive from a no-op touch).
   - `file_state_notice(stale) -> LLMMessage | None` — returns None when `stale` is empty (the whole
     point: SILENT when nothing changed); else a NAMED block "# Files changed on disk since you last read
     them … re-read with file_read before editing" listing ONLY the stale paths (no bodies).
   - Working-set source = `messages.py:42-70 _workspace_paths_from_events` (our existing `(mutated,
     read_only)`, mutated-first).
2. `view_render.py:61-236` — emit a FULL body only for files in (stale ∪ mutated/deliverable ∪
   never-shown-in-full); collapse every other tracked file to a one-line "✓ {path} — current, shown
   earlier" pointer. Keep the disk-fresh re-read and the post-projection placement (correct; it stays
   out of `View.of` condensation). Budget by chars (already does); keep `ordered = mutated + read_only`.
3. `view_render.py:193-199` — REPLACE the oversize "re-read / do NOT file_write" marker with a
   windowed-view directive: show head + `… [{n} more chars — this file is large. To see a region:
   file_read(path, offset=L, limit=M). To change it: file_edit with a content anchor, or
   file_replace_lines on a freshly-read range.] …` + tail. The windowed read ALREADY EXISTS
   (`files.py:112-117` `FileReadArgs.offset/limit`); stop forbidding writes.
4. `dedup.py:9-15` — UN-gate the RENDER-TIME history body-collapse for ALL tiers. NEW
   `collapse_superseded_reads(messages, events)`: for each path, find the latest authoritative copy
   (newest file_read result OR the live snapshot) and rewrite every EARLIER file_read output for that
   path to `"[superseded file_read of {path} — current content shown later in this prompt]"`. Keep F9's
   EXECUTION-time short-circuit assist-gated (it changes behavior, not just rendering).
5. `ViewBuilder.build` (`view_render.py:~327-394`) — assemble, after condensation and outside `View.of`:
   `collapse_superseded_reads(history)` → `file_state_notice(stale)` → reduced working-set bodies →
   (optional, deferred) `structural_map`.
**Tests:** unchanged file across turns → body shown once then collapsed to pointer, NOT re-dumped;
externally-changed file → named in the stale notice; oversize file → windowed directive, no "do-NOT-write"
marker; the 13-file golden case → < 20 reads (10.1).
**Risk:** LOW-MED (refactor of `view_render.py`/`dedup.py`; new `file_state.py`).

### 10.7 W3 — syntax-gate every write + auto-revert  *(net-new; highest-value; clobber fix at the seam)*
**Root cause (CONFIRMED):** `files.py` has NO syntax/lint gate anywhere — every writer does a raw
`await ctx.sandbox.write_file(...)`: `file_write:267-273`, `file_edit:445`, `file_append:305`,
`file_replace_lines:513`, `file_insert_lines:563`. A weak model that regenerates a file from lossy
memory clobbers it with no check. SWE-agent's lint-gated AUTO-REVERT (the flow that ~doubled its
SWE-bench) is the fix. (OpenHands lints+reports but does NOT revert — SWE-agent is the reverter.)
**Changes (clean-room of SWE-agent `windowed_edit_linting/bin/edit` + OpenHands diff-filter):**
1. `files.py` NEW `_syntax_errors(path, text) -> list[str]`:
   `.py`→`compile(text,path,"exec")`; `.json`→`json.loads`; `.html/.css/.js/.ts/.tsx/.jsx/.yaml/.yml`
   → tree-sitter parse, collect ERROR nodes; unsupported suffix → `[]` (never block what we can't parse).
2. `files.py` NEW `async _gated_write(ctx, path, new_bytes, old_text) -> ToolOutcome | None`:
   compute `pre = _syntax_errors(path, old_text)` (or `[]` for a new file) and `post =
   _syntax_errors(path, new_text)`; `introduced = [e for e in post if e not in pre]` (the diff-filter —
   only NEW breakage, so pre-existing-messy files aren't punished). Write the new bytes (autosave). If
   `introduced` and `old_text is not None`: REWRITE old_text back (AUTO-REVERT) and return a FAILURE
   `ToolOutcome(success=False, error="syntax_gate_reverted", content="Your edit to {path} introduced
   syntax error(s); it was NOT applied (previous content kept): … Fix the snippet and try a DIFFERENT
   edit. DO NOT re-run the same failed edit — it will fail identically.")`. Else return None (caller
   proceeds to its normal success outcome).
3. Route all 5 writers through it (old text is already read at each site — reuse the F3 read at
   `files.py:~251` for file_write; `text` at `:~417` for file_edit; the range reads for
   replace_lines/insert_lines; `existing|""` for append).
**Tests:** writing unparseable Python over a good file → reverted, old content intact, failure message;
new file with a syntax error → applied (no old to revert to) but flagged; a file type we don't parse →
written normally; a pre-existing lint error is NOT counted as "introduced".
**Risk:** MED (touches every writer; the diff-filter is what makes it safe). **Depends on:** nothing;
**must precede W4.**

### 10.8 W4 — capability-gated edit format (whole vs anchored)  *(uses existing assist seam)*
**Evidence (CONFIRMED):** Aider defaults unknown/weak models to `edit_format="whole"` (`models.py`) and
promotes only recognized-capable models to `diff`; the Qwen3 benchmark shows whole = higher pass-rate +
~100% well-formed for weak OPEN models (diff-matching is what they fail). Our existing weak-tier signal
is `ctx.assist` (`files.py:243,269`).
**Changes:**
1. `core/llm/types.py:~48` — add `Requirement.ANCHORED_EDIT = "anchored_edit"`.
2. `core/llm/config.py` (`ModelEntry.capabilities`) — set `ANCHORED_EDIT` ONLY on models that benchmark
   well on diff edits (the "gpt-oss-120b if it benchmarks" gate). Default-off ⇒ unknown/weak → whole.
3. Tool-layer policy keyed on `ctx.assist` / `ANCHORED_EDIT`:
   - **Weak tier:** route toward whole-file `file_write` (+ W3 gate); keep `_forgiving_replace`'s
     first-occurrence tolerance for `file_edit` (the weak affordance, `files.py:343-368`).
   - **Capable tier:** offer a NEW `file_str_replace` tool — clean-room of OpenHands `str_replace`:
     read whole current file, find ALL occurrences via escaped match, require EXACTLY ONE (multiple →
     "Multiple occurrences … please ensure it is unique" with line numbers; zero → whitespace-strip
     retry once, then "did not appear verbatim"). Anchored = structurally clobber-proof.
   - **Withhold** `file_str_replace` from the weak tier via Disco's existing capability tool-withholding
     (one policy point; no per-call branching in the loop).
**Tests:** weak model gets whole-file path + no `file_str_replace`; capable model gets `file_str_replace`
with unique-match enforcement; a non-unique `old_str` → error, no write.
**Risk:** MED. **Depends on:** W3 (the gate the whole-file weak path relies on).

### 10.9 W5 — C18 done-condition + unified finish-gate  *(consolidation)*
**Root cause (CONFIRMED):** C18 `file_exists` resolves against a None `workspace_path`/`executor.sandbox`
(`plan_conditions.py:163-197`; `base.py:97-118` exposes no `workspace_path`; `ProcessSandboxInstance`
stores private `_workspace` at `process.py:41`) → falls back to the agent-server CWD, not `/workspace` →
every `plan_step(done)` answers "missing" even for served files (the "steps slow to check off"
whiplash). Separately, the finish-verify loop is ALREADY bounded (cap-3 at `finish.py:50/59/720` + noop
valve) — the only UNcapped gate is `gate_execution_nudge` (`finish.py:658-659` "No cap").
**Changes:**
1. `plan_conditions.py:163-197` — resolve `file_exists` against the REAL sandbox FS (the same
   `getattr(executor,"sandbox",None)` + workspace path the snapshot uses), not the agent-server CWD. Add
   a `workspace_path` accessor to the Sandbox protocol (or expose `_workspace` via a property on
   `ProcessSandboxInstance`) so C18 and the snapshot share one resolution. "Done" = ONE existence/build
   check run in the resolved workspace cwd — never a model-asserted checkbox.
2. `finish.py:658` — give `gate_execution_nudge` an explicit cap-3 for parity, releasing with a visible
   warning rather than relying solely on the noop valve. (Optional but recommended: factor the four
   cap-3 gates into one `verify-once` envelope helper to remove drift.)
**Tests:** a file present in the live snapshot → C18 reports present (not "missing"); plan steps check off
once the artifact exists; execution-nudge stops after 3.
**Risk:** LOW-MED. **Files:** `plan_conditions.py`, `finish.py`, `base.py`/`process.py` (protocol).

### 10.10 W6 — browser + preview + prompts + vision-verify  *(former A5/A6/A7/B3; mechanical)*
**Root causes (CONFIRMED):** browser element walker matches only `a,button,input,select,textarea,
[role="button"],[onclick]` (`_browser_daemon.py:179-204`) → a dock built from `<div>`+addEventListener
has no `[data-pmx-index]` → every `click(index)` times out 30s. Preview serves the workspace ROOT
(`session.py:420-422`) but the app is in a `macos-clone/` subdir → "text, no design"; post-teardown
`wake_for_preview`→None → "preview not available" (`preview.py:46-51`). No vision (DRIVER_VISION off,
`browser.py:194`) yet the prompt mandates "a build you have not seen render is not finished"
(`prompts.py:212-214`) → unbounded visual-verify pressure.
**Changes:**
1. Browser: broaden the element walker to text/CSS-selector clicks (match by visible text + arbitrary
   CSS), index `<div>`/`[onclick]`/cursor:pointer elements; cut the click timeout to a few seconds.
   (`browser.py`, `_browser_daemon.py`.)
2. Preview: detect the app's index.html directory and serve THAT subdir (not workspace root); keep the
   managed preview session alive after `finish()`. (`session.py`, `preview_service.py`, `preview.py`.)
3. Prompts: anti-monolith steer + single-verify-pass framing; make the visual-verify mandate
   vision-aware (only demand "see it render" when a vision-capable verifier exists). (`prompts.py`.)
4. Vision auto-enable per §2: runtime capability detection (llama.cpp `/props.modalities.vision`,
   OpenRouter `input_modalities`, Anthropic family rule, OpenAI static table) at `wiring.py:~41`; change
   `browser.py:194` to capture a screenshot when the verifier has `Requirement.VISION` OR a
   `vision_escalation_model` is configured.
5. No-test UI finish-gate: define "done" = build exit 0 ∧ lint clean ∧ ONE Firefox screenshot, all run
   ONCE through the existing bounded gate. Extend `gate_browser_verify` (`finish.py:704-757`) to capture
   one Firefox (NOT Chromium — host can't rasterize in Chromium) screenshot as the vision artifact,
   satisfying the `prompts.py:212-214` mandate with real evidence.
**Tests:** click a div-based dock icon → no timeout; preview of a subdir app → CSS-bearing 200; vision
auto-enables for a vision-capable driver; finish-gate captures a screenshot. Live: Playwright/Firefox
screenshot of the rendered macOS design.
**Risk:** LOW. **Files:** `browser.py`, `_browser_daemon.py`, `session.py`, `preview_service.py`,
`preview.py`, `prompts.py`, `wiring.py`, `config.py`, `finish.py`.

### 10.11 DEFERRED — D1 tree-sitter structural map  *(the ONLY high-risk piece; off critical path)*
NEW `packages/core/src/disco/core/loop/repo_map.py`, default-OFF. Engages only when project file count
exceeds `_REPO_MAP_FILE_THRESHOLD=40`. Signatures-only (tree-sitter tags), EXCLUDES the working set,
ranked, binary-search to a ~1024-token budget, `.disco/tags.cache` mtime-keyed cache (Aider `repomap.py`
pattern). Behind a flag for A/B vs the W2 snapshot. **Schedule only after W1–W6 ship and only if real
build sizes justify it** — for from-scratch builds there is almost nothing to map (the OSS winners all
use on-demand reads, not maps, for write-heavy work).

### 10.12 Live acceptance (after W1–W6)
Re-run `build a simple macosx clone` + the `do not create a single monolithic html file` steer on the
disco-live runner. Assert: the 10.1 metric table is met, AND a Playwright/Firefox screenshot shows the
rendered macOS-style design (dock, menu bar, windows) — visual evidence, sent via SendUserFile. Then,
and only then, mark Track A complete.

---

## 11. TRACK B — DEEP RESEARCH EXECUTION PLAN (RATIFIED 2026-06-17)

The Deep Research polish track. Fully PARALLEL to Track A — no file collisions (Track B is
`agent-server` + `retrieval` + `frontend`; Track A is `core` loop + `tools`). Low engine risk.
Runs on a separate worker. Source issues: §1 verbatim notes in `docs/dylans-runthru-6-17-26-v2.md`;
research lanes R2 (recency) + R4 (export template) in `docs/runthru-v2-research-digest.md`.

> **CAUTION on line numbers:** as-of-research (2026-06-17); re-locate symbols before editing (Serena
> `find_symbol` / by name). Symbol + behavior is authoritative, line number is a hint.

### 11.0 Invariants for the whole track
- **Two surfaces use ResearchAgent:** `research` + `deep_research` (prose answer = completion, never
  gated). Don't touch BuildAgent paths.
- **OFF = byte-identical:** every new toggle (recency, attach) defaults OFF and, when off, must produce
  byte-identical output to today. The MD export path keeps byte-parity always.
- **Driver:** dev runs on `or-gpt-oss-120b-free` (OpenRouter, FREE models only in dev). Never local
  llama-server while the freeze investigation is open ([[perpleximanus-openrouter-only]]).
- **Fitness gates per item:** the four Python gates (basedpyright 0 / lint-imports / arch-budget /
  diagram) for agent-server/retrieval changes; for frontend, `npm run typecheck:build` + `npx vitest
  run` + `npx vite build`. **Visual evidence MANDATORY** for every UI-touching item: a real Firefox/
  Playwright screenshot in the running app + SendUserFile — a green vitest count is NOT evidence.
- **Harness-from-real-samples:** any feature change is verified against VERBATIM captured real DR output
  (pull from the real service, inject through the common endpoint); real results only.

### 11.1 Sequencing
```
DR-1 (correctness one-liners + export/audio)  ── ship FIRST (smallest, unblocks user pain)
DR-2 (branded export template)                 ── after DR-1's shared report_export.py edits
DR-3 (recency toggle)                          ── PARALLEL to DR-1/DR-2 (different files); 2nd sub-worker
DR-4 (attach files)                            ── LAST (P2; depends on upload plumbing)
(separate small wave) keep-searching-on-no-answer ── tracked, independent, schedule with DR-3
```
DR-2 depends on DR-1 only because both edit `report_export.py` (sequence to avoid conflicts). DR-3 and
DR-4 are independent file sets and can run concurrently with DR-1/DR-2.

---

### 11.2 DR-1 — correctness one-liners + export/audio fixes  *(ship first)*
Seven small, high-signal fixes. Each is independently testable.

- **D1 — follow-up citation leak (1-char fix).** `packages/agent-server/src/disco/agent_server/
  deep_research_service.py:701` — change `f"[{pid}] ({src})"` → `f"[[{pid}]] ({src})"`. The single
  `[pid]` renders as a raw citation id (`238afc_p11`) in follow-up answers instead of a linked chip; the
  double-bracket form matches the main report's citation tokenizer. **Test:** a follow-up answer renders
  a clickable citation chip, not a raw id. **Risk:** trivial.
- **C1 — TTS markdown normalizer.** NEW `_normalize_for_tts(text)` (strip markdown: headings, `**bold**`,
  `[links](url)`, list bullets, code fences, citation chips) applied at BOTH audio-synthesis sites
  (single-voice + podcast). The on-screen TRANSCRIPT stays RAW markdown — only the text handed to the
  TTS engine is normalized. **Locate:** the report-audio generation path in agent-server (the
  `generate_report_audio`/`_generate_turn_script` area). **Test:** synthesized script contains no `*`,
  `#`, `[`, backticks; transcript unchanged. **Risk:** low.
- **C2 — acronym-first-mention prompt.** In the audio-script generation prompt, instruct: expand each
  acronym on first mention ("HTTP/3 (Hypertext Transfer Protocol version 3)") so TTS doesn't read
  letter-soup. **Test:** a known-acronym report's script expands on first use. **Risk:** low (prompt).
- **B1 — bottom-card MD follow-ups.** `frontend/src/components/research/NeedMoreCard.tsx:154-159` — the
  bottom (non-FSA) markdown export path drops `followUpSeqs`; route it through the server endpoint that
  includes follow-up Q&A (same path the top card uses). **Test:** bottom-card MD export contains the
  follow-up section. Visual evidence. **Risk:** low.
- **B2 — DOCX follow-ups at all 4 layers.** `NeedMoreCard.tsx:176, :214` carry `fmt!=="docx"` guards that
  exclude follow-ups from DOCX; remove the exclusion at all 4 layers so DOCX matches PDF/MD. **Test:**
  DOCX export includes follow-ups. Visual/file evidence. **Risk:** low.
- **B3 — audio mode-dialog render + cache key.** `NeedMoreCard.tsx:555-565` — the `AudioModeDialog` only
  renders in the idle branch, so it's unreachable once a report is loaded; lift it out of the idle
  branch. Also change the audio cache key from a hardcoded/`conv_`-based key to a CONTENT-HASH so
  regenerating after an edit busts the cache. **Test:** the audio mode dialog opens on a loaded report;
  editing content yields a fresh audio file. Visual evidence. **Risk:** low-MED (UI state).
- **B4 — personalized filenames (×4).** Kill the hardcoded `audio-overview.mp3` (`NeedMoreCard.tsx:588`)
  and the `conv_<id>` export filenames; derive from the report title (slugified) for all 4 export types
  (PDF/DOCX/MD/audio). **Test:** downloaded files are named from the report title. **Risk:** low.

### 11.3 DR-2 — branded export template  *(after DR-1's report_export.py edits)*
**Why:** the #1 export formatting defect is `nl2br` (`packages/agent-server/src/disco/agent_server/
report_export.py:120`) flattening structure; and DOCX has no styled reference doc
(`serialize_docx:198-221`). Replace string-flattening with structured-model rendering, branded via the
§1 theme engine.
**Changes:**
1. Replace `_markdown_to_html(serialize_markdown(...))` with NEW `_build_pdf_html(report, follow_ups)`
   that builds HTML from the STRUCTURED report model (sections, citations, follow-ups), NOT the
   flattened markdown string. **DROP `nl2br`** entirely.
2. Full print-CSS skeleton (from R4 notes): cover block (Fraunces title, Newsreader-italic subtitle,
   accent rule, metadata row) + running header/footer + numbered sections (Schibsted Grotesk
   `02 — Title`) + inline `[[id]]` source chips + follow-up Q&A on its OWN page + sources appendix; TOC
   when >10 sections; 2-col sources when >30; page-break hygiene.
3. Consume the OFL fonts + tokens from the **`core/brand`** engine (§1.1-1.4) — NOT a new agent-server
   bundle. `_build_pdf_html` prepends `core.brand.font_face_css() + theme_css_vars(theme) +
   print_skeleton_css()`; `serialize_pdf` loads via WeasyPrint `FontConfiguration` (§1.3). **AVOID** the
   `:first-letter{float}` drop-cap — WeasyPrint asserts on it; use an inline accent-colored glyph (§1.4).
   **DR-2 and §1.5 are the SAME `_build_pdf_html` work** (DR-2 = structure/`nl2br`-drop; §1.5 = theme/token
   wiring) — build as ONE change, sequenced after DR-1's `report_export.py` edits.
4. DOCX: build a styled `reference.docx` (mirroring the same type/brand) and pass pandoc
   `--reference-doc` (+ `--toc`). MD path UNCHANGED (byte-parity).
5. Wire through the §1 THEME ENGINE: Disco-brand default (light) + a neutral/plain theme option; theme
   is a per-export setting, both reports and (Track C) decks consume the same token set.
**Tests:** PDF renders cover + numbered sections + follow-up page + sources appendix with the bundled
fonts (Firefox/visual evidence on the generated PDF); DOCX opens in Word/LibreOffice with the reference
styles + TOC; MD byte-identical to today; a >10-section report gets a TOC; a >30-source report gets
2-col sources. **Risk:** MED (rendering). **Files:** `report_export.py` (`_build_pdf_html`,
`serialize_pdf`, `serialize_docx`); the font/token engine is `core/brand` (§1, NOT a new agent-server
fonts dir); new `reference.docx` (DOCX styles, mirrors the `core/brand` type/brand).

### 11.4 DR-3 — recency toggle  *(parallel; OFF = byte-identical)*
**Why:** no way to bias research toward recent sources; no date injection at any of the 6 prompt sites.
**Design:** `recency_window: "month" | "week" | None(default)`. Two layers — time-filtered search +
date injection into prompts (the latter is the highest-ROI, provider-independent win).
**Changes:**
1. **E1 — time-filtered search.** Add `time_filter: str | None` to the `SearchProvider` protocol;
   implement in `DdgsSearchProvider` (pass `timelimit="m"|"w"`) and `SearxngSearchProvider` (pass
   `time_range="month"|"week"`). **Avoid `"day"`** (near-zero results). ~60–80% honored — net-positive.
2. **E2 — thread the window.** `recency_window` on `CreateConversationBody` → `DeepResearchService.
   set_recency` → `DeepResearchRun` → `gather_for_subquestion` → `RetrievalRequest` (+ add
   `recency_window` to `RetrievalRequest`).
3. **E3 — UI.** A `RecencySelector` next to the depth-tier control (Off / Past month / Past week).
4. **E4 — date+recency prompt injection** (highest ROI): inject the current date + a recency directive
   into `decompose_query` (BIGGEST win), `synthesize_section`, and `coherence_pass`. (`_gap_reason` is a
   v2 stretch.)
**OFF path:** when `recency_window is None`, no `time_filter` is passed and no date/recency text is
injected → byte-identical to today (assert this in a test).
**Tests:** Off → byte-identical search calls + prompts; "month" → ddgs `timelimit="m"` / SearXNG
`time_range="month"` + date line present in decompose; UI selector persists. **Risk:** LOW-MED.
**v2 stretch (NOT now):** `site:reddit.com`/`site:news.ycombinator.com` extra gather legs; last30days-
style entity/engagement scrape (needs platform API keys; we're keyless).

### 11.5 DR-4 — attach files  *(P2, LAST; v1 = plaintext/md/csv only)*
**Why:** users can't attach source files to a research/agent task at start (build steer-attach works
mid-run; the initial box and DR/basic have none).
**Changes:**
1. **F1 — initial-box upload.** Pre-create the conversation id → mount the `UploadComposer` in the
   INITIAL message box across build/agent/DR (today only build's mid-run steer-attach works).
2. **F2 — DR files-as-sources.** Uploaded files → `Passages` with `corpus_id=user_upload` → into the
   vector store → retrievable + CITED alongside web sources in the report.
3. **F3 — basic-research seed_passages.** Feed uploads as seed passages for basic (non-DR) research.
**Scope v1:** plaintext / markdown / csv only (no PDF/binary parsing in v1).
**Tests:** attach a .md at task start → it appears as a cited source in the DR report; basic research
uses it as a seed. Visual evidence. **Risk:** MED (upload plumbing + retrieval wiring). **Depends on:**
the existing upload/Passages infra.

### 11.6 Separate small wave — keep-searching-on-no-answer  *(tracked; schedule with DR-3)*
Issue #1/F1: when a search leg returns nothing usable, do a BOUNDED reformulate-and-retry rather than
giving up. Implement in `streaming.py` (the DR streaming driver) as a capped retry (reformulate the
subquery N≤2 times on empty/low-yield). Bounded to avoid a search loop. **Test:** an initially-empty
subquery reformulates once and proceeds; never exceeds the cap. **Risk:** LOW-MED.

### 11.7 Track B acceptance
Run 2–3 real Deep Research reports end-to-end (HTTP/3, fasting, vector-DB style topics). Assert: D1
citation chips render in follow-ups; TTS script is markdown-free with acronyms expanded; PDF/DOCX/MD all
carry follow-ups with the branded template + bundled fonts; recency Off is byte-identical and "month"
visibly biases sources; attached .md is cited. Visual evidence (Firefox screenshots + the generated
PDF/DOCX) sent via SendUserFile for every UI/export item.

---

## 12. TRACK C — gating summary
Track C (artifacts/slides/editor/image-gen) is GATED behind **K1** (§10.3) + Track-A Phase A. The full
§10-grade build specs now exist: **§3** (slides + artifacts + image-gen, items C1–C8) and **§4** (the
in-browser editor + element→agent substrate, items 4.1–4.6). Two execution gates remain inside those specs:
(1) **K1 must merge** before any artifact path ships (the `_snip_args` guard, §10.3); (2) the **#34
experiment (§3 C4) must return `docs/slides-experiment-verdict.md`** before the authoring-schema-bound items
(C1 Layer-1 fields, C2 prompts, the §4 deck-patch/editor) are coded — everything marked **[experiment-gated]**
/ **schema-frozen-by-#34** in §3/§4 waits on that verdict; the schema-INDEPENDENT items (C5/C6/C7, the §4
overlay/bridge/envelope plumbing) can proceed once K1 lands. §3 + §4 are authoritative.
