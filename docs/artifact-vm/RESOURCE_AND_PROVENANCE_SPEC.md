# Resource & Provenance Spec — `ResourceManifest` + asset provenance

> **Read `_GROUNDING.md` first.** Documentation only — no runtime behavior implemented or
> claimed. This spec defines how every external/generated/user asset enters a Disco
> artifact, how it is copied, tracked, and validated before export, and the hard rules
> that keep artifacts self-contained, honest, and free of host-path / cross-project leaks.
>
> Cross-references: `DIRECT_MANIPULATION_SPEC.md` (`data-disco-media-slot` is the DOM peer
> of a manifest slot), `CONTENT_AND_DESIGN_DISCIPLINE_SPEC.md` (content provenance tags +
> "no fake logos/photos/testimonials"), `_GROUNDING.md` (real Disco names, export
> pipelines §8), and the oracle fixtures named throughout (`fixtures/oracles/*.json`).

---

## 0. Status legend

| Marker | Meaning |
| --- | --- |
| **(EXISTS)** | Real today. Referenced, not re-specced. |
| **(GAP)** | New surface proposed by this pack. Not implemented. |

The **`ResourceManifest`** at `.disco/resource_manifest.json`, copied-asset discipline,
the bulk-import gate, and the media-slot model are all **(GAP)**. CD-TOOLS governed
artifact routing, `safe_write_file`, and the export pipelines (`static_standalone`,
`cloudflare_project`, `deck_export`, `document_pdf` — `_GROUNDING.md §8`) are
**(EXISTS)** and are the enforcement hosts this spec hooks into.

---

## 1. The thesis

> An artifact is **self-contained and portable**: every asset it references is copied into
> the project under a **project-relative** path, recorded in a manifest with its
> provenance, and present at export time. No artifact may reference a host absolute path,
> another project's files, a private/internal resource, or a fabricated asset (fake logo,
> stock-as-real photo, invented testimonial). The manifest is the **single source of truth
> for "what is in this artifact and where did it come from."**

---

## 2. `ResourceManifest` schema

**Stored at:** `.disco/resource_manifest.json` (per conversation workspace, alongside
`.disco/appspec.json` / `.disco/tweaks.json` — see `_GROUNDING.md §7`).

### 2.1 Top-level shape

| Field | Type | Required | Meaning |
| --- | --- | --- | --- |
| `version` | int | yes | Manifest schema version (start at `1`). |
| `artifact_version` | string | yes | The `data-disco-version` (`v_…`) this manifest matches. |
| `entries` | array<ResourceEntry> | yes | All tracked resources (may be empty `[]`). |
| `generated_at` | string (RFC3339) | yes | Host write timestamp. |

### 2.2 `ResourceEntry` schema

| Field | Type | Required | Constraint / meaning |
| --- | --- | --- | --- |
| `id` | string | yes | Stable id, `^res_[A-Za-z0-9_]+$`. Join key for `data-disco-media-slot` (`slot_x` ↔ `res_x`) and content tags. |
| `source_kind` | enum | yes | one of `user` \| `research` \| `generated` \| `external`. |
| `source_ref` | string | yes | Where it came from (see §2.3). Never a host absolute path. |
| `copied_path` | string \| null | yes | **Project-relative POSIX path only.** `null` only when `status="missing"` or `"requires_user_input"`. |
| `media_type` | string | yes | MIME type, e.g. `image/png`, `image/svg+xml`, `video/mp4`, `font/woff2`. |
| `license` | string \| null | yes | SPDX id or license name; `null` only if `source_kind="user"` and user-owned. |
| `provenance` | object | yes | `{ attribution?, url?, retrieved_at?, prompt?, model? }` — how to credit/reproduce. |
| `status` | enum | yes | `present` \| `missing` \| `requires_user_input` \| `placeholder`. |
| `slot` | string \| null | no | The `data-disco-media-slot` id it fills (`slot_…`), if any. |
| `checksum` | string \| null | no | `sha256:<hex>` of the copied bytes when `status="present"`. |

