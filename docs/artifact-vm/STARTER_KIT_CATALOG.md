# Artifact-VM — Starter Kit Catalog (SPEC ONLY)

> **Read `_GROUNDING.md` first.** Real kit registry: `packages/core/src/disco/core/kits/starter.py`.
> Real scaffolder: `packages/tools/src/disco/tools/builtin/scaffold_starter.py`.
> **This is specification, not code.** Only **`app_shell`** and **`lead_form`** are real
> registered kit ids today (`StarterKitRegistry._BUILTINS`). Every other kit in this catalog is
> **(GAP)** — proposed new kit data to author. Nothing here is implemented.
>
> **Direct-edit metadata** uses the `data-disco-*` attribute family. Those attributes are
> defined in the sibling spec **`DIRECT_MANIPULATION_SPEC.md` (GAP — to author)**; this catalog
> cross-references them. The pinned set used below:
> `data-disco-field`, `data-disco-section`, `data-disco-file`, `data-disco-media-slot`,
> `data-disco-screen-label`, `data-disco-metric-id`, `data-disco-comment-anchor`.
>
> **Fixture status (verified 2026-06-30):** the directories
> `fixtures/starter-kits/{admin_table, app_shell, browser_window, deck_stage, document_frame,
> image_slot, lead_form, metrics_overlay, mobile_frame, tweak_panel}/` **exist but are currently
> empty** — the reference HTML is to be ADDED at `<kit>/<kit>.html`. The kits
> `workflow_setup_card`, `form_receipt`, `daily_brief_layout`, `animation_stage` have **no
> directory at all** → marked **fixture-TODO**. Each row below records the expected fixture path
> honestly (dir-present-HTML-pending vs no-dir).

---

## Catalog summary table

| Kit | Registered today? | Maps to ContractKind | Backing CD starter component (proposed) | Direct-edit support | Fixture file (status) |
| --- | --- | --- | --- | --- | --- |
| `app_shell` | **YES** (`starter.py`) | `static.site`, `interactive.prototype` | App Shell / Page Layout | section + field anchors | `fixtures/starter-kits/app_shell/app_shell.html` *(dir present, HTML pending)* |
| `lead_form` | **YES** (AppKit default) | `appkit.leadgen` | Lead / Contact Form | field + section + media-slot anchors | `fixtures/starter-kits/lead_form/lead_form.html` *(dir present, HTML pending)* |
| `admin_table` | **GAP** | `appkit.leadgen` (admin view) | Data Table / Admin Grid | section + field + comment-anchor | `fixtures/starter-kits/admin_table/admin_table.html` *(dir present, HTML pending)* |
| `browser_window` | **GAP** | `interactive.prototype`, `static.site` | Browser Chrome / Window frame | screen-label + section | `fixtures/starter-kits/browser_window/browser_window.html` *(dir present, HTML pending)* |
| `mobile_frame` | **GAP** | `interactive.prototype` | Device / Phone Frame | screen-label + section | `fixtures/starter-kits/mobile_frame/mobile_frame.html` *(dir present, HTML pending)* |
| `deck_stage` | **GAP** | `deck` | Slide / Presentation Stage | section(=slide) + field + media-slot | `fixtures/starter-kits/deck_stage/deck_stage.html` *(dir present, HTML pending)* |
| `document_frame` | **GAP** | `document` | Document / Article Layout | section + field + media-slot | `fixtures/starter-kits/document_frame/document_frame.html` *(dir present, HTML pending)* |
| `image_slot` | **GAP** | `deck`, `document`, `static.site` | Image / Media Placeholder | media-slot + field | `fixtures/starter-kits/image_slot/image_slot.html` *(dir present, HTML pending)* |
| `metrics_overlay` | **GAP** | `appkit.leadgen`, `workflow.output` | Stat / Metric Card | metric-id + field | `fixtures/starter-kits/metrics_overlay/metrics_overlay.html` *(dir present, HTML pending)* |
| `tweak_panel` | **GAP** | `appkit.leadgen`, `interactive.prototype` | Controls / Settings Panel | field (bound to TweakField keys) | `fixtures/starter-kits/tweak_panel/tweak_panel.html` *(dir present, HTML pending)* |
| `workflow_setup_card` | **GAP** | `workflow.output` | Form / Setup Card | field + section | `fixtures/starter-kits/workflow_setup_card/workflow_setup_card.html` *(fixture-TODO, no dir)* |
| `form_receipt` | **GAP** | `appkit.leadgen`, `workflow.output` | Confirmation / Receipt | field + comment-anchor | `fixtures/starter-kits/form_receipt/form_receipt.html` *(fixture-TODO, no dir)* |
| `daily_brief_layout` | **GAP** | `document`, `workflow.output` | Dashboard / Brief Layout | section + metric-id + field | `fixtures/starter-kits/daily_brief_layout/daily_brief_layout.html` *(fixture-TODO, no dir)* |
| `animation_stage` | **GAP** | `interactive.prototype`, `deck` | Canvas / Motion Stage | section + field | `fixtures/starter-kits/animation_stage/animation_stage.html` *(fixture-TODO, no dir)* |

