# Artifact-VM — WorkflowPromptPack Requirements (SPEC ONLY)

> **Read `_GROUNDING.md` first.** That file is the single source of truth for real Disco
> names; this document only maps requirements onto it. **This is specification, not code.**
> Nothing here describes implemented runtime behavior. Anything that does not exist in the
> codebase as of 2026-06-30 is marked **(GAP)** and written as *work to author*.
>
> Scope: the seven `ContractKind`s — `static.site`, `appkit.leadgen`, `deck`, `document`,
> `interactive.prototype`, `workflow.output`, `custom`. Five packs exist; two are GAPs.
>
> Real loader: `packages/core/src/disco/core/workflows/prompt_pack.py`
> (markdown → slugged sections, fence-aware, duplicate-section = error).
> Real assembler: `packages/core/src/disco/core/workflows/assembly.py`.
> Real pack files: `packages/core/src/disco/core/workflows/prompt_packs/*.md`.

---

## (a) The REQUIRED_SECTIONS contract (pinned)

Every pack is markdown. Each `## Header` is slugged to a section key. The loader pins
**exactly these 11 keys** (`prompt_pack.REQUIRED_SECTIONS`); a pack missing any one of
them fails to load, and a duplicate header is an error.

| # | Required key | Header that slugs to it | What it must contain |
| --- | --- | --- | --- |
| 1 | `role` | `## Role` | Who the model is for this kind; that the host owns preview/verify/export; "operate inside the rails". |
| 2 | `artifact_contract` | `## Artifact contract` | The `ContractKind` id + the exact `required_files`; shape guidance for the deliverable. |
| 3 | `workflow_steps` | `## Workflow steps` | Ordered, bounded build procedure ending in the kind's finalizer (+ export where one exists). |
| 4 | `allowed_tools` | `## Allowed tools` | Only tool names that exist in the registry, matching the kind's `bootstrap`+`edit` packs. |
| 5 | `forbidden_tools` | `## Forbidden tools` | Anti-patterns (manual server/port, raw rewrite for small edits, fake content). |
| 6 | `targeted_edit_law` | `## Targeted edit law` | "Change the smallest thing"; rewrite-during-edit = contract violation. |
| 7 | `preview_rule` | `## Preview rule` | Preview only through the host (`preview_start`/host render); never hand-serve. |
| 8 | `verify_rule` | `## Verify rule` | What the finalizer proves; "clean console ≠ rendered"; the kind's `VerificationLevel`. |
| 9 | `export_rule` | `## Export rule` | The host export pipeline name + stages, or an explicit "no export pipeline yet". |
| 10 | `context_policy` | `## Context policy` | Keep goal+todo (+kind refs); snip resolved explorations; never compact unresolved verifier failures. |
| 11 | `done_criteria` | `## Done criteria` | files-exist AND renders/works AND finalizer-passed (AND export where applicable); existence alone ≠ done. |

**Header→slug rule (assumed):** lowercased, spaces→`_`, so `## Targeted edit law` →
`targeted_edit_law`. Authors MUST use the exact headers in the middle column; a renamed
header silently produces an unrecognized key and a missing required key. *(Tests below
pin this.)*

---

## (b) Cross-pack rules table

These hold for **every** pack regardless of kind. A pack that contradicts a row is wrong.