### 2.3 `source_kind` → expected `source_ref` form

| `source_kind` | `source_ref` examples | Truth source |
| --- | --- | --- |
| `user` | `upload:hero.png`, `figma:file/ABC123?node=4:12`, `local-folder:assets/logo.svg` | the user-supplied original |
| `research` | `https://example.com/report.pdf#p3`, `search:<query-id>` | a cited, retrievable external source |
| `generated` | `image_generate:<job-id>` (**EXISTS** tool, `_GROUNDING.md §4`) | the generation job + prompt/model in `provenance` |
| `external` | `https://cdn.example.com/img.png`, `github:owner/repo@sha:path` | a public, accessible URL/repo |

### 2.4 JSON example

```json
{
  "version": 1,
  "artifact_version": "v_2026_06_30_0008",
  "generated_at": "2026-06-30T12:00:00Z",
  "entries": [
    {
      "id": "res_hero_image",
      "source_kind": "generated",
      "source_ref": "image_generate:job_7f3a",
      "copied_path": "assets/hero_image.png",
      "media_type": "image/png",
      "license": "generated-no-restriction",
      "provenance": { "model": "comfyui-sdxl", "prompt": "abstract product hero, teal" },
      "status": "present",
      "slot": "slot_hero_image",
      "checksum": "sha256:9f2b…"
    },
    {
      "id": "res_company_logo",
      "source_kind": "user",
      "source_ref": "upload:acme-logo.svg",
      "copied_path": "assets/acme-logo.svg",
      "media_type": "image/svg+xml",
      "license": null,
      "provenance": { "attribution": "user-owned" },
      "status": "present",
      "slot": "slot_logo",
      "checksum": "sha256:1c44…"
    },
    {
      "id": "res_team_photo",
      "source_kind": "user",
      "source_ref": "requires_user_input:team_photo",
      "copied_path": null,
      "media_type": "image/*",
      "license": null,
      "provenance": {},
      "status": "requires_user_input",
      "slot": "slot_team_photo"
    }
  ]
}
```

---

## 3. The rules (each: rule · rationale · enforcement point · failure mode · test)

### R1 — Copied assets only (no live external refs in the deliverable)
- **Rule.** Every referenced asset MUST be **copied into the project** and referenced by
  its `copied_path`. The rendered/exported artifact MUST NOT load assets from a remote URL
  or a host path at view time.
- **Rationale.** Self-contained + portable export; offline-safe; no link rot, no tracking.
- **Enforcement.** Export preflight (hooks the existing `static_standalone` /
  `cloudflare_project` / `deck_export` / `document_pdf` pipelines, `_GROUNDING.md §8`);
  CD-TOOLS governed routing copies on import.
- **Failure mode.** A `<img src="https://…">` survives to export; manifest entry
  `external` with `copied_path:null`.
- **Test.** Validation checklist §6 "every referenced asset present" + a fixture asserting
  no remote `src`/`href` to media in exported HTML.

### R2 — No absolute host paths
- **Rule.** No `source_ref`, `copied_path`, or any `data-disco-file` may be an absolute
  host path (`/home/…`, `/Users/…`, `C:\…`) or contain `..`.
- **Rationale.** Absolute paths leak the host filesystem and never resolve on another
  machine; `..` escapes the workspace.
- **Enforcement.** Manifest write + export preflight; the `data-disco-file` regex in
  `DIRECT_MANIPULATION_SPEC.md §3.3`.
- **Failure mode.** `copied_path:"/home/dylan/projects/other/logo.png"`.
- **Test.** Oracle `absolute_host_path_fail.json` (FAIL verdict).

### R3 — No direct private references
- **Rule.** No reference to private/internal resources: localhost services, `file://`,
  internal IPs/hostnames, auth-gated URLs, secrets, or paths inside another user's space.