> CD-component mappings are marked *(proposed)* in the header because the exact Claude Design
> component names are not pinned in `_GROUNDING.md`; treat them as the design intent, not a
> verified registry name. Per grounding §10, CD `dc_*` artifact concepts are realized by the
> kind-specific real tools (`file_*`, `app_*`, `slides_generate`/`deck_patch`).

---

## Per-kit specifications

Each kit lists: purpose; user value; files expected; minimal fixture outline (+ path);
required `data-disco-*` metadata; editable fields; required tests; CD-component mapping;
how it supports direct editing. Every kit ends with **Failure modes** + **Tests required**
(grounding §11.3).

---

### 1. `app_shell` — REGISTERED (`starter.py`)

- **Purpose:** Minimal, self-contained HTML page frame (doctype, viewport, inline `<style>`,
  `<main>` with an `<h1>` + intro `<p>`) the model edits instead of hand-drawing chrome.
- **User value:** A page that renders styled content immediately; no blank-page false start.
- **Files expected:** `index.html` (only). Source: `_APP_SHELL_HTML` in `starter.py`,
  parameterized by `title` (HTML-escaped).
- **Minimal fixture outline** (`fixtures/starter-kits/app_shell/app_shell.html`, *dir present,
  HTML pending* — should mirror `_APP_SHELL_HTML`):
  ```
  <!doctype html><html lang=en><head> meta charset, meta viewport, <title>{title}</title>,
  inline <style> (:root vars, body, main, h1, p) </head>
  <body><main data-disco-section="main">
    <h1 data-disco-field="headline">{title}</h1>
    <p data-disco-field="intro">Start building…</p>
  </main></body></html>
  ```
- **Metadata required:** `data-disco-section="main"`; `data-disco-field` on the heading and
  intro; `data-disco-file="index.html"` on `<html>` (so edits resolve to the entrypoint).
- **Editable fields:** `headline`, `intro`; theme via `:root` CSS vars (`--fg`/`--bg`/`--accent`).
- **CD component:** App Shell / Page Layout *(proposed)*.
- **Direct editing:** click a `data-disco-field` → in-place text edit → routed through
  `file_edit` on `index.html` (targeted-edit law); color vars editable via a tweak surface.
- **Failure modes:** companion CSS/JS file split (half-stream → blank); unescaped `title`
  breaking markup; field anchors missing → no direct-edit affordance.
- **Tests required:** scaffolds to exactly `{index.html}`; output is path-safe; renders styled;
  `title` is HTML-escaped; every `data-disco-field` resolves to `index.html`.

---

### 2. `lead_form` — REGISTERED (AppKit default, `starter.py`)

- **Purpose:** The default AppKit lead-gen app — a `hero` section + a `lead_form` section —
  produced from `lead_form_appspec(title)`. Single source shared with `app_create`.
- **User value:** A working landing + lead-capture starting point that already persists/admin-
  wires through AppKit (no hand-built form).
- **Files expected:** `.disco/appspec.json` (AppSpec, indent=2 + trailing newline) **and**
  `index.html` (from `render_html(spec)`) — byte-identical to `AppSpecStore.write`.
- **Minimal fixture outline** (`fixtures/starter-kits/lead_form/lead_form.html`, *dir present,
  HTML pending*): rendered AppSpec → hero (`headline`, `subhead`, `cta_text="Get started"`) +
  lead_form (`title="Contact us"`, `submit_text="Send"`). The canonical source of truth is the
  AppSpec, not the HTML.
