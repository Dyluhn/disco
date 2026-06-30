# Export Pipeline Spec — Artifact-VM Support Pack

> **Status:** SPEC ONLY. No runtime behavior is described as implemented. Every
> pipeline below is the *host-owned* export contract for the Artifact VM. Read
> [`_GROUNDING.md`](./_GROUNDING.md) first — it is the single source of truth for the
> real Disco names referenced here. Long-session context handling that these pipelines
> consume (the `resource_manifest.json`, handoff packages, version snapshots) is
> specified in [`CONTEXT_AND_ITERATION_SPEC.md`](./CONTEXT_AND_ITERATION_SPEC.md).
>
> **Scope.** This document defines *host-owned* export pipelines: the host (not the
> model) owns bundling, validation, and delivery so that a weak model cannot produce a
> "done" claim without real bytes on disk. P10b proved live export **bytes** flow from a
> real `DeliverableEvent` (`packages/core/src/disco/core/events.py`,
> `class DeliverableEvent`). Four pipelines exist today as
> `ExportContract(name, pipeline=(...))` rows in
> `packages/core/src/disco/core/contract/registry.py`; the rest are **(GAP)**.

---

## 0. Vocabulary anchor (from grounding)

| Concept | Real name / location |
| --- | --- |
| Per-kind export declaration | `ExportContract{ name, pipeline: tuple[str,...] }` in `contract/models.py` |
| Registered exports | `static_standalone`, `cloudflare_project`, `deck_export`, `document_pdf` (`registry.py`) |
| Stage shape | `(preflight,) bundle, validate, deliver` — `deck_export` has **no** preflight |
| Handoff event | `DeliverableEvent{ title, path, artifact_kind: "app"|"files", deployment_url }` (`events.py`) |
| Delivery mode | derived from `ContractKind` — **app** = `static.site`/`appkit.leadgen`/`interactive.prototype`; **files** = `deck`/`document`/`workflow.output`/`custom` |
| Finalizer family | `ready_for_*_verification` (regex `^ready_for_[a-z0-9_]+_verification$`), host-routed |

**Pipeline stage glossary (canonical 5 stages this spec uses):**

1. **preflight** — cheap fail-fast precondition checks *before* any copying (required
   files exist, kind matches, no obviously-poisoned inputs). May be omitted only where a
   materializer guarantees the source (deck).
2. **prepare/copy** — assemble a clean staging tree from the live workspace into an
   isolated export root; resolve frozen/snip context to fresh source (see
   `CONTEXT_AND_ITERATION_SPEC.md` §snip) and rewrite paths to be relative.
3. **bundle** — produce the concrete artifact bytes (zip, PDF, PPTX, deploy payload).
4. **validate** — assert the produced bytes satisfy fail-closed rules (non-zero, no host
   paths, manifest present) **before** anything is shown/delivered.
5. **deliver** — emit a `DeliverableEvent` (and any URL) so the UI presents a real
   handoff. Delivery is **only** reachable after `validate` passes.

> The registered `pipeline` tuples name `preflight, bundle, validate, deliver` — the
> **prepare/copy** work is folded into `bundle` in the real tuples. This spec separates
> prepare/copy as its own stage for clarity and for the GAP pipelines; an implementer may
> fold it back into `bundle`. Ordering invariant is unchanged: **validate strictly
> precedes deliver**.

---

## 1. Pipeline summary table