| Rule | Requirement | Backed by |
| --- | --- | --- |
| Host owns preview | Model never runs `python -m http.server` / picks a port; preview via host. | CD-TOOLS; all packs §preview_rule |
| Host owns verify | Finalizer is a **host finalizer** string (`^ready_for_[a-z0-9_]+_verification$`), not a model-callable builtin. | grounding §4 NOTE |
| Host owns export | Export is a named host pipeline `(preflight,) bundle, validate, deliver`; never hand-zip. | grounding §8 |
| Targeted edits only | Localized change uses edit tools (`file_edit`/`file_replace_lines`/`app_*`/`deck_patch`); full rewrite to make a small edit = violation. | CD-TOOLS edit law |
| Fresh-read before exact edit | Exact-text/line edits read the current region first (no stale offsets). | CD-TOOLS fresh-read guard |
| No clobber of entrypoints | Artifact entrypoints route through edit tools / `safe_write_file`; raw `file_write` is repair/scaffold only. | CD-TOOLS governed routing |
| No elision markers | No `// ... rest unchanged` / `# ...` placeholders in editable source. | CD-TOOLS prompt discipline |
| No fake content | No invented testimonials/logos/stats/numbers; use placeholders + media slots. | appkit pack; applies to all |
| Resource-copy through tools | Assets are written through file tools into the workspace (path-safe, no `..`, no absolute); never referenced from outside the workspace. | `_safe_rel` in `starter.py` |
| Render ≠ exists | A blank/unstyled page with a clean console is a FAIL; verify visible/working output. | all packs §verify_rule |
| Allowed/forbidden tools must be real | Every named tool exists in the registry (grounding §4); forbidden names should also be real to be meaningful. | grounding §4 |
| Snip discipline | Resolved explorations may be snipped once a durable summary exists; unresolved verifier failures are never compacted. | plan-progress/CD-TOOLS |
| Ask-questions policy | In autonomous build, do NOT block on questions; reason + build; only genuinely-nonsensical tail items defer. | repo norm; encoded per-pack below |

---

## (c) Pack status table

| ContractKind | Pack file | Status | Headline issue |
| --- | --- | --- | --- |
| `static.site` | `build_static_site.md` | **exists** | needs-tightening: no explicit *ask-questions*, *resource-copy*, *no-fake-content* lines |
| `appkit.leadgen` | `build_appkit_leadgen.md` | **exists** | strongest pack; minor: no explicit ask-questions / resource-copy line |
| `deck` | `build_deck.md` | **exists** | needs-tightening: no fake-content guard; no resource/image-copy rule (image-embed C7 landed) |
| `document` | `build_document.md` | **exists** | needs-tightening: no fake-content guard; no resource/asset-copy rule |
| `interactive.prototype` | `build_interactive_prototype.md` | **exists** | export_rule says "no pipeline yet" (matches registry GAP); add resource-copy / ask line |
| `workflow.output` | — | **GAP** | no pack, empty bootstrap, no export — full DRAFT below |
| `custom` | — | **GAP** | no pack; `rewrite_allowed=True`; needs guard-rails — full DRAFT below |

"needs-tightening" = the 11 required sections are present (the file loads) but cross-pack
rules in (b) are not all spelled out in the prose; see each pack's **Likely-missing flags**.

---

## Per-pack requirements

Each pack below lists the **full requirement surface** the task demands, then a
**§-by-§ map** to the 11 required sections with **likely-missing flags** against the
current file (where one exists).

---

### 1. `static.site` — `build_static_site.md` (exists, needs-tightening)

- **Role:** Build a self-contained static website; host owns preview/verify/export.
- **Artifact (kind + required_files):** `static.site`; required `index.html`. One file with
  inline CSS + end-of-body JS renders most reliably; split only for genuinely large/multi-page.
- **Allowed tools (exact):** `scaffold_starter`, `file_write`, `file_edit`,
  `file_replace_lines`, `preview_start`, `preview_status`, `preview_logs`, `preview_stop`,
  finalizer `ready_for_static_site_verification`.
- **Forbidden tools (exact):** manual `python -m http.server` / manual port selection; raw
  whole-file `file_write` to make a small edit (use `file_edit`).
- **Required starter kits:** `app_shell` (real; `kits/starter.py`).
- **When to ask questions:** Do not block. If brief is thin, choose a sensible single-page
  structure and build; defer only nonsensical tail items.
- **Targeted edit law:** Copy/color change `file_edit`s the exact region; rewrite-during-edit
  = violation; fresh-read the region first.
- **Show rule (GAP family):** Today only `preview_start` + the `verify_web_app` screenshot;
  the proposed `show_artifact_to_agent` / `show_artifact_to_user` are **(GAP)**.
- **Verify rule:** `ready_for_static_site_verification` (level **standard**): page must render
  visible, styled content; empty/unstyled + clean console = FAIL.
- **Export rule:** host `static_standalone` pipeline (preflight → bundle → validate → deliver).
- **Resource-copy rules:** assets written into the workspace via file tools (path-safe, no
  `..`/absolute); inline small assets to avoid half-streamed companion files. **(add — likely-missing)**