- **Metadata required:** `data-disco-section` per AppSection (`hero`, `lead`); `data-disco-field`
  per AppSpec field (`headline`/`subhead`/`cta_text`/`title`/`submit_text`);
  `data-disco-file=".disco/appspec.json"` (edits route to the spec, not the rendered HTML);
  `data-disco-media-slot` for any hero image.
- **Editable fields:** `headline`, `subhead`, `cta_text`, lead-form `title`, `submit_text`;
  design via `app_set_design`; tweaks via `app_set_tweak`.
- **CD component:** Lead / Contact Form *(proposed)*.
- **Direct editing:** edits route to AppSpec via the `app_*` semantic tools (not raw HTML), then
  re-render — this is the strict AppKit edit law.
- **Failure modes:** editing rendered HTML instead of the AppSpec (drift); fake testimonials/
  logos/stats; media without a slot.
- **Tests required:** scaffolds to `{.disco/appspec.json, index.html}`; AppSpec round-trips
  (`model_dump_json`); HTML matches `render_html`; matches `app_create`'s default byte-for-byte;
  every field anchor resolves to the AppSpec.

---

### 3. `admin_table` — **(GAP)**

- **Purpose:** A sortable/scannable admin grid that lists captured lead-form submissions (the
  "admin view" the `appkit.leadgen` verifier requires).
- **User value:** The owner sees real submissions persist — closes the lead-gen loop the strict
  verifier checks.
- **Files expected:** an admin partial/page (e.g. `admin.html` or an AppSpec admin section) that
  reads persisted submissions; wires to the same store as the lead form.
- **Minimal fixture outline** (`fixtures/starter-kits/admin_table/admin_table.html`, *dir
  present, HTML pending*): a `<table>` with header row + a `data-disco-section="submissions"`
  body that templates one row per record; an empty-state row.
- **Metadata required:** `data-disco-section="submissions"`; `data-disco-field` on column
  headers; `data-disco-comment-anchor` per row (so a reviewer can annotate a record);
  `data-disco-file` to the admin entry.
- **Editable fields:** column set/labels, empty-state copy, page title.
- **CD component:** Data Table / Admin Grid *(proposed)*.
- **Direct editing:** header/label edits via `file_edit`/`app_update_content`; rows are
  data-bound (not hand-edited).
- **Failure modes:** hard-coded fake rows (must read real persisted data); no empty-state;
  reading a different store than the form writes (round-trip break).
- **Tests required:** renders an empty-state with zero records; renders N rows for N persisted
  records; reads the same store the lead form writes; every anchor resolves.

---

### 4. `browser_window` — **(GAP)**

- **Purpose:** A desktop browser-chrome frame (address bar, tab, controls) wrapping a prototype
  viewport — for screens/flows that should read as "a web app".
- **User value:** Prototypes look like the real product; screen labels make multi-screen flows legible.
- **Files expected:** a frame partial wrapping the prototype content region.
- **Minimal fixture outline** (`fixtures/starter-kits/browser_window/browser_window.html`, *dir
  present, HTML pending*): chrome bar (dots, URL field) + `data-disco-section="viewport"` content slot.
- **Metadata required:** `data-disco-screen-label` on the frame (names the screen);
  `data-disco-section="viewport"`; `data-disco-field="url"` for the address text.
- **Editable fields:** screen label, URL text, chrome theme.
- **CD component:** Browser Chrome / Window frame *(proposed)*.
- **Direct editing:** label/URL via `file_edit`; the viewport slot holds the real prototype content.
- **Failure modes:** chrome that intercepts/blocks real interactions; fixed pixel width breaking
  responsive content; missing screen label → unlabeled multi-screen flow.
- **Tests required:** renders chrome + a usable viewport slot; does not capture clicks meant for
  content; screen label resolves; responsive at common widths.

---

### 5. `mobile_frame` — **(GAP)**