- **Rationale.** Privacy + security; the artifact must not depend on the host's private
  network/credentials.
- **Enforcement.** Manifest write validation (deny-list of schemes/hosts) + export
  preflight; ties to the security-analyzer contract (`security-analyzer-contract.md`).
- **Failure mode.** `source_ref:"http://localhost:8000/secret.png"` or an asset embedding
  a bearer token in its URL.
- **Test.** A fixture asserting private-scheme/host refs are rejected at manifest write.

### R4 — Copy only targeted resources (no bulk slurp)
- **Rule.** Import only the specific assets the artifact needs. Copying an entire folder /
  repo / Figma file wholesale is forbidden without passing the **bulk import gate** (R5).
- **Rationale.** Avoids dragging in unrelated, unlicensed, or private files; keeps the
  manifest meaningful.
- **Enforcement.** Import tool (governed routing) copies named resources only; a
  glob/folder import routes through R5.
- **Failure mode.** A "copy assets/" that pulls 400 unrelated files into the project.
- **Test.** Fixture: a folder import without the gate → rejected; targeted import → one
  entry per named asset.

### R5 — Bulk import gate
- **Rule.** Any import of more than `N` assets at once (proposed default `N=10`) OR any
  whole-folder/whole-repo import MUST pass an explicit gate: enumerate candidates, require
  a per-asset or explicit-confirm decision, and reject anything that fails R2/R3/R7.
- **Rationale.** Bulk is the most common vector for path leaks, license violations, and
  private-file inclusion.
- **Enforcement.** Import tool gate (**GAP**), before any copy occurs.
- **Failure mode.** Silent bulk copy bypassing per-asset license/provenance capture.
- **Test.** Fixture: bulk import of 25 assets without confirmation → gated/rejected; each
  admitted asset has a complete manifest entry.

### R6 — Media metadata captured
- **Rule.** Every media entry records `media_type`, dimensions/duration where applicable
  (in `provenance` or an optional `meta` sub-object), `checksum` when present, and
  `license`/attribution.
- **Rationale.** Enables correct rendering (aspect ratio, slot fit), license compliance,
  and de-duplication.
- **Enforcement.** Import tool populates metadata at copy time.
- **Failure mode.** An image with no media_type/checksum → can't validate or attribute.
- **Test.** Fixture: present media entry must have `media_type` + `checksum`.

### R7 — Media slots for missing assets (honest placeholders)
- **Rule.** When a real asset is not available, render an explicit **media slot**
  (`data-disco-media-slot="slot_…"`, `DIRECT_MANIPULATION_SPEC.md §3.9`) with a manifest
  entry `status:"requires_user_input"`/`"placeholder"` and `copied_path:null`. NEVER
  substitute a fake/stock asset presented as real.
- **Rationale.** Honesty — the user must see "asset needed here", not a fabricated
  stand-in (ties to `CONTENT_AND_DESIGN_DISCIPLINE_SPEC.md` "no fake logos/photos").
- **Enforcement.** App render emits the placeholder; export preflight allows
  `requires_user_input` slots but flags them in the export report.
- **Failure mode.** A generic stock photo rendered as if it were the user's product.
- **Test.** Oracle `media_slot_placeholder_pass.json` (PASS — honest empty slot).

### R8 — Source-truth fidelity (GitHub / Figma / local-folder)
- **Rule.** For `external`/`user` assets sourced from GitHub, Figma, or a local folder,
  `source_ref` MUST pin the exact source (repo@sha:path, figma node id, folder-relative
  path) so the original is reproducible/auditable.
- **Rationale.** Provenance must be verifiable; "from GitHub" is not enough.
- **Enforcement.** Import tool requires a pinned ref for these kinds.
- **Failure mode.** `source_ref:"github"` with no repo/sha/path.
- **Test.** Fixture: GitHub/Figma entry missing the pin → rejected.