- **No-fake-content rules:** no invented stats/testimonials/logos; placeholders + media slots. **(add — likely-missing)**
- **Done criteria:** `index.html` exists AND renders styled content AND finalizer passed.
- **Verifier behavior:** standard; renders-check, not console-only.
- **Export behavior:** `static_standalone` delivers real bytes (P10b-proven family).
- **Context/snipping:** keep goal+todo; snip resolved explorations; never compact unresolved fails.

**§-map & likely-missing flags**

| Section | Present? | Note |
| --- | --- | --- |
| role / artifact_contract / workflow_steps / allowed_tools / forbidden_tools / targeted_edit_law / preview_rule / verify_rule / export_rule / context_policy / done_criteria | yes (all 11) | file loads |
| (cross-rule) no-fake-content | **missing** | add a line |
| (cross-rule) resource-copy | **missing** | add a line |
| (cross-rule) ask-questions / no-block | **missing** | add a line |

---

### 2. `appkit.leadgen` — `build_appkit_leadgen.md` (exists, strongest)

- **Role:** Build a local-business lead-gen app (landing + working lead-capture that persists +
  shows in admin); host owns preview/verify/export.
- **Artifact (kind + required_files):** `appkit.leadgen`; required `.disco/appspec.json` and
  `index.html`. App described by AppSpec/DesignSpec; page generated from them.
- **Allowed tools (exact):** `app_create`, `app_update_content`, `app_add_section`,
  `app_remove_section`, `app_reorder_section`, `app_set_design`, `app_set_tweak`,
  `app_snapshot_version`, `preview_*`, finalizer `ready_for_app_verification`. Raw `file_write`
  is repair-only.
- **Forbidden tools (exact):** fake testimonials/logos/stats; manual port/server; raw whole-app
  rewrite for a small content/section change.
- **Required starter kits:** `lead_form` (real; IS the AppKit default — `app_create` scaffolds
  from it). Related GAP kits this kind would use: `admin_table`, `form_receipt`, `tweak_panel`.
- **When to ask questions:** Do not block; pick a reasonable business structure; build.
- **Targeted edit law:** copy/section/theme change touches ONLY that field/section via the
  `app_*` semantic op; never a full-app rewrite.
- **Show rule:** preview via `preview_start`; `show_artifact_*` family is **(GAP)**.
- **Verify rule:** `ready_for_app_verification` (level **strict**): route loads, console clean
  enough, a fake lead actually persists, admin shows it; blank render = FAIL.
- **Export rule:** `cloudflare_project` pipeline (require verified app first); handoff includes
  specs, migrations, OWNER_GUIDE.
- **Resource-copy rules:** media via media slots; assets written into workspace path-safe. **(make explicit)**
- **No-fake-content rules:** present & strong — no fake testimonials/logos/stats.
- **Done criteria:** required files exist AND a fake lead persists AND admin shows it AND
  finalizer passed AND Cloudflare export + handoff exist.
- **Verifier behavior:** strict (persistence + admin round-trip).
- **Export behavior:** `cloudflare_project` (preflight → bundle → validate → deliver) + handoff.
- **Context/snipping:** keep goal+todo+AppSpec/DesignSpec refs; snip resolved; never compact fails.
- **Skills (real):** `appkit.leadgen`, `cloudflare_export`, `design_recipe`.

**§-map & likely-missing flags**

| Section | Present? | Note |
| --- | --- | --- |
| all 11 | yes | file loads; strongest pack |
| (cross-rule) resource-copy / media-slot wording | partial | media slots mentioned; make asset path-safety explicit |
| (cross-rule) ask-questions / no-block | **missing** | add a line |

---

### 3. `deck` — `build_deck.md` (exists, needs-tightening)

- **Role:** Author a presentation deck; host owns rendering/verify/export.
- **Artifact (kind + required_files):** `deck`; required `deck.authored.json` (AuthoredDeck
  source `slides_generate` writes and `deck_patch` edits).