- **Purpose:** A phone-device frame (notch/bezel, status bar) wrapping a mobile-shaped prototype.
- **User value:** Mobile prototypes are evaluated at a real device size.
- **Files expected:** a device-frame partial wrapping the content region.
- **Minimal fixture outline** (`fixtures/starter-kits/mobile_frame/mobile_frame.html`, *dir
  present, HTML pending*): bezel + status bar + `data-disco-section="screen"` slot at a device viewport.
- **Metadata required:** `data-disco-screen-label`; `data-disco-section="screen"`.
- **Editable fields:** screen label, status-bar text, frame style/size preset.
- **CD component:** Device / Phone Frame *(proposed)*.
- **Direct editing:** label/status via `file_edit`; content lives in the screen slot.
- **Failure modes:** frame swallowing touch/scroll; non-device viewport; missing label.
- **Tests required:** renders at a device viewport; scroll/touch reach the content; label resolves.

---

### 6. `deck_stage` — **(GAP)**

- **Purpose:** A single slide stage (16:9 safe area, title + body + notes region) the deck author
  fills; the per-slide unit behind `deck`.
- **User value:** Slides have consistent, readable type and a story-telling title; notes travel
  with the slide.
- **Files expected:** part of the `deck.authored.json` source model (NOT a raw HTML entrypoint);
  the fixture is a reference render of one stage.
- **Minimal fixture outline** (`fixtures/starter-kits/deck_stage/deck_stage.html`, *dir present,
  HTML pending*): 16:9 stage, `data-disco-section="slide"`, title field, body field, media slot,
  notes region.
- **Metadata required:** `data-disco-section="slide"`; `data-disco-field` for title/body;
  `data-disco-media-slot` for slide imagery (C7 embed); a notes region tagged with
  `data-disco-comment-anchor`.
- **Editable fields:** slide title, body, layout variant, image slot, speaker notes.
- **CD component:** Slide / Presentation Stage *(proposed)*.
- **Direct editing:** edits route through `deck_patch` (RFC-6902) on the slide — never a raw HTML
  edit, never a full regenerate for one slide.
- **Failure modes:** tiny/overflowing type; image not embedded (placeholder); editing the render
  instead of `deck.authored.json`; notes lost on edit.
- **Tests required:** stage renders 16:9 with readable type; image slot embeds bytes (C7);
  `deck_patch` edits one slide without touching others; notes preserved on edit.

---

### 7. `document_frame` — **(GAP)**

- **Purpose:** A print-safe single-column document layout (title, page-flow body, figure slots)
  for `document` reports.
- **User value:** Reports render and print cleanly (single flow → non-raster PDF).
- **Files expected:** complements `report.md`; the fixture is a reference render of the print frame.
- **Minimal fixture outline** (`fixtures/starter-kits/document_frame/document_frame.html`, *dir
  present, HTML pending*): single `data-disco-section="document"` column, title field, body flow,
  figure media slots, print-safe CSS.
- **Metadata required:** `data-disco-section="document"`; `data-disco-field` for title/headings;
  `data-disco-media-slot` for figures; `data-disco-comment-anchor` per paragraph for review.
- **Editable fields:** title, headings, body copy, figures.
- **CD component:** Document / Article Layout *(proposed)*.
- **Direct editing:** edits route to `report.md` via `file_edit`/`file_replace_lines` (targeted,
  fresh-read); never a full rewrite for a paragraph.
- **Failure modes:** multi-column/broken layout → bad print; raster images; whole-doc rewrite for
  a small edit.
- **Tests required:** renders single-flow print-safe; figures are print-safe (non-raster export);
  targeted edits don't reflow the whole doc; anchors resolve.

---

### 8. `image_slot` — **(GAP)**

- **Purpose:** A reusable media placeholder that declares where a generated/real image goes
  (with alt text + caption) without faking the asset.
- **User value:** No fake stock images; clear placeholder → real `image_generate` asset embeds in place.
- **Files expected:** an embeddable partial usable inside site/deck/document kits.
- **Minimal fixture outline** (`fixtures/starter-kits/image_slot/image_slot.html`, *dir present,
  HTML pending*): a figure with a `data-disco-media-slot` placeholder box + `<figcaption>` field +
  alt-text field; visible "image pending" empty-state.
- **Metadata required:** `data-disco-media-slot` (the slot id); `data-disco-field` for caption +
  alt text.