### R9 — Inaccessible-resource failure behavior
- **Rule.** If a referenced resource cannot be fetched/copied (404, auth failure, network
  error), the import FAILS loudly: record `status:"missing"`, `copied_path:null`, and do
  NOT (a) fabricate a substitute or (b) leave a dangling live URL. The host surfaces the
  failure to the user.
- **Rationale.** No silent fallbacks to fake assets; no broken exports.
- **Enforcement.** Import tool error handling; export preflight rejects `status:"missing"`
  entries that are still referenced (R1/R10).
- **Failure mode.** A failed fetch silently replaced by a placeholder logo presented as
  the real one, or a broken `<img>` shipped.
- **Test.** Fixture: unreachable URL → entry `status:"missing"`; export preflight FAILs if
  that resource is referenced.

### R10 — No fake logos / photos / testimonials
- **Rule.** No fabricated brand logos, no stock/AI images presented as the user's real
  assets, no invented testimonials/customer names/quotes. Such content must be a labeled
  placeholder (`generated_placeholder` content tag — see
  `CONTENT_AND_DESIGN_DISCIPLINE_SPEC.md`) or `requires_user_input`.
- **Rationale.** Core honesty rule; fabricated trust signals are the worst slop.
- **Enforcement.** Content/design lint (`CONTENT_AND_DESIGN_DISCIPLINE_SPEC.md` fake-stat
  / fake-testimonial rules) + manifest provenance (no `external` logo without a verifiable
  `source_ref`).
- **Failure mode.** A "TechCrunch" logo or a "— Jane D., CEO" testimonial with no source.
- **Test.** Shared with the design-slop lint oracles + a manifest check that any
  logo-typed asset has a verifiable provenance or is a flagged placeholder.

---

## 4. Cross-project path rules (explicit)

| Rule | Allowed | Forbidden |
| --- | --- | --- |
| `copied_path` scope | inside this conversation workspace, project-relative | any path outside, absolute, or `..` |
| Referencing another project | never | `../other-project/assets/x.png`, `github:` to a *private* repo not granted |
| Symlinks escaping workspace | resolved + rejected if target is outside | a symlink pointing at `/home/...` |
| Shared asset reuse | copy a fresh per-project copy | live-link another project's file |

A cross-project asset reference is a hard failure → oracle
`cross_project_asset_reference_fail.json`.

---

## 5. Media metadata + slots (binding to the DOM grammar)

- A media slot in the DOM (`data-disco-media-slot="slot_hero_image"`) binds to the
  manifest entry whose `slot` field equals that id (and conventionally `id:"res_hero_image"`).
- **Empty slot** (`status:"requires_user_input"`/`"placeholder"`, `copied_path:null`)
  renders the honest placeholder (R7) — oracle `media_slot_placeholder_pass.json`.
- **Filled slot** (`status:"present"`) renders `<img src="{copied_path}">` (project-
  relative, R1/R2) with `alt`/dimensions from metadata (R6).
- Filling/emptying a slot MUST keep the slot id stable (it is the join key — see
  `DIRECT_MANIPULATION_SPEC.md §3.9 preservation`).

---

## 6. Pre-export validation checklist (host runs before export)

Run by the export preflight stage (the existing `(preflight,) bundle, validate, deliver`
pipelines, `_GROUNDING.md §8`). ALL must pass or export is blocked.

- [ ] **Manifest present + parseable** — `.disco/resource_manifest.json` exists and is
  valid JSON of schema `version` ≥ 1. Missing/invalid → oracle
  `missing_resource_manifest_fail.json` (FAIL).
- [ ] **Manifest complete** — every asset *referenced* in the artifact (every
  `data-disco-media-slot`, every `<img>/<video>/<source>/font`) has a manifest entry; no
  referenced asset is absent from the manifest.
- [ ] **Every referenced asset present** — each referenced entry has `status:"present"`
  and its `copied_path` exists on disk with a matching `checksum`. (`requires_user_input`
  slots are allowed but reported, R7.)