- **Allowed tools (exact):** `slides_generate` (use `filename="deck"`), `deck_patch`
  (RFC-6902), `preview_*`, finalizer `ready_for_deck_verification`. Image embed via
  `image_generate` assets (C7 landed) where slides need media.
- **Forbidden tools (exact):** raw `file_write`/rewrite of the deck JSON for a small edit (use
  `deck_patch`); manual server.
- **Required starter kits:** none registered. GAP kits this kind would use: `deck_stage`,
  `image_slot`, `animation_stage`.
- **When to ask questions:** Do not block; pick a story arc and author it.
- **Targeted edit law:** changing slide N or a section header touches ONLY that slide/field via
  `deck_patch`; full regenerate for one edit = violation.
- **Show rule:** preview the rendered deck via host preview; `show_artifact_*` is **(GAP)**.
- **Verify rule:** `ready_for_deck_verification` (level **standard**): slides present, type
  readable, no broken slide; a deck that won't render = FAIL.
- **Export rule:** `deck_export` pipeline (bundle → validate → deliver, **no preflight**);
  speaker notes travel with the export.
- **Resource-copy rules:** generated slide images embed into PPTX+HTML (C7); assets carried as
  bytes-on-element, preserved on edit. **(add explicit line — likely-missing)**
- **No-fake-content rules:** no invented stats/quotes on slides; placeholders for data. **(add — likely-missing)**
- **Done criteria:** `deck.authored.json` exists AND deck renders AND finalizer passed AND export delivered.
- **Verifier behavior:** standard (render-check).
- **Export behavior:** `deck_export`; notes preserved.
- **Context/snipping:** keep goal+todo+outline; snip resolved authoring; never compact render fails.

**§-map & likely-missing flags**

| Section | Present? | Note |
| --- | --- | --- |
| all 11 | yes | file loads |
| (cross-rule) no-fake-content | **missing** | add a line |
| (cross-rule) resource/image-copy (C7) | **missing** | add a line |
| (cross-rule) ask-questions / no-block | **missing** | add a line |

---

### 4. `document` — `build_document.md` (exists, needs-tightening)

- **Role:** Author a written document/report; host owns print-to-PDF, verify, export.
- **Artifact (kind + required_files):** `document`; required `report.md`. Single page-flow
  column, print-safe; PDF from the browser print path (no raster PDF).
- **Allowed tools (exact):** `file_write` (draft), `file_edit`, `file_replace_lines`,
  `preview_*`, finalizer `ready_for_document_verification`.
- **Forbidden tools (exact):** raster/image-based PDF; whole-document rewrite for a paragraph;
  manual server.
- **Required starter kits:** none registered. GAP kits this kind would use: `document_frame`,
  `daily_brief_layout`, `image_slot`.
- **When to ask questions:** Do not block; choose a sensible outline and draft.
- **Targeted edit law:** revision touches the smallest region (`file_edit`/`file_replace_lines`
  on a freshly-read range); no full rewrite for a localized change.
- **Show rule:** preview the rendered document via host; `show_artifact_*` is **(GAP)**.
- **Verify rule:** `ready_for_document_verification` (level **standard**): renders + print-safe
  (single flow, no broken layout); won't render/print = FAIL.
- **Export rule:** `document_pdf` pipeline (preflight → bundle → validate → deliver);
  print-from-HTML, never rasterize.
- **Resource-copy rules:** embedded figures/assets written into the workspace path-safe; print-safe images. **(add — likely-missing)**
- **No-fake-content rules:** no fabricated figures/citations/quotes; placeholders for unknown data. **(add — likely-missing)**
- **Done criteria:** `report.md` exists AND renders print-safe AND finalizer passed AND a
  non-raster PDF delivered.
- **Verifier behavior:** standard (render + print-safety).
- **Export behavior:** `document_pdf` (print-from-HTML).
- **Context/snipping:** keep goal+todo+outline; snip resolved drafting; never compact layout/print fails.

**§-map & likely-missing flags**

| Section | Present? | Note |
| --- | --- | --- |
| all 11 | yes | file loads |
| (cross-rule) no-fake-content | **missing** | add a line |
| (cross-rule) resource-copy | **missing** | add a line |
| (cross-rule) ask-questions / no-block | **missing** | add a line |

