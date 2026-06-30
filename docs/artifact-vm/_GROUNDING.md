# Artifact-VM Support Pack — Grounding Facts (READ FIRST)

> This file is the **single source of truth** for every spec/fixture in this pack.
> It records the *real* Disco names that already exist in the codebase as of
> 2026-06-30, branch `disclaude/experimental-20260628T025508Z` (HEAD `7c698a3c`).
> Every spec MUST map Claude Design concepts onto these real names — do NOT invent a
> parallel vocabulary. Where a name does **not** yet exist, it is marked **(GAP)** and
> the spec proposes it as new work.

## 0. Where things live (read-only — this pack never edits these)

| Concern | Real path |
| --- | --- |
| Contract value objects | `packages/core/src/disco/core/contract/models.py` |
| Contract registry (per-kind) | `packages/core/src/disco/core/contract/registry.py` |
| TweakSpec definitions | `packages/core/src/disco/core/tweaks.py` |
| Tweak IO tools | `packages/tools/src/disco/tools/builtin/tweaks_io.py` |
| AppKit semantic tools | `packages/tools/src/disco/tools/builtin/appkit.py` |
| File tools (`exact_replace`, `safe_write_file`, …) | `packages/tools/src/disco/tools/builtin/files.py` |
| Starter scaffolder | `packages/tools/src/disco/tools/builtin/scaffold_starter.py` |
| Starter kit data | `packages/core/src/disco/core/kits/starter.py` |
| Brand registry | `packages/core/src/disco/core/kits/brand.py` |
| Prompt pack loader | `packages/core/src/disco/core/workflows/prompt_pack.py` |
| Prompt pack assembler | `packages/core/src/disco/core/workflows/assembly.py` |
| Bundled packs | `packages/core/src/disco/core/workflows/prompt_packs/*.md` |
| Preview/verify tools | `packages/tools/src/disco/tools/builtin/{preview,verify_app,live_view}.py` |
| Deck pipeline | `packages/tools/src/disco/tools/builtin/{slides,_deck_patch,_pptx_render}.py` |

## 1. ContractKind (real, on-the-wire ids) — `models.py`

```
APPKIT_LEADGEN        = "appkit.leadgen"
STATIC_SITE           = "static.site"
INTERACTIVE_PROTOTYPE = "interactive.prototype"
DECK                  = "deck"
DOCUMENT              = "document"
WORKFLOW_OUTPUT       = "workflow.output"
CUSTOM                = "custom"
```

`DeliveryMode` is derived from kind (never stored independently):
- **app** (open in live preview): `appkit.leadgen`, `static.site`, `interactive.prototype`
- **files** (download/inspect): `deck`, `document`, `workflow.output`, `custom`

## 2. Contract value objects (frozen Pydantic v2, `extra="forbid"`)

- `ArtifactContract{ kind, required_files: tuple[str,...], starter_kit: str|None }`
  - `.delivery_mode` property derived from kind.
- `ToolPack{ name, tools: tuple[str,...], description }`
- `EditContract{ edit_tools, repair_tools, rewrite_allowed: bool=False }`
- `VerificationContract{ finalizer: str, level: VerificationLevel }`
  - finalizer MUST match regex `^ready_for_[a-z0-9_]+_verification$`.
- `VerificationLevel`: `load_only` | `standard` | `strict`
- `ExportContract{ name, pipeline: tuple[str,...] }`
- `BuildContract{ kind, artifact, bootstrap: ToolPack, edit: EditContract, verify, export: ExportContract|None, prompt_pack: str|None, ui_card: str|None, skills: tuple[str,...] }`
  - invariant: `BuildContract.kind == artifact.kind`.
  - `BuildContract.minimal(kind, finalizer="ready_for_artifact_verification")`.
- `BuildContractRegistry`: `get(kind)`, `get_for_brief(brief, strict_kind=True)`, `default()`.

## 3. Per-kind contracts as REGISTERED today — `registry.py`

| kind | required_files | starter_kit | bootstrap tools | edit_tools | repair/rewrite | finalizer (level) | export | prompt_pack | ui_card | skills |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| static.site | `index.html` | `app_shell` | scaffold_starter, file_write, preview_start | file_edit, file_replace_lines | repair: file_write | ready_for_static_site_verification (standard) | static_standalone | build_static_site | SiteCard | — |
| appkit.leadgen | `.disco/appspec.json`, `index.html` | `lead_form` | app_create | app_update_content, app_add_section, app_remove_section, app_reorder_section, app_set_design, app_set_tweak, app_snapshot_version | repair: file_write, file_edit | ready_for_app_verification (**strict**) | cloudflare_project | build_appkit_leadgen | AppCard | appkit.leadgen, cloudflare_export, design_recipe |
| deck | `deck.authored.json` | — | slides_generate | deck_patch | — | ready_for_deck_verification (standard) | deck_export | build_deck | DeckCard | — |
| document | `report.md` | — | file_write | file_edit, file_replace_lines | — | ready_for_document_verification (standard) | document_pdf | build_document | DocumentCard | — |
| interactive.prototype | `index.html` | `app_shell` | scaffold_starter, file_write, preview_start | file_edit, file_replace_lines | — | ready_for_prototype_verification (standard) | — **(GAP: no export)** | build_interactive_prototype | PrototypeCard | — |
| workflow.output | — | — | — **(GAP: empty bootstrap)** | — **(GAP)** | — | ready_for_workflow_output_verification (standard) | — **(GAP)** | — **(GAP: no pack)** | WorkflowCard | — |
| custom | — | — | file_write, file_edit, shell | file_edit, file_replace_lines | repair: file_write; **rewrite_allowed=True** | ready_for_artifact_verification (standard) | — | — **(GAP: no pack)** | ArtifactCard | — |