- [ ] **No absolute paths** — no entry/`data-disco-file` is absolute or contains `..`
  (R2) → else `absolute_host_path_fail.json`.
- [ ] **No cross-project refs** — no entry resolves outside this workspace (R4/§4) → else
  `cross_project_asset_reference_fail.json`.
- [ ] **No private refs** — no localhost/file://internal-IP/auth-gated/secret refs (R3).
- [ ] **No live remote media** — no remote `src`/`href` to media survives to the export
  (R1).
- [ ] **No dangling references** — no element references a `status:"missing"` resource (R9).
- [ ] **License/provenance** — every `external`/`research`/`generated` entry has a
  `license` (or generated-no-restriction) and a reproducible `provenance`/`source_ref`
  (R6/R8).
- [ ] **No fabricated assets** — no logo/testimonial-typed asset lacks verifiable
  provenance (R10; shared with design lint).
- [ ] **`artifact_version` matches** — manifest `artifact_version` equals the rendered
  `data-disco-version` (stale-manifest guard; CD-TOOLS fresh-read spirit).

---

## 7. Failure modes

- [ ] **Missing/invalid manifest** at export (`missing_resource_manifest_fail.json`).
- [ ] **Absolute host path** in `copied_path`/`source_ref`/`data-disco-file`
  (`absolute_host_path_fail.json`).
- [ ] **Cross-project asset reference** (`cross_project_asset_reference_fail.json`).
- [ ] **Private/internal reference** (localhost, file://, internal IP, secret).
- [ ] **Live remote media** surviving to export (uncopied asset).
- [ ] **Bulk slurp** bypassing the import gate.
- [ ] **Dangling reference** to a `missing` resource.
- [ ] **Fabricated logo/photo/testimonial** presented as real (no provenance).
- [ ] **Incomplete metadata** (no media_type/checksum/license).
- [ ] **Unpinned source** for GitHub/Figma/local-folder.
- [ ] **Stale manifest** (`artifact_version` ≠ rendered version).
- [ ] **Silent fake-fallback** on an inaccessible resource instead of `status:"missing"`.

---

## 8. Tests required

- [ ] **Schema validation** — `ResourceManifest`/`ResourceEntry` accept the §2.4 example
  and reject: missing required fields, bad `source_kind`/`status` enums, `copied_path`
  non-null when `status≠present`, malformed `id`.
- [ ] **Path safety** — `absolute_host_path_fail.json` and
  `cross_project_asset_reference_fail.json` both yield FAIL; project-relative passes.
- [ ] **Private-ref rejection** — localhost/file://internal-IP/secret refs rejected at
  manifest write.
- [ ] **Bulk gate** — >N or folder/repo import without confirmation is gated; targeted
  import yields one complete entry per asset.
- [ ] **Inaccessible resource** — unreachable ref → `status:"missing"`, no fabricated
  substitute, export preflight FAILs if still referenced.
- [ ] **Media slot** — `media_slot_placeholder_pass.json` (honest empty slot PASS);
  filled-slot render uses `copied_path` and stable slot id.
- [ ] **No-fake-asset** — fabricated logo/testimonial without provenance → FAIL (shared
  with `CONTENT_AND_DESIGN_DISCIPLINE_SPEC.md` lint oracles).
- [ ] **Missing manifest** — `missing_resource_manifest_fail.json` (FAIL) at export.
- [ ] **Pre-export checklist** — a golden artifact passes all §6 items; each negative
  fixture trips exactly its intended item.
- [ ] **Source-truth pin** — GitHub/Figma/local entries require a pinned `source_ref`.
- [ ] **End-to-end (live, host-driven)** — a real build that imports one user asset + one
  generated asset, leaves one slot `requires_user_input`, and exports cleanly via an
  existing pipeline (live run proves it works; fixtures prove no regression).