---

### 5. `interactive.prototype` — `build_interactive_prototype.md` (exists, needs-tightening)

- **Role:** Build an interactive, stateful prototype (multi-step flows, validation, persistence);
  host owns preview/verify.
- **Artifact (kind + required_files):** `interactive.prototype`; required `index.html`.
  Self-contained page, inline styles + end-of-body JS; state in the page (persists where the flow needs it).
- **Allowed tools (exact):** `scaffold_starter`, `file_write`, `file_edit`, `file_replace_lines`,
  `preview_*`, finalizer `ready_for_prototype_verification`.
- **Forbidden tools (exact):** manual port/server; raw whole-file rewrite for a localized behavior change.
- **Required starter kits:** `app_shell` (real). GAP kits this kind would use: `browser_window`,
  `mobile_frame`, `tweak_panel`.
- **When to ask questions:** Do not block; pick a flow and build it.
- **Targeted edit law:** a change to one interaction/state path touches ONLY that code; no full rewrite.
- **Show rule:** preview via `preview_start`; drive real interactions, not just a static load;
  `show_artifact_*` is **(GAP)**.
- **Verify rule:** `ready_for_prototype_verification` (level **standard**): each button/step
  fires, validation triggers, state persists; a page that loads but whose handlers don't fire = FAIL.
- **Export rule:** **no dedicated export pipeline** (matches registry GAP); delivered as workspace
  files; do not hand-zip (a pipeline lands with P10).
- **Resource-copy rules:** assets into workspace path-safe; inline where possible to paint reliably. **(add — likely-missing)**
- **No-fake-content rules:** no fake data behind interactions; placeholders. **(add — likely-missing)**
- **Done criteria:** `index.html` exists AND every interactive path works in preview AND finalizer
  passed; non-interactive static render ≠ done.
- **Verifier behavior:** standard, but interaction-exercising (handlers must fire).
- **Export behavior:** none yet (GAP).
- **Context/snipping:** keep goal+todo+interaction map; snip resolved; never compact broken-interaction fails.

**§-map & likely-missing flags**

| Section | Present? | Note |
| --- | --- | --- |
| all 11 | yes | file loads; export_rule correctly states "no pipeline yet" |
| (cross-rule) no-fake-content | **missing** | add a line |
| (cross-rule) resource-copy | **missing** | add a line |
| (cross-rule) ask-questions / no-block | **missing** | add a line |

---

### 6. `workflow.output` — **(GAP: no pack)** — requirements to author

- **Role:** Deliver the **output artifact(s) of a multi-step workflow/automation run** (the
  files a workflow produces — e.g. a generated dataset, a rendered report bundle, a batch of
  assets) as a downloadable deliverable. `DeliveryMode = files`. Host owns delivery/verify.
- **Artifact (kind + required_files):** `workflow.output`; **required_files currently empty in
  the registry (GAP)**. *Proposed:* require a manifest `.disco/workflow_output.json`
  (declares produced files + their roles) **(GAP — new required file)**, plus at least one
  produced output file. Until the registry is updated, the manifest is a pack convention.
- **Allowed tools (exact, today):** bootstrap is **empty (GAP)**. *Proposed allowed set from
  real tools:* `file_write`, `file_edit`, `file_replace_lines`, `file_append`, `file_list`,
  `file_read`, `run_project_script`, `code_exec`, `shell` (to drive the workflow),
  finalizer `ready_for_workflow_output_verification`.
- **Forbidden tools (exact):** manual port/server for any preview; raw rewrite of a produced
  file to make a small change; **no fabricated outputs** (the deliverable must be the real run product).
- **Required starter kits:** none today. *Proposed GAP kit:* `workflow_setup_card` (declares
  inputs/steps/outputs) and `form_receipt`/`metrics_overlay` where the workflow reports results.
- **When to ask questions:** Do not block; infer the workflow's intended outputs from the brief
  and produce them; defer only nonsensical tail items.
- **Targeted edit law:** edits to a produced file or the manifest touch the smallest region via
  edit tools; never regenerate the whole output bundle for one change.