## 4. Real tool names registered today (grep `name="…"`)

**File / artifact source ops:** `file_write`, `file_read`, `file_list`, `file_append`,
`file_edit`, `file_insert_lines`, `file_replace_lines`, `file_str_replace`,
`exact_replace`, `safe_write_file`.
**AppKit semantic ops:** `app_create`, `app_update_content`, `app_add_section`,
`app_remove_section`, `app_reorder_section`, `app_set_design`, `app_set_tweak`,
`app_snapshot_version`.
**Deck/slides:** `slides_generate`, `deck_patch`. **Sheets:** `sheet_generate`.
**Scaffold:** `scaffold_starter`.
**Preview:** `preview_start`, `preview_status`, `preview_logs`, `preview_stop`,
`server_status`, `viewport`. **Verify:** `verify_web_app`, `verify_app` (alias seen).
**Run:** `run_project_script`, `code_exec`, `shell`, `shell_exec`, `shell_view`,
`shell_wait`, `shell_write_to_process`, `shell_kill_process`.
**Retrieval:** `search`, `extract`. **Media:** `image_generate`, `audio_overview`.
**Plan/loop:** `plan_step`, `submit_plan`, `update_plan_progress`, `think`,
`context_memory`, `delegate_explore`.

> NOTE: there is **no** `ready_for_*_verification` registered as an ordinary builtin tool
> name in `files.py`; finalizers are host-routed via the `VerificationContract.finalizer`
> string (regex-pinned). Treat finalizers as **host finalizers**, not model-free tools.

## 5. Prompt packs that exist today — `workflows/prompt_packs/`

`build_static_site.md`, `build_appkit_leadgen.md`, `build_deck.md`,
`build_document.md`, `build_interactive_prototype.md`.
**(GAP)** no `build_workflow_output.md`, no `build_custom.md`.

**Required sections every pack MUST define** (`prompt_pack.REQUIRED_SECTIONS`, pinned):
```
role, artifact_contract, workflow_steps, allowed_tools, forbidden_tools,
targeted_edit_law, preview_rule, verify_rule, export_rule, context_policy, done_criteria
```
Packs are markdown; `## Header` → slugged section key; fence-aware; duplicate section = error.

## 6. Starter kits that exist today — `kits/starter.py`

Only **`app_shell`** and **`lead_form`** are real kit ids (plus field tokens like
`hero`, `headline`, `lead`, `cta_text`, `viewport`). Everything else requested by the
catalog (`admin_table`, `browser_window`, `mobile_frame`, `deck_stage`, `document_frame`,
`image_slot`, `metrics_overlay`, `tweak_panel`, `workflow_setup_card`, `form_receipt`,
`daily_brief_layout`, `animation_stage`) is **(GAP)** — propose as new kit data.

## 7. TweakSpec (real) — `tweaks.py`

`TweakEditor`: `text | color | int | float | boolean | enum | palette`.
`TweakField{ key, label, editor, affects: tuple[str,...], controls_behavior, default,
options, colors, min, max, step }`. Grounding law: a tweak must control behavior OR ≥1
field; TEXT/COLOR must control behavior or ≥2 fields. Colors normalize to `#rrggbb`.
Persisted at `.disco/tweaks.json` (AppKit). Values live in `AppSpec.tweaks`.

## 8. Export pipelines that exist today

All shaped `(preflight,) bundle, validate, deliver`:
- `static_standalone` (static.site)
- `cloudflare_project` (appkit.leadgen)
- `deck_export` (deck — `bundle, validate, deliver`, no preflight)
- `document_pdf` (document)
P10b proved live export **bytes** flow from a real `DeliverableEvent`.

## 9. CD-TOOLS work already landed (do not re-spec as new; reference as DONE)

CD-TOOLS 1..10 COMPLETE + live-proven (8/8 MiniMax-M3 edit runs non-thrashing):
- **fresh-read guard** before exact-text edits,
- **atomic exact replacement** (`exact_replace`),
- **safe writes** (`safe_write_file` — refuses to clobber artifact entrypoints),
- **governed artifact routing** (artifact entrypoints go through edit tools, not raw write),
- **verifier-only read scope**, and
- **tool prompt discipline** (no elision markers like `// ... rest unchanged` in editable source).

This pack's tool contracts must say "fulfilled by CD-TOOLS" where that is the case, and
only propose NEW surface where a gap remains.

## 10. Naming convention this pack adopts

- Claude Design `dc_*` artifact tools → Disco `artifact_*` **conceptual family** that is
  *realized today* by the kind-specific tools above (`file_*` for site/doc/prototype,
  `app_*` for appkit, `slides_generate`/`deck_patch` for deck). The `artifact_*` names are
  the **proposed unifying façade**; always note the concrete tool that backs it.
- `show_html` → `show_artifact_to_agent` (**GAP** — agent-private preview; today only
  `preview_start` + `verify_web_app` screenshot exist).
- `show_to_user` → `show_artifact_to_user` (**GAP** — explicit user-visible preview state).
- `ready_for_verification` → the real `ready_for_*_verification` finalizer family.
- `present_fs_item_for_download` → `present_artifact_for_download` (**GAP**).

## 11. Hard rules for every author in this pack

1. Map onto §1–§8 real names. Mark anything new **(GAP)**.
2. Never describe runtime behavior as implemented. This is spec/fixture only.
3. Every tool/spec section ends with **Failure modes** + **Tests required**.
4. Prefer exhaustive checklists/tables over prose.
5. Fixtures must be valid JSON and include a `_meta` block (purpose/expected verdict).
6. Assume the future reader has NO chat context — be self-contained.