| # | Pipeline | Real export name / GAP | ContractKind(s) served | Delivery mode | Fail-closed checks | Oracle fixtures |
| --- | --- | --- | --- | --- | --- | --- |
| 1 | Static standalone HTML | `static_standalone` | `static.site` | app | zero-byte, abs-host-path, manifest-present, cross-project-ref, entrypoint-present | `export_zero_bytes_fail.json`, `absolute_host_path_fail.json`, `missing_resource_manifest_fail.json`, `cross_project_asset_reference_fail.json` |
| 2 | Cloudflare project export | `cloudflare_project` | `appkit.leadgen` (+ `interactive.prototype` **GAP**) | app | zero-byte, abs-host-path, manifest-present, cross-project-ref, appspec-present, no-secrets-in-bundle | `export_zero_bytes_fail.json`, `absolute_host_path_fail.json`, `missing_resource_manifest_fail.json`, `cross_project_asset_reference_fail.json`, `cloudflare_secret_in_bundle_fail.json` **(GAP fixture)** |
| 3 | PDF / `open_for_print` | `document_pdf` | `document` | files | zero-byte, valid-PDF-header, page-count≥1, abs-host-path-in-source, manifest-present | `export_zero_bytes_fail.json`, `pdf_zero_pages_fail.json` **(GAP)**, `missing_resource_manifest_fail.json`, `absolute_host_path_fail.json` |
| 4 | PPTX editable | `deck_export` | `deck` | files | zero-byte, valid-OOXML-zip, slide-count==authored-count, image-embed-present, manifest-present | `export_zero_bytes_fail.json`, `pptx_slide_count_mismatch_fail.json` **(GAP)**, `pptx_image_placeholder_leak_fail.json` **(GAP)**, `missing_resource_manifest_fail.json` |
| 5 | PPTX screenshot | **(GAP)** `deck_export_screenshot` | `deck` | files | zero-byte, image-count==slide-count, no-blank-frames, manifest-present | `export_zero_bytes_fail.json`, `screenshot_blank_frame_fail.json` **(GAP)**, `screenshot_count_mismatch_fail.json` **(GAP)** |
| 6 | Developer handoff | **(GAP)** `developer_handoff` | `static.site`, `interactive.prototype`, `appkit.leadgen`, `custom` | files | zero-byte, abs-host-path, manifest-present, cross-project-ref, no-secrets, README-present | `export_zero_bytes_fail.json`, `absolute_host_path_fail.json`, `missing_resource_manifest_fail.json`, `cross_project_asset_reference_fail.json`, `handoff_secret_leak_fail.json` **(GAP)** |
| 7 | Owner handoff | **(GAP)** `owner_handoff` | all kinds | files | zero-byte, manifest-present, context-pack-present, abs-host-path, no-secrets | `export_zero_bytes_fail.json`, `missing_resource_manifest_fail.json`, `missing_context_pack_fail.json` **(GAP)**, `absolute_host_path_fail.json` |
| 8 | Deployment handoff | **(GAP)** `deployment_handoff` (URL-bearing wrapper over `cloudflare_project`) | `appkit.leadgen`, `static.site` | app | zero-byte (payload), URL-reachable, deployment-url-set, no-secrets-in-bundle | `export_zero_bytes_fail.json`, `deployment_url_unreachable_fail.json` **(GAP)**, `cloudflare_secret_in_bundle_fail.json` **(GAP)** |
| 9 | Public URL handoff | **(GAP)** `public_url_handoff` | `appkit.leadgen`, `static.site`, `interactive.prototype` | app | URL-reachable, url-is-https, not-loopback-url, deployment-url-set | `deployment_url_unreachable_fail.json` **(GAP)**, `loopback_url_handoff_fail.json` **(GAP)** |
| 10 | Canva-style external handoff (future optional) | **(GAP)** `external_canva_handoff` | `deck`, `document` | files | zero-byte, valid-source-format, external-auth-present, no-secrets, round-trip-shape | `export_zero_bytes_fail.json`, `external_auth_missing_fail.json` **(GAP)**, `external_round_trip_shape_fail.json` **(GAP)** |

> Fixtures named without **(GAP)** are the four canonical ones the grounding names
> (`export_zero_bytes_fail.json`, `cross_project_asset_reference_fail.json`,
> `absolute_host_path_fail.json`, `missing_resource_manifest_fail.json`) and are reused
> across pipelines. Fixtures marked **(GAP)** are proposed new oracles this pack should add
> under `fixtures/oracles/`. Every fixture MUST be valid JSON with a `_meta` block
> (`purpose`, `expected_verdict`) per grounding §11.5.

---

## 2. Per-pipeline specs

Each spec uses the same 9-part shape:
**(a) identity · (b) preflight · (c) prepare/copy · (d) bundle · (e) validate ·
(f) deliver · (g) fallback · (h) user-facing copy · (i) internal flags / fail-closed /
oracles.**

---

### Pipeline 1 — Static standalone HTML — `static_standalone` (REAL)

**(a) Identity.** Real export `static_standalone`; declared
`pipeline=("preflight","bundle","validate","deliver")` for `static.site` in
`registry.py:_static_site`. Serves **ContractKind `static.site`**. **Delivery mode: app**
(`DeliverableEvent.artifact_kind="app"`, opened in live preview). Required source file:
`index.html`. Finalizer: `ready_for_static_site_verification` (standard).

**(b) preflight.**
- [ ] `index.html` exists and is non-empty in the project root.
- [ ] All `<link>`/`<script>`/`<img>` `src`/`href` referenced local assets resolve to
      files inside the project root (no `..` escapes, no other-project paths).