- **Show rule:** `files` mode → no live app preview; surface the produced file list to the user
  via the proposed `present_artifact_for_download` **(GAP)** / `show_artifact_to_user` **(GAP)**.
- **Verify rule:** `ready_for_workflow_output_verification` (level **standard**): every file the
  manifest declares actually exists, is non-empty, and matches its declared role/type; a manifest
  that lists a missing or empty file = FAIL.
- **Export rule:** **no export pipeline today (GAP).** *Proposed:* a `workflow_output_bundle`
  pipeline `(preflight,) bundle, validate, deliver` that zips the manifest + produced files into
  one deliverable (mirror `static_standalone`).
- **Resource-copy rules:** all produced files written into the workspace path-safe (no `..`,
  no absolute); external inputs copied in, never referenced from outside the workspace.
- **No-fake-content rules:** outputs must be the genuine product of the run; no hand-faked
  result files or invented metrics.
- **Done criteria:** the manifest exists AND every declared file exists+non-empty+role-matched
  AND the finalizer passed (AND, once the pipeline lands, a bundle delivered).
- **Verifier behavior:** standard; manifest-vs-filesystem reconciliation.
- **Export behavior:** GAP today; proposed `workflow_output_bundle`.
- **Context/snipping:** keep goal+todo+manifest in view; snip resolved step explorations; never
  compact an unresolved missing-output failure.

*(Full ready-to-author DRAFT skeleton in the next section.)*

---

### 7. `custom` — **(GAP: no pack)** — requirements to author

- **Role:** Build an **arbitrary deliverable that does not fit a named kind** — a freeform
  escape hatch. `DeliveryMode = files`. Because the shape is unknown, the pack's job is to impose
  the *minimum* rails (targeted edits, no fakes, real verification) without assuming an entrypoint.
- **Artifact (kind + required_files):** `custom`; **required_files empty (intentional — the
  shape is unknown).** *Proposed convention:* the model declares its own entrypoint/output set in
  `.disco/custom_manifest.json` **(GAP — pack convention, not a registry-required file)**.
- **Allowed tools (exact, today):** bootstrap `file_write`, `file_edit`, `shell`; edit tools
  `file_edit`, `file_replace_lines`; **`rewrite_allowed=True`** for this kind (the only kind where
  full rewrite is contract-permitted). Plus `file_read`, `file_list`, `code_exec`,
  `run_project_script`, `preview_*` (if the deliverable is web-shaped), finalizer
  `ready_for_artifact_verification`.
- **Forbidden tools (exact):** manual port/server (use `preview_*` if previewing); **even with
  `rewrite_allowed=True`, do not rewrite to dodge a fresh-read** — prefer targeted edits; no
  fabricated content.
- **Required starter kits:** none (the shape is unknown; scaffold nothing by default).
- **When to ask questions:** Do not block; choose the most literal reading of the brief and build
  the real thing; defer only genuinely nonsensical tail items.
- **Targeted edit law:** prefer the smallest edit even though rewrite is *allowed*; a rewrite is a
  fallback for structural change, not a shortcut to skip a fresh-read.
- **Show rule:** if web-shaped, preview via `preview_start`; otherwise surface the produced file
  list via proposed `present_artifact_for_download` **(GAP)**.
- **Verify rule:** `ready_for_artifact_verification` (level **standard**): the declared
  entrypoint/output exists and does what the brief asked (renders / runs / produces the file);
  existence alone = FAIL.
- **Export rule:** **none today (GAP).** *Proposed:* fall back to a generic
  `workflow_output_bundle`-style bundle if the deliverable is file-shaped.
- **Resource-copy rules:** everything written into the workspace path-safe; nothing referenced
  from outside.
- **No-fake-content rules:** no invented data/results regardless of shape.
- **Done criteria:** the declared deliverable exists AND satisfies the brief's intent
  (render/run/output) AND the finalizer passed.
- **Verifier behavior:** standard; brief-intent satisfaction over a fixed entrypoint.
- **Export behavior:** GAP; proposed generic bundle.
- **Context/snipping:** keep goal+todo+the self-declared manifest; snip resolved; never compact fails.

*(Full ready-to-author DRAFT skeleton in the next section.)*

---