- **Editable fields:** alt text, caption, slot aspect ratio; the bound media asset.
- **CD component:** Image / Media Placeholder *(proposed)*.
- **Direct editing:** drop/generate an image → bound to the slot id (bytes-on-element for deck via
  C7; file asset for site/doc); caption/alt via `file_edit`.
- **Failure modes:** fake/stock placeholder shipped as final; missing alt text; slot id collision;
  image referenced from outside the workspace.
- **Tests required:** renders a clear empty-state; binding an asset replaces the placeholder;
  alt/caption resolve; asset is workspace-local (path-safe); deck path embeds bytes (C7).

---

### 9. `metrics_overlay` — **(GAP)**

- **Purpose:** A stat/metric card cluster (KPI value + label + trend) for dashboards, lead-gen
  results, or workflow outputs.
- **User value:** Results are shown as real, addressable metrics — not prose.
- **Files expected:** an embeddable partial; metrics bound to real data where available.
- **Minimal fixture outline** (`fixtures/starter-kits/metrics_overlay/metrics_overlay.html`, *dir
  present, HTML pending*): a grid of metric cards, each with `data-disco-metric-id`, a value
  field, a label field, and an empty/"no data" state.
- **Metadata required:** `data-disco-metric-id` per card; `data-disco-field` for value + label.
- **Editable fields:** metric label, format/units; the bound value source.
- **CD component:** Stat / Metric Card *(proposed)*.
- **Direct editing:** label/format via `file_edit`; value is data-bound by `metric-id` (not
  hand-typed for real runs).
- **Failure modes:** invented numbers (no-fake-content violation); duplicate `metric-id`; no
  empty-state when data is absent.
- **Tests required:** renders empty-state with no data; binds real values by `metric-id`; ids
  unique; labels resolve; no fabricated values.

---

### 10. `tweak_panel` — **(GAP)**

- **Purpose:** A live controls panel exposing the build's `TweakField`s (text/color/int/float/
  boolean/enum/palette) so the user can adjust behavior/appearance.
- **User value:** Safe, bounded knobs (validated against `TweakSpec`) instead of code editing.
- **Files expected:** a panel partial bound to `.disco/tweaks.json` (AppKit) / `AppSpec.tweaks`.
- **Minimal fixture outline** (`fixtures/starter-kits/tweak_panel/tweak_panel.html`, *dir
  present, HTML pending*): one control per `TweakField` (input type by `editor`), each carrying
  the field `key`; grouped by `affects`.
- **Metadata required:** `data-disco-field` per control, valued with the `TweakField.key`
  (bridges UI control → tweak key); `data-disco-section="tweaks"`.
- **Editable fields:** the tweak values themselves (within `min`/`max`/`step`/`options`/`colors`);
  colors normalize to `#rrggbb`.
- **CD component:** Controls / Settings Panel *(proposed)*.
- **Direct editing:** changing a control calls `app_set_tweak` (AppKit) / writes `tweaks.json`;
  must honor the grounding law (a tweak controls behavior OR ≥1 field; TEXT/COLOR → behavior or
  ≥2 fields).
- **Failure modes:** a control that affects nothing (violates the tweak law); color not
  normalized; value out of declared bounds; panel drift from `TweakSpec`.
- **Tests required:** every control maps to a real `TweakField.key`; honors the tweak law;
  colors normalize to `#rrggbb`; out-of-bounds rejected; panel matches the registered `TweakSpec`.

---

### 11. `workflow_setup_card` — **(GAP, fixture-TODO)**

- **Purpose:** A setup card that declares a workflow's inputs/steps/expected outputs (the front
  matter for a `workflow.output` build).
- **User value:** The user sees what the workflow will take and produce before it runs.
- **Files expected:** a card partial + a contribution to `.disco/workflow_output.json` (the
  proposed manifest).
- **Minimal fixture outline** (`fixtures/starter-kits/workflow_setup_card/workflow_setup_card.html`,
  *fixture-TODO, no dir yet*): a card with input fields, a steps list, and a declared-outputs list.
- **Metadata required:** `data-disco-section="workflow_setup"`; `data-disco-field` per input;
  `data-disco-file=".disco/workflow_output.json"` (binds declared outputs to the manifest).