- [ ] No reference points at an **absolute host path** (`/home/...`, `/var/...`,
      `file://`, `C:\`).
- [ ] `resource_manifest.json` exists (or is regenerated) listing every asset to bundle
      (see `CONTEXT_AND_ITERATION_SPEC.md`).

**(c) prepare/copy.** Copy project root → isolated export staging dir; resolve any
snip/frozen context to fresh source before snapshotting; rewrite all asset paths to be
**relative to the bundle root**; strip `.disco/` internal context unless `--include-context`.

**(d) bundle.** Produce a single self-contained directory (and a `.zip` of it) whose
entrypoint is `index.html` and whose every referenced asset is present inside the bundle.

**(e) validate.**
- [ ] Bundle total size > 0 and `index.html` size > 0 (**zero-byte guard**).
- [ ] No file in the bundle, and no path *inside* any bundled text file, is an absolute
      host path.
- [ ] `resource_manifest.json` present in bundle and every manifest entry resolves to a
      real bundled file (and vice versa — no orphan files, no dangling refs).
- [ ] No asset reference resolves outside the bundle (**cross-project asset reference**).
- [ ] Re-running export over an unchanged workspace produces a byte-identical-or-
      semantically-equal bundle (**idempotent re-export**).

**(f) deliver.** Emit `DeliverableEvent{ title, path=<bundle root>, artifact_kind="app" }`.
Only reachable if (e) fully passed.

**(g) fallback.** If preflight finds a missing asset → fail closed, do NOT bundle, return
a structured `export_blocked` with the offending ref. If bundle dir is empty → fail
closed (never deliver an empty app). Never silently drop a missing asset.

**(h) user-facing copy.**
- Success: `"Exported your site as a standalone bundle (N files). Open it from the
  preview, or download the .zip."`
- Zero-byte: `"Export blocked: the site bundle came out empty. The source index.html
  has no content yet — add content and re-export."`
- Missing asset: `"Export blocked: index.html references 'assets/logo.png' but that file
  isn't in the project. Add it (or remove the reference) and re-export."`
- Absolute path: `"Export blocked: a link points at an absolute path on the build
  machine ('/home/...'). Standalone bundles must use relative paths."`

**(i) internal flags / fail-closed / oracles.**
- Flags: `bundle_byte_count`, `entrypoint_present`, `abs_path_hits[]`,
  `cross_project_refs[]`, `manifest_present`, `manifest_orphans[]`, `idempotent_hash`.
- Fail-closed: any flag in the failing set ⇒ block at validate, never deliver.
- Oracles: `export_zero_bytes_fail.json`, `absolute_host_path_fail.json`,
  `missing_resource_manifest_fail.json`, `cross_project_asset_reference_fail.json`.

---

### Pipeline 2 — Cloudflare project export — `cloudflare_project` (REAL)

**(a) Identity.** Real export `cloudflare_project`;
`pipeline=("preflight","bundle","validate","deliver")` for `appkit.leadgen`
(`registry.py:_appkit_leadgen`). Serves **`appkit.leadgen`** today; **(GAP)**: also a
candidate export for `interactive.prototype` (which has no export — grounding §3).
**Delivery mode: app.** Required source files: `.disco/appspec.json`, `index.html`.
Finalizer: `ready_for_app_verification` (**strict**). Skill: `cloudflare_export`.

**(b) preflight.**
- [ ] `.disco/appspec.json` exists, is valid JSON, and parses to a known AppSpec shape.
- [ ] `index.html` exists (the rendered app entrypoint) and is non-empty.
- [ ] AppSpec `tweaks` resolve against `.disco/tweaks.json` (no dangling tweak keys).
- [ ] No secret/credential material in the project tree slated for upload (scan for
      API-key shapes, `.env`, private keys).
- [ ] `resource_manifest.json` present.

**(c) prepare/copy.** Render AppSpec → static deploy payload (HTML + assets) in staging;
resolve snip/frozen context first; strip `.disco/` internals from the *deploy* payload
(keep them only in an optional owner-handoff sidecar); rewrite asset paths relative to the
deploy root; **redact** any matched secret before bundling (do not just warn).

**(d) bundle.** Produce the Cloudflare-project deploy payload (a directory matching the
provider's expected project layout) plus a `.zip` mirror for local download.

**(e) validate.**
- [ ] Payload size > 0; entrypoint present (**zero-byte guard**).
- [ ] `.disco/appspec.json` present in the *handoff* sidecar (not the public payload).
- [ ] No absolute host paths anywhere in payload text.
- [ ] No asset reference escapes the payload root (**cross-project ref**).
- [ ] `resource_manifest.json` present and consistent.
- [ ] **No secrets** present in the bundle (post-redaction re-scan must be clean).
- [ ] Idempotent re-export.

**(f) deliver.** Emit `DeliverableEvent{ artifact_kind="app", path=<payload root>,
deployment_url=<if a deploy target was used> }`. If a real deploy occurred, set
`deployment_url`; otherwise leave empty and present the local payload.

**(g) fallback.** No deploy target configured → still produce + deliver the local payload
(degrade to a downloadable Cloudflare-ready project, not a failure). Secret detected and
un-redactable → fail closed, never upload. Missing `appspec.json` → fail closed.

**(h) user-facing copy.**
- Success (deployed): `"Deployed your app to Cloudflare. Open it at <url>, or download the
  project."`
- Success (local only): `"Built a Cloudflare-ready project (no deploy target set).
  Download it and run 'wrangler deploy', or connect a target and re-export."`
- Secret blocked: `"Export blocked: a credential was found in the files about to be
  uploaded. Move secrets out of the project (use environment variables) and re-export."`

**(i) internal flags / fail-closed / oracles.**
- Flags: `payload_byte_count`, `appspec_present`, `secret_hits[]`, `abs_path_hits[]`,
  `cross_project_refs[]`, `manifest_present`, `deployment_url_set`, `idempotent_hash`.
- Fail-closed: missing appspec, any secret hit, zero-byte payload, abs-path, cross-ref.
- Oracles: `export_zero_bytes_fail.json`, `absolute_host_path_fail.json`,
  `missing_resource_manifest_fail.json`, `cross_project_asset_reference_fail.json`,
  `cloudflare_secret_in_bundle_fail.json` **(GAP)**.

---

### Pipeline 3 — PDF / `open_for_print` — `document_pdf` (REAL)

**(a) Identity.** Real export `document_pdf`;
`pipeline=("preflight","bundle","validate","deliver")` for `document`
(`registry.py:_document`). Serves **`document`**. **Delivery mode: files.** Required
source: `report.md`. Finalizer: `ready_for_document_verification` (standard). The
`open_for_print` affordance (print-optimized view) is the **same** pipeline with a
print-CSS preflight flag.

**(b) preflight.**
- [ ] `report.md` exists and is non-empty.
- [ ] All embedded image/asset references in the markdown resolve to local files.
- [ ] No absolute host path in any reference.
- [ ] `resource_manifest.json` present (lists images, fonts, css used by the PDF render).
- [ ] (print mode) print stylesheet resolves.

**(c) prepare/copy.** Copy markdown + referenced assets to staging; resolve snip/frozen
context; inline or relative-path the assets; select the render template/brand (see
`core/kits/brand.py`).

**(d) bundle.** Render markdown → PDF bytes (single `.pdf`). For `open_for_print`,
additionally produce the print-CSS HTML view.

**(e) validate.**
- [ ] PDF byte length > 0 (**zero-byte guard**).
- [ ] PDF starts with a valid `%PDF-` header and ends with a valid trailer.
- [ ] Page count ≥ 1 (**`pdf_zero_pages_fail` guard** — GAP fixture).
- [ ] No absolute host path leaked into PDF metadata / embedded text.
- [ ] `resource_manifest.json` present and every referenced image is embedded (no broken
      image boxes).
- [ ] Idempotent re-export (same source → same page count + same embedded asset set).

**(f) deliver.** Emit `DeliverableEvent{ artifact_kind="files", path=<report.pdf> }`.

**(g) fallback.** Render engine error → fail closed with the engine message; never deliver
a 0-page or header-less PDF. Missing image → fail closed (do not render a broken-image
box silently); report the missing asset.

**(h) user-facing copy.**
- Success: `"Exported your document as a PDF (N pages)."`
- Zero pages: `"Export blocked: the PDF rendered with no pages. report.md may be empty —
  add content and re-export."`
- Missing image: `"Export blocked: report.md embeds 'figs/chart.png' which isn't in the
  project. Add it (or remove it) and re-export."`

**(i) internal flags / fail-closed / oracles.**
- Flags: `pdf_byte_count`, `pdf_header_ok`, `page_count`, `embedded_assets[]`,
  `missing_assets[]`, `abs_path_hits[]`, `manifest_present`, `idempotent_hash`.
- Fail-closed: zero-byte, bad header, zero pages, missing embedded asset, abs-path.
- Oracles: `export_zero_bytes_fail.json`, `pdf_zero_pages_fail.json` **(GAP)**,
  `missing_resource_manifest_fail.json`, `absolute_host_path_fail.json`.

---

### Pipeline 4 — PPTX editable — `deck_export` (REAL)

**(a) Identity.** Real export `deck_export`; declared `pipeline=("bundle","validate",
"deliver")` — **no preflight** — for `deck` (`registry.py:_deck`). Serves **`deck`**.
**Delivery mode: files.** Required source: `deck.authored.json` (the AuthoredDeck
sidecar). Materialized by `slides_generate`; rendered by `_pptx_render` /
`_deck_patch` (grounding §0). Finalizer: `ready_for_deck_verification` (standard).

> **Why no preflight:** the deck has no hand-authored file map — `slides_generate` *is*
> the materializer, so `deck.authored.json` is guaranteed by construction. This pack
> recommends a **(GAP)** lightweight preflight be added anyway (assert
> `deck.authored.json` parses + slide count > 0) to close the fail-fast gap; until then
> the equivalent checks run inside `validate`.

**(b) preflight.** *(absent in the real tuple — proposed GAP additions in italics)*
- [ ] *`deck.authored.json` exists and parses to ≥1 slide.*
- [ ] *Every slide image reference resolves to a real generated asset (no `[image]`
      placeholder strings — ties to the C7 image-embed fix).*

**(c) prepare/copy.** Resolve AuthoredDeck → render model; pull in generated slide images
as embeddable bytes (the C7 "bytes-on-Element" path); apply brand/template chrome.

**(d) bundle.** Render → editable `.pptx` (OOXML zip) with one slide per authored slide and
all images embedded (NOT `[image]` placeholders). Also produce the HTML deck mirror.

**(e) validate.**
- [ ] `.pptx` byte length > 0 (**zero-byte guard**).
- [ ] File is a valid OOXML zip (openable; `[Content_Types].xml` present).
- [ ] Rendered slide count **==** authored slide count
      (**`pptx_slide_count_mismatch_fail` — GAP**).
- [ ] Every image-bearing slide has an **embedded** image part (no `[image]` placeholder
      text leaked — **`pptx_image_placeholder_leak_fail` — GAP**).
- [ ] `resource_manifest.json` present, lists every embedded image.
- [ ] Idempotent re-export.

**(f) deliver.** Emit `DeliverableEvent{ artifact_kind="files", path=<deck.pptx> }`.

**(g) fallback.** Image asset missing at render → fail closed (do not embed a placeholder
string into a "finished" PPTX — that is exactly the C7 bug). OOXML render error → fail
closed with the render message.

**(h) user-facing copy.**
- Success: `"Exported your deck as an editable PowerPoint (N slides, M images embedded)."`
- Placeholder leak: `"Export blocked: slide 4 still has an unrendered image placeholder.
  The image didn't generate — regenerate it and re-export."`
- Count mismatch: `"Export blocked: the deck has N authored slides but only rendered M.
  Re-run export."`

**(i) internal flags / fail-closed / oracles.**
- Flags: `pptx_byte_count`, `ooxml_valid`, `authored_slide_count`, `rendered_slide_count`,
  `embedded_image_count`, `placeholder_leaks[]`, `manifest_present`, `idempotent_hash`.
- Fail-closed: zero-byte, invalid OOXML, count mismatch, placeholder leak.
- Oracles: `export_zero_bytes_fail.json`, `pptx_slide_count_mismatch_fail.json` **(GAP)**,
  `pptx_image_placeholder_leak_fail.json` **(GAP)**, `missing_resource_manifest_fail.json`.

---

### Pipeline 5 — PPTX screenshot — `deck_export_screenshot` **(GAP)**

**(a) Identity.** **(GAP)** — no registered export. Proposed name
`deck_export_screenshot`, a sibling of `deck_export` that produces **flattened image
renders** (one PNG per slide) for non-editable visual handoff / thumbnails. Serves
**`deck`**. **Delivery mode: files.** Proposed
`pipeline=("preflight","bundle","validate","deliver")`.

**(b) preflight.**
- [ ] `deck.authored.json` parses to ≥1 slide.
- [ ] Render surface (headless renderer) available; brand/template resolves.

**(c) prepare/copy.** Render each slide to a frame buffer at a fixed export resolution.

**(d) bundle.** Produce one PNG per slide + a `.zip`; optionally a contact-sheet PDF.

**(e) validate.**
- [ ] Each PNG byte length > 0 (**zero-byte guard**, per frame).
- [ ] Image count **==** slide count (**`screenshot_count_mismatch_fail` — GAP**).
- [ ] No frame is blank/all-one-color above a threshold
      (**`screenshot_blank_frame_fail` — GAP**).
- [ ] `resource_manifest.json` present listing each frame.
- [ ] Idempotent re-export.

**(f) deliver.** `DeliverableEvent{ artifact_kind="files", path=<screenshots dir/zip> }`.

**(g) fallback.** A slide fails to render → fail closed for that frame; never emit a deck
with a missing/blank frame silently.

**(h) user-facing copy.**
- Success: `"Exported N slide images from your deck."`
- Blank frame: `"Export blocked: slide 3 rendered blank. Check its content and re-export."`

**(i) internal flags / fail-closed / oracles.**
- Flags: `frame_count`, `slide_count`, `blank_frames[]`, `per_frame_bytes[]`,
  `manifest_present`.
- Fail-closed: any zero-byte frame, count mismatch, blank frame.
- Oracles: `export_zero_bytes_fail.json`, `screenshot_blank_frame_fail.json` **(GAP)**,
  `screenshot_count_mismatch_fail.json` **(GAP)**.

---

### Pipeline 6 — Developer handoff — `developer_handoff` **(GAP)**

**(a) Identity.** **(GAP)** — proposed `developer_handoff`. A **source-bundle** export: the
editable project (source files + build instructions + resource manifest) for a developer to
continue work. Serves **`static.site`, `interactive.prototype`, `appkit.leadgen`,
`custom`**. **Delivery mode: files.** Proposed
`pipeline=("preflight","bundle","validate","deliver")`. This is the durable-context
counterpart of the deploy exports; it ships the `.disco/` context dir (see
`CONTEXT_AND_ITERATION_SPEC.md` §handoff packages).

**(b) preflight.**
- [ ] Required source files for the kind exist (e.g. `index.html`, or `.disco/appspec.json`).
- [ ] `.disco/` context present (`todo.md`, `decisions.md`, `resource_manifest.json`).
- [ ] No secrets in the tree.
- [ ] No absolute host paths in source.

**(c) prepare/copy.** Copy full project incl. `.disco/`; resolve snip context; generate a
`README.md` (how to run / build / deploy) if absent; redact secrets.

**(d) bundle.** `.zip` of the project (source + `.disco/` + README + manifest).

**(e) validate.**
- [ ] Bundle size > 0 (**zero-byte guard**).
- [ ] No absolute host paths; no cross-project refs.
- [ ] `resource_manifest.json` present and consistent.
- [ ] `README.md` present (build/run instructions).
- [ ] No secrets (post-redaction re-scan clean).
- [ ] Idempotent re-export.

**(f) deliver.** `DeliverableEvent{ artifact_kind="files", path=<handoff.zip> }`.

**(g) fallback.** Missing README and cannot auto-generate → fail closed (a dev handoff
without run instructions is incomplete). Secret un-redactable → fail closed.

**(h) user-facing copy.**
- Success: `"Packaged a developer handoff: full source, build instructions, and asset
  manifest."`
- Secret blocked: `"Export blocked: a credential is in the source. Move it to an env var
  and re-export."`

**(i) internal flags / fail-closed / oracles.**
- Flags: `bundle_byte_count`, `readme_present`, `context_dir_present`, `secret_hits[]`,
  `abs_path_hits[]`, `cross_project_refs[]`, `manifest_present`, `idempotent_hash`.
- Fail-closed: zero-byte, missing README, secret hit, abs-path, cross-ref.
- Oracles: `export_zero_bytes_fail.json`, `absolute_host_path_fail.json`,
  `missing_resource_manifest_fail.json`, `cross_project_asset_reference_fail.json`,
  `handoff_secret_leak_fail.json` **(GAP)**.

---

### Pipeline 7 — Owner handoff — `owner_handoff` **(GAP)**

**(a) Identity.** **(GAP)** — proposed `owner_handoff`. A **durable-context** export for the
non-technical owner: the finished artifact PLUS its ContextPack (decisions, todo, version
history) so a *future* session (or a different model) can resume with full memory. Serves
**all kinds**. **Delivery mode: files.** Proposed
`pipeline=("preflight","bundle","validate","deliver")`. The ContextPack contents are
specified in `CONTEXT_AND_ITERATION_SPEC.md` §ContextPack and §handoff packages.

**(b) preflight.**
- [ ] The kind's primary deliverable exists (the built artifact from pipelines 1–5).
- [ ] ContextPack present (`.disco/context_pack.json` **(GAP)**, `decisions.md`, `todo.md`).
- [ ] `resource_manifest.json` present.
- [ ] No secrets.

**(c) prepare/copy.** Assemble: finished artifact + ContextPack + version snapshots index +
a human summary (`HANDOFF.md`).

**(d) bundle.** `.zip` containing the artifact, `.disco/` context, and `HANDOFF.md`.

**(e) validate.**
- [ ] Bundle size > 0 (**zero-byte guard**).
- [ ] ContextPack present and parseable (**`missing_context_pack_fail` — GAP**).
- [ ] `resource_manifest.json` present.
- [ ] No absolute host paths; no secrets.
- [ ] Idempotent re-export.

**(f) deliver.** `DeliverableEvent{ artifact_kind="files", path=<owner_handoff.zip> }`.

**(g) fallback.** ContextPack missing and cannot be reconstructed from event log → fail
closed (an owner handoff without context is just a file bundle — that is pipeline 6).

**(h) user-facing copy.**
- Success: `"Packaged an owner handoff: the finished work plus its full decision history,
  so you (or a future session) can pick up exactly where this left off."`
- Missing context: `"Export blocked: no decision/context record was found for this
  project, so a resumable owner handoff can't be built."`

**(i) internal flags / fail-closed / oracles.**
- Flags: `bundle_byte_count`, `context_pack_present`, `handoff_md_present`,
  `manifest_present`, `abs_path_hits[]`, `secret_hits[]`, `idempotent_hash`.
- Fail-closed: zero-byte, missing context pack, secret hit, abs-path.
- Oracles: `export_zero_bytes_fail.json`, `missing_resource_manifest_fail.json`,
  `missing_context_pack_fail.json` **(GAP)**, `absolute_host_path_fail.json`.

---

### Pipeline 8 — Deployment handoff — `deployment_handoff` **(GAP)**

**(a) Identity.** **(GAP)** — proposed `deployment_handoff`, a URL-bearing wrapper that runs
`cloudflare_project` (pipeline 2) and then performs / confirms the deploy, surfacing a live
`deployment_url`. Serves **`appkit.leadgen`, `static.site`**. **Delivery mode: app.**
Proposed `pipeline=("preflight","bundle","validate","deliver")` where `deliver` sets
`DeliverableEvent.deployment_url`.

**(b) preflight.**
- [ ] Pipeline 2 (`cloudflare_project`) preflight passes (appspec/entrypoint/secrets).
- [ ] A deploy target/credential is configured (else degrade — see fallback).

**(c) prepare/copy.** Reuse pipeline 2 prepare/copy → deploy payload.

**(d) bundle.** Reuse pipeline 2 bundle → deploy payload + zip.

**(e) validate.**
- [ ] Payload size > 0 (**zero-byte guard**).
- [ ] No secrets in payload.
- [ ] **Post-deploy:** `deployment_url` is set and **reachable** (HTTP 2xx/3xx)
      (**`deployment_url_unreachable_fail` — GAP**).

**(f) deliver.** `DeliverableEvent{ artifact_kind="app", deployment_url=<live url>,
path=<payload> }`. The UI surfaces "Open deployed app".

**(g) fallback.** No deploy target → degrade to pipeline 2 local-payload delivery (success,
not failure) with copy explaining how to connect a target. Deploy succeeds but URL
unreachable → fail closed (do not claim a live deploy that 404s).

**(h) user-facing copy.**
- Success: `"Deployed and live at <url>."`
- Unreachable: `"Deploy reported success but <url> isn't responding. Not marking this as
  live — check the deploy logs and retry."`
- No target: `"No deploy target connected. Built a deployable project instead — connect a
  target to go live."`

**(i) internal flags / fail-closed / oracles.**
- Flags: `payload_byte_count`, `secret_hits[]`, `deployment_url_set`, `url_reachable`,
  `http_status`, `idempotent_hash`.
- Fail-closed: zero-byte, secret hit, url-claimed-but-unreachable.
- Oracles: `export_zero_bytes_fail.json`, `deployment_url_unreachable_fail.json` **(GAP)**,
  `cloudflare_secret_in_bundle_fail.json` **(GAP)**.

---

### Pipeline 9 — Public URL handoff — `public_url_handoff` **(GAP)**

**(a) Identity.** **(GAP)** — proposed `public_url_handoff`. Surfaces a **shareable public
URL** for an app (a tunnel/share link rather than a full deploy). Serves **`appkit.leadgen`,
`static.site`, `interactive.prototype`**. **Delivery mode: app.** Proposed
`pipeline=("preflight","validate","deliver")` (no bundle — it shares the already-built app).
Ties to `DeliverableEvent.deployment_url` and the share service path noted in grounding.

**(b) preflight.**
- [ ] A built/served app exists (a prior app-mode deliverable or live preview root).
- [ ] A share mechanism (tunnel/share-service) is available.

**(c) prepare/copy.** *(none — reuses the live served app; no new bytes)*

**(d) bundle.** *(none)*

**(e) validate.**
- [ ] A `deployment_url` is produced and set (**not empty**).
- [ ] URL is **https** and **not loopback** (`localhost`/`127.0.0.1`/`0.0.0.0`)
      (**`loopback_url_handoff_fail` — GAP**) — a loopback URL is not a public handoff.
- [ ] URL is **reachable** (HTTP 2xx/3xx) (**`deployment_url_unreachable_fail` — GAP**).

**(f) deliver.** `DeliverableEvent{ artifact_kind="app", deployment_url=<public url> }`.

**(g) fallback.** Only a loopback URL is available → fail closed for *public* handoff;
offer the local preview instead (do not present a localhost link as "shareable").

**(h) user-facing copy.**
- Success: `"Your app is shareable at <url> — anyone with the link can open it."`
- Loopback: `"Can't create a public link: only a local address is available. The app
  works in your preview, but it isn't reachable from outside this machine yet."`

**(i) internal flags / fail-closed / oracles.**
- Flags: `deployment_url_set`, `url_is_https`, `url_is_loopback`, `url_reachable`,
  `http_status`.
- Fail-closed: empty URL, loopback URL, unreachable URL.
- Oracles: `deployment_url_unreachable_fail.json` **(GAP)**,
  `loopback_url_handoff_fail.json` **(GAP)**.

---

### Pipeline 10 — Canva-style external handoff — `external_canva_handoff` **(GAP, future optional)**

**(a) Identity.** **(GAP, FUTURE OPTIONAL)** — proposed `external_canva_handoff`. Exports a
deck/document into an **external editing tool's** import format (e.g. a Canva-importable
package) so the owner can keep editing outside Disco. Serves **`deck`, `document`**.
**Delivery mode: files** (an importable package; optionally an external URL). Proposed
`pipeline=("preflight","bundle","validate","deliver")`. Marked optional/future: only build
if a concrete external target is committed.

**(b) preflight.**
- [ ] Source artifact exists (`deck.authored.json` or `report.md`).
- [ ] External-tool auth/integration configured (**`external_auth_missing_fail` — GAP**).
- [ ] Target import format is supported for this kind.

**(c) prepare/copy.** Map Disco's authored model → the external tool's import schema;
resolve images to embeddable assets.

**(d) bundle.** Produce the external-import package (the format the target tool ingests).

**(e) validate.**
- [ ] Package size > 0 (**zero-byte guard**).
- [ ] Package matches the external schema shape (**`external_round_trip_shape_fail` —
      GAP** — a round-trip import-shape check).
- [ ] No secrets.
- [ ] Slide/section count preserved vs source.
- [ ] Idempotent re-export.

**(f) deliver.** `DeliverableEvent{ artifact_kind="files", path=<package>,
deployment_url=<external edit url if created> }`.

**(g) fallback.** External auth missing → fail closed with a connect-account message
(do not produce a half-package the external tool will reject). Unsupported feature in
source → degrade gracefully (drop with a logged warning) only for non-structural elements;
fail closed if structure (slides/sections) cannot be represented.

**(h) user-facing copy.**
- Success: `"Exported to a Canva-importable package — import it to keep editing in Canva."`
- Auth missing: `"Connect your Canva account to export there. Until then, you can export a
  standard PPTX/PDF."`

**(i) internal flags / fail-closed / oracles.**
- Flags: `package_byte_count`, `external_auth_present`, `schema_shape_ok`,
  `section_count_preserved`, `secret_hits[]`, `idempotent_hash`.
- Fail-closed: zero-byte, schema-shape mismatch, structural loss, secret hit, auth missing.
- Oracles: `export_zero_bytes_fail.json`, `external_auth_missing_fail.json` **(GAP)**,
  `external_round_trip_shape_fail.json` **(GAP)**.

---

## 3. Global fail-closed rules (apply to EVERY pipeline)

- [ ] **FC-1 Zero-byte guard.** Never deliver a bundle, file, frame, or payload whose byte
      length is 0, nor whose declared entrypoint is empty. (`export_zero_bytes_fail.json`)
- [ ] **FC-2 No absolute host paths.** No bundled file path, and no path *inside* any
      bundled text/metadata, may be an absolute host path (`/home`, `/var`, `file://`,
      drive letters). (`absolute_host_path_fail.json`)
- [ ] **FC-3 Resource manifest present + consistent.** Every files/app bundle MUST contain a
      `resource_manifest.json`; every manifest entry resolves to a real bundled file and
      every bundled asset appears in the manifest (no orphans, no dangling refs).
      (`missing_resource_manifest_fail.json`)
- [ ] **FC-4 No cross-project asset references.** No reference may resolve outside the
      bundle/payload root or into another project. (`cross_project_asset_reference_fail.json`)
- [ ] **FC-5 Validate strictly precedes deliver.** `deliver` is unreachable unless `validate`
      returned a clean verdict for ALL applicable checks. No "deliver, then validate".
- [ ] **FC-6 No secrets in any outbound bundle.** Any export that leaves the VM (deploy,
      handoff, external) must re-scan post-redaction and be clean, or fail closed.
- [ ] **FC-7 Fail closed, never silent-drop.** A missing asset / unrenderable element /
      unreachable URL blocks the export with a structured reason — never a silent omission
      and never a placeholder string in a "finished" artifact (the C7 lesson).
- [ ] **FC-8 Idempotent re-export.** Re-exporting an unchanged workspace yields a
      semantically-equal result (same file set, page/slide/frame counts, manifest); export
      is side-effect-free on the source workspace.
- [ ] **FC-9 No model-asserted completion.** The host owns bundle/validate/deliver; the
      model cannot emit a `DeliverableEvent` that bypasses host validation. A finalizer
      (`ready_for_*_verification`) gates entry; the export stages gate the bytes.

---

## 4. Tests required (checklist — every pipeline)

- [ ] **T-1 Zero-byte guard.** For each pipeline, an empty/empty-entrypoint source is
      blocked at `validate`, no `DeliverableEvent` is emitted. (oracle:
      `export_zero_bytes_fail.json`)
- [ ] **T-2 No absolute host paths in bundle.** A source containing an absolute host path
      reference is blocked. (oracle: `absolute_host_path_fail.json`)
- [ ] **T-3 Resource manifest present.** A bundle missing `resource_manifest.json`, or with
      a manifest that has orphans/dangling refs, is blocked. (oracle:
      `missing_resource_manifest_fail.json`)
- [ ] **T-4 Cross-project asset reference.** A reference escaping the bundle root is blocked.
      (oracle: `cross_project_asset_reference_fail.json`)
- [ ] **T-5 Validate-before-deliver ordering.** Assert `deliver` is never invoked when any
      `validate` flag fails (state-machine/ordering test; inject a validate failure and
      confirm no `DeliverableEvent`).
- [ ] **T-6 Idempotent re-export.** Export twice over an unchanged workspace; assert
      semantically-equal output and zero mutation of the source tree.
- [ ] **T-7 Real-bytes deliver (P10b parity).** A passing export emits a `DeliverableEvent`
      whose `path` points at real non-zero bytes (live, not a fixture stub) — the P10b
      property generalized to every pipeline.
- [ ] **T-8 Fail-closed copy.** Each fail-closed branch returns the exact user-facing
      message specified in this doc (copy-stability test).
- [ ] **T-9 Per-pipeline structural checks.** PDF page-count≥1; PPTX slide-count==authored
      & no placeholder leak; screenshot frame-count==slide-count & no blank frame;
      deployment/public-url reachable & non-loopback; secrets-clean for outbound bundles.
- [ ] **T-10 Delivery-mode correctness.** `artifact_kind` on the emitted `DeliverableEvent`
      matches the kind's derived `DeliveryMode` (app vs files) for every pipeline.
- [ ] **T-11 GAP-fixture authoring.** Each **(GAP)** oracle named above exists under
      `fixtures/oracles/`, is valid JSON, and has a `_meta{ purpose, expected_verdict }`.

> Cross-reference: the context inputs these tests assume (manifest, ContextPack, snip
> resolution before snapshot) are defined in
> [`CONTEXT_AND_ITERATION_SPEC.md`](./CONTEXT_AND_ITERATION_SPEC.md). Real names and the
> registered four pipelines are in [`_GROUNDING.md`](./_GROUNDING.md) §8.