## Ready-to-author DRAFT skeletons (the two GAP packs)

These are usable first drafts. Each fills **all 11 required sections** with the exact headers
the loader expects. Save as `prompt_packs/build_workflow_output.md` and `prompt_packs/build_custom.md`.
Mark every proposed-but-not-yet-real name **(GAP)** so reviewers know it is pending.

### DRAFT — `build_workflow_output.md`

```markdown
# Workflow Prompt Pack — Workflow Output

## Role
You deliver the output artifacts of a multi-step workflow run as a downloadable package.
This is a files-mode deliverable: there is no live app to open. The host owns delivery and
verification; you run the workflow and produce the real output files inside the rails.

## Artifact contract
A `workflow.output` deliverable. You MUST write a manifest at `.disco/workflow_output.json`
(GAP: proposed required file) that lists every produced file and its role, plus at least one
produced output file. The deliverable IS those files — never a hand-faked stand-in.

## Workflow steps
1. Read the brief; decide the workflow's intended outputs.
2. Run the workflow (run_project_script / code_exec / shell) to PRODUCE the real files.
3. Write `.disco/workflow_output.json` declaring each produced file + role (file_write).
4. ready_for_workflow_output_verification.
5. (When the pipeline lands) export the bundle. (GAP: workflow_output_bundle)

## Allowed tools
file_write, file_edit, file_replace_lines, file_append, file_list, file_read,
run_project_script, code_exec, shell, ready_for_workflow_output_verification.

## Forbidden tools
No manual port/server. No raw rewrite of a produced file or the manifest for a small change.
No fabricated output files or invented metrics.

## Targeted edit law
Edits to a produced file or the manifest touch the smallest region (file_edit /
file_replace_lines on a freshly-read range). Never regenerate the whole output bundle to
change one file. Fresh-read before any exact edit.

## Preview rule
This is files-mode: there is no live preview. Surface the produced file list to the user via
the host (present_artifact_for_download — GAP). Do not hand-serve anything.

## Verify rule
ready_for_workflow_output_verification (standard) reconciles the manifest against the
filesystem: every declared file exists, is non-empty, and matches its declared role/type. A
manifest that names a missing or empty file is a FAIL. Existence-without-content is a FAIL.

## Export rule
No export pipeline exists yet (GAP). When it lands, export via workflow_output_bundle
(preflight -> bundle -> validate -> deliver), zipping the manifest + produced files. Never
hand-zip the workspace.

## Context policy
Keep the goal + todo + the manifest in view. Snip resolved step explorations once a durable
summary exists. Never compact an unresolved missing-output / empty-output failure.

## Done criteria
`.disco/workflow_output.json` exists AND every declared file exists, is non-empty, and matches
its role AND ready_for_workflow_output_verification passed (AND, once the pipeline lands, the
bundle was delivered). A manifest alone, or empty outputs, is NOT done.
```

### DRAFT — `build_custom.md`