- **Editable fields:** input values, step descriptions, declared output names.
- **CD component:** Form / Setup Card *(proposed)*.
- **Direct editing:** field edits write the manifest via `file_edit`/`file_write` (targeted).
- **Failure modes:** declared outputs that the run never produces (manifest/FS mismatch → verifier
  FAIL); inputs with no effect on the run.
- **Tests required:** declared outputs reconcile with produced files; manifest is valid JSON;
  fields resolve to the manifest; no orphan declared output.

---

### 12. `form_receipt` — **(GAP, fixture-TODO)**

- **Purpose:** A confirmation/receipt shown after a successful lead-form (or workflow) submission
  — the success-state counterpart to `lead_form`.
- **User value:** The submitter gets clear, real confirmation; closes the form UX loop.
- **Files expected:** a receipt partial rendered post-submit; echoes persisted submission fields.
- **Minimal fixture outline** (`fixtures/starter-kits/form_receipt/form_receipt.html`,
  *fixture-TODO, no dir yet*): a confirmation panel echoing submitted field values + a reference id.
- **Metadata required:** `data-disco-field` per echoed value; `data-disco-comment-anchor` on the
  reference id (for follow-up annotation).
- **Editable fields:** confirmation copy, which fields are echoed, reference-id format.
- **CD component:** Confirmation / Receipt *(proposed)*.
- **Direct editing:** copy via `file_edit`/`app_update_content`; echoed values are data-bound from
  the persisted submission.
- **Failure modes:** showing a receipt without real persistence (fake success); echoing fields the
  store didn't save; no reference id.
- **Tests required:** renders only after a real persisted submission; echoed fields match the
  stored record; reference id present + unique; copy resolves.

---

### 13. `daily_brief_layout` — **(GAP, fixture-TODO)**

- **Purpose:** A dashboard/brief layout (header, summary, metric strip, sections) for a recurring
  brief — a `document`/`workflow.output` shaped deliverable.
- **User value:** A scannable daily/periodic brief that mixes prose + metrics.
- **Files expected:** a layout for `report.md`-shaped content or a workflow-output page; embeds
  `metrics_overlay` + `image_slot`.
- **Minimal fixture outline** (`fixtures/starter-kits/daily_brief_layout/daily_brief_layout.html`,
  *fixture-TODO, no dir yet*): dated header, summary block, `metrics_overlay` strip, content
  sections, figure slots.
- **Metadata required:** `data-disco-section` per block; `data-disco-metric-id` for the metric
  strip; `data-disco-field` for date/summary/headings; `data-disco-media-slot` for figures.
- **Editable fields:** date, summary, section copy, metrics source, figures.
- **CD component:** Dashboard / Brief Layout *(proposed)*.
- **Direct editing:** prose via `file_edit` (targeted, fresh-read); metrics data-bound by id;
  print-safe like `document_frame`.
- **Failure modes:** invented metrics; broken print layout; stale date; whole-doc rewrite for a
  section.
- **Tests required:** renders print-safe; metrics bind by id (no fabricated values); date/summary
  resolve; targeted edits don't reflow the whole brief.

---

### 14. `animation_stage` — **(GAP, fixture-TODO)**

- **Purpose:** A motion/canvas stage hosting CSS/JS animation or a canvas scene for interactive
  prototypes or animated deck moments.
- **User value:** Animated/interactive demos run in a controlled, previewable stage.
- **Files expected:** a stage partial with a canvas/animation root + inline JS (end-of-body for
  reliable paint).
- **Minimal fixture outline** (`fixtures/starter-kits/animation_stage/animation_stage.html`,
  *fixture-TODO, no dir yet*): a `data-disco-section="stage"` root, a canvas/animation container,
  inline JS hook, a play/reset control.
- **Metadata required:** `data-disco-section="stage"`; `data-disco-field` for any tunable
  parameters (duration/easing/labels).
- **Editable fields:** animation parameters, labels, play/reset behavior.
- **CD component:** Canvas / Motion Stage *(proposed)*.
- **Direct editing:** parameters via `file_edit` or a bound `tweak_panel`; behavior verified by
  the prototype verifier (handlers must actually fire).
- **Failure modes:** animation that never starts (handler doesn't fire — a clean console isn't
  proof); inline JS split into a companion file (paint race); unbounded loop hogging the frame.