```markdown
# Workflow Prompt Pack — Custom

## Role
You build an arbitrary deliverable that does not fit a named kind. Because the shape is
unknown, you impose the minimum rails yourself: declare your entrypoint/outputs, make
targeted edits, fake nothing, and prove the thing actually works. The host owns verification;
you build the real deliverable inside the rails.

## Artifact contract
A `custom` deliverable. The registry pins no required files (intentional). You MUST declare
your own entrypoint/output set in `.disco/custom_manifest.json` (GAP: pack convention) so the
verifier and the user know what the deliverable is. This is the one kind where full rewrite is
contract-permitted (rewrite_allowed=True) — use it for genuine structural change only.

## Workflow steps
1. Read the brief; choose the most literal deliverable that satisfies it.
2. Declare the entrypoint/outputs in `.disco/custom_manifest.json` (file_write).
3. Build the real deliverable (file_* tools; shell / code_exec / run_project_script as needed).
4. If web-shaped, preview_start and confirm it renders/works.
5. ready_for_artifact_verification.

## Allowed tools
file_write, file_edit, file_replace_lines, file_read, file_list, code_exec,
run_project_script, shell, preview_* (only if web-shaped), ready_for_artifact_verification.

## Forbidden tools
No manual port/server (use preview_* if previewing). No fabricated data or results. Do NOT use
the rewrite allowance to skip a fresh-read or to dodge a targeted edit.

## Targeted edit law
Prefer the smallest edit even though rewrite is allowed. A full rewrite is a fallback for
structural change, never a shortcut around a fresh-read. Fresh-read before any exact edit.

## Preview rule
If the deliverable is web-shaped, preview only through preview_start (host-owned port). If it
is files-shaped, there is no preview; surface outputs via present_artifact_for_download (GAP).

## Verify rule
ready_for_artifact_verification (standard) confirms the declared entrypoint/output exists AND
does what the brief asked (renders / runs / produces the file). Existence alone is a FAIL; a
clean console is not proof the deliverable works.

## Export rule
No dedicated export pipeline exists yet (GAP). If the deliverable is file-shaped, fall back to
a generic bundle (workflow_output_bundle-style — GAP). Never hand-zip the workspace.

## Context policy
Keep the goal + todo + the self-declared manifest in view. Snip resolved explorations once
summarized. Never compact an unresolved verifier failure.

## Done criteria
The declared deliverable exists AND satisfies the brief's intent (renders / runs / produces the
output) AND ready_for_artifact_verification passed. A declared-but-non-working deliverable is
NOT done.
```

---

## Tests required (every pack — checklist)

**Loader / structure**
- [ ] Each pack file loads via `prompt_pack` without error.
- [ ] Each pack defines **all 11** `REQUIRED_SECTIONS` (no missing key).
- [ ] No duplicate `## Header` in any pack (loader errors on duplicate).
- [ ] Every `## Header` slugs to exactly one of the 11 keys (no stray/unrecognized header).
- [ ] Fence-aware parse: `## ...` inside a fenced code block (e.g. the DRAFT skeletons, if a
      pack ever embeds one) is NOT treated as a section header.

**Tool-name validity**
- [ ] Every tool named in `allowed_tools` exists in the registry (grounding §4).
- [ ] Every tool named in `forbidden_tools` either exists in the registry or is an explicit
      anti-pattern phrase (e.g. `python -m http.server`).
- [ ] `allowed_tools` is a superset of the kind's `BuildContract` `bootstrap` + `edit` tools.
- [ ] The finalizer named in `verify_rule`/`workflow_steps` matches the kind's
      `VerificationContract.finalizer` and the regex `^ready_for_[a-z0-9_]+_verification$`.

**Contract consistency (no contradiction with the kind's BuildContract)**
- [ ] `artifact_contract` lists exactly the kind's `required_files` (or states the GAP for
      `workflow.output`/`custom`).
- [ ] The starter kit named matches `artifact.starter_kit` (or "none" where the registry has none).
- [ ] `export_rule` names the kind's real export pipeline, or explicitly states "no pipeline yet"
      where the registry has none (`interactive.prototype`, `workflow.output`, `custom`).
- [ ] `verify_rule` states the kind's `VerificationLevel` (`load_only`/`standard`/`strict`)
      consistent with the registry (e.g. `appkit.leadgen` = strict).
- [ ] No pack permits raw rewrite of an entrypoint except `custom` (`rewrite_allowed=True`).

**Cross-pack rule coverage (from table (b))**
- [ ] Every pack states the host-owned preview/verify/export rails.
- [ ] Every pack states the targeted-edit law + fresh-read + no-elision discipline.
- [ ] Every pack states no-fake-content (flag the four needs-tightening packs that omit it).
- [ ] Every pack states resource-copy path-safety (no `..`/absolute).
- [ ] Every pack states "render/works ≠ exists" in `verify_rule`.
- [ ] Every pack states the no-block / defer-don't-ask autonomous policy.
- [ ] Every pack states snip discipline (never compact unresolved verifier failures).

**GAP packs**
- [ ] `build_workflow_output.md` and `build_custom.md` exist and pass all of the above.
- [ ] Each GAP/proposed name in those packs is marked **(GAP)**.
- [ ] If/when `workflow.output` and `custom` get bootstrap tools + export pipelines in the
      registry, the packs are re-tested for consistency (remove the GAP markers).
```