- **Tests required:** the stage actually animates/runs in preview (verifier exercises it); JS is
  inline/end-of-body; parameters resolve; play/reset fire; no runaway loop.

---

## Tests required (catalog-wide checklist)

**Registry / scaffolding**
- [ ] `app_shell` and `lead_form` resolve via `StarterKitRegistry.get(...)`; all GAP kits resolve
      to `None` today (documents the gap).
- [ ] Each registered kit's `scaffold(title)` returns only path-safe, workspace-relative paths
      (`_safe_rel`: no absolute, no `..`, non-empty).
- [ ] `lead_form` scaffold is byte-identical to `app_create`'s default (single-source invariant).
- [ ] `app_shell` scaffolds exactly `{index.html}`; `lead_form` exactly
      `{.disco/appspec.json, index.html}`.

**Fixtures**
- [ ] Reference HTML is added at `fixtures/starter-kits/<kit>/<kit>.html` for the 10 dirs that
      already exist (currently empty).
- [ ] The 4 fixture-TODO kits get a directory + reference HTML created.
- [ ] Each fixture includes a `_meta` block (purpose + expected verdict) per grounding §11.5
      (for JSON fixtures) / an HTML comment equivalent for HTML fixtures.
- [ ] Each fixture renders standalone (inline styles; no half-stream companion file).

**Direct-edit metadata**
- [ ] Every editable element carries the correct `data-disco-*` attribute from the pinned set.
- [ ] `data-disco-file` on each edit anchor resolves to the kind's real entrypoint
      (`index.html` / `.disco/appspec.json` / `deck.authored.json` / `report.md` / manifest).
- [ ] Ids are unique where required (`data-disco-metric-id`, `data-disco-media-slot`,
      `data-disco-section`).
- [ ] AppKit kits route edits through `app_*` tools (to the AppSpec), not raw HTML edits.
- [ ] `tweak_panel` controls each map to a real `TweakField.key` and honor the tweak law.

**Contract consistency**
- [ ] Each kit's `Maps to ContractKind` is a real `ContractKind` id.
- [ ] A kit named as a contract's `starter_kit` exists in the registry before that contract ships
      (today only `app_shell`/`lead_form` are safe to name).
- [ ] No-fake-content holds for `admin_table`, `metrics_overlay`, `image_slot`, `form_receipt`,
      `daily_brief_layout` (data-bound, never fabricated).

---

## Registration plan (GAP work — NOT yet implemented)

> All of the following is proposed and unimplemented. Do not describe it as done.

1. **Kit data** — add each GAP kit's builder to `kits/starter.py`, registered in
   `StarterKitRegistry._BUILTINS` (mirroring `_app_shell`/`_lead_form`): a `builder(title) ->
   {rel_path: text}` returning path-safe paths, with the `data-disco-*` anchors baked in.
2. **Scaffolder** — `scaffold_starter` (`tools/builtin/scaffold_starter.py`) already materializes
   a named kit; once a kit is in `_BUILTINS`, `scaffold_starter` can name it. No tool change
   needed beyond the registry addition.
3. **Contract wiring** — only after a kit is registered may a `BuildContract.artifact.starter_kit`
   name it (e.g. point `appkit.leadgen`'s admin flow at `admin_table`); naming an unregistered kit
   is a false affordance (grounding warns against bare-string kits).
4. **Fixtures first** — land the reference HTML under `fixtures/starter-kits/<kit>/` (it already
   has empty dirs for 10 kits) before the builder, so the builder is authored against a verified
   render.
5. **Direct-manipulation spec** — author `DIRECT_MANIPULATION_SPEC.md` defining the `data-disco-*`
   attribute family this catalog cross-references; the kits and that spec must agree.
6. **Sequencing** — recommended order by ContractKind coverage: `admin_table` (closes the
   appkit.leadgen verifier loop) → `image_slot` (shared by deck/doc/site) → `deck_stage` +
   `document_frame` → `tweak_panel`/`metrics_overlay` → frames (`browser_window`/`mobile_frame`) →
   the `workflow.output` set (`workflow_setup_card`/`form_receipt`/`daily_brief_layout`) →
   `animation_stage`.
```
