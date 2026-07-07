# Disco Artifact VM — Compiled Pack

> Single-file compilation of every markdown doc in the Artifact-VM support pack.
> Generated 2026-06-30 from branch `artifact-vm-spec`. Fixtures (JSON/HTML) are NOT inlined here — see `fixtures/`.
> SPEC/FIXTURE ONLY — no runtime code. Source of truth for names: the _GROUNDING section.

---

## Table of contents

1. **ARTIFACT_VM_SESSION** — `ARTIFACT_VM_SESSION.md`
2. **README** — `docs/artifact-vm/README.md`
3. **_GROUNDING** — `docs/artifact-vm/_GROUNDING.md`
4. **DISCO_ARTIFACT_VM_SPEC** — `docs/artifact-vm/DISCO_ARTIFACT_VM_SPEC.md`
5. **ARTIFACT_TOOL_CONTRACTS** — `docs/artifact-vm/ARTIFACT_TOOL_CONTRACTS.md`
6. **TOOL_PARITY_MATRIX** — `docs/artifact-vm/TOOL_PARITY_MATRIX.md`
7. **PROMPT_PACK_REQUIREMENTS** — `docs/artifact-vm/PROMPT_PACK_REQUIREMENTS.md`
8. **STARTER_KIT_CATALOG** — `docs/artifact-vm/STARTER_KIT_CATALOG.md`
9. **EXPORT_PIPELINE_SPEC** — `docs/artifact-vm/EXPORT_PIPELINE_SPEC.md`
10. **CONTEXT_AND_ITERATION_SPEC** — `docs/artifact-vm/CONTEXT_AND_ITERATION_SPEC.md`
11. **DIRECT_MANIPULATION_SPEC** — `docs/artifact-vm/DIRECT_MANIPULATION_SPEC.md`
12. **RESOURCE_AND_PROVENANCE_SPEC** — `docs/artifact-vm/RESOURCE_AND_PROVENANCE_SPEC.md`
13. **CONTENT_AND_DESIGN_DISCIPLINE_SPEC** — `docs/artifact-vm/CONTENT_AND_DESIGN_DISCIPLINE_SPEC.md`
14. **MERGE_PLAN** — `docs/artifact-vm/MERGE_PLAN.md`



---

<!-- ============================================================ -->
# [1] ARTIFACT_VM_SESSION.md

<!-- ============================================================ -->

# Artifact VM / Claude Design Parity Support Pack — Session Record

**Session date:** 2026-06-30
**Worktree:** `/home/dylan/projects/disclaude-artifact-vm-spec`
**Branch:** `artifact-vm-spec` (forked from `disclaude/experimental-20260628T025508Z` @ `7c698a3c`)
**Type:** Support / spec / fixture work ONLY. No runtime code changed. No implementation claimed complete.

---

## 1. Session scope

Produce an independent, mergeable **Artifact VM parity pack** that extracts everything
useful from Claude Design and maps it onto Disco-native contracts, tools, prompt packs,
starter kits, export pipelines, oracle fixtures, and a merge plan.

The thesis being supported:
> chat = control plane · project filesystem = durable memory · artifact contract =
> working context · host tools own creation/edit/show/verify/export · verifier/oracles
> own truth · model operates inside constrained workflow/tool contracts.

**In scope:** specs (`docs/artifact-vm/`), draft schemas (as docs), fixtures
(`fixtures/`), starter-kit reference HTML, oracle JSON, prompt-pack drafts, export/handoff
templates, migration/merge plan.

**Out of scope (explicitly NOT done this session):** any runtime behavior, any production
edit, any change to active harness/lifecycle/provider files, any live model soak.

## 2. Forbidden files (NOT touched this session)

These file families belong to the main hardening campaign and were **only read**, never
edited:

- `runtime.py`
- `lifecycle.py`
- inference gateway / `provider relay` / MiniMax relay
- preview manager (`preview.py` runtime)
- PiKernel sidecar lifecycle
- `ToolExecutor` dispatch
- `BuildPhaseTracker`
- REL / P1B product harness implementation
- provider ledger implementation
- product evidence classifier implementation

Additionally, **nothing** under `/home/dylan/projects/disclaude` (the main worktree) was
edited. All output lives only in this separate worktree.

> Read-only grounding was performed on the contract/registry/tweaks/prompt-pack/starter
> source so that the pack maps onto **real** names. See `docs/artifact-vm/_GROUNDING.md`.

## 3. Deliverables checklist

| # | Deliverable | Path | Status |
| --- | --- | --- | --- |
| — | Grounding facts (real Disco names) | `docs/artifact-vm/_GROUNDING.md` | ✅ |
| 1 | Session record | `ARTIFACT_VM_SESSION.md` | ✅ |
| 2 | Artifact VM spec | `docs/artifact-vm/DISCO_ARTIFACT_VM_SPEC.md` | ✅ |
| 3 | Tool parity matrix | `docs/artifact-vm/TOOL_PARITY_MATRIX.md` | ✅ |
| 4 | Artifact tool contracts | `docs/artifact-vm/ARTIFACT_TOOL_CONTRACTS.md` | ✅ |
| 5 | Prompt pack requirements | `docs/artifact-vm/PROMPT_PACK_REQUIREMENTS.md` | ✅ |
| 6 | Starter kit catalog | `docs/artifact-vm/STARTER_KIT_CATALOG.md` | ✅ |
| 7 | Export pipeline spec | `docs/artifact-vm/EXPORT_PIPELINE_SPEC.md` | ✅ |
| 8 | Context & iteration spec | `docs/artifact-vm/CONTEXT_AND_ITERATION_SPEC.md` | ✅ |
| 9 | Direct manipulation spec | `docs/artifact-vm/DIRECT_MANIPULATION_SPEC.md` | ✅ |
| 10 | Resource & provenance spec | `docs/artifact-vm/RESOURCE_AND_PROVENANCE_SPEC.md` | ✅ |
| 11 | Content & design discipline spec | `docs/artifact-vm/CONTENT_AND_DESIGN_DISCIPLINE_SPEC.md` | ✅ |
| 12 | Artifact fixtures (7) | `fixtures/artifact-vm/*.artifact.json` | ✅ |
| 13 | Oracle fixtures (14) | `fixtures/oracles/*.json` | ✅ |
| 14 | Starter-kit reference HTML (10) | `fixtures/starter-kits/**/*.html` | ✅ |
| 15 | Merge plan | `docs/artifact-vm/MERGE_PLAN.md` | ✅ |
| 16 | Pack index | `docs/artifact-vm/README.md` | ✅ |

## 4. Assumptions

1. **Disco already ships the contract skeleton.** `BuildContract`/`ArtifactContract`/
   `ContractKind`/`BuildContractRegistry`/`TweakSpec` exist and are frozen Pydantic v2
   value objects. The pack maps onto them rather than reinventing.
2. **CD-TOOLS 1..10 are DONE and live-proven.** `exact_replace`, `safe_write_file`,
   fresh-read guards, governed artifact routing, verifier-only read scope, and elision
   discipline already exist. The pack references them as fulfilled, not as new work.
3. **`artifact_*` is a proposed *unifying façade*** over today's kind-specific tools
   (`file_*`, `app_*`, `slides_generate`/`deck_patch`). The façade may be implemented as a
   thin router or may stay conceptual; the pack documents both the façade and the concrete
   backing tool for every entry.
4. **Finalizers are host-routed strings**, not model-callable builtins; they match
   `^ready_for_[a-z0-9_]+_verification$`.
5. **MiniMax-M3 direct API is the final soak model; OpenRouter is forbidden for final
   soak.** No soak was run this session.
6. The future implementer reads `_GROUNDING.md` first; every spec is otherwise
   self-contained and assumes no chat history.

## 5. How to merge later

Recommended order (detail in `docs/artifact-vm/MERGE_PLAN.md`):
1. **Specs/docs first** — land `docs/artifact-vm/*` as reference; zero runtime risk.
2. **Fixtures second** — wire `fixtures/oracles/*` and `fixtures/artifact-vm/*` into the
   evidence/oracle test suites as golden inputs (no behavior change, pure test data).
3. **Prompt-pack drafts third** — fill the `build_workflow_output` / `build_custom` gaps
   and tighten existing packs against `PROMPT_PACK_REQUIREMENTS.md`. Low risk; packs are
   data loaded at runtime.
4. **Starter-kit data fourth** — add the 12 missing kits to `kits/starter.py` +
   `scaffold_starter`, validated by the starter-kit fixtures.
5. **Runtime tool layer LAST** — only after reliability hardening completes, implement the
   `artifact_*` façade / `show_artifact_to_*` / `present_artifact_for_download` gaps,
   gated by the oracle fixtures as acceptance tests.

Merge must be reviewed by Codex/GPT (adversarial) before any runtime step — see
`MERGE_PLAN.md §"what not to merge blindly"`.

## 6. What was intentionally NOT implemented

- No `artifact_*` tool implementations, routers, or dispatch wiring.
- No `show_artifact_to_agent` / `show_artifact_to_user` runtime.
- No `present_artifact_for_download` / handoff-package runtime.
- No new starter-kit code in `kits/starter.py` (only fixture HTML + catalog spec).
- No new/edited prompt packs in `workflows/prompt_packs/` (drafts live in the spec doc).
- No changes to finalizers, export pipelines, preview manager, or the ToolExecutor.
- No live model run / soak / screenshot evidence (this is not a feature-completion).
- No edits to oracle/evidence-classifier production code (only fixture JSON authored).

## 7. Provenance of "real names"

All real names in `_GROUNDING.md` were read directly from the main worktree on
2026-06-30 (`models.py`, `registry.py`, `tweaks.py`, `appkit.py`, `files.py`,
`scaffold_starter.py`, `starter.py`, `prompt_pack.py`, the bundled packs). Names marked
**(GAP)** were confirmed absent at read time and are proposed as new work.


---

<!-- ============================================================ -->
# [2] docs/artifact-vm/README.md

<!-- ============================================================ -->

# Artifact-VM Parity Pack — Index

> ## STATUS BANNER: SPEC / FIXTURE ONLY — no runtime code changed.
> Nothing in this pack edits the main Disco worktree. It is reference documentation,
> golden fixtures, and starter-kit reference HTML. Every real Disco name used here is
> grounded in [`_GROUNDING.md`](./_GROUNDING.md), read directly from the codebase on
> 2026-06-30 (branch `disclaude/experimental-20260628T025508Z`, HEAD `7c698a3c`).

## Overview

This pack extracts everything useful from **Claude Design** and maps it onto **Disco-native**
contracts, tools, prompt packs, starter kits, export pipelines, and oracle fixtures. It is an
independent, mergeable support pack: it documents what Disco already has (mapping Claude
Design `dc_*` tools onto real `file_*` / `app_*` / `slides_generate` / export-pipeline names),
marks what is missing **(GAP)**, references CD-TOOLS 1..10 as **DONE + live-proven**, and ships
golden fixtures plus a risk-sequenced merge plan so the work can land safely after the main
reliability campaign.

## Thesis

> **chat = control plane · project filesystem = durable memory · artifact contract = working
> context · host tools own creation/edit/show/verify/export · verifier/oracles own truth ·
> model operates inside constrained workflow/tool contracts.**

Disco already realizes most of this through `BuildContract` / `ArtifactContract` /
`ContractKind` / `BuildContractRegistry` / `TweakSpec` and the CD-TOOLS governed-routing +
verifier-only-scope work. This pack closes the parity gaps without reinventing that skeleton.

## Documents in `docs/artifact-vm/`

| Doc | One-line description | Intended reader |
| --- | --- | --- |
| `_GROUNDING.md` | Single source of truth: real Disco names (contracts, tools, packs, kits, exports) + GAP markers | **Everyone — read first** |
| `DISCO_ARTIFACT_VM_SPEC.md` | The Artifact-VM model: control-plane/memory/contract/host-tools/oracle thesis applied to Disco | Architect / reviewer |
| `TOOL_PARITY_MATRIX.md` | One row per Claude Design tool → Disco equivalent, status, tests, risk, priority + gap rollup | Implementer / planner |
| `ARTIFACT_TOOL_CONTRACTS.md` | Per-tool contracts for the proposed `artifact_*` façade + GAP tools (failure modes + tests) | Tool implementer |
| `PROMPT_PACK_REQUIREMENTS.md` | The pinned 11 required pack sections + drafts for the two GAP packs | Prompt-pack author (P3) |
| `STARTER_KIT_CATALOG.md` | The full kit catalog: 2 real + 12 GAP kits, each with required output (P7) | Scaffold implementer |
| `EXPORT_PIPELINE_SPEC.md` | Export pipelines (real 4 + download/print/public-URL GAPs) on the `DeliverableEvent` flow (P10b) | Export implementer |
| `CONTEXT_AND_ITERATION_SPEC.md` | Working-context / iteration model: filesystem-as-memory, artifact-as-context | Loop/runtime reviewer |
| `DIRECT_MANIPULATION_SPEC.md` | Declarative manipulation: `dc_set_props` → AppKit semantic ops + TweakSpec grounding-law (P9) | Direct-manip implementer |
| `RESOURCE_AND_PROVENANCE_SPEC.md` | Resources, image tools, download/handoff, provenance + leak avoidance | Resource implementer |
| `CONTENT_AND_DESIGN_DISCIPLINE_SPEC.md` | Content/design discipline: branding, templates, quality gates | Designer / reviewer |
| `MERGE_PLAN.md` | Risk-sequenced merge order, campaign acceleration, adversarial review gates, collision points | Merge owner |
| `README.md` | This index | Everyone |

## Fixtures

| Directory | Contains |
| --- | --- |
| `fixtures/artifact-vm/` | **7** `*.artifact.json` — one valid `ArtifactContract`/`BuildContract` example per `ContractKind`, each with a `_meta` block (purpose + expected verdict). Golden inputs for contract/registry tests. |
| `fixtures/oracles/` | **14** `*.json` — golden pass/fail cases (5 pass, 9 fail) for the Artifact-VM guards (targeted-edit law, show↔verify separation, verification gate, export fail-closed, resource/provenance, comment anchors, direct-edit reconciliation, verifier-only tool scope). Each carries a `_meta` block + expected verdict; fail cases carry a stable `failure_code`. |
| `fixtures/starter-kits/` | **10** reference `*.html` — the 2 real kits (`app_shell`, `lead_form`) plus 8 catalog kits, grouped by delivery surface. Acceptance targets for `scaffold_starter` kit additions (P7). |

### Artifact fixtures (7) — across the `ContractKind`s

1. `fixtures/artifact-vm/static_site_minimal.artifact.json` (`static.site`)
2. `fixtures/artifact-vm/static_site_with_tweaks.artifact.json` (`static.site` + tweaks)
3. `fixtures/artifact-vm/appkit_leadgen.artifact.json` (`appkit.leadgen`)
4. `fixtures/artifact-vm/deck_with_stage.artifact.json` (`deck`)
5. `fixtures/artifact-vm/document_print.artifact.json` (`document`)
6. `fixtures/artifact-vm/prototype_stateful.artifact.json` (`interactive.prototype`)
7. `fixtures/artifact-vm/workflow_output_daily_brief.artifact.json` (`workflow.output`)

### Oracle fixtures (14) — golden pass/fail cases per Artifact-VM guard

Pass cases (5):
1. `fixtures/oracles/targeted_edit_pass.json` (targeted-edit law)
2. `fixtures/oracles/ready_for_verification_pass.json` (verification gate)
3. `fixtures/oracles/media_slot_placeholder_pass.json` (honest media slot)
4. `fixtures/oracles/comment_anchor_preserved_pass.json` (anchor preservation)

Fail cases (9), each carrying a stable `failure_code`:
5. `fixtures/oracles/targeted_edit_rewrite_fail.json` (`EDIT_WHOLE_FILE_REWRITE`)
6. `fixtures/oracles/missing_show_to_user_fail.json` (`VERIFY_WITHOUT_SHOW_TO_USER`)
7. `fixtures/oracles/ready_for_verification_dirty_fail.json` (`VERIFY_DIRTY_WORKING_TREE`)
8. `fixtures/oracles/export_zero_bytes_fail.json` (`EXPORT_ZERO_BYTES`)
9. `fixtures/oracles/cross_project_asset_reference_fail.json` (`ASSET_CROSS_PROJECT_REFERENCE`)
10. `fixtures/oracles/absolute_host_path_fail.json` (`ASSET_ABSOLUTE_HOST_PATH`)
11. `fixtures/oracles/missing_resource_manifest_fail.json` (`RESOURCE_MANIFEST_MISSING_ENTRY`)
12. `fixtures/oracles/comment_anchor_duplicated_fail.json` (`COMMENT_ANCHOR_DUPLICATED`)
13. `fixtures/oracles/direct_edit_clobbered_fail.json` (`DIRECT_EDIT_CLOBBERED`)
14. `fixtures/oracles/verifier_only_tool_used_by_main_agent_fail.json` (`VERIFIER_ONLY_TOOL_CALLED_BY_MAIN_AGENT`)

> These encode the Artifact-VM's mandatory rules (targeted-edit law, show↔verify
> separation, ready-for-verification clean-tree gate, export fail-closed, resource/
> provenance rules, comment-anchor preservation, direct-edit reconciliation, and
> verifier-only tool scope). The real finalizer family they exercise matches
> `^ready_for_[a-z0-9_]+_verification$` (grounding §3/§4).

### Starter-kit reference HTML (10) — 2 real + 8 catalog

1. `fixtures/starter-kits/app_shell/index.html` (**real** kit, grounding §6)
2. `fixtures/starter-kits/lead_form/index.html` (**real** kit, grounding §6)
3. `fixtures/starter-kits/admin_table/index.html` (GAP)
4. `fixtures/starter-kits/tweak_panel/tweaks.html` (GAP)
5. `fixtures/starter-kits/metrics_overlay/metrics.html` (GAP)
6. `fixtures/starter-kits/image_slot/media-slot.html` (GAP)
7. `fixtures/starter-kits/browser_window/browser-window.html` (GAP)
8. `fixtures/starter-kits/mobile_frame/mobile-frame.html` (GAP)
9. `fixtures/starter-kits/deck_stage/deck.html` (GAP)
10. `fixtures/starter-kits/document_frame/document.html` (GAP)

> The 2 real kits are reference baselines (their output MUST NOT drift on merge — see
> `MERGE_PLAN.md §4`). The 8 GAP kits are proposed catalog additions; the remaining catalog
> GAP kits not shipped as reference HTML here (`workflow_setup_card`, `form_receipt`,
> `daily_brief_layout`, `animation_stage`) are specified in `STARTER_KIT_CATALOG.md`.

## Read in this order (future implementer)

1. **`_GROUNDING.md`** — learn the real names; never invent a parallel vocabulary.
2. **`README.md`** (this file) — map of the pack.
3. **`DISCO_ARTIFACT_VM_SPEC.md`** — the model and thesis.
4. **`TOOL_PARITY_MATRIX.md`** — what exists, what is GAP, priorities.
5. The per-area specs as needed: `ARTIFACT_TOOL_CONTRACTS.md`,
   `PROMPT_PACK_REQUIREMENTS.md`, `STARTER_KIT_CATALOG.md`, `EXPORT_PIPELINE_SPEC.md`,
   `CONTEXT_AND_ITERATION_SPEC.md`, `DIRECT_MANIPULATION_SPEC.md`,
   `RESOURCE_AND_PROVENANCE_SPEC.md`, `CONTENT_AND_DESIGN_DISCIPLINE_SPEC.md`.
6. **Fixtures** — `fixtures/artifact-vm/*` and `fixtures/oracles/*` as golden inputs;
   `fixtures/starter-kits/**` as scaffold acceptance targets.
7. **`MERGE_PLAN.md`** — last: how it all lands safely after the reliability campaign.

## Ground rules carried from `_GROUNDING.md §11`

1. Map onto real names; mark anything new **(GAP)**.
2. Never describe runtime behavior as implemented — this is spec/fixture only.
3. Every tool/spec section ends with **Failure modes + Tests required**.
4. Prefer exhaustive checklists/tables over prose.
5. Fixtures are valid JSON with a `_meta` block (purpose / expected verdict).
6. Assume the future reader has no chat context — every doc is self-contained.


---

<!-- ============================================================ -->
# [3] docs/artifact-vm/_GROUNDING.md

<!-- ============================================================ -->

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


---

<!-- ============================================================ -->
# [4] docs/artifact-vm/DISCO_ARTIFACT_VM_SPEC.md

<!-- ============================================================ -->

# Disco Artifact VM — Specification

> **Status:** SPEC / DESIGN ONLY. No runtime behavior described here is implemented unless it
> is explicitly tagged as already-shipped CD-TOOLS work or maps to a real name in
> [`_GROUNDING.md`](./_GROUNDING.md). Everything marked **(GAP)** is proposed new work.
>
> **Source of truth for names:** [`_GROUNDING.md`](./_GROUNDING.md). This document MUST NOT
> invent a parallel vocabulary; it maps the Claude-Design "Artifact VM" concept onto the
> real Disco contract value objects, tool names, registries, and pipelines recorded there.
>
> **Companion docs (read together):**
> - [`ARTIFACT_TOOL_CONTRACTS.md`](./ARTIFACT_TOOL_CONTRACTS.md) — exact per-tool schemas, failure codes, phase guards.
> - [`DIRECT_MANIPULATION_SPEC.md`](./DIRECT_MANIPULATION_SPEC.md) — `data-disco-*` semantic markup (sibling pack doc).
> - [`RESOURCE_AND_PROVENANCE_SPEC.md`](./RESOURCE_AND_PROVENANCE_SPEC.md) — resource manifest + provenance (sibling pack doc).

---

## 1. Purpose — what an "Artifact VM" is

An **Artifact VM** is a *host-owned artifact runtime*: a constrained execution environment in
which the model never freely "writes files and serves a preview", but instead operates on a
**typed artifact** through a fixed set of **host-owned operations** (create / edit / show /
verify / export) that are gated by a **contract** resolved from the brief.

The "VM" framing is deliberate. Like a virtual machine, it has:

| VM concept | Artifact-VM realization in Disco |
| --- | --- |
| Instruction set | The artifact tool families (§5) — the *only* legal operations on the artifact |
| Memory | The project filesystem (durable) + the artifact contract (working set) |
| Program counter / state | The artifact **lifecycle state machine** (§3) |
| Privileged instructions (ring 0) | Host-owned ops: create, show-to-user, verify, export — model cannot forge them |
| User-mode instructions (ring 3) | Model-issued edits, reads, props — all routed through host tools |
| Memory protection | Governed artifact routing (CD-TOOLS): entrypoints cannot be raw-written |
| Traps / faults | Failure codes (per-tool, §ARTIFACT_TOOL_CONTRACTS) that the host handles, not the model |

### Contrast: "generic agent writes files + serves preview" vs Artifact VM

| Aspect | Generic file-writing agent | Disco Artifact VM |
| --- | --- | --- |
| What an artifact *is* | An emergent pile of files | A typed `ArtifactContract{kind, required_files, starter_kit}` |
| Who decides legal edits | The model, ad hoc | `EditContract{edit_tools, repair_tools, rewrite_allowed}` resolved from kind |
| Entrypoint writes | `write_file('index.html', …)` any time | Routed through kind edit tools; `safe_write_file` *refuses* to clobber entrypoints (CD-TOOLS) |
| Small edits | Re-emit whole file (Mode-B thrash) | `exact_replace` / semantic `app_*` ops; no full rewrites |
| "Show the user" | Implicit (same as preview) | A distinct lifecycle state (`show_artifact_to_user` **(GAP)**) |
| "Is it correct?" | Model self-asserts | Host **finalizer** `ready_for_*_verification` + verifier/oracles own truth |
| Export | Model zips something | Host-owned `ExportContract.pipeline` emitting real `DeliverableEvent` bytes |
| Truth source | The model's narration | Verifier diagnostics (screenshot/eval) the **main agent cannot call** |

The Artifact VM is the missing *runtime* that turns Disco's contracts (already in
`contract/models.py` + `registry.py`) into a single coherent, host-policed loop.

---

## 2. Why Disco needs this (thesis tie-in)

Disco's architecture thesis:

1. **Chat is the control plane**, not the work surface. The conversation issues intents.
2. **The project filesystem is durable memory.** State survives context windows, restarts,
   and sandbox wipes.
3. **The artifact contract is the working context.** `BuildContract` (resolved from the brief
   by `BuildContractRegistry.get_for_brief`) is the small, typed working set the model reasons over.
4. **Host tools own create / edit / show / verify / export.** The model proposes; the host disposes.
5. **Verifier / oracles own truth.** `VerificationContract.finalizer` + verifier-only
   diagnostics decide "done", never the model's self-report.
6. **The model operates inside constrained workflow + tool contracts.** Prompt packs
   (`workflows/prompt_packs/*.md`) + the per-kind tool allow/forbid lists fence behavior.

The Artifact VM is the component that **enforces #4–#6 at runtime**. Without it, the contracts
exist as data but nothing makes the model honor them.

### How it kills Mode-B edit-elision thrash (CD-TOOLS)

**Mode B** = the failure where a weak/medium model, asked to make a small change, re-emits the
whole entrypoint and "elides" the unchanged middle (`// ... rest unchanged …`), which then gets
*executed verbatim*, deleting the elided body. Live data: 8/8 MiniMax-M3 runs thrashed before
CD-TOOLS; 8/8 non-thrashing after.

The Artifact VM bakes the CD-TOOLS countermeasures into the runtime contract:

| CD-TOOLS rule (DONE) | How the Artifact VM relies on it |
| --- | --- |
| **fresh-read guard** | Before any exact-text edit the host reads current source so the model edits *what exists now*, not a stale memory |
| **atomic exact replacement** (`exact_replace`) | Small edits are a single old→new swap; the unchanged body is never re-emitted, so it cannot be elided |
| **safe writes** (`safe_write_file`) | Refuses to clobber a registered artifact entrypoint — full rewrites of `index.html` are blocked at the tool |
| **governed artifact routing** | Entrypoint mutations *must* go through the kind's `edit_tools`; raw write is not in the allow-list |
| **verifier-only read scope** | Screenshot/eval diagnostics belong to the verifier; the main agent can't substitute its own "looks fine" |
| **elision discipline** | Prompt packs forbid `// ... unchanged` markers in editable source |

The Artifact VM's job is to make these rules *structural* (phase guards + tool routing) rather
than merely *advisory* (prompt text).

---

## 3. Artifact lifecycle (state machine)

The VM advances an artifact through phases. Each phase admits only certain tools; the host
enforces the transition. The canonical phase set (used by `ARTIFACT_TOOL_CONTRACTS.md` phase
guards) is: **create · edit · preview · verify · export** plus terminal **handoff**.

### 3.1 ASCII state diagram

```
                         (brief text)
                              │
                              ▼
                     ┌─────────────────┐
                     │     BRIEF       │  intent captured in chat (control plane)
                     └───────┬─────────┘
                             │ resolve BuildContractRegistry.get_for_brief()
                             ▼
                     ┌─────────────────┐
                     │    CONTRACT     │  BuildContract bound (kind, artifact, edit, verify, export)
                     └───────┬─────────┘
                             │ bootstrap ToolPack (e.g. scaffold_starter / app_create / slides_generate)
                             ▼
                     ┌─────────────────┐
        ┌───────────▶│     CREATE      │  required_files materialized; starter_kit applied
        │            └───────┬─────────┘
        │                    │ first source present
        │                    ▼
        │            ┌─────────────────┐
        │   edit ◀───┤      EDIT       │◀────── repair (EditContract.repair_tools)
        │   loop     └───────┬─────────┘
        │                    │ artifact runnable?
        │                    ▼
        │            ┌─────────────────┐
        └────────────┤    PREVIEW      │  preview_start / show_artifact_to_agent (GAP, agent-private)
       (fix found)   └───────┬─────────┘
                             │ host: show_artifact_to_user (GAP) → user sees it
                             ▼
                     ┌─────────────────┐
                     │  SHOW_TO_USER   │  SEPARATE from verification (rule §6.7)
                     └───────┬─────────┘
                             │ model calls finalizer: ready_for_<kind>_verification
                             ▼
                     ┌─────────────────┐      fail (verifier verdict)
                     │     VERIFY      │───────────────────────┐
                     │ (verifier owns  │                        │
                     │   truth)        │                        ▼
                     └───────┬─────────┘                  back to EDIT
                             │ pass (VerificationLevel met)
                             ▼
                     ┌─────────────────┐
                     │     EXPORT      │  ExportContract.pipeline → DeliverableEvent (bytes)
                     └───────┬─────────┘
                             │ present_artifact_for_download (GAP)
                             ▼
                     ┌─────────────────┐
                     │    HANDOFF      │  terminal: artifact delivered to user
                     └─────────────────┘
```

### 3.2 Per-transition table

| # | Transition | Trigger | Host action | Tools allowed in target phase | Guard conditions | Failure handling | Realized by (real Disco machinery) |
| --- | --- | --- | --- | --- | --- | --- | --- |
| T0 | — → BRIEF | User message | Capture intent in chat | (none — control plane) | — | n/a | Chat loop |
| T1 | BRIEF → CONTRACT | Build intent detected | `BuildContractRegistry.get_for_brief(brief, strict_kind=True)` resolves `BuildContract`; fall back to `.default()` / `BuildContract.minimal()` | (none yet) | brief maps to a known `ContractKind`; else `custom` | No kind match → `custom` contract (`rewrite_allowed=True`) | `contract/registry.py`, `contract/models.py` |
| T2 | CONTRACT → CREATE | Contract bound | Run `bootstrap: ToolPack` (e.g. `scaffold_starter`+`file_write`+`preview_start`, or `app_create`, or `slides_generate`) | bootstrap tools only | `starter_kit` exists (`app_shell`/`lead_form` only today) | Missing kit → **(GAP)** must error, not silently skip | `bootstrap` field; `scaffold_starter.py`; `kits/starter.py` |
| T3 | CREATE → EDIT | `required_files` materialized | Index artifact parts (§4); load `EditContract` | `EditContract.edit_tools` (+`exact_replace`, `safe_write_file`) | all `required_files` present | Missing required file → stay in CREATE | `ArtifactContract.required_files`; `files.py`; `appkit.py` |
| T4 | EDIT → EDIT (loop) | Model edit intent | Route to kind edit tool; **fresh-read guard** before exact edits | same as EDIT | edit targets non-entrypoint via raw write is rejected | Elision / stale text → `exact_replace` returns failure code; retry with fresh read | CD-TOOLS (`exact_replace`, governed routing) |
| T5 | EDIT → PREVIEW | Artifact runnable | `preview_start`; optionally `show_artifact_to_agent` **(GAP)** for agent-private inspection | preview/status/logs/stop; read-only source | for **app** delivery only (`appkit.leadgen`,`static.site`,`interactive.prototype`) | Preview crash → `preview_logs`, back to EDIT | `preview.py`, `live_view.py` |
| T6 | PREVIEW → SHOW_TO_USER | Host decides artifact presentable | `show_artifact_to_user` **(GAP)** marks user-visible state | (display only; no source mutation) | preview healthy (`preview_status` ok) | n/a (user can still request edits → EDIT) | **(GAP)** — today conflated with `preview_start` |
| T7 | SHOW_TO_USER → VERIFY | Model calls finalizer | Host routes `ready_for_<kind>_verification` to verifier; runs verifier-only diagnostics | **verifier-only**: screenshot/eval (NOT main-agent tools) | finalizer string matches `^ready_for_[a-z0-9_]+_verification$` | bad finalizer name → host rejects | `VerificationContract.finalizer`; `verify_web_app.py` |
| T8 | VERIFY → EDIT | Verifier verdict = fail | Surface verdict + diagnostics to model | back to EDIT toolset | `VerificationLevel` (`load_only`/`standard`/`strict`) not met | Loop until pass or budget exhausted | `VerificationContract.level` |
| T9 | VERIFY → EXPORT | Verifier verdict = pass | Run `ExportContract.pipeline` (`preflight,bundle,validate,deliver`) | export pipeline (host-owned) | `export` contract present (GAP for `interactive.prototype`,`workflow.output`,`custom`) | pipeline stage error → abort export, stay VERIFY-passed | `ExportContract`; `static_standalone`/`cloudflare_project`/`deck_export`/`document_pdf` |
| T10 | EXPORT → HANDOFF | Bytes produced | Emit `DeliverableEvent`; `present_artifact_for_download` **(GAP)** | (terminal; download only) | non-empty deliverable bytes | empty bytes → export failure | P10b (proven `DeliverableEvent` bytes flow) |

### 3.3 Re-entry / loops

- **EDIT ⇄ PREVIEW** is the inner authoring loop (app kinds).
- **VERIFY → EDIT** is the correction loop, bounded by the verification budget.
- A user "change X" message at any presentable phase re-enters **EDIT** (it does not require
  re-resolving the contract; the `BuildContract` is sticky for the artifact's life).
- **files**-delivery kinds (`deck`, `document`, `workflow.output`, `custom`) skip the live
  PREVIEW/SHOW_TO_USER browser loop; their "preview" is a rendered file inspection.

---

## 4. Artifact parts (the artifact contract grammar)

An artifact is the sum of typed **parts**. Each part is held by a concrete backing file or
sidecar today. The grammar below is what the VM indexes on entry to EDIT (T3).

| Part | Holds what | Backing file today | Required? | Who writes it |
| --- | --- | --- | --- | --- |
| **markup** (entrypoint) | The primary authored surface | `index.html` (site/prototype/appkit), `report.md` (document), `deck.authored.json` (deck) | **Yes** — it is the `required_files` head | `scaffold_starter`→`file_*` (site/doc/proto); `app_create`/`app_*` (appkit); `slides_generate`/`deck_patch` (deck) |
| **logic** (JS/behavior) | Client behavior, interactivity | inline `<script>` in `index.html`, or `*.js` listed in manifest | Optional | `file_edit`/`exact_replace` (site/proto); `app_set_tweak.controls_behavior` (appkit) |
| **props / tweaks metadata** | User-editable knobs + values | `.disco/tweaks.json` (AppKit); values in `AppSpec.tweaks` | Optional (appkit-centric) | `app_set_tweak`; `tweaks_io.py`; **(GAP)** `artifact_set_props` façade |
| **manifest** | `required_files`, kit id, declared parts | `.disco/appspec.json` (appkit); else implied by `ArtifactContract.required_files` | **Yes (appkit)** / implied elsewhere | `app_create`; **(GAP)** explicit manifest for non-appkit kinds |
| **runtime wrapper** | Preview/serve harness | host preview server (`preview_start`) — not a file the model owns | Host-owned | Host (`preview.py`); model never writes it |
| **semantic metadata** | `data-disco-*` direct-manipulation hooks | attributes inside `index.html` | Optional | semantic edit tools — see [`DIRECT_MANIPULATION_SPEC.md`](./DIRECT_MANIPULATION_SPEC.md) |
| **resource manifest** | Images/audio/data assets + provenance | resource sidecar — see [`RESOURCE_AND_PROVENANCE_SPEC.md`](./RESOURCE_AND_PROVENANCE_SPEC.md) | Optional | `image_generate`/`audio_overview`; provenance writer **(GAP)** |
| **verification status** | Last finalizer + verifier verdict + level met | host-side artifact state **(GAP — no on-disk file today)** | **Yes (logical)** | Host finalizer route (`ready_for_*_verification`) |
| **export status** | Last pipeline run + `DeliverableEvent` ref | host-side artifact state **(GAP)** | Optional | `ExportContract.pipeline` |

Notes:
- The **markup entrypoint** is the protected part: `safe_write_file` refuses to clobber it and
  governed routing forces edits through `edit_tools` (CD-TOOLS).
- **verification status** and **export status** are *logical* parts the VM must track; today they
  are ephemeral host state with **no durable file** — see Open Questions (§7).

---

## 5. Tool families (conceptual)

Each conceptual `artifact_*` family is the **proposed unifying façade**; the concrete backing
tool that exists today is named explicitly. Exact schemas live in
[`ARTIFACT_TOOL_CONTRACTS.md`](./ARTIFACT_TOOL_CONTRACTS.md).

| Conceptual tool | Role (1 paragraph) | Backing tool today |
| --- | --- | --- |
| **artifact_write** | Create/overwrite a *non-entrypoint* artifact source file during CREATE, or a brand-new file during EDIT. It is the unifying façade over the kind's create path; it must route through governed artifact routing so it can never silently clobber the protected entrypoint. | `file_write` (site/doc/proto), `app_create`/`app_update_content` (appkit), `slides_generate` (deck) |
| **artifact_markup_str_replace** | Apply a single atomic old→new replacement to the markup entrypoint after a fresh read. This is the primary small-edit instruction and the direct anti-Mode-B lever. | `exact_replace` (CD-TOOLS); also `file_str_replace`/`file_edit`/`file_replace_lines` |
| **artifact_logic_str_replace** | Same atomic replacement discipline, scoped to logic/JS regions of the artifact. | `exact_replace` over `<script>`/`*.js`; appkit behavior via `app_set_tweak` |
| **artifact_set_props** | Set user-facing knobs/tweaks (props metadata) without touching markup; validates against `TweakSpec` grounding law. | `app_set_tweak` (+`tweaks_io.py`); **(GAP)** as a generic façade for non-appkit kinds |
| **artifact_read_parts** | Read the indexed parts of the artifact (markup/logic/props/manifest/status) for the model to reason over — the fresh-read source for exact edits. | `file_read`/`file_list` (+ appkit `app_*` read paths); **(GAP)** as a unified parts reader |
| **exact_replace** | Atomic, fresh-read-guarded exact text replacement. The canonical CD-TOOLS edit primitive. | `exact_replace` (**DONE**, CD-TOOLS) |
| **safe_write_file** | Write a file but **refuse** to clobber a registered artifact entrypoint; the guardrail that forces entrypoint edits through edit tools. | `safe_write_file` (**DONE**, CD-TOOLS) |
| **run_project_script** | Run a project-defined script (build/lint/dev task) in the sandbox during create/edit/verify. | `run_project_script` (**DONE**) |
| **show_artifact_to_agent** | Render an **agent-private** preview (screenshot/DOM) so the model can inspect its own work without marking it user-visible. | **(GAP)** — today only `preview_start` + verifier `verify_web_app` screenshot exist |
| **show_artifact_to_user** | Promote the artifact to an explicit **user-visible** state; distinct from verification. | **(GAP)** — today conflated with `preview_start` |
| **ready_for_artifact_verification** | The finalizer the model calls to hand the artifact to the verifier; host-routed, not a free tool. | `ready_for_<kind>_verification` finalizer family (**DONE** as host route) |
| **present_artifact_for_download** | Deliver the exported bytes to the user as a downloadable artifact. | **(GAP)** — backed by `DeliverableEvent` (P10b proved bytes flow) |

> Per-kind backing detail (from grounding §3): site/document/prototype back their façade with
> `file_*`; appkit with `app_*`; deck with `slides_generate`/`deck_patch`. The façade always
> documents the concrete tool it dispatches to.

---

## 6. Mandatory rules (MUST / MUST-NOT)

Each rule lists rationale, enforcement, and the failure mode if violated.

### 6.1 — MUST NOT: generic `safe_write_file` may not write artifact entrypoints
- **Rationale:** entrypoints are the protected markup part; a raw full write is the Mode-B vector.
- **Enforcement:** `safe_write_file` refuses to clobber a registered entrypoint (CD-TOOLS, DONE);
  governed routing keeps raw write out of the kind allow-list.
- **Failure mode if violated:** whole-file re-emit → elided body executed verbatim → data loss.

### 6.2 — MUST: small edits use exact / semantic replacement
- **Rationale:** atomic old→new never re-emits the unchanged body, so it cannot be elided.
- **Enforcement:** kind `edit_tools` are `exact_replace`/`file_str_replace`/`file_replace_lines`
  (site/doc/proto) or `app_*` semantic ops (appkit); full rewrite not offered.
- **Failure mode if violated:** Mode-B thrash returns; live-proven regression.

### 6.3 — MUST: editing requires fresh source when exact text is needed
- **Rationale:** the model's remembered text drifts from disk after prior edits.
- **Enforcement:** fresh-read guard (CD-TOOLS) — host reads current source before an exact edit;
  `exact_replace` fails if the `old` text is not found verbatim.
- **Failure mode if violated:** edit silently misses or hits the wrong region → corrupted source.

### 6.4 — MUST NOT: no elision markers in editable source
- **Rationale:** `// ... rest unchanged …` is data, and the host executes it as written.
- **Enforcement:** prompt-pack `targeted_edit_law` section forbids it; edit tools never accept a
  partial body for an entrypoint (only `exact_replace` diffs).
- **Failure mode if violated:** the elided region is physically deleted.

### 6.5 — MUST: entrypoint mutations route through the kind's `edit_tools`
- **Rationale:** the contract, not the model, defines legal mutations (governed routing).
- **Enforcement:** `EditContract.edit_tools` is the allow-list; out-of-list calls on the
  entrypoint are rejected by the host.
- **Failure mode if violated:** uncontrolled writes bypass all CD-TOOLS guards.

### 6.6 — MUST: `rewrite_allowed` is honored per-contract only
- **Rationale:** only `custom` sets `rewrite_allowed=True`; all typed kinds forbid full rewrite.
- **Enforcement:** `EditContract.rewrite_allowed` gate checked before any whole-file write.
- **Failure mode if violated:** typed artifacts lose their CD-TOOLS protection.

### 6.7 — MUST: `show_to_user` and verification are SEPARATE states
- **Rationale:** showing the user ≠ asserting correctness; collapsing them lets the model claim
  "done" by merely rendering.
- **Enforcement:** distinct lifecycle states (T6 vs T7); finalizer `ready_for_*_verification`
  is the only path into VERIFY. (`show_artifact_to_user` is **GAP**.)
- **Failure mode if violated:** unverified artifacts handed off as "done".

### 6.8 — MUST NOT: verifier-only diagnostics are not main-agent tools
- **Rationale:** the verifier owns truth; if the main agent can screenshot/eval itself it will
  rationalize "looks fine".
- **Enforcement:** verifier-only read scope (CD-TOOLS) — diagnostic tools are bound to the
  verifier role, absent from the main-agent toolset.
- **Failure mode if violated:** self-graded artifacts; false "verified".

### 6.9 — MUST: exports are host-owned pipelines
- **Rationale:** deliverable integrity (real bytes) must not depend on the model zipping files.
- **Enforcement:** `ExportContract.pipeline` runs host-side (`preflight,bundle,validate,deliver`);
  output is a `DeliverableEvent` (P10b proved bytes).
- **Failure mode if violated:** corrupt/empty downloads; unvalidated bundles.

### 6.10 — MUST: phase guards reject out-of-phase tool calls
- **Rationale:** the VM's instruction set is phase-scoped (§3.2 / per-tool allowed phases).
- **Enforcement:** host phase guard checks each tool's allowed phases before dispatch.
- **Failure mode if violated:** e.g. an export during CREATE produces garbage; a finalizer during
  EDIT skips correction.

---

## 7. Open questions / decisions for the implementer

1. **Durable verification/export status.** Today these are ephemeral host state with no on-disk
   file (§4). Decision: add a `.disco/artifact_state.json` part, or keep host-memory only? The
   VM benefits from durability across sandbox wipes (Disco's resume thesis).
2. **`artifact_*` façade vs direct kind tools.** Should the façade (§5) be a real dispatch layer,
   or remain documentation that maps to `file_*`/`app_*`/`deck_*`? A real façade simplifies prompt
   packs but adds a translation seam.
3. **`show_artifact_to_agent` / `show_artifact_to_user` split (GAP).** Needs a concrete
   mechanism: agent-private screenshot channel vs user-visible preview promotion. Defines T5/T6.
4. **Missing exports (GAP).** `interactive.prototype`, `workflow.output`, `custom` have no
   `ExportContract`. Decide whether T9 is skippable (handoff = "open preview") for these.
5. **Missing prompt packs / kits (GAP).** No `build_workflow_output.md`, no `build_custom.md`;
   only `app_shell`+`lead_form` kits exist. The catalog requests many more kits (grounding §6).
6. **`workflow.output` empty bootstrap (GAP).** Its `BuildContract` has empty bootstrap/edit/
   export — the VM has no instructions to run. Define them or route to `custom`.
7. **Manifest for non-appkit kinds.** Only appkit has `.disco/appspec.json`; other kinds imply
   the manifest from `required_files`. Should the VM materialize an explicit manifest part?
8. **Provenance enforcement.** How strictly does VERIFY check the resource manifest /
   provenance (see [`RESOURCE_AND_PROVENANCE_SPEC.md`](./RESOURCE_AND_PROVENANCE_SPEC.md))?

---

## 8. Failure modes (VM-wide)

| # | Failure mode | Phase | Root cause | Detection | Host action / mitigation |
| --- | --- | --- | --- | --- | --- |
| F1 | Mode-B edit elision | EDIT | Whole-file re-emit with `// … unchanged` | `exact_replace` `old` not found; entrypoint write rejected | Reject; require fresh-read + atomic replace (CD-TOOLS) |
| F2 | Entrypoint clobbered | CREATE/EDIT | Raw write to `index.html`/`report.md`/`deck.authored.json` | `safe_write_file` entrypoint guard | Refuse write; route to `edit_tools` |
| F3 | Stale-source edit miss | EDIT | Model edits remembered (drifted) text | fresh-read guard mismatch | Fail edit; re-read; retry |
| F4 | Self-graded "verified" | VERIFY | Main agent runs diagnostics itself | verifier-only scope absent → call rejected | Block; only finalizer route reaches verifier |
| F5 | Show ≡ verify conflation | SHOW_TO_USER/VERIFY | Treating render as "done" | distinct states T6/T7 | Require finalizer to advance |
| F6 | Missing required file | CREATE→EDIT | `required_files` not materialized | T3 guard | Stay in CREATE; re-run bootstrap |
| F7 | Missing starter kit | CREATE | kit id not `app_shell`/`lead_form` (GAP) | kit lookup fail | Error (do not silently skip) |
| F8 | No export for kind | EXPORT | `interactive.prototype`/`workflow.output`/`custom` lack `ExportContract` (GAP) | export contract absent | Skip to HANDOFF as preview, or error per decision §7.4 |
| F9 | Export pipeline stage error | EXPORT | preflight/bundle/validate/deliver fault | stage exception | Abort export; remain VERIFY-passed |
| F10 | Empty deliverable bytes | EXPORT→HANDOFF | deliver produced no bytes | `DeliverableEvent` empty | Fail handoff; surface error |
| F11 | Bad finalizer name | VERIFY | finalizer ≠ `^ready_for_[a-z0-9_]+_verification$` | regex check | Reject finalizer call |
| F12 | Out-of-phase tool call | any | tool used outside allowed phases | phase guard | Reject with phase-guard failure code |
| F13 | Kind/artifact mismatch | CONTRACT | `BuildContract.kind != artifact.kind` | model invariant | Reject contract construction |
| F14 | `workflow.output` no instructions | CREATE | empty bootstrap/edit (GAP) | empty ToolPack | Route to `custom` or error per §7.6 |

---

## 9. Tests required (VM as a whole)

- [ ] **Contract resolution:** brief → correct `ContractKind` for each of the 7 kinds; unknown
      brief → `custom`; `BuildContract.kind == artifact.kind` invariant holds (F13).
- [ ] **Bootstrap per kind:** running `bootstrap` materializes all `required_files` (F6); missing
      kit errors (F7).
- [ ] **Phase guard matrix:** every tool rejected outside its allowed phases (F12) — one test per
      (tool, forbidden phase).
- [ ] **Entrypoint protection:** `safe_write_file` refuses each kind's entrypoint (F2); raw write
      not in any kind's edit allow-list (rule 6.5).
- [ ] **Anti-Mode-B:** exact edit with stale `old` text fails (F3); elision marker in entrypoint
      rejected (F1, rule 6.4); 8/8-style repeated small edits stay non-thrashing.
- [ ] **rewrite_allowed gate:** only `custom` permits whole-file rewrite (rule 6.6); all typed
      kinds reject it.
- [ ] **Show vs verify separation:** advancing to HANDOFF requires a finalizer call (F5);
      `show_artifact_to_user` does not set verification status (rule 6.7).
- [ ] **Verifier-only scope:** main-agent attempt to call a verifier diagnostic is rejected (F4).
- [ ] **Finalizer regex:** good/bad finalizer names accepted/rejected (F11).
- [ ] **Verification loop:** fail verdict returns to EDIT; pass at the contract's
      `VerificationLevel` advances to EXPORT.
- [ ] **Export pipelines:** each existing pipeline (`static_standalone`, `cloudflare_project`,
      `deck_export`, `document_pdf`) emits non-empty `DeliverableEvent` bytes (F9, F10); kinds
      without export handled per decision §7.4 (F8).
- [ ] **Handoff:** `present_artifact_for_download` (GAP) delivers the exported bytes.
- [ ] **GAP guards:** `workflow.output` empty bootstrap path errors or reroutes (F14).
- [ ] **Cross-doc consistency:** every tool named here matches a row in
      [`ARTIFACT_TOOL_CONTRACTS.md`](./ARTIFACT_TOOL_CONTRACTS.md) and a real name in
      [`_GROUNDING.md`](./_GROUNDING.md) (or is tagged GAP).


---

<!-- ============================================================ -->
# [5] docs/artifact-vm/ARTIFACT_TOOL_CONTRACTS.md

<!-- ============================================================ -->

# Artifact VM — Tool Contracts (documentation-only schemas)

> **Status:** SPEC / DOCUMENTATION ONLY. No tool here is implemented by this pack. Each entry
> states its **concrete backing tool today** (a real name from
> [`_GROUNDING.md`](./_GROUNDING.md)) or **(GAP)** if it is proposed new work.
>
> **Read with:** [`DISCO_ARTIFACT_VM_SPEC.md`](./DISCO_ARTIFACT_VM_SPEC.md) (lifecycle, phases,
> rules) and [`_GROUNDING.md`](./_GROUNDING.md) (real names). Phase names below are the lifecycle
> phases defined in that spec §3: **create · edit · preview · verify · export**.

## Legend / house style

- Schemas are **frozen Pydantic v2** value objects, `model_config = ConfigDict(extra="forbid",
  frozen=True)`. JSON examples are what `model_dump(mode="json")` would emit.
- Field tables: `field | type | required | default | constraints`.
- `required = yes` means no default; `required = no` means the default applies.
- "Allowed phases" = phases in which the host phase guard accepts the call (rule §6.10 of the
  spec). "Forbidden phases" lists the rest with the reason.
- Failure codes are host-handled traps; the model sees the code, the host takes the action.
- **CD-TOOLS** = the shipped edit-discipline pack (fresh-read guard, atomic `exact_replace`,
  `safe_write_file` entrypoint guard, governed routing, verifier-only scope, elision discipline).

## Summary table (all 12 tools)

| # | Tool (conceptual) | Backing today | Allowed phases | Key failure codes |
| --- | --- | --- | --- | --- |
| 1 | `artifact_write` | `file_write` / `app_create` / `slides_generate` | create, edit | `ENTRYPOINT_PROTECTED`, `PHASE_FORBIDDEN`, `PARENT_MISSING` |
| 2 | `artifact_markup_str_replace` | `exact_replace` (CD-TOOLS) | edit | `OLD_NOT_FOUND`, `OLD_NOT_UNIQUE`, `STALE_SOURCE`, `ELISION_DETECTED` |
| 3 | `artifact_logic_str_replace` | `exact_replace` over JS / `app_set_tweak` | edit | `OLD_NOT_FOUND`, `OLD_NOT_UNIQUE`, `STALE_SOURCE`, `ELISION_DETECTED` |
| 4 | `artifact_set_props` | `app_set_tweak` (+`tweaks_io.py`) | create, edit | `TWEAK_LAW_VIOLATION`, `UNKNOWN_TWEAK`, `BAD_COLOR` |
| 5 | `artifact_read_parts` | `file_read`/`file_list` (+ appkit reads) | create, edit, preview, verify, export | `PART_NOT_FOUND`, `ARTIFACT_NOT_INDEXED` |
| 6 | `exact_replace` | `exact_replace` (**DONE**, CD-TOOLS) | edit | `OLD_NOT_FOUND`, `OLD_NOT_UNIQUE`, `STALE_SOURCE` |
| 7 | `safe_write_file` | `safe_write_file` (**DONE**, CD-TOOLS) | create, edit | `ENTRYPOINT_PROTECTED`, `PARENT_MISSING` |
| 8 | `show_artifact_to_agent` | **(GAP)** (`preview_start`+verifier screenshot today) | preview | `PREVIEW_DOWN`, `NOT_APP_DELIVERY` |
| 9 | `show_artifact_to_user` | **(GAP)** | preview | `PREVIEW_DOWN`, `NOT_PRESENTABLE` |
| 10 | `ready_for_artifact_verification` | `ready_for_<kind>_verification` (**DONE**, host route) | verify | `BAD_FINALIZER_NAME`, `REQUIRED_FILES_MISSING`, `NOT_SHOWN` |
| 11 | `present_artifact_for_download` | **(GAP)** (`DeliverableEvent`, P10b) | export | `NOT_VERIFIED`, `EXPORT_MISSING`, `EMPTY_DELIVERABLE` |
| 12 | `run_project_script` | `run_project_script` (**DONE**) | create, edit, verify | `SCRIPT_NOT_FOUND`, `NONZERO_EXIT`, `TIMEOUT` |

---

## 1. `artifact_write`

- **Purpose:** Create or overwrite a *non-entrypoint* artifact source file (or a brand-new file).
- **Backing today:** `file_write` (site/document/prototype); `app_create` / `app_update_content`
  (appkit); `slides_generate` (deck) — façade dispatches per `ContractKind`.

**Input schema**

| field | type | required | default | constraints |
| --- | --- | --- | --- | --- |
| `path` | `str` | yes | — | project-relative; must not be a registered entrypoint unless `rewrite_allowed` |
| `content` | `str` | yes | — | full file body; no elision markers |
| `kind` | `ContractKind` | yes | — | one of grounding §1 ids |
| `create_parents` | `bool` | no | `false` | if false, parent dir must exist |

```json
{ "path": "assets/app.css", "content": "body{margin:0}", "kind": "static.site", "create_parents": false }
```

**Output**

| field | type | notes |
| --- | --- | --- |
| `path` | `str` | written path |
| `bytes_written` | `int` | length on disk |
| `created` | `bool` | true if new file |

```json
{ "path": "assets/app.css", "bytes_written": 14, "created": true }
```

- **Allowed phases:** create, edit.
- **Forbidden phases:** preview/verify/export — source mutation after the artifact is shown or
  being verified would invalidate the verifier's basis.
- **Failure codes**

| code | when | recoverable? | host action |
| --- | --- | --- | --- |
| `ENTRYPOINT_PROTECTED` | `path` is a registered entrypoint and `rewrite_allowed=false` | yes | reject; route to edit tool |
| `PHASE_FORBIDDEN` | called in preview/verify/export | yes | reject; advise return to edit |
| `PARENT_MISSING` | parent dir absent and `create_parents=false` | yes | reject; retry with flag |

- **Tests required:** happy path (new + overwrite non-entrypoint); each failure code; idempotency
  (same content twice → `created=false`, identical bytes); phase-guard rejection in verify.
- **CD-TOOLS inheritance:** governed artifact routing + `safe_write_file` entrypoint guard — it
  cannot be used to clobber a protected entrypoint.

---

## 2. `artifact_markup_str_replace`

- **Purpose:** Apply one atomic old→new replacement to the markup entrypoint after a fresh read.
- **Backing today:** `exact_replace` (CD-TOOLS); fallbacks `file_str_replace` / `file_edit` /
  `file_replace_lines`.

**Input schema**

| field | type | required | default | constraints |
| --- | --- | --- | --- | --- |
| `path` | `str` | yes | — | must be the markup entrypoint for the kind |
| `old` | `str` | yes | — | must match current source verbatim (fresh-read guard) |
| `new` | `str` | yes | — | no elision markers |
| `expect_count` | `int` | no | `1` | required occurrences of `old`; ≥1 |

```json
{ "path": "index.html", "old": "<h1>Hi</h1>", "new": "<h1>Welcome</h1>", "expect_count": 1 }
```

**Output**

| field | type | notes |
| --- | --- | --- |
| `path` | `str` | edited file |
| `replacements` | `int` | count applied |

```json
{ "path": "index.html", "replacements": 1 }
```

- **Allowed phases:** edit.
- **Forbidden phases:** create (entrypoint not yet final), preview/verify/export (no post-show
  mutation).
- **Failure codes**

| code | when | recoverable? | host action |
| --- | --- | --- | --- |
| `OLD_NOT_FOUND` | `old` not present (stale memory) | yes | re-read source (fresh-read guard), retry |
| `OLD_NOT_UNIQUE` | occurrences ≠ `expect_count` | yes | widen `old` context, retry |
| `STALE_SOURCE` | source changed since last read | yes | re-read, retry |
| `ELISION_DETECTED` | `new` contains an elision marker | no | reject; require full replacement text |

- **Tests required:** happy path; each failure code; idempotency (re-applying when `new` already
  present → `OLD_NOT_FOUND`); phase-guard rejection in create.
- **CD-TOOLS inheritance:** the core anti-Mode-B primitive — fresh-read guard + atomic exact
  replacement + elision discipline.

---

## 3. `artifact_logic_str_replace`

- **Purpose:** Atomic replacement scoped to logic/JS regions of the artifact.
- **Backing today:** `exact_replace` over `<script>` / `*.js`; appkit behavior via `app_set_tweak`.

**Input schema**

| field | type | required | default | constraints |
| --- | --- | --- | --- | --- |
| `path` | `str` | yes | — | logic file or entrypoint containing inline script |
| `old` | `str` | yes | — | verbatim match (fresh-read guard) |
| `new` | `str` | yes | — | no elision markers |
| `expect_count` | `int` | no | `1` | ≥1 |

```json
{ "path": "index.html", "old": "const N = 1;", "new": "const N = 2;", "expect_count": 1 }
```

**Output**

| field | type | notes |
| --- | --- | --- |
| `path` | `str` | edited file |
| `replacements` | `int` | count applied |

```json
{ "path": "index.html", "replacements": 1 }
```

- **Allowed phases:** edit.
- **Forbidden phases:** create / preview / verify / export — same rationale as tool 2.
- **Failure codes:** identical set to tool 2 (`OLD_NOT_FOUND`, `OLD_NOT_UNIQUE`, `STALE_SOURCE`,
  `ELISION_DETECTED`).
- **Tests required:** happy path; each failure code; idempotency; phase-guard rejection; appkit
  path routes behavior change through `app_set_tweak` (controls_behavior).
- **CD-TOOLS inheritance:** same as tool 2 — fresh-read + atomic exact + elision discipline.

---

## 4. `artifact_set_props`

- **Purpose:** Set user-facing knobs/tweaks (props metadata) without touching markup.
- **Backing today:** `app_set_tweak` (+ `tweaks_io.py`); **(GAP)** as a generic façade for
  non-appkit kinds.

**Input schema**

| field | type | required | default | constraints |
| --- | --- | --- | --- | --- |
| `key` | `str` | yes | — | must be a known `TweakField.key` |
| `value` | `str \| int \| float \| bool` | yes | — | type must match the field's `TweakEditor` |
| `editor` | `TweakEditor` | no | (field's) | `text\|color\|int\|float\|boolean\|enum\|palette` |

```json
{ "key": "accent", "value": "#3366ff", "editor": "color" }
```

**Output**

| field | type | notes |
| --- | --- | --- |
| `key` | `str` | tweak set |
| `value` | `str` | normalized value (colors → `#rrggbb`) |
| `persisted_to` | `str` | `.disco/tweaks.json` |

```json
{ "key": "accent", "value": "#3366ff", "persisted_to": ".disco/tweaks.json" }
```

- **Allowed phases:** create, edit.
- **Forbidden phases:** preview/verify/export — props are source; changing them after show
  invalidates verification.
- **Failure codes**

| code | when | recoverable? | host action |
| --- | --- | --- | --- |
| `TWEAK_LAW_VIOLATION` | tweak doesn't control behavior or required #fields (text/color need ≥2) | no | reject; cite grounding §7 law |
| `UNKNOWN_TWEAK` | `key` not a defined `TweakField` | yes | reject; list valid keys |
| `BAD_COLOR` | color value not normalizable to `#rrggbb` | yes | reject; retry |

- **Tests required:** happy path (each editor type); each failure code; idempotency (same value →
  no-op write); color normalization; phase-guard rejection in verify.
- **CD-TOOLS inheritance:** governed routing (props are a non-entrypoint part; never a raw write).

---

## 5. `artifact_read_parts`

- **Purpose:** Read the indexed parts of the artifact (the fresh-read source for exact edits).
- **Backing today:** `file_read` / `file_list` (+ appkit `app_*` read paths); **(GAP)** as a
  unified parts reader.

**Input schema**

| field | type | required | default | constraints |
| --- | --- | --- | --- | --- |
| `parts` | `tuple[str,...]` | no | `("markup",)` | subset of `markup\|logic\|props\|manifest\|semantic\|resources\|verification\|export` |
| `include_content` | `bool` | no | `true` | false → metadata only |

```json
{ "parts": ["markup", "props"], "include_content": true }
```

**Output**

| field | type | notes |
| --- | --- | --- |
| `parts` | `list[object]` | each: `{name, backing_file, present, content?}` |

```json
{ "parts": [ { "name": "markup", "backing_file": "index.html", "present": true, "content": "<!doctype html>…" },
             { "name": "props", "backing_file": ".disco/tweaks.json", "present": false } ] }
```

- **Allowed phases:** create, edit, preview, verify, export (read-only is always safe).
- **Forbidden phases:** none.
- **Failure codes**

| code | when | recoverable? | host action |
| --- | --- | --- | --- |
| `PART_NOT_FOUND` | requested part name invalid | yes | reject; list valid part names |
| `ARTIFACT_NOT_INDEXED` | artifact not yet in EDIT (parts not indexed) | yes | run create/index first |

- **Tests required:** happy path (each part); missing part returns `present=false` not error;
  `PART_NOT_FOUND` for bad name; read allowed in every phase.
- **CD-TOOLS inheritance:** supplies the **fresh source** that the fresh-read guard requires
  before exact edits.

---

## 6. `exact_replace`

- **Purpose:** Atomic, fresh-read-guarded exact text replacement (the CD-TOOLS edit primitive).
- **Backing today:** `exact_replace` (**DONE**, CD-TOOLS).

**Input schema**

| field | type | required | default | constraints |
| --- | --- | --- | --- | --- |
| `path` | `str` | yes | — | project-relative |
| `old` | `str` | yes | — | verbatim match required |
| `new` | `str` | yes | — | no elision markers |
| `expect_count` | `int` | no | `1` | ≥1 |

```json
{ "path": "report.md", "old": "## Intro", "new": "## Introduction", "expect_count": 1 }
```

**Output**

| field | type | notes |
| --- | --- | --- |
| `path` | `str` | edited file |
| `replacements` | `int` | count applied |

```json
{ "path": "report.md", "replacements": 1 }
```

- **Allowed phases:** edit.
- **Forbidden phases:** create/preview/verify/export — no post-show source mutation; entrypoint
  not final during create.
- **Failure codes:** `OLD_NOT_FOUND`, `OLD_NOT_UNIQUE`, `STALE_SOURCE` (see tool 2 table).
- **Tests required:** happy path; each failure code; idempotency (`OLD_NOT_FOUND` on re-apply);
  multi-occurrence with `expect_count`; phase-guard rejection.
- **CD-TOOLS inheritance:** this *is* CD-TOOLS atomic exact replacement + fresh-read guard.

---

## 7. `safe_write_file`

- **Purpose:** Write a file but refuse to clobber a registered artifact entrypoint.
- **Backing today:** `safe_write_file` (**DONE**, CD-TOOLS).

**Input schema**

| field | type | required | default | constraints |
| --- | --- | --- | --- | --- |
| `path` | `str` | yes | — | rejected if a registered entrypoint |
| `content` | `str` | yes | — | full body; no elision markers |
| `create_parents` | `bool` | no | `false` | parent dir must exist if false |

```json
{ "path": "data/seed.json", "content": "[]", "create_parents": true }
```

**Output**

| field | type | notes |
| --- | --- | --- |
| `path` | `str` | written path |
| `bytes_written` | `int` | length on disk |
| `created` | `bool` | true if new |

```json
{ "path": "data/seed.json", "bytes_written": 2, "created": true }
```

- **Allowed phases:** create, edit.
- **Forbidden phases:** preview/verify/export — source mutation after show invalidates verifier.
- **Failure codes**

| code | when | recoverable? | host action |
| --- | --- | --- | --- |
| `ENTRYPOINT_PROTECTED` | `path` is a registered entrypoint | yes | refuse; route to edit tool (rule §6.1) |
| `PARENT_MISSING` | parent absent and `create_parents=false` | yes | retry with flag |

- **Tests required:** happy path; `ENTRYPOINT_PROTECTED` for each kind's entrypoint; `PARENT_MISSING`;
  idempotency; phase-guard rejection in verify.
- **CD-TOOLS inheritance:** this *is* the CD-TOOLS safe-write entrypoint guard.

---

## 8. `show_artifact_to_agent`

- **Purpose:** Render an agent-private preview (screenshot/DOM) for the model to self-inspect.
- **Backing today:** **(GAP)** — today only `preview_start` + verifier `verify_web_app` screenshot
  exist; there is no agent-private inspection channel.

**Input schema**

| field | type | required | default | constraints |
| --- | --- | --- | --- | --- |
| `route` | `str` | no | `"/"` | preview route to capture |
| `viewport` | `str` | no | `"desktop"` | `desktop\|mobile` (cf. `viewport` tool) |

```json
{ "route": "/", "viewport": "desktop" }
```

**Output**

| field | type | notes |
| --- | --- | --- |
| `screenshot_ref` | `str` | host ref to captured image (agent-private) |
| `console_errors` | `list[str]` | preview console errors, if any |

```json
{ "screenshot_ref": "host://preview/abc123.png", "console_errors": [] }
```

- **Allowed phases:** preview.
- **Forbidden phases:** create/edit (nothing running), verify (verifier owns diagnostics —
  rule §6.8), export.
- **Failure codes**

| code | when | recoverable? | host action |
| --- | --- | --- | --- |
| `PREVIEW_DOWN` | preview server not healthy | yes | `preview_logs`, restart, retry |
| `NOT_APP_DELIVERY` | kind is files-delivery (deck/document/workflow/custom) | no | reject; use file inspection |

- **Tests required:** happy path (app kinds); `PREVIEW_DOWN`; `NOT_APP_DELIVERY` for each files kind;
  phase-guard rejection in verify (must not double as verifier diagnostic).
- **CD-TOOLS inheritance:** verifier-only read scope — this is **agent-private** and must NOT be
  the verifier's truth channel; it cannot satisfy verification.

---

## 9. `show_artifact_to_user`

- **Purpose:** Promote the artifact to an explicit user-visible state (distinct from verification).
- **Backing today:** **(GAP)** — today conflated with `preview_start`.

**Input schema**

| field | type | required | default | constraints |
| --- | --- | --- | --- | --- |
| `route` | `str` | no | `"/"` | route the user lands on |
| `note` | `str` | no | `""` | optional caption shown with the preview |

```json
{ "route": "/", "note": "Draft landing page — review the hero copy." }
```

**Output**

| field | type | notes |
| --- | --- | --- |
| `shown` | `bool` | true once user-visible |
| `preview_url` | `str` | user-facing preview url |

```json
{ "shown": true, "preview_url": "https://preview.disco/abc123/" }
```

- **Allowed phases:** preview.
- **Forbidden phases:** create/edit (not presentable), verify/export (already past show).
- **Failure codes**

| code | when | recoverable? | host action |
| --- | --- | --- | --- |
| `PREVIEW_DOWN` | preview server unhealthy | yes | restart, retry |
| `NOT_PRESENTABLE` | `required_files` incomplete | yes | return to edit/create |

- **Tests required:** happy path; `PREVIEW_DOWN`; `NOT_PRESENTABLE`; **does not** set verification
  status (rule §6.7); idempotency (re-show = same url).
- **CD-TOOLS inheritance:** enforces the show-vs-verify separation that keeps verifier-only scope
  meaningful.

---

## 10. `ready_for_artifact_verification`

- **Purpose:** The finalizer the model calls to hand the artifact to the verifier (host-routed).
- **Backing today:** `ready_for_<kind>_verification` finalizer family (**DONE** as host route;
  e.g. `ready_for_static_site_verification`, `ready_for_app_verification`).

**Input schema**

| field | type | required | default | constraints |
| --- | --- | --- | --- | --- |
| `finalizer` | `str` | yes | — | must match `^ready_for_[a-z0-9_]+_verification$` |
| `summary` | `str` | no | `""` | what changed since last verify |

```json
{ "finalizer": "ready_for_static_site_verification", "summary": "Hero copy + CTA wired." }
```

**Output**

| field | type | notes |
| --- | --- | --- |
| `accepted` | `bool` | finalizer accepted, routed to verifier |
| `level` | `VerificationLevel` | `load_only\|standard\|strict` from contract |

```json
{ "accepted": true, "level": "standard" }
```

- **Allowed phases:** verify (the call *enters* verify from show_to_user).
- **Forbidden phases:** create/edit (premature; artifact not shown), preview (must pass through
  show_to_user, rule §6.7), export (already verified).
- **Failure codes**

| code | when | recoverable? | host action |
| --- | --- | --- | --- |
| `BAD_FINALIZER_NAME` | string fails the regex | no | reject; cite naming law (grounding §2) |
| `REQUIRED_FILES_MISSING` | `required_files` incomplete | yes | return to create/edit |
| `NOT_SHOWN` | artifact never reached SHOW_TO_USER | yes | call `show_artifact_to_user` first |

- **Tests required:** happy path (each kind's finalizer); `BAD_FINALIZER_NAME` (regex fail);
  `REQUIRED_FILES_MISSING`; `NOT_SHOWN`; correct `level` returned per contract; the finalizer
  routes to the verifier, not to a main-agent tool.
- **CD-TOOLS inheritance:** verifier-only read scope — the finalizer is the *only* bridge from the
  main agent to verifier truth; the agent cannot self-verify.

---

## 11. `present_artifact_for_download`

- **Purpose:** Deliver exported bytes to the user as a downloadable artifact.
- **Backing today:** **(GAP)** — backed by `DeliverableEvent` (P10b proved bytes flow).

**Input schema**

| field | type | required | default | constraints |
| --- | --- | --- | --- | --- |
| `export_name` | `str` | yes | — | must match the contract's `ExportContract.name` |
| `filename` | `str` | no | (export default) | suggested download filename |

```json
{ "export_name": "static_standalone", "filename": "site.zip" }
```

**Output**

| field | type | notes |
| --- | --- | --- |
| `delivered` | `bool` | true once bytes emitted |
| `bytes` | `int` | deliverable size |
| `event` | `str` | `DeliverableEvent` ref |

```json
{ "delivered": true, "bytes": 20480, "event": "deliverable://abc123" }
```

- **Allowed phases:** export.
- **Forbidden phases:** create/edit/preview/verify — nothing to deliver until the pipeline ran.
- **Failure codes**

| code | when | recoverable? | host action |
| --- | --- | --- | --- |
| `NOT_VERIFIED` | artifact not VERIFY-passed | yes | run finalizer + pass verify first |
| `EXPORT_MISSING` | kind has no `ExportContract` (GAP kinds) | no | reject; handoff as preview per spec §7.4 |
| `EMPTY_DELIVERABLE` | pipeline produced no bytes | yes | re-run export pipeline |

- **Tests required:** happy path (each existing pipeline: `static_standalone`, `cloudflare_project`,
  `deck_export`, `document_pdf`) → non-empty bytes; `NOT_VERIFIED`; `EXPORT_MISSING` for
  `interactive.prototype`/`workflow.output`/`custom`; `EMPTY_DELIVERABLE`; phase-guard rejection
  before export.
- **CD-TOOLS inheritance:** host-owned export — model never assembles bytes; consistent with
  governed routing and host-owned pipelines (spec rule §6.9).

---

## 12. `run_project_script`

- **Purpose:** Run a project-defined script (build/lint/dev task) in the sandbox.
- **Backing today:** `run_project_script` (**DONE**).

**Input schema**

| field | type | required | default | constraints |
| --- | --- | --- | --- | --- |
| `script` | `str` | yes | — | must be a declared project script name |
| `args` | `tuple[str,...]` | no | `()` | passthrough args |
| `timeout_s` | `int` | no | `120` | 1–600 |

```json
{ "script": "build", "args": [], "timeout_s": 120 }
```

**Output**

| field | type | notes |
| --- | --- | --- |
| `exit_code` | `int` | process exit code |
| `stdout` | `str` | captured stdout (truncated) |
| `stderr` | `str` | captured stderr (truncated) |

```json
{ "exit_code": 0, "stdout": "built in 1.2s", "stderr": "" }
```

- **Allowed phases:** create, edit, verify (build/lint as a verify aid).
- **Forbidden phases:** preview (use preview tools), export (host owns the pipeline — rule §6.9).
- **Failure codes**

| code | when | recoverable? | host action |
| --- | --- | --- | --- |
| `SCRIPT_NOT_FOUND` | script name not declared | yes | reject; list scripts |
| `NONZERO_EXIT` | script exits non-zero | yes | surface stderr; return to edit |
| `TIMEOUT` | exceeds `timeout_s` | yes | kill; raise/adjust timeout |

- **Tests required:** happy path; `SCRIPT_NOT_FOUND`; `NONZERO_EXIT`; `TIMEOUT`; idempotency for
  pure scripts; phase-guard rejection in export.
- **CD-TOOLS inheritance:** does not mutate artifact source directly, so it is outside the edit
  guards; it must never be used to bypass governed routing (e.g. a script that rewrites an
  entrypoint is still subject to the entrypoint protection in spirit — flag in review).

---

## Cross-references

- Lifecycle / phases / rules: [`DISCO_ARTIFACT_VM_SPEC.md`](./DISCO_ARTIFACT_VM_SPEC.md)
- Real Disco names, registries, GAP list: [`_GROUNDING.md`](./_GROUNDING.md)
- `data-disco-*` semantic markup: [`DIRECT_MANIPULATION_SPEC.md`](./DIRECT_MANIPULATION_SPEC.md)
- Resource manifest + provenance: [`RESOURCE_AND_PROVENANCE_SPEC.md`](./RESOURCE_AND_PROVENANCE_SPEC.md)


---

<!-- ============================================================ -->
# [6] docs/artifact-vm/TOOL_PARITY_MATRIX.md

<!-- ============================================================ -->

# Tool Parity Matrix — Claude Design → Disco

> **Status: SPEC ONLY — no runtime code changed.**
> Source of truth for every real name below: [`_GROUNDING.md`](./_GROUNDING.md).
> This matrix maps each Claude Design ("dc_*") tool/pattern onto the **real** Disco
> tool/contract that already realizes it, or marks it **(GAP)** where no Disco surface
> exists yet. CD-TOOLS 1..10 are treated as **DONE + live-proven** (grounding §9) and are
> never re-specced as new work.

## Legend

- **Disco equivalent** — the concrete real tool/concept name from grounding §1–§8, or
  **(GAP)** for a proposed-new surface. Where Claude Design uses a `dc_*` artifact tool,
  the Disco column names the *concrete backing tool* and, where relevant, the proposed
  unifying **`artifact_*` façade** (grounding §10). The façade is a conceptual/thin-router
  wrapper, not new behavior.
- **Status assumption** —
  - `DONE via X` — already shipped; X names the mechanism (CD-TOOLS, files.py, registry,
    export pipeline, etc.).
  - `PARTIAL` — backing capability exists for *some* kinds/roles but not the full surface.
  - `GAP` — no Disco surface exists; proposed as new work.
- **Required tests** — minimum acceptance tests the future implementer must add. Per
  grounding §11.3 every tool section needs *Failure modes + Tests required*; this column is
  the Tests-required digest.
- **Risk if missing** — what breaks (or stays manual/thrash-prone) without it.
- **Priority** — P0 (blocks correct/safe artifact lifecycle), P1 (needed for parity, not
  safety-critical), P2 (polish / nice-to-have / Disco policy makes it low-value).

---

## Matrix

| # | Claude Design role | Disco equivalent (real name or (GAP)) | Status assumption | Required tests | Risk if missing | Priority |
| --- | --- | --- | --- | --- | --- | --- |
| 1 | `dc_write` — create/overwrite artifact source | `file_write` / `safe_write_file` (sites/docs/prototype/custom); `app_create` (appkit); governed artifact routing sends entrypoints through edit tools | **DONE via CD-TOOLS + files.py** (grounding §4, §9) | entrypoint-clobber refusal; fresh full-file write; routing sends `index.html`/`appspec.json` through governed path | Raw write clobbers a live artifact entrypoint mid-build | P0 |
| 2 | `dc_html_str_replace` — semantic replace in HTML artifact | `file_str_replace` / `file_edit` (backing); proposed `artifact_markup_str_replace` **(GAP-façade)** | **DONE via files.py**; façade **GAP** | exact-match replace; ambiguous-match rejection; no-elision-marker guard; routes through CD-TOOLS governance | Edits bypass governed routing / silent multi-match clobber | P1 |
| 3 | `dc_js_str_replace` — semantic replace in JS artifact | `file_str_replace` / `file_edit` (backing); proposed `artifact_script_str_replace` **(GAP-façade)** | **DONE via files.py**; façade **GAP** | exact-match replace; ambiguous-match rejection; elision-marker guard | Same as #2 for script source | P1 |
| 4 | `dc_set_props` — declarative component/prop set | `app_set_design`, `app_set_tweak`, `app_update_content` (AppKit semantic ops, grounding §4) | **PARTIAL** (appkit kinds only; no declarative prop edit for site/prototype/custom) | tweak grounding-law enforcement (§7); design set persists to `.disco/tweaks.json`; content update is targeted | Non-appkit kinds have no declarative prop edit → fall back to raw text edits | P1 |
| 5 | `str_replace_edit` — generic exact replacement | `exact_replace` | **DONE via CD-TOOLS** (atomic exact replacement + fresh-read guard, §9) | stale-read rejection (fresh-read guard); atomic single replace; reject on 0 or >1 matches; no elision markers | Stale clobber / edit-elision thrash (the Mode-B failure CD-TOOLS killed) | P0 |
| 6 | `write_file` — write file bytes | `file_write` / `safe_write_file` | **DONE via files.py + CD-TOOLS** (safe write refuses entrypoint clobber, §9) | byte-faithful write; `safe_write_file` refuses to clobber declared `required_files` entrypoints | Unsafe overwrite of contract-required files | P0 |
| 7 | `read_file` — read file bytes | `file_read` (+ `file_list`, verifier-only read scope) | **DONE via files.py + CD-TOOLS** (verifier-only read scope, §9) | read returns current bytes; verifier role read-scope enforced; build role cannot read outside artifact root | Verifier read-scope leak / model reads outside workspace | P1 |
| 8 | `run_script` — execute build/dev script | `run_project_script` / `code_exec` / `shell` family (`shell_exec`, `shell_view`, `shell_wait`, `shell_kill_process`) | **DONE via files.py/tools** (grounding §4) | sandboxed exec; timeout/kill; stdout+stderr capture; non-zero exit surfaced | Unsandboxed or hanging script exec | P1 |
| 9 | `copy_starter_component` — scaffold from a starter | `scaffold_starter` (+ kit data in `kits/starter.py`) | **PARTIAL** — tool exists but only `app_shell` + `lead_form` kits are real; 12 catalog kits **(GAP)** (grounding §6) | scaffold each registered kit produces its `required_files`; unknown-kit error; idempotent re-scaffold guard | Missing kits → model hand-writes scaffolding → rewrite thrash | P1 |
| 10 | `show_html` — agent-private render inspection | `show_artifact_to_agent` **(GAP)** — today only `preview_start` + `verify_web_app` screenshot exist | **GAP** | agent-private preview returns render/screenshot WITHOUT changing user-visible state; no DeliverableEvent emitted | Agent edits blind; cannot self-inspect render before showing user | P2 |
| 11 | `show_to_user` — present artifact to user | `show_artifact_to_user` **(GAP)** — today implicit via `preview_start`/`live_view` | **GAP** | explicit user-visible preview state event; distinct from agent-private show (#10) | No explicit "present to user" affordance → ambiguous preview ownership | P2 |
| 12 | `ready_for_verification` — finalize for verification | `ready_for_*_verification` host-finalizer family (regex `^ready_for_[a-z0-9_]+_verification$`), per-kind in registry | **DONE via VerificationContract** (host-routed strings, grounding §3/§4) | regex contract holds; per-kind finalizer routes correctly; `VerificationLevel` (load_only/standard/strict) enforced; not exposed as a model-free builtin | Finalizer bypass → unverified artifact ships | P0 |
| 13 | `eval_js` / `screenshot` (verifier-only) | `verify_web_app` / `verify_app` screenshot (backing); in-page `eval_js` probe **(GAP)**; both under verifier-only read scope | **PARTIAL** — screenshot DONE; scripted in-page assertion GAP | screenshot capture at viewport; verifier-only scope; (GAP) eval_js returns serialized result; sandboxed | Verifier can capture pixels but cannot assert DOM/runtime state | P2 |
| 14 | `questions_v2` — structured clarifying questions | **(GAP)** — no ask mechanism in grounding; Disco policy is *defer-don't-block* in autonomous runs (reason through, never block) | **GAP** (intentional / low-value) | if ever built: questions are non-blocking + resumable; autonomous runs never hard-block | Low — Disco deliberately avoids blocking questions; absence is by design | P2 |
| 15 | `read_skill_prompt` — load a skill prompt at build time | `BuildContract.skills` mounting (the `skills` tuple, e.g. `appkit.leadgen`/`cloudflare_export`/`design_recipe`) | **PARTIAL** — contract declares skills, but no runtime tool/loader proven to inject them into prompt assembly | declared `skills` are resolved + injected into prompt-pack assembly; missing skill errors; ordering deterministic | Declared skills silently not injected → contract lies about context | P1 |
| 16 | `image_metadata` — probe image dims/format | **(GAP)** — `image_generate` exists; no metadata probe | **GAP** | returns width/height/format/bytes for a workspace image; missing-file error | Layout decisions made blind to real image dimensions | P2 |
| 17 | `view_image` — inspect an image in-context | **(GAP)** — only `verify_web_app` screenshot surfaces pixels | **GAP** | model/verifier can view a provided or generated image; scope-checked | Model cannot inspect provided/generated images before placing them | P2 |
| 18 | `image_slot` — declarative image placeholder | `image_slot` starter kit **(GAP)** (grounding §6); for decks, C7 slide image-embed is **DONE** (reference) | **GAP** (starter kit) | slot renders placeholder; accepts a generated/provided asset by id; preserved on edit | Ad-hoc image embedding instead of governed slots | P2 |
| 19 | `copy_files` — bulk copy assets | **(GAP)** — no bulk-copy tool; `file_read`+`file_write` cover single-file | **GAP** (single-file PARTIAL via file ops) | bulk copy preserves bytes + relative paths; overwrite policy honored | Multi-asset bundling is manual + error-prone | P2 |
| 20 | `gen_pptx` — generate a PPTX deck | `slides_generate` + `_pptx_render` + `deck_patch`, exported via `deck_export` | **DONE via slides pipeline** (grounding §4/§8; C7 image-embed + branded template landed) | pptx renders; images embed (C7); branded template applied when `theme.branded`; `deck_export` emits bytes | n/a — DONE | P0 (done) |
| 21 | `super_inline_html` — single-file standalone HTML | `static_standalone` export pipeline (`bundle, validate, deliver`) | **DONE via export pipeline** (grounding §8) | all CSS/JS/assets inlined to one file; validates; delivers bytes | n/a — DONE | P1 (done) |
| 22 | `present_fs_item_for_download` — download affordance | `present_artifact_for_download` **(GAP)**; backing delivery already proven via `DeliverableEvent` bytes (P10b) | **GAP** (backing delivery DONE) | download affordance emits a `DeliverableEvent`; per-file selection; filename + mime correct | No explicit per-file download surface; relies on whole-export delivery | P1 |
| 23 | `open_for_print` — print/PDF view | `document_pdf` export (DONE for documents); print-view for app/site kinds **(GAP)** | **PARTIAL** — `document_pdf` DONE; print path for non-document kinds GAP | print CSS applied; PDF bytes produced; pagination correct | No print/PDF path for non-document artifacts | P2 |
| 24 | `get_public_file_url` — mint a public URL | `cloudflare_project` export deploy URL (DONE for appkit.leadgen); general public-URL minting **(GAP)** | **PARTIAL** — appkit deploy URL DONE; general minting GAP | appkit deploy returns reachable public URL; (GAP) general file → signed/public URL | No general public-URL surface outside the cloudflare appkit path | P2 |
| 25 | handoff to Claude Code — escape to full-code agent | **(GAP)** — no handoff package; closest today is export bundle + the build harness | **GAP** | handoff package bundles source + `BuildContract` + provenance + verification verdicts; re-openable | No clean escape hatch from constrained artifact build to full-code editing | P2 |

---

## Priority rollup (open work = GAP or PARTIAL)

Fully **DONE** (8): `dc_write` (1), `str_replace_edit` (5), `write_file` (6), `read_file`
(7), `run_script` (8), `ready_for_verification` (12), `gen_pptx` (20), `super_inline_html`
(21). These map onto CD-TOOLS / files.py / registry / export pipelines and are referenced,
not re-specced.

Open items needing new surface — **17 total**:

| Priority | Count | Items |
| --- | --- | --- |
| **P0 gaps** | **0** | — (all P0 lifecycle/safety tools are DONE via CD-TOOLS) |
| **P1 gaps** | **6** | `artifact_markup_str_replace` façade (2), `artifact_script_str_replace` façade (3), `dc_set_props` non-appkit (4), `copy_starter_component` 12 missing kits (9), `read_skill_prompt` skills mounting (15), `present_artifact_for_download` (22) |
| **P2 gaps** | **11** | `show_artifact_to_agent` (10), `show_artifact_to_user` (11), verifier `eval_js` (13), `questions_v2` (14, intentional), `image_metadata` (16), `view_image` (17), `image_slot` kit (18), `copy_files` (19), `open_for_print` non-document (23), `get_public_file_url` general (24), handoff-to-Claude-Code (25) |

**Reading of the rollup:** the safety-critical lifecycle (create / exact-edit / safe-write /
finalize / verify) is already closed by CD-TOOLS — **zero P0 gaps**. Parity work is
concentrated in (a) the `artifact_*` unifying façade + non-appkit declarative edits, (b)
the starter-kit catalog (12 missing kits), (c) skills mounting, and (d) the show/present/
download/handoff surfaces. None of these are blockers for safe builds; they are parity and
ergonomics. See [`MERGE_PLAN.md`](./MERGE_PLAN.md) for sequencing and
[`README.md`](./README.md) for the full pack index.


---

<!-- ============================================================ -->
# [7] docs/artifact-vm/PROMPT_PACK_REQUIREMENTS.md

<!-- ============================================================ -->

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


---

<!-- ============================================================ -->
# [8] docs/artifact-vm/STARTER_KIT_CATALOG.md

<!-- ============================================================ -->

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


---

<!-- ============================================================ -->
# [9] docs/artifact-vm/EXPORT_PIPELINE_SPEC.md

<!-- ============================================================ -->

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


---

<!-- ============================================================ -->
# [10] docs/artifact-vm/CONTEXT_AND_ITERATION_SPEC.md

<!-- ============================================================ -->

# Context & Iteration Spec — Artifact-VM Support Pack

> **Status:** SPEC ONLY. No runtime behavior is described as implemented. Read
> [`_GROUNDING.md`](./_GROUNDING.md) first for the real Disco names. The export
> pipelines that *consume* the durable context defined here (`resource_manifest.json`,
> ContextPack, handoff packages, version snapshots) are specified in
> [`EXPORT_PIPELINE_SPEC.md`](./EXPORT_PIPELINE_SPEC.md).
>
> **Purpose.** Define how the Artifact VM keeps a long-running build session coherent: how
> durable project memory is stored on disk under `.disco/`, who reads/writes each piece,
> how a "snip" of frozen context is resolved back to fresh source before an exact-text
> edit (the CD-TOOLS fresh-read guard), when a major revision gets a new version snapshot
> versus an in-place targeted edit, and how the verifier's diagnostics are kept *out* of
> the main agent's working context.

---

## 0. Vocabulary anchor (from grounding)

| Concept | Real name / status |
| --- | --- |
| AppKit version snapshot tool | `app_snapshot_version` (REAL — `appkit.py`, in `appkit.leadgen` edit pack) |
| AppKit tweak persistence | `.disco/tweaks.json` (REAL — grounding §7); values in `AppSpec.tweaks` |
| AppKit appspec | `.disco/appspec.json` (REAL — required file for `appkit.leadgen`) |
| Fresh-read guard before exact-text edits | CD-TOOLS 1 (DONE — grounding §9) |
| Atomic exact replacement | `exact_replace` (REAL — CD-TOOLS) |
| Safe writes (no entrypoint clobber) | `safe_write_file` (REAL — CD-TOOLS) |
| Verifier-only read scope | CD-TOOLS (DONE — grounding §9) |
| No elision markers in editable source | CD-TOOLS tool-prompt discipline (DONE) |
| `context_policy` pack section | REQUIRED prompt-pack section (grounding §5) |
| `context_memory` tool | REAL (grounding §4) |
| Human-visible preview (GAP) | `show_artifact_to_user` / `show_artifact_to_agent` (grounding §10) |
| ContextPack / `todo.md` / `decisions.md` / `direct_edits.json` / `overrides.css` / `resource_manifest.json` / `context_pack.json` | **(GAP)** — proposed durable-context files this spec defines |
| comment anchors / screen labels | **(GAP)** — proposed addressing scheme this spec defines |

> Only `app_snapshot_version`, `.disco/tweaks.json`, `.disco/appspec.json`, and the
> CD-TOOLS guards are REAL today. Every other artifact named below is **(GAP)** — proposed
> new durable-context surface. The CD-TOOLS work it builds on is DONE; do not re-spec it.

---

## 1. The `.disco/` context directory — file layout

A long-running project's durable context lives in a single host-owned directory at the
project root: `.disco/`. It is **never** part of a public deploy payload (stripped by
pipelines 1/2 in `EXPORT_PIPELINE_SPEC.md`) but **is** included in owner/developer handoff
packages (pipelines 6/7).

```
<project-root>/
├── index.html                      # (kind-dependent) the artifact entrypoint
├── report.md                       # (document kind) authored source
├── deck.authored.json              # (deck kind) AuthoredDeck sidecar (REAL)
├── assets/                         # images, fonts, css referenced by the artifact
│   └── logo.png
└── .disco/                         # host-owned durable context (this spec)
    ├── appspec.json                # (appkit) AppSpec (REAL)
    ├── tweaks.json                 # (appkit) tweak values (REAL, grounding §7)
    ├── context_pack.json           # (GAP) ContextPack: the resumable session memory index
    ├── todo.md                     # (GAP) the living plan / outstanding work
    ├── decisions.md                # (GAP) append-only decision log (why, not just what)
    ├── direct_edits.json           # (GAP) record of user/agent targeted edits + anchors
    ├── overrides.css               # (GAP) user style overrides layered over the artifact
    ├── resource_manifest.json      # (GAP) authoritative asset inventory (export reads this)
    ├── anchors.json                # (GAP) comment-anchor + screen-label registry
    ├── snapshots/                  # (GAP) version snapshots (cross-ref app_snapshot_version)
    │   ├── index.json              #   (GAP) ordered snapshot index w/ labels + timestamps
    │   ├── v0001/                  #   (GAP) a frozen copy of the artifact at a revision
    │   └── v0002/
    └── verifier/                   # (GAP) verifier-only scratch — NEVER read by main agent
        ├── screenshots/            #   (GAP) verify_web_app frames
        └── diagnostics.json        #   (GAP) eval/screenshot findings (isolated)
```

> `app_snapshot_version` (REAL) is the AppKit-specific writer for `snapshots/`; this spec
> generalizes the `snapshots/` directory to **all** kinds as **(GAP)**.

---

## 2. Per-item specs

Each item: **what it is · where it lives · who reads/writes · lifecycle · failure modes ·
tests required.**

---

### 2.1 ContextPack — `.disco/context_pack.json` **(GAP)**

- **What it is.** The resumable session-memory index: a compact JSON pointing at the other
  context files, the current artifact kind/contract, the active version snapshot, the open
  `todo.md` items, and a pointer into the event log. It is what an owner-handoff (pipeline
  7) ships so a *future* session resumes with full memory. The realization of the grounding
  `context_policy` pack section and the `context_memory` tool as a durable on-disk file.
- **Where it lives.** `.disco/context_pack.json`.
- **Who reads/writes.** *Writes:* the host (on plan updates, snapshots, deliveries).
  *Reads:* the loop on session resume; the owner_handoff export (pipeline 7); never the
  public deploy payload.
- **Lifecycle.** Created at first build action → updated on every snapshot/decision/delivery
  → frozen into owner/developer handoff bundles → re-hydrated on resume.
- **Failure modes.** Stale pointer to a deleted snapshot; pointer to a `todo.md` that
  drifted from reality; corruption (unparseable JSON) wedging resume; bloat (embedding full
  source instead of pointers).
- **Tests required.**
  - [ ] Resume from a ContextPack reconstructs kind/contract/active-snapshot/todo correctly.
  - [ ] A dangling pointer (snapshot deleted) is detected and reported, not silently
        followed.
  - [ ] Corrupt JSON fails closed on resume with a structured "context unreadable" message,
        not a crash.
  - [ ] ContextPack stays under a size bound (pointers, not inlined source).

---

### 2.2 `todo.md` — `.disco/todo.md` **(GAP)**

- **What it is.** The living plan: outstanding and completed work for the project, in
  human-readable markdown. The durable counterpart to the in-loop `plan_step` /
  `update_plan_progress` tools (grounding §4). One source of truth for "what's left".
- **Where it lives.** `.disco/todo.md`.
- **Who reads/writes.** *Writes:* the host on plan-progress updates (capable model uses
  declarative `update_plan_progress`; small model uses NL + done-at-finish — per the
  plan-progress redesign). *Reads:* the loop's finish gate; the owner-handoff summary.
- **Lifecycle.** Seeded from the initial plan → items checked off as work lands → emptied of
  open items is a precondition the finish gate consults (but does not solely trust — finish
  is decoupled from bookkeeping per the plan-progress unify work).
- **Failure modes.** State-drift (todo says done, artifact disagrees); incremental
  per-step checklist thrash (the failure the plan-progress redesign killed — do NOT
  reintroduce per-step writes for small models); orphaned items after a copy-vs-edit.
- **Tests required.**
  - [ ] Plan-progress update is reflected in `todo.md` for both capable and small model
        modes.
  - [ ] Finish gate reads effective plan-progress from ONE shared reader (no `plan_step`-only
        path that ignores `update_plan_progress`).
  - [ ] A done-claim in `todo.md` that contradicts artifact state does NOT alone satisfy the
        finish gate.

---

### 2.3 `decisions.md` — `.disco/decisions.md` **(GAP)**

- **What it is.** An append-only log of *why* choices were made (template chosen, library
  picked, a feature deferred and the reason). Durable rationale that survives context
  condensation, so a resumed/handed-off session does not re-litigate settled decisions.
- **Where it lives.** `.disco/decisions.md`.
- **Who reads/writes.** *Writes:* host appends on each material decision. *Reads:* the agent
  on resume; the owner/developer handoff summary; the user (it is human-readable).
- **Lifecycle.** Append-only; never rewritten in place (rewriting loses the audit value).
  Carried into all handoff packages.
- **Failure modes.** In-place edits destroying history; decisions that contradict
  `todo.md`/artifact (drift); secrets accidentally logged (must be redacted before any
  outbound handoff — ties to EXPORT FC-6).
- **Tests required.**
  - [ ] Appends are append-only (a write that truncates prior entries is rejected).
  - [ ] No secret-shaped content survives into a handoff copy of `decisions.md`.
  - [ ] Resume surfaces prior decisions to the agent's working context (no re-litigation).

---

### 2.4 `direct_edits.json` — `.disco/direct_edits.json` **(GAP)**

- **What it is.** A structured record of targeted edits applied to the artifact — each entry
  ties a change to a **comment anchor** / **screen label** (see §2.8/§2.9) and the tool that
  made it (`exact_replace`, `file_replace_lines`, an `app_*` semantic op). It lets the host
  replay/audit edits and is the bridge between human "change this bit" requests and the
  exact-text edit tools.
- **Where it lives.** `.disco/direct_edits.json`.
- **Who reads/writes.** *Writes:* the host after each successful targeted edit. *Reads:* the
  copy-vs-edit decision rule (§3); the verifier (to know what changed); developer handoff.
- **Lifecycle.** Appended per edit; an entry references the snapshot it applied to; reset
  semantics: a new version snapshot starts a fresh edit run scoped to that snapshot.
- **Failure modes.** Anchor drift (an edit recorded against an anchor that later moved);
  recording an edit that did not actually apply (must be written only after the tool
  confirms the exact replacement landed — fresh-read guard, §4); divergence from the actual
  file.
- **Tests required.**
  - [ ] An entry is written ONLY after the underlying exact-text edit verifiably applied.
  - [ ] Replaying recorded edits over the referenced snapshot reproduces current state.
  - [ ] An edit against a stale anchor is rejected and re-resolved (no blind apply).

---

### 2.5 `overrides.css` — `.disco/overrides.css` **(GAP)**

- **What it is.** A user/agent style-override layer applied *over* the artifact without
  mutating its authored source — so visual tweaks survive a major-revision copy and can be
  toggled. The CSS analog of `AppSpec.tweaks` for non-appkit (HTML) kinds.
- **Where it lives.** `.disco/overrides.css`; linked last in the artifact's `<head>` at
  preview/export time (precedence over authored styles).
- **Who reads/writes.** *Writes:* the host on a style-override request. *Reads:* preview;
  export prepare/copy (it is **inlined/copied** into the bundle by pipelines 1/2 — it is
  real output, not internal-only); the verifier renders with it applied.
- **Lifecycle.** Persists across version snapshots (carried forward, since it is a layer);
  can be promoted into authored source by an explicit "bake in" action.
- **Failure modes.** Override silently masking a real bug (the artifact looks right only
  because of overrides — verifier must test WITH and note WITHOUT); override not carried
  into a snapshot copy; override referencing a missing asset.
- **Tests required.**
  - [ ] `overrides.css` is included in static/cloudflare export bundles and listed in the
        `resource_manifest.json`.
  - [ ] Overrides survive a copy-to-new-version snapshot.
  - [ ] Removing overrides reveals (does not hide) underlying artifact state to the verifier.

---

### 2.6 `resource_manifest.json` — `.disco/resource_manifest.json` **(GAP)**

- **What it is.** The authoritative inventory of every asset the artifact depends on
  (images, fonts, css, generated slide images, included data files) with a relative path and
  a content hash per entry. The **export pipelines' fail-closed FC-3 input** — every bundle
  must contain a manifest and it must be consistent (no orphans, no dangling refs). See
  `EXPORT_PIPELINE_SPEC.md` §3 FC-3.
- **Where it lives.** `.disco/resource_manifest.json` (source of truth); **copied into the
  bundle root** at export so the delivered artifact carries its own manifest.
- **Who reads/writes.** *Writes:* the host whenever an asset is added/removed (image_generate,
  file_write of an asset, deck image embed). *Reads:* EVERY export pipeline's preflight +
  validate; the verifier.
- **Lifecycle.** Continuously maintained during the build; snapshotted with each version;
  validated at export and shipped inside the bundle.
- **Failure modes.** Orphan (file present, not in manifest) → export FC-3 block; dangling ref
  (manifest entry, no file) → FC-3 block; stale hash (file changed, hash not updated) →
  idempotency/verification mismatch; absolute host path in a manifest entry → FC-2 block.
- **Tests required.**
  - [ ] Adding an asset updates the manifest; removing one removes the entry.
  - [ ] Manifest with an orphan or a dangling ref is rejected at export validate.
  - [ ] Manifest entries are all relative paths (no absolute host paths) and hashes match
        file contents.
  - [ ] The manifest is present inside every files/app export bundle.

---

### 2.7 Snip / resolved context — `.disco/snapshots/` + fresh-read **(GAP dir; CD-TOOLS guard REAL)**

- **What it is.** "Snip" = a *frozen* excerpt of source that was placed into the model's
  working context earlier in the session (e.g. the host showed lines 40–60 of `index.html`).
  Because the file may have changed since, the snip is **stale by construction**. Resolved
  context = the act of re-reading the *current* file region before relying on it. The
  fresh-read guard (CD-TOOLS 1, DONE) enforces this before any exact-text edit.
- **Where it lives.** Snips are transient (in the model's context window / condensation
  records); the *authoritative* source is always the live file under the project root.
  Version snapshots in `.disco/snapshots/` are the durable frozen copies (distinct from
  transient snips).
- **Who reads/writes.** *Reads/resolves:* the host before dispatching `exact_replace` /
  `file_replace_lines`. *Writes:* none — resolution is a read that refreshes; it never
  trusts the snip.
- **Lifecycle.** Snip enters context → time passes / edits land → fresh-read resolves snip to
  current bytes → exact-text edit operates on current bytes (never on the frozen snip).
- **Failure modes.** Editing against a frozen snip whose text no longer matches (the Mode-B
  edit-elision thrash CD-TOOLS killed); elision markers (`// ... unchanged`) treated as real
  source (forbidden by CD-TOOLS tool-prompt discipline); resolving to the wrong file region.
- **Tests required.**
  - [ ] An `exact_replace` whose `old` text comes from a now-stale snip is forced to
        re-read fresh source first and either matches current bytes or fails closed (never
        edits a phantom region).
  - [ ] An elision marker present in a snip never reaches executed edit args.
  - [ ] Fresh-read resolves the correct file + region for the anchor.

#### Worked example — snip resolves frozen context before an exact-text edit

1. **Snip enters context.** Earlier the host showed the agent:
   ```
   <snip file="index.html" lines="40-44">
   40  <h1 class="hero">Welcome</h1>
   41  <p class="sub">Old tagline</p>
   </snip>
   ```
   This text is now *frozen* in the agent's context.
2. **Time passes.** A prior targeted edit changed line 41 to
   `<p class="sub">A fresher tagline</p>`. The snip in context is now stale.
3. **Agent requests an edit.** It calls `exact_replace(file="index.html",
   old="<p class=\"sub\">Old tagline</p>", new="<p class=\"sub\">Welcome aboard</p>")`,
   using the **frozen** `old` text.
4. **Fresh-read guard fires (CD-TOOLS 1).** Before applying, the host RE-READS the current
   bytes of `index.html` around the anchor. It finds `Old tagline` is no longer present
   (the live line says `A fresher tagline`).
5. **Fail closed, re-resolve.** The host does NOT edit a phantom region. It returns a
   structured mismatch: `"exact_replace target not found — the file changed since you last
   saw it. Current line 41 is: '<p class=\"sub\">A fresher tagline</p>'. Re-issue the edit
   against current text."` The agent re-reads, then issues `old="A fresher tagline"` and
   the atomic replacement lands on real current bytes.
6. **Record.** Only after the replacement verifiably applied does the host append an entry to
   `direct_edits.json` (§2.4) tied to the comment anchor (§2.8).

> This is the durable-context realization of CD-TOOLS' fresh-read guard + atomic
> `exact_replace` + no-elision discipline (grounding §9). The export side relies on the same
> property: prepare/copy resolves snip/frozen context to fresh source before snapshotting
> (`EXPORT_PIPELINE_SPEC.md` stage glossary, "prepare/copy").

---

### 2.8 Comment anchors — `.disco/anchors.json` **(GAP)**

- **What it is.** Stable, human-meaningful handles embedded as comments in the artifact
  source (e.g. `<!-- @anchor:hero -->`) that name a region independently of line numbers, so
  edits and the verifier can refer to "the hero section" even after lines shift. The registry
  maps anchor → current location.
- **Where it lives.** Anchors live inline in source; the registry is `.disco/anchors.json`.
- **Who reads/writes.** *Writes:* host when a region is named (scaffold, app_add_section).
  *Reads:* `direct_edits.json` recording; the fresh-read resolver (an anchor resolves to a
  current region); user-facing "change the hero" requests.
- **Lifecycle.** Created with a region; updated when the region moves; removed with the
  region. Carried into snapshots.
- **Failure modes.** Anchor drift (registry points at a moved/deleted region); duplicate
  anchor ids; anchor comments leaking into a public bundle (acceptable in HTML comments but
  should be stripped for production polish — flag, do not fail).
- **Tests required.**
  - [ ] An anchor resolves to the correct current region after intervening edits shift lines.
  - [ ] A deleted region's anchor is removed/invalidated, not left dangling.
  - [ ] Duplicate anchor ids are rejected at write time.

---

### 2.9 Screen labels + human 1-based indexing **(GAP)**

- **What it is.** Human-facing labels for screens/sections/slides ("Screen 1: Landing",
  "Slide 3") using **1-based** indexing (humans count from 1), distinct from any 0-based
  internal array index. The mapping is recorded alongside anchors in `.disco/anchors.json`.
- **Where it lives.** `.disco/anchors.json` (label ↔ internal index ↔ anchor).
- **Who reads/writes.** *Writes:* host as screens/slides are created/reordered. *Reads:*
  every user-facing message ("Slide 3 rendered blank" — see `EXPORT_PIPELINE_SPEC.md`
  pipeline 5 copy); the verifier; reorder operations (`app_reorder_section`).
- **Lifecycle.** Labels track creation order but are re-derived on reorder so "Slide 3"
  always means the third slide as the human sees it.
- **Failure modes.** Off-by-one (showing a 0-based index to a human); label not updated after
  a reorder; mismatch between the label in a user message and the actual screen.
- **Tests required.**
  - [ ] Every user-facing index is 1-based; the internal index is converted exactly once at
        the boundary.
  - [ ] After a reorder, labels are re-derived so the displayed Nth == the actual Nth.
  - [ ] A user message referencing "Slide N" maps to the same slide the verifier inspects.

---

### 2.10 Version snapshots — `.disco/snapshots/` (cross-ref `app_snapshot_version`) **(GAP dir; tool REAL for appkit)**

- **What it is.** Frozen, restorable copies of the whole artifact at a labeled revision.
  `app_snapshot_version` (REAL, in the `appkit.leadgen` edit pack) is the AppKit writer;
  this spec generalizes snapshots to **all** kinds. Each snapshot has an id (`v0001`), a
  label, a timestamp, and a copy of the artifact + `.disco/` context at that point.
- **Where it lives.** `.disco/snapshots/v0001/...`, indexed by `.disco/snapshots/index.json`.
- **Who reads/writes.** *Writes:* host on a major revision (see §3 copy-vs-edit rule) or an
  explicit `app_snapshot_version` call. *Reads:* restore/rollback; owner-handoff version
  history; the copy-vs-edit decision.
- **Lifecycle.** Created at a major-revision boundary → never mutated (a snapshot is
  immutable) → referenced by ContextPack as the active baseline → shipped (index + latest, or
  all, per handoff policy) in owner handoff.
- **Failure modes.** Snapshot bloat (snapshotting on every tiny edit); mutating a snapshot in
  place (breaks immutability/audit); ContextPack pointing at a pruned snapshot; snapshot that
  omits `.disco/` context (un-resumable).
- **Tests required.**
  - [ ] A snapshot is immutable after creation (writes into a snapshot dir are rejected).
  - [ ] Restore from a snapshot reproduces both artifact AND `.disco/` context exactly.
  - [ ] Snapshots are created at major-revision boundaries, NOT on every small edit
        (anti-bloat — ties to §3).
  - [ ] `app_snapshot_version` (appkit) writes into the same `snapshots/` layout this spec
        defines.

---

### 2.11 Verifier context isolation — `.disco/verifier/` **(GAP dir; verifier-only read scope REAL)**

- **What it is.** A hard boundary: the verifier's diagnostics (screenshots from
  `verify_web_app`, eval/LLM-judge findings) are written to `.disco/verifier/` and **never
  enter the main agent's working context** unless promoted as an explicit, structured
  finding. Builds on CD-TOOLS' verifier-only read scope (DONE, grounding §9). Prevents the
  verifier's raw output (large screenshots, internal eval chatter) from polluting/condensing
  the builder's context.
- **Where it lives.** `.disco/verifier/screenshots/`, `.disco/verifier/diagnostics.json`.
- **Who reads/writes.** *Writes:* the verifier only. *Reads:* the host, which decides what (if
  anything) to surface to the main agent as a typed finding; the verifier on a re-check. The
  **main agent never reads `.disco/verifier/` directly.**
- **Lifecycle.** Written per verify pass → consulted by the host's gate → pruned/rotated;
  promoted findings become structured messages, not raw dumps.
- **Failure modes.** Leakage (raw screenshot bytes or verifier reasoning landing in the
  builder's context — the exact pollution this isolates); the builder editing to satisfy a
  *raw* diagnostic it should not have seen; verifier reading the builder's scratch and
  conflating roles.
- **Tests required.**
  - [ ] Main-agent context after a verify pass contains NO raw verifier diagnostic content
        (only promoted, typed findings).
  - [ ] The verifier writes only under `.disco/verifier/` and reads only its allowed scope
        (CD-TOOLS verifier-only read scope).
  - [ ] A promoted finding is a structured message (anchor + screen label + verdict), not a
        screenshot blob.

---

### 2.12 Handoff packages as durable context — (cross-ref `EXPORT_PIPELINE_SPEC.md` 6/7) **(GAP)**

- **What it is.** The developer-handoff and owner-handoff exports (pipelines 6 and 7) are the
  *durable, portable* form of all the context above: they bundle `.disco/` (ContextPack,
  decisions, todo, snapshots index, manifest) with the artifact so a future session — or a
  different model, or a human — can resume with full memory.
- **Where it lives.** Produced by the export pipelines; the source is `.disco/`.
- **Who reads/writes.** *Writes:* pipelines 6/7 (`EXPORT_PIPELINE_SPEC.md`). *Reads:* a
  resumed session re-hydrating from a delivered handoff; the receiving human/developer.
- **Lifecycle.** Snapshot-of-context at delivery time → portable → re-hydrated on resume.
- **Failure modes.** Handoff missing the ContextPack (un-resumable — owner_handoff FC,
  `missing_context_pack_fail.json`); secrets carried into a handoff (FC-6); stale manifest.
- **Tests required.**
  - [ ] An owner handoff contains a parseable ContextPack + decisions + todo + snapshot index.
  - [ ] Re-hydrating from a handoff reconstructs a working session (kind/contract/active
        snapshot/open todos).
  - [ ] No secrets survive into a handoff (shared with `EXPORT_PIPELINE_SPEC.md` FC-6).

---

## 3. Copy-vs-edit decision rule (checklist)

When the user/agent wants to change the artifact, the host decides between an **in-place
targeted edit** (cheap, exact-text via `exact_replace`/`file_replace_lines`/`app_*`) and a
**copy to a new version snapshot** (major revision). Use this checklist; **default to
in-place edit** — snapshot only when a trigger fires (anti-bloat, §2.10).

**Take an IN-PLACE targeted edit when ALL of these hold:**
- [ ] The change is localized (one or a few anchors/regions).
- [ ] The artifact's structure/identity is preserved (same screens, same intent).
- [ ] The change is reversible by another small edit.
- [ ] No risk of clobbering an entrypoint (otherwise route through `safe_write_file`).
- [ ] The fresh-read guard can resolve the target to current bytes (§2.7).

**Create a NEW VERSION SNAPSHOT (copy) when ANY of these hold:**
- [ ] **Major revision:** a structural rewrite, new layout, or a "make me a different
      version / try another direction" request (the user wants to compare/keep the old one).
- [ ] **Destructive/irreversible** change where the prior state must be restorable.
- [ ] **Branch point:** the user wants A/B variants kept side by side.
- [ ] An `app_snapshot_version` call (appkit) is made explicitly.
- [ ] Pre-export checkpoint of a milestone the owner should be able to return to.

**Never:**
- [ ] Snapshot on every small edit (bloat — §2.10 failure mode).
- [ ] Rewrite the whole artifact to make a localized change (the rewrite-thrash the build-loop
      fixes target; in-place targeted edit is the law for editable kinds — only `custom` has
      `rewrite_allowed=True`, grounding §3).
- [ ] Mutate an existing snapshot in place.

**Tests required.**
- [ ] A localized change takes the in-place path and does NOT create a snapshot.
- [ ] A "make another version" request creates a new snapshot and preserves the prior one.
- [ ] A rewrite of an editable (non-`custom`) kind to achieve a localized change is rejected
      in favor of a targeted edit.

---

## 4. Verifier context isolation (rule restated)

- **Rule.** Verifier-only diagnostics (screenshots, eval/LLM-judge output) live in
  `.disco/verifier/` and are read by the host gate only. The main agent's working context
  receives, at most, **promoted, typed findings** (anchor + screen label + verdict), never
  raw verifier artifacts. (CD-TOOLS verifier-only read scope, DONE — grounding §9.)
- **Why.** Raw verifier output is large and noisy; letting it into the builder's context
  causes condensation pressure and tempts the builder to "fix the screenshot" rather than the
  artifact. Isolation keeps the builder's context about the *artifact and the plan*.
- **Boundary checklist.**
  - [ ] Verifier writes ONLY under `.disco/verifier/`.
  - [ ] Main agent NEVER reads `.disco/verifier/` directly.
  - [ ] Only typed, structured findings cross the boundary (no blobs).
  - [ ] Findings reference human screen labels (§2.9) and comment anchors (§2.8).

---

## 5. Tests required (global checklist)

- [ ] **CT-1 ContextPack round-trip.** Build → owner_handoff → resume reconstructs
      kind/contract/active-snapshot/open-todos.
- [ ] **CT-2 Plan-progress single reader.** Finish gate and `todo.md` read effective
      plan-progress from ONE shared reader (no `plan_step`-only path).
- [ ] **CT-3 Decisions append-only + redacted.** Appends never truncate; no secrets in a
      handoff copy.
- [ ] **CT-4 direct_edits write-after-verify.** An edit is recorded only after the exact-text
      replacement verifiably applied.
- [ ] **CT-5 overrides survive + export.** `overrides.css` carries across snapshots and is
      bundled + manifested by static/cloudflare export.
- [ ] **CT-6 manifest consistency.** Orphan/dangling/abs-path manifest entries are rejected
      at export validate (shared with `EXPORT_PIPELINE_SPEC.md` FC-2/FC-3).
- [ ] **CT-7 snip fresh-read.** An exact-text edit from a stale snip is forced to re-read
      fresh source and fails closed on mismatch (CD-TOOLS 1); no elision marker reaches edit
      args.
- [ ] **CT-8 anchors stable under drift.** Anchors resolve to correct current regions after
      line shifts; duplicates rejected; deleted-region anchors invalidated.
- [ ] **CT-9 1-based indexing.** Every user-facing index is 1-based; reorder re-derives
      labels; user "Slide N" == verifier's slide N.
- [ ] **CT-10 snapshot immutability + restore.** Snapshots are immutable and restore artifact
      + `.disco/` exactly; created at major-revision boundaries only.
- [ ] **CT-11 copy-vs-edit routing.** Localized → in-place (no snapshot); "another version" →
      snapshot (prior preserved); rewrite-for-localized rejected for editable kinds.
- [ ] **CT-12 verifier isolation.** No raw verifier diagnostic enters main-agent context;
      only typed findings cross; verifier confined to `.disco/verifier/`.
- [ ] **CT-13 handoff durability.** Owner handoff is re-hydratable; missing ContextPack fails
      closed (`missing_context_pack_fail.json`, GAP).

> Cross-references: real export consumption of this context is in
> [`EXPORT_PIPELINE_SPEC.md`](./EXPORT_PIPELINE_SPEC.md); real names + CD-TOOLS DONE work in
> [`_GROUNDING.md`](./_GROUNDING.md) §7, §9, §10.


---

<!-- ============================================================ -->
# [11] docs/artifact-vm/DIRECT_MANIPULATION_SPEC.md

<!-- ============================================================ -->

# Direct Manipulation Spec — the `data-disco-*` grammar

> **Read `_GROUNDING.md` first.** This spec is **documentation only** — no runtime
> behavior is implemented or claimed. It formalizes the attribute grammar that makes
> Disco artifacts *directly manipulable*: click a rendered element → resolve to a source
> location → make a **targeted** edit (never a rewrite) → preserve comment anchors and
> user direct-edits.
>
> **Where this sits relative to what already exists:**
> - **P8 "Semantic Direct Manipulation" is COMPLETE** in the main campaign — so a real
>   foundation exists (AppKit semantic tools, the selection bridge, the `data-oid`
>   stamper). This spec does **not** re-invent that; it *formalizes the attribute
>   grammar* on top of it.
> - The shipped substrate (see `docs/A1-click-to-edit-design.md`) stamps
>   `data-oid="{relpath}:{sourceline}"` and resolves it via `resolveRef()` →
>   `SourceRef{kind:'source', oid, file, line}`. That is the **coarse, file+line**
>   resolution layer. It exists and works.
> - The `data-disco-*` family specified here is the **(GAP)** richer, *semantic*
>   resolution layer that sits **alongside** `data-oid`: it names *what* an element is
>   (a field, a section, a metric, a media slot) rather than only *where* its source is
>   (file+line). `data-oid` answers "which file/line"; `data-disco-*` answers "which
>   semantic target + how to edit it without a rewrite".
>
> Cross-references: `_GROUNDING.md` (real Disco names), `RESOURCE_AND_PROVENANCE_SPEC.md`
> (media slots / `data-disco-media-slot`), `CONTENT_AND_DESIGN_DISCIPLINE_SPEC.md`
> (content tags surfaced on fields), and the oracle fixtures named throughout
> (`fixtures/oracles/*.json`).

---

## 0. Status legend

| Marker | Meaning |
| --- | --- |
| **(EXISTS)** | Real today (P8 / CD-TOOLS / A2 substrate). Referenced, not re-specced. |
| **(GAP)** | New surface proposed by this pack. Not implemented. |

`data-oid` is **(EXISTS)**. Every `data-disco-*` attribute below is **(GAP)** unless
stated otherwise.

---

## 1. Design goals (the contract this grammar must honor)

1. **Rendered element → source location** must be deterministic and reversible.
2. **Targeted edits only.** A click-to-edit must produce an `exact_replace` (CD-TOOLS,
   atomic) on the smallest enclosing source span — **never** a whole-file rewrite.
   `EditContract.rewrite_allowed` is `False` for every direct-manipulable kind (only
   `custom` allows rewrite — see `_GROUNDING.md §3`).
3. **Anchors survive.** Comment anchors and version markers MUST be preserved verbatim
   across any targeted edit. Duplication or loss is a **failure** (oracle:
   `comment_anchor_preserved_pass.json` / `comment_anchor_lost_fail.json`).
4. **User direct edits are not clobbered.** Edits the *user* makes by direct manipulation
   (persisted to `direct_edits.json` / `overrides.css`) reconcile with subsequent *agent*
   edits without silent loss (oracle: `direct_edit_clobbered_fail.json`).
5. **No false affordance.** An element gets an edit affordance **only** if it carries a
   resolvable `data-disco-field` (or an ancestor `data-oid` pointing at a real workspace
   file). Non-stamped → walk-up select still works, edit affordance is gated off
   (mirrors `docs/A1-click-to-edit-design.md §"No false affordance"`).

---

## 2. The attribute family — overview table

| Attribute | Granularity | Maps to | Emitted by | Unique? | Survives edits? |
| --- | --- | --- | --- | --- | --- |
| `data-disco-field` | one editable value | a source text span (file+span / appspec field path) | app render / starter kit | unique per artifact | **MUST** preserve |
| `data-disco-section` | a content block | an AppKit section / HTML region | app render / `app_add_section` | unique per artifact | **MUST** preserve |
| `data-disco-file` | element ↔ backing file | a workspace-relative file path | app render / stamper | not unique (many els/file) | **MUST** preserve |
| `data-disco-flow` | a multi-step user flow | a named flow in the appspec/flowspec | app render | unique per flow | **MUST** preserve |
| `data-disco-screen-label` | a named screen/route | a screen id (prototype/app) | app render | unique per screen | **MUST** preserve |
| `data-disco-comment-anchor` | a review/comment pin | a stable anchor id (no source line) | app render (persisted) | globally unique | **MUST** preserve verbatim |
| `data-disco-metric-id` | a single stat/number | a metric in the metrics registry / appspec | app render | unique per metric | **MUST** preserve |
| `data-disco-version` | artifact version stamp | a snapshot id (`app_snapshot_version`) | host render / snapshot | one per rendered doc | **MUST** preserve |
| `data-disco-media-slot` | an image/video slot | a `ResourceManifest` slot id | app render / starter kit | unique per slot | **MUST** preserve |

> **Relationship to `data-oid` (EXISTS):** `data-oid="{relpath}:{line}"` is the coarse
> fallback. When both are present, `data-disco-field` (semantic) wins for resolution;
> `data-oid` is used to *locate the file* and as the resolution fallback when no
> `data-disco-*` is found on the click target or its ancestors.

---

## 3. Per-attribute specifications

### 3.1 `data-disco-field`

- **Meaning.** Marks a single, independently-editable **value** (a headline, a paragraph,
  a CTA label, a price). The atomic unit of click-to-edit.
- **Value grammar (BNF):**
  ```
  field-ref     ::= source-field | appspec-field
  source-field  ::= relpath ":" span-start "-" span-end [ "#" field-name ]
  appspec-field ::= "appspec:" json-pointer
  relpath       ::= <workspace-relative POSIX path, no leading "/", no "..">
  span-start    ::= line ":" col
  span-end      ::= line ":" col
  line          ::= 1*DIGIT          ; 1-based
  col           ::= 1*DIGIT          ; 1-based, UTF-16 code units
  json-pointer  ::= "/" 1*( unreserved / "~0" / "~1" )   ; RFC 6901
  field-name    ::= 1*( ALPHA / DIGIT / "_" / "-" )
  ```
- **Value regex (validation):**
  ```
  ^(?:[^/\0][^\0]*:\d+:\d+-\d+:\d+(?:#[A-Za-z0-9_-]+)?|appspec:/[^\s]+)$
  ```
- **Example HTML:**
  ```html
  <!-- static.site / interactive.prototype (file+span form) -->
  <h1 data-disco-field="index.html:12:1-12:38#hero_headline"
      data-disco-file="index.html">Launch faster with Disco</h1>

  <!-- appkit.leadgen (appspec field-path form) -->
  <h1 data-disco-field="appspec:/sections/0/headline"
      data-disco-section="hero">Launch faster with Disco</h1>
  ```
- **Maps to.** *file+span form* → an exact source span in `relpath` (edited via
  `exact_replace`, CD-TOOLS atomic). *appspec form* → a field path in
  `.disco/appspec.json`, edited via `app_update_content` / `app_set_tweak` (AppKit
  semantic tools, **EXISTS**) — NOT raw file write (governed artifact routing, CD-TOOLS).
- **Emitted by.** App render (AppKit) for the appspec form; the **server-side stamper**
  (A1.1, committed `e561000`, **EXISTS**) for the file+span form. Starter kits seed the
  initial fields. The **agent never hand-writes** `data-disco-field` — it is host-emitted.
- **Uniqueness.** The `field-name` suffix (when present) is unique within its file. The
  full `field-ref` is unique within the artifact at a given version.
- **Preservation.** On a targeted edit the *value text changes*; the **attribute and its
  ref are re-stamped by the host at render time** (serve-time only — the agent's actual
  source files stay unstamped, per A1.1b). Line/col drift after an edit is expected and
  re-derived on the next render; the **`#field-name` is stable** and is the preferred join
  key across versions.

### 3.2 `data-disco-section`

- **Meaning.** A content block / region (hero, features, pricing, footer). The unit that
  `app_add_section` / `app_remove_section` / `app_reorder_section` (**EXISTS**) operate on.
- **Value grammar (BNF):**
  ```
  section-ref ::= section-id
  section-id  ::= ALPHA *( ALPHA / DIGIT / "_" / "-" )
  ```
- **Regex:** `^[A-Za-z][A-Za-z0-9_-]*$`
- **Example:**
  ```html
  <section data-disco-section="pricing" data-disco-file="index.html"> … </section>
  ```
- **Maps to.** An AppKit section in `.disco/appspec.json` (`/sections/<i>` with matching
  `id`), or an HTML region delimited by stamped boundary comments for static sites.
- **Emitted by.** App render / `app_add_section`. Starter kit seeds initial sections.
- **Uniqueness.** Unique within the artifact. A reorder changes DOM order but **not** the
  `section-id`.
- **Preservation.** `section-id` is **immutable** for the life of the section; remove +
  re-add creates a *new* id (never reuse). Reorder/edit MUST keep ids stable.

### 3.3 `data-disco-file`

- **Meaning.** Names the workspace file backing this element. The coarse locator; the
  semantic peer of `data-oid`'s file half.
- **Value grammar:** a workspace-relative POSIX path.
  ```
  file-ref ::= relpath
  relpath  ::= segment *( "/" segment )
  segment  ::= 1*( unreserved-char )   ; no ".", no "..", no leading "/"
  ```
- **Regex:** `^(?!/)(?!.*(?:^|/)\.\.?(?:/|$))[^\0]+$`  (rejects absolute paths and
  `.`/`..` segments — see `RESOURCE_AND_PROVENANCE_SPEC.md` "no absolute host paths").
- **Example:** `data-disco-file="components/pricing.html"`
- **Maps to.** A real file in the conversation workspace. MUST be project-relative; an
  absolute host path is a hard failure (oracle: `absolute_host_path_fail.json`).
- **Emitted by.** Stamper / app render.
- **Uniqueness.** **Not** unique — many elements share one file. (It is a *grouping* key.)
- **Preservation.** Stable unless the file is renamed; a rename re-stamps all descendants.

### 3.4 `data-disco-flow`

- **Meaning.** Marks the root element of a named, multi-step user flow (signup flow,
  checkout flow). Lets a mention like "the checkout flow" resolve to a flow definition
  rather than a single element.
- **Value grammar:**
  ```
  flow-ref  ::= flow-id [ "#" step-id ]
  flow-id   ::= ALPHA *( ALPHA / DIGIT / "_" / "-" )
  step-id   ::= ALPHA *( ALPHA / DIGIT / "_" / "-" )
  ```
- **Regex:** `^[A-Za-z][A-Za-z0-9_-]*(?:#[A-Za-z][A-Za-z0-9_-]*)?$`
- **Example:**
  ```html
  <form data-disco-flow="checkout#payment" data-disco-screen-label="payment"> … </form>
  ```
- **Maps to.** A flow entry in the appspec/flowspec (**GAP** — flowspec is new; for
  `interactive.prototype` it maps to a sequence of `data-disco-screen-label` screens).
- **Emitted by.** App render.
- **Uniqueness.** `flow-id` unique per artifact; `step-id` unique within its flow.
- **Preservation.** Both ids immutable; reordering steps preserves ids.

### 3.5 `data-disco-screen-label`

- **Meaning.** Names a screen / route / view (esp. `interactive.prototype`). Lets "the
  payment screen" resolve to a screen, and lets the host build a screen index.
- **Value grammar:**
  ```
  screen-ref ::= screen-id
  screen-id  ::= ALPHA *( ALPHA / DIGIT / "_" / "-" )
  ```
- **Regex:** `^[A-Za-z][A-Za-z0-9_-]*$`
- **Example:** `<main data-disco-screen-label="dashboard"> … </main>`
- **Maps to.** A screen id in the prototype's screen registry / a route entry.
- **Emitted by.** App render.
- **Uniqueness.** Unique per artifact.
- **Preservation.** Immutable for the screen's life.

### 3.6 `data-disco-comment-anchor`

- **Meaning.** A **stable pin** for a review comment / annotation. Critically, it carries
  **no source line** — it is a content-independent identity so a comment stays attached to
  "this element" even as surrounding source shifts. This is the anchor whose preservation
  is load-bearing.
- **Value grammar:**
  ```
  anchor-ref ::= "ca_" 22*22( base62 )      ; ULID-like, fixed length
  base62     ::= ALPHA / DIGIT
  ```
- **Regex:** `^ca_[A-Za-z0-9]{22}$`
- **Example:**
  ```html
  <p data-disco-field="index.html:40:1-40:60#cta_sub"
     data-disco-comment-anchor="ca_01HZX4P7Q2R8S3T9V6W1Y0">Try it free for 14 days</p>
  ```
- **Maps to.** A row in the comment store keyed by anchor id (host-side). It does **not**
  map to a source line — that is the point.
- **Emitted by.** App render, **persisted**. Created when a user (or agent) first leaves a
  comment on an element. The host writes it into the source as a stable id and re-emits it
  on every render.
- **Uniqueness.** **Globally unique** (across the artifact and across versions). Reuse is
  forbidden; a lost anchor must NOT be regenerated under a new id (that orphans the
  comment).
- **Preservation rules (HARD):**
  1. A targeted edit on an element MUST carry its `data-disco-comment-anchor` through
     **verbatim** — same id, exactly once.
  2. **Duplication is failure** — two elements with the same anchor id (oracle:
     `comment_anchor_duplicated_fail.json`).
  3. **Loss is failure** — an anchor present in version N missing in version N+1 with no
     explicit delete (oracle: `comment_anchor_lost_fail.json`).
  4. A *passing* edit preserves every pre-existing anchor unchanged (oracle:
     `comment_anchor_preserved_pass.json`).
  5. Anchors are preserved even when the element's **text** is fully replaced — the anchor
     is on the element identity, not the text.

### 3.7 `data-disco-metric-id`

- **Meaning.** Marks a single rendered **statistic / number** (e.g. "10,000 users",
  "99.9% uptime"). Lets the design-discipline lint and provenance checks find every stat,
  and lets "change the uptime number" resolve precisely.
- **Value grammar:**
  ```
  metric-ref ::= "m_" metric-name
  metric-name ::= ALPHA *( ALPHA / DIGIT / "_" )
  ```
- **Regex:** `^m_[A-Za-z][A-Za-z0-9_]*$`
- **Example:**
  ```html
  <span data-disco-metric-id="m_uptime_pct"
        data-disco-field="index.html:88:14-88:19#uptime">99.9%</span>
  ```
- **Maps to.** A metric entry in the metrics registry (**GAP**) carrying its
  **provenance tag** (`provided_by_user` / `derived_from_research` /
  `generated_placeholder` / `requires_user_input` — see
  `CONTENT_AND_DESIGN_DISCIPLINE_SPEC.md`). A `generated_placeholder` metric is a
  **fake-stat** lint finding unless backed.
- **Emitted by.** App render.
- **Uniqueness.** Unique per artifact.
- **Preservation.** `metric-id` immutable; the displayed value may change via targeted
  edit, but the provenance tag MUST travel with it.

### 3.8 `data-disco-version`

- **Meaning.** Stamps the rendered document with the artifact version it was produced
  from. One per rendered doc (on `<html>` or `<body>`). Lets the host detect a
  stale-preview / fresh-read mismatch (CD-TOOLS fresh-read guard).
- **Value grammar:**
  ```
  version-ref ::= "v_" snapshot-id
  snapshot-id ::= 1*( ALPHA / DIGIT / "_" / "-" )
  ```
- **Regex:** `^v_[A-Za-z0-9_-]+$`
- **Example:** `<body data-disco-version="v_2026_06_30_0007"> … </body>`
- **Maps to.** A snapshot produced by `app_snapshot_version` (**EXISTS**) or the host's
  per-render version counter.
- **Emitted by.** Host render only (never the agent, never the kit).
- **Uniqueness.** Exactly one per rendered document; monotonically increasing.
- **Preservation.** Re-stamped on every render (it *must* change when content changes).
  A click-to-edit resolution MUST verify the page's `data-disco-version` matches the
  current source version before editing — a mismatch triggers a **fresh-read** (CD-TOOLS)
  instead of editing a stale span.

### 3.9 `data-disco-media-slot`

- **Meaning.** Marks an image/video placeholder or filled media element. The DOM peer of a
  `ResourceManifest` slot. Used when real assets are missing (honest placeholder, never a
  fake logo/photo).
- **Value grammar:**
  ```
  slot-ref ::= "slot_" slot-name
  slot-name ::= ALPHA *( ALPHA / DIGIT / "_" )
  ```
- **Regex:** `^slot_[A-Za-z][A-Za-z0-9_]*$`
- **Example (empty placeholder):**
  ```html
  <figure data-disco-media-slot="slot_hero_image"
          data-disco-field="appspec:/media/hero">
    <div class="disco-media-placeholder" aria-label="Image slot: hero_image (no asset yet)"></div>
  </figure>
  ```
- **Maps to.** A `ResourceManifest` entry (`RESOURCE_AND_PROVENANCE_SPEC.md`) by
  `slot-name`. When filled, the manifest's `copied_path` (project-relative only) is the
  src. A passing empty-slot render is the oracle `media_slot_placeholder_pass.json`.
- **Emitted by.** App render / starter kits (`image_slot`, `metrics_overlay` kits are
  **GAP**, see `_GROUNDING.md §6`).
- **Uniqueness.** Unique per artifact.
- **Preservation.** `slot-name` immutable; filling/emptying a slot MUST keep the id (it is
  the join key to the manifest).

---

## 4. Resolution algorithm (rendered element → source location)

> Inputs: a clicked/mentioned DOM element `E` and the current artifact version `V`.
> Output: a **resolved edit target** `{kind, file?, span?, appspec_pointer?, anchors[],
> media_slot?, metric_id?}` or `NO_TARGET` (→ no edit affordance).

```
resolve(E, V):
  1. STALE GUARD:
     doc_version = nearest_ancestor(E, "data-disco-version")?.value
     if doc_version != V:                       # preview is stale
        return FRESH_READ_REQUIRED              # CD-TOOLS fresh-read; do NOT edit
  2. SEMANTIC FIELD (preferred):
     f = E.attr("data-disco-field") or nearest_ancestor(E, "data-disco-field")
     if f present and validates(field-ref):
        if f starts with "appspec:":
           target.kind = "appspec"; target.appspec_pointer = json_pointer(f)
        else:
           target.kind = "source"; (target.file, target.span, name) = parse(f)
        goto 6
  3. COARSE FALLBACK (data-oid, EXISTS):
     o = nearest_ancestor(E, "data-oid")        # resolveRef() walk-up, EXISTS
     if o present and o.file is a real workspace file:
        target.kind = "source"; (target.file, target.line) = parse_oid(o)
        target.span = whole_line(target.line)   # coarse; edit still exact_replace
        goto 6
  4. NO resolvable field/oid:
     return NO_TARGET                           # walk-up select ok; NO edit affordance
  6. ATTACH CONTEXT (collected from E and ancestors, nearest-wins):
     target.section     = nearest_ancestor(E, "data-disco-section")?.value
     target.file        = target.file or nearest_ancestor(E, "data-disco-file")?.value
     target.flow        = nearest_ancestor(E, "data-disco-flow")?.value
     target.screen      = nearest_ancestor(E, "data-disco-screen-label")?.value
     target.metric_id   = E.attr("data-disco-metric-id")
     target.media_slot  = nearest_ancestor(E, "data-disco-media-slot")?.value
  7. COLLECT ANCHORS TO PRESERVE (load-bearing):
     target.anchors = all "data-disco-comment-anchor" on E and on every descendant
                      that lies inside target.span      # must round-trip verbatim
  8. VALIDATE file path:
     if target.file is absolute or contains "..":
        return ERROR(absolute_host_path)        # RESOURCE_AND_PROVENANCE_SPEC
  9. return target
```

**Edit dispatch from a resolved target:**

| `target.kind` | Tool used | Rewrite allowed? |
| --- | --- | --- |
| `source` (static.site / prototype / document) | `exact_replace` on `target.file` at `target.span` (CD-TOOLS atomic) | **No** — `EditContract.rewrite_allowed=False` |
| `appspec` (appkit.leadgen) | `app_update_content` / `app_set_tweak` at `appspec_pointer` (AppKit, EXISTS) | **No** |
| `media_slot` | manifest update + render; see `RESOURCE_AND_PROVENANCE_SPEC.md` | n/a |

The host always wraps the edit instruction so it stays **targeted**, e.g.
`"In {file} at {span} (field {name}; preserve anchors {anchors}): {instruction}"` —
mirrors the shipped `steer()` wire (`docs/A1-click-to-edit-design.md §A1.4`).

---

## 5. Comment-anchor preservation (the load-bearing invariant)

Anchors are the one attribute whose loss is silent and expensive (orphaned review
comments). Rules, restated as enforceable checks:

| # | Rule | Detect | Oracle |
| --- | --- | --- | --- |
| C1 | Every anchor in `target.span` before the edit is present after, exactly once. | diff anchor-set(before) vs anchor-set(after) | `comment_anchor_preserved_pass.json` |
| C2 | No anchor appears twice in the whole artifact. | global anchor multiset count == 1 each | `comment_anchor_duplicated_fail.json` |
| C3 | No anchor present at version N disappears at N+1 without an explicit delete event. | set-difference across versions | `comment_anchor_lost_fail.json` |
| C4 | Anchor id format is `^ca_[A-Za-z0-9]{22}$`; never regenerated for the "same" comment. | regex + stable-id ledger | (covered by C2/C3) |
| C5 | Text replacement that empties an element still keeps its anchor. | edit oracle: empty-text + anchor present | `comment_anchor_preserved_pass.json` |

> A correct `exact_replace` (CD-TOOLS) that swaps **only the inner text** of a span
> trivially preserves attributes including the anchor. Anchors break when an edit is
> actually a **rewrite** of the enclosing element/file — which is exactly what
> `targeted_edit_rewrite_fail.json` captures.

---

## 6. Direct-edit override reconciliation

Users can edit *directly* in the preview (drag, inline-type, recolor). Those edits are
**not** agent edits and are persisted out-of-band so a later agent edit cannot silently
erase them.

- **Where user direct edits live:**
  - `direct_edits.json` — structured per-field overrides:
    `{ "<field-ref or #field-name>": { "value": "...", "ts": "...", "by": "user" } }`
  - `overrides.css` — user style overrides keyed by a stable selector
    (`[data-disco-field="…"]` / `[data-disco-section="…"]`).
- **Reconciliation algorithm (agent edit lands on a field with a user override):**
  ```
  on agent_edit(field f, new_value):
    u = direct_edits.json[key_of(f)]
    if u is None:
       apply exact_replace(f, new_value)                 # no conflict
    else if u.value == current_source_value(f):
       # user override already reflected; agent edit supersedes intentionally
       apply exact_replace(f, new_value); record supersede(u)
    else:
       # CONFLICT: user changed it, agent also wants to change it
       DO NOT clobber. Emit a reconciliation record:
         keep user value in source; attach agent's proposed value as a pending
         suggestion on the field; surface both in UI.
       NEVER overwrite u.value silently.
  ```
- **Failure being guarded:** an agent edit overwrites a user's direct edit with no
  conflict record → oracle `direct_edit_clobbered_fail.json` (FAIL verdict). A passing
  reconcile keeps the user value and records the supersede/conflict.
- **CSS overrides** are layered **after** generated CSS at render time (last-wins) and are
  never rewritten by the agent; an agent style change that would contradict
  `overrides.css` is recorded as a conflict, not applied over it.

---

## 7. Targeted-edit oracle expectations (passing vs rewrite)

| Property | Passing targeted edit (`targeted_edit_pass.json`) | Rewrite (FAIL) (`targeted_edit_rewrite_fail.json`) |
| --- | --- | --- |
| Tool | `exact_replace` (CD-TOOLS, atomic) | `file_write` / full `safe_write_file` of entrypoint |
| Bytes changed | only the resolved span | whole file / whole element subtree |
| Lines outside span | **byte-identical** | changed/reflowed |
| Comment anchors | all preserved verbatim (C1–C5) | dropped or duplicated |
| `data-disco-*` ids on untouched elements | unchanged | re-numbered / lost |
| Unrelated sections | unchanged | reformatted |
| `rewrite_allowed` honored | yes (False) | violated |
| Verdict | **PASS** | **FAIL** |

> A passing edit is defined operationally: **the post-edit file equals the pre-edit file
> with exactly one contiguous span replaced.** Anything broader is a rewrite, even if the
> rendered output "looks the same".

---

## 8. Worked end-to-end example

**Scenario:** user clicks the hero headline in a built `static.site` preview and changes
it to "Ship faster with Disco".

1. **Render (host).** The preview-edit route (`A1.1b`, server-stamped) serves:
   ```html
   <body data-disco-version="v_2026_06_30_0007">
     <section data-disco-section="hero" data-disco-file="index.html">
       <h1 data-disco-field="index.html:12:1-12:24#hero_headline"
           data-disco-comment-anchor="ca_01HZX4P7Q2R8S3T9V6W1Y0">Launch faster with Disco</h1>
     </section>
   </body>
   ```
2. **Click.** User clicks the `<h1>`. The selection overlay (A2, **EXISTS**) captures `E`.
3. **Resolve** (`resolve(E, V=v_2026_06_30_0007)`):
   - Stale guard: page `data-disco-version` == current `V` → proceed.
   - `data-disco-field` present + valid → `kind=source`,
     `file=index.html`, `span=12:1-12:24`, `name=hero_headline`.
   - Context: `section=hero`, `file=index.html`.
   - Anchors in span: `["ca_01HZX4P7Q2R8S3T9V6W1Y0"]`.
   - Path check: relative, no `..` → ok.
4. **Affordance.** Target is resolvable → inline edit input appears (no false affordance).
5. **Edit instruction → steer** (A1.4 wire, **EXISTS**):
   `"In index.html at 12:1-12:24 (field hero_headline; preserve anchor
   ca_01HZX4P7Q2R8S3T9V6W1Y0): change to 'Ship faster with Disco'"`.
6. **Override check.** `direct_edits.json["#hero_headline"]` is absent → no conflict.
7. **Apply.** `exact_replace(index.html, "Launch faster with Disco" @12:1-12:24,
   "Ship faster with Disco")` — atomic, CD-TOOLS. Only that span changes.
8. **Re-render.** Host re-stamps; new `data-disco-version="v_2026_06_30_0008"`; the
   `<h1>` now reads "Ship faster with Disco" and **still carries**
   `data-disco-comment-anchor="ca_01HZX4P7Q2R8S3T9V6W1Y0"` (C1 satisfied).
9. **Oracle check.** Post-file == pre-file with one span replaced; anchor preserved →
   matches `targeted_edit_pass.json` + `comment_anchor_preserved_pass.json`. A run that
   instead rewrote `index.html` would match `targeted_edit_rewrite_fail.json` /
   `comment_anchor_lost_fail.json`.

---

## 9. Failure modes

- [ ] **Stale resolution** — editing against a span from an older `data-disco-version`
  (no fresh-read guard) → wrong bytes edited.
- [ ] **Rewrite masquerading as edit** — whole-file `file_write` instead of
  `exact_replace`; passes visual check but fails the byte-diff oracle.
- [ ] **Comment-anchor loss** — anchor dropped during edit → orphaned review comment.
- [ ] **Comment-anchor duplication** — same `ca_…` on two elements → ambiguous comment.
- [ ] **Anchor regeneration** — a lost anchor re-created under a new id → comment still
  orphaned, now silently.
- [ ] **Direct-edit clobber** — agent overwrites a user's `direct_edits.json` value with
  no conflict record.
- [ ] **Override drift** — `overrides.css` rewritten/ignored by the agent.
- [ ] **Absolute / `..` path in `data-disco-file`** — escapes the workspace.
- [ ] **False affordance** — edit UI shown on a non-resolvable element (no
  `data-disco-field`, no real-file `data-oid`).
- [ ] **Agent-emitted `data-disco-*`** — the agent hand-writing stamper-owned attributes
  (must be host/render-emitted only).
- [ ] **Mutated immutable id** — a `section-id` / `metric-id` / `slot-id` / anchor changed
  by an edit.
- [ ] **appspec vs source confusion** — an appkit field edited via raw `file_write`
  instead of `app_update_content` (violates governed artifact routing, CD-TOOLS).

---

## 10. Tests required

- [ ] **Grammar validators** — for each `data-disco-*`, a property test that the regex/BNF
  accepts the valid examples here and rejects: absolute paths, `..`, empty ids, wrong
  prefix, and (for anchors) wrong length.
- [ ] **Resolution unit tests** — `resolve(E, V)` returns the correct target for: direct
  field hit, ancestor field hit, `data-oid` fallback, no-target, stale-version
  (FRESH_READ_REQUIRED), absolute-path error.
- [ ] **Anchor preservation** — golden oracles: `comment_anchor_preserved_pass.json`,
  `comment_anchor_lost_fail.json`, `comment_anchor_duplicated_fail.json` (C1–C5).
- [ ] **Targeted-vs-rewrite** — `targeted_edit_pass.json` (one-span byte-diff) vs
  `targeted_edit_rewrite_fail.json` (whole-file change); assert verdicts.
- [ ] **Direct-edit reconciliation** — `direct_edit_clobbered_fail.json` (FAIL) plus a
  passing reconcile fixture (user value kept + conflict recorded).
- [ ] **Path safety** — any `data-disco-file` resolving outside the workspace →
  `absolute_host_path_fail.json` verdict (shared with
  `RESOURCE_AND_PROVENANCE_SPEC.md`).
- [ ] **No false affordance** — render a non-stamped element; assert no edit affordance,
  walk-up select still available.
- [ ] **Idempotent stamping** — re-stamping a stamped doc is a no-op (reuses A1.1 stamper
  unit-test property, **EXISTS**).
- [ ] **End-to-end (live, host-driven)** — the §8 round-trip on a real static site:
  click → resolve → `exact_replace` → re-render → anchor preserved (a *live model* run is
  the only proof the feature works; oracle fixtures prove "didn't regress").


---

<!-- ============================================================ -->
# [12] docs/artifact-vm/RESOURCE_AND_PROVENANCE_SPEC.md

<!-- ============================================================ -->

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


---

<!-- ============================================================ -->
# [13] docs/artifact-vm/CONTENT_AND_DESIGN_DISCIPLINE_SPEC.md

<!-- ============================================================ -->

# Content & Design Discipline Spec — `ContentStyle`, content provenance, design-slop lint

> **Read `_GROUNDING.md` first.** Documentation only — no runtime behavior implemented or
> claimed. This spec defines (1) `ContentStyle` as a value object, (2) the content-
> provenance tag system that keeps every word/number sourced, and (3) the design-slop lint
> that catches the visual tells of AI filler — plus the rationale override mechanism that
> lets an intentional design decision downgrade a finding with an audit trail.
>
> Cross-references: `DIRECT_MANIPULATION_SPEC.md` (`data-disco-metric-id` carries a
> provenance tag; `data-disco-field` surfaces it in UI), `RESOURCE_AND_PROVENANCE_SPEC.md`
> ("no fake logos/photos/testimonials", media slots for missing assets), `_GROUNDING.md`
> (TweakSpec §7, brand registry, real names), and oracle fixtures in `fixtures/oracles/`.

---

## 0. Status legend

| Marker | Meaning |
| --- | --- |
| **(EXISTS)** | Real today. Referenced, not re-specced. |
| **(GAP)** | New surface proposed by this pack. Not implemented. |

`ContentStyle`, the content-provenance tags, the design-slop lint, and the
`DesignSpec`-rationale downgrade are all **(GAP)**. They build on **(EXISTS)** substrate:
TweakSpec (`tweaks.py`, `_GROUNDING.md §7`), the brand registry (`kits/brand.py`), and
the `data-disco-*` grammar (`DIRECT_MANIPULATION_SPEC.md`).

---

## 1. Content-tag table (read this first)

Every user-visible content unit (a `data-disco-field`, a `data-disco-metric-id`, a media
slot) carries exactly one **provenance tag**. Tags are stored per-field in
`.disco/content_provenance.json` (keyed by `data-disco-field` `#field-name` /
`data-disco-metric-id`) and surfaced in the editor UI as a small badge on each field.

| Tag | Semantics | Where stored | Surfaced in UI as | Export gate |
| --- | --- | --- | --- | --- |
| `provided_by_user` | Verbatim or lightly-edited user-supplied content. | `content_provenance.json[field]` | green "from you" badge | always allowed |
| `derived_from_research` | Synthesized from a cited, retrievable source (`search`/`extract`, `_GROUNDING.md §4`); MUST carry a `source_ref`. | `content_provenance.json[field].source_ref` | blue "researched" badge w/ source link | allowed if `source_ref` resolves |
| `generated_placeholder` | Model-written stand-in (lorem-ish, sample copy, sample stat). | `content_provenance.json[field]` | amber "placeholder" badge | **flagged** at export; blocked for stats/logos/testimonials |
| `requires_user_input` | A field/slot the user must fill (no honest content possible yet). | `content_provenance.json[field]` | red "needs you" badge | **blocked** from a "final" export; allowed as draft |

**Rules:**
1. Every `data-disco-metric-id` (a stat/number) MUST be `provided_by_user` or
   `derived_from_research`. A `generated_placeholder` stat is a **fake stat** (lint
   `fake-stats`, §3) — never ship it as real.
2. Testimonials/logos follow `RESOURCE_AND_PROVENANCE_SPEC.md §R10`: fabricated → must be
   `generated_placeholder`/`requires_user_input`, never presented as real.
3. No **filler content** (`generated_placeholder` text that adds nothing) survives a final
   export without being flagged.
4. The tag travels with the field across targeted edits
   (`DIRECT_MANIPULATION_SPEC.md §3.7` preservation).

---

## 2. `ContentStyle` value object

A frozen value object (proposed Pydantic v2, `extra="forbid"`, mirroring the contract
value objects in `_GROUNDING.md §2`) capturing the intended voice of an artifact's copy.
Stored at `.disco/content_style.json`; referenced by the prompt-pack assembler
(`workflows/assembly.py`, **EXISTS**) so the model writes copy in-style.

| Field | Type | Required | Allowed values / meaning |
| --- | --- | --- | --- |
| `tone` | enum | yes | `professional` \| `friendly` \| `playful` \| `authoritative` \| `technical` \| `minimal` |
| `density` | enum | yes | `terse` \| `balanced` \| `detailed` — words-per-section budget. |
| `voice` | enum | yes | `first_person` \| `second_person` \| `third_person` \| `brand` (brand-registry voice, `kits/brand.py`). |
| `audience` | string | yes | Free-text audience descriptor, e.g. "small-business owners", "developers". |
| `reading_level` | enum | no | `general` \| `expert` (default `general`). |
| `banned_phrases` | tuple<string> | no | Phrases to avoid (e.g. "unlock", "seamless", "game-changer"). |

**JSON example:**
```json
{
  "tone": "friendly",
  "density": "balanced",
  "voice": "second_person",
  "audience": "small-business owners",
  "reading_level": "general",
  "banned_phrases": ["unlock", "seamless", "supercharge"]
}
```

**Invariants:** `tone`/`density`/`voice` required; `audience` non-empty; `banned_phrases`
de-duplicated. `ContentStyle` constrains generation but does **not** override provenance
rules — style never licenses a fake stat.

---

## 3. Design-slop lint

A static linter over rendered HTML/CSS + the `data-disco-*` grammar. Each rule has a
detection heuristic, severity, and a `downgradable?` flag (whether a `DesignSpec`
rationale may lower it — §4). Run in the verify stage (alongside `verify_web_app`,
**EXISTS**, `_GROUNDING.md §4`).

### 3.1 Lint-rule table

| Rule id | Heuristic | Severity | Downgradable? |
| --- | --- | --- | --- |
| `generic-gradients` | CSS `linear-gradient(...)` matching the cliché palettes (purple→blue `#667eea`/`#764ba2`-family, "indigo→violet") used on hero/background; ≥1 occurrence. | warn | yes |
| `emoji-as-icons` | An emoji char (Unicode `Emoji_Presentation`) used as a UI affordance/icon inside a button/link/list-marker (not in prose). | warn | yes |
| `left-border-cards` | A card/box styled with only `border-left: Npx solid …` as its accent (the "callout bar" cliché), ≥2 instances. | info | yes |
| `fake-stats` | A `data-disco-metric-id` tagged `generated_placeholder`, OR a number-bearing element with no metric-id and no provenance, esp. round/implausible ("10,000+", "99.9%"). | **error** | **no** |
| `fake-testimonials` | A testimonial-pattern block (quote + attributed name/role/company) whose attribution has no `ResourceManifest`/provenance backing. | **error** | **no** |
| `overused-generic-fonts` | Body/heading font stack relying on a default system/Google cliché ("Inter"/"Roboto"/"Poppins") AND no brand-registry font declared. | info | yes |
| `inline-spacing-not-gap` | Layout spacing done via per-child `margin`/`<br>`/`&nbsp;` runs or empty spacer divs instead of `flex`/`grid` + `gap`. | warn | yes |

### 3.2 Per-rule detail

#### `generic-gradients` (warn, downgradable)
- **Detect.** Parse CSS; match `linear-gradient`/`radial-gradient` whose stops fall in the
  flagged cliché set; weight higher on full-bleed hero/background.
- **Example violation.**
  ```css
  .hero { background: linear-gradient(135deg, #667eea 0%, #764ba2 100%); }
  ```
- **Example fix.** Use brand-registry colors (`kits/brand.py`) or a solid/duotone tied to
  the brand: `background: var(--brand-surface);` or a gradient built from brand tokens.

#### `emoji-as-icons` (warn, downgradable)
- **Detect.** Emoji code points inside interactive/affordance elements or as list markers.
- **Example violation.** `<button>🚀 Get started</button>`,
  `<li>✅ Fast</li>`.
- **Example fix.** Use a real icon set / inline SVG: `<button><svg …/> Get started</button>`.

#### `left-border-cards` (info, downgradable)
- **Detect.** Cards whose only visual accent is `border-left`, ≥2 instances.
- **Example violation.** `.card { border-left: 4px solid #6366f1; padding: 1rem; }`
- **Example fix.** Use real card structure (subtle shadow/border-radius/background) or a
  brand accent that isn't the default left-bar cliché.

#### `fake-stats` (error, NOT downgradable)
- **Detect.** Any `data-disco-metric-id` with content tag `generated_placeholder`, or a
  conspicuous statistic with no provenance.
- **Example violation.**
  ```html
  <span data-disco-metric-id="m_users">10,000+ happy customers</span>
  <!-- content_provenance: generated_placeholder -->
  ```
- **Example fix.** Tag from a real source (`provided_by_user`/`derived_from_research` with
  `source_ref`), or convert to a `requires_user_input` field with an honest placeholder.

#### `fake-testimonials` (error, NOT downgradable)
- **Detect.** Quote + attribution pattern with no manifest/provenance backing.
- **Example violation.**
  ```html
  <blockquote>"Disco changed our business!" — Jane D., CEO, Acme</blockquote>
  ```
- **Example fix.** Remove, or replace with a `requires_user_input` testimonial slot
  (honest "add a real customer quote here").

#### `overused-generic-fonts` (info, downgradable)
- **Detect.** Cliché default font stack with no brand font declared.
- **Example violation.** `body { font-family: Poppins, sans-serif; }` with no brand font.
- **Example fix.** Declare the brand-registry font, or a deliberate pairing justified in
  the `DesignSpec`.

#### `inline-spacing-not-gap` (warn, downgradable)
- **Detect.** Spacer divs / `<br><br>` / per-child margins / `&nbsp;` runs used for layout
  instead of `flex`/`grid` `gap`.
- **Example violation.**
  ```html
  <div style="margin-bottom:8px"></div><div style="margin-bottom:8px"></div>
  ```
- **Example fix.** `display:flex; flex-direction:column; gap:8px;` on the container.

---

## 4. `DesignSpec` rationale downgrade (the override mechanism)

A finding on a **downgradable** rule (§3.1) may be lowered (e.g. `warn → info`, or
suppressed to `ack`) when an intentional design decision is recorded in the artifact's
`DesignSpec`. **`error` rules (`fake-stats`, `fake-testimonials`) are NEVER downgradable**
— honesty is not a style choice.

- **Where stored.** `.disco/design_spec.json`, an array of override records.
- **Override record schema:**

  | Field | Type | Required | Meaning |
  | --- | --- | --- | --- |
  | `rule_id` | enum | yes | A downgradable rule id (§3.1). |
  | `selector` | string | yes | The element/CSS selector or `data-disco-*` ref it applies to. |
  | `rationale` | string | yes | Why this is intentional (non-empty, ≥1 sentence). |
  | `downgrade_to` | enum | yes | `info` \| `ack` (cannot raise severity). |
  | `by` | enum | yes | `user` \| `agent` — who decided. |
  | `ts` | string (RFC3339) | yes | When. |

- **Example.**
  ```json
  {
    "rule_id": "generic-gradients",
    "selector": ".hero",
    "rationale": "Brand guidelines specify a purple→blue hero gradient; matches the logo.",
    "downgrade_to": "ack",
    "by": "user",
    "ts": "2026-06-30T12:30:00Z"
  }
  ```
- **Audit trail.** Every applied downgrade is logged with the originating record; the
  lint report shows the original severity, the downgrade, and the rationale. A downgrade
  with an empty/auto-generated rationale is itself a finding (no rubber-stamping). An
  attempted downgrade of an `error` rule is rejected and recorded as a violation.

---

## 5. How content tags reach the UI

- Each `data-disco-field` / `data-disco-metric-id` renders with a small provenance badge
  (§1 table) read from `content_provenance.json`.
- `requires_user_input` fields render the red "needs you" badge AND block a *final*
  export (`RESOURCE_AND_PROVENANCE_SPEC.md §6` / content gate); they are allowed in draft.
- `generated_placeholder` fields render amber and are listed in the export report so the
  user can replace them before shipping.

---

## 6. Failure modes

- [ ] **Fake stat shipped** — a `generated_placeholder`/unsourced number presented as real
  (`fake-stats`, error, non-downgradable).
- [ ] **Fake testimonial shipped** — unbacked quote+attribution (`fake-testimonials`,
  error, non-downgradable).
- [ ] **Fabricated logo/photo as real** — see `RESOURCE_AND_PROVENANCE_SPEC.md §R10`.
- [ ] **Filler content** — `generated_placeholder` prose that conveys nothing, unflagged.
- [ ] **Missing provenance tag** — a content field with no tag in
  `content_provenance.json`.
- [ ] **Real asset missing → no honest slot** — content rendered without a media slot when
  the asset is absent (should be `requires_user_input`, R7).
- [ ] **Improper downgrade** — an `error` rule downgraded; or a downgrade with an empty
  rationale; or a downgrade missing its audit record.
- [ ] **Design slop unaddressed** — generic gradient / emoji-icons / left-border cards /
  cliché fonts / inline spacing with no fix and no DesignSpec rationale.
- [ ] **Style overrides provenance** — `ContentStyle` used to justify fabricated content.
- [ ] **Tag lost on edit** — provenance tag dropped during a targeted edit
  (`DIRECT_MANIPULATION_SPEC.md §3.7`).

---

## 7. Tests required

- [ ] **`ContentStyle` schema** — accepts the §2 example; rejects bad enums, empty
  `audience`, duplicate `banned_phrases`.
- [ ] **Content-tag store** — every content field has exactly one valid tag;
  `derived_from_research` requires a resolvable `source_ref`.
- [ ] **`fake-stats` (error)** — a `generated_placeholder` metric → FAIL; the same number
  tagged `derived_from_research` with a valid source → PASS. Assert NON-downgradable
  (DesignSpec override of `fake-stats` is rejected).
- [ ] **`fake-testimonials` (error)** — unbacked testimonial → FAIL; `requires_user_input`
  slot → PASS. Assert non-downgradable.
- [ ] **Each downgradable rule** — a golden violation fixture trips the rule at its stated
  severity; a matching DesignSpec rationale downgrades it (with audit record); an
  empty-rationale override is itself flagged.
- [ ] **Downgrade audit** — every applied downgrade carries `rule_id`/`selector`/
  `rationale`/`downgrade_to`/`by`/`ts`; report shows original + downgraded severity.
- [ ] **Export gates** — `requires_user_input` blocks a final export but allows draft;
  `generated_placeholder` appears in the export report.
- [ ] **Tag preservation** — a targeted edit preserves the field's provenance tag
  (shared with `DIRECT_MANIPULATION_SPEC.md` targeted-edit oracles).
- [ ] **Honest-placeholder integration** — missing asset → media slot +
  `requires_user_input`, never a fabricated substitute (shared with
  `RESOURCE_AND_PROVENANCE_SPEC.md` `media_slot_placeholder_pass.json`).
- [ ] **End-to-end (live, host-driven)** — a real build that uses `ContentStyle`, sources
  one stat from research, leaves one testimonial as `requires_user_input`, and trips +
  fixes one downgradable slop rule (live run proves it works; fixtures prove no
  regression).


---

<!-- ============================================================ -->
# [14] docs/artifact-vm/MERGE_PLAN.md

<!-- ============================================================ -->

# Merge Plan — Artifact-VM Parity Pack

> **Status: SPEC ONLY — no runtime code changed.**
> This plan describes how the Artifact-VM parity pack merges into Disco **after** the main
> reliability/hardening campaign completes. It is sequenced lowest-risk-first so that no
> step can destabilize the live build loop. Real names per [`_GROUNDING.md`](./_GROUNDING.md);
> tool status per [`TOOL_PARITY_MATRIX.md`](./TOOL_PARITY_MATRIX.md).

## 0. Governing principle

The pack is layered so that **nothing executable lands until everything declarative,
test-only, and data-only has landed and been reviewed first.** Each layer is independently
revertible. The runtime tool layer is gated behind the oracle fixtures acting as acceptance
tests, and behind adversarial Codex/GPT review.

## 1. Merge order (risk-justified)

| Phase | What lands | Why it is at this risk tier |
| --- | --- | --- |
| **1. Specs / docs first** | All of `docs/artifact-vm/*.md` (this pack's specs, matrix, this plan, README) | **Zero runtime risk.** Pure reference text. Nothing imports it; nothing executes it. Landing first gives every later PR a stable citation target and lets reviewers anchor on agreed vocabulary. |
| **2. Fixtures second** | `fixtures/artifact-vm/*.artifact.json` + `fixtures/oracles/*.json` wired into the existing evidence/oracle test suites as **golden inputs** | **Test-data only, no behavior change.** Risk is limited to test-suite breakage (caught in CI). Wiring them now means later runtime changes are guarded by real golden cases from day one. MUST validate JSON shape against the real evidence-classifier input contract before wiring (see §4). |
| **3. Prompt-pack drafts third** | New `build_workflow_output.md` + `build_custom.md`; tightened existing packs against `PROMPT_PACK_REQUIREMENTS.md` | **Low risk — packs are data loaded at runtime, not code.** A malformed pack fails the `prompt_pack.REQUIRED_SECTIONS` validator at load (fail-closed), so a bad pack cannot silently corrupt a build. Must close the two GAP packs (grounding §5) and keep all 11 required sections. |
| **4. Starter-kit data fourth** | Add the 12 missing kits to `kits/starter.py` + `scaffold_starter`, validated by `fixtures/starter-kits/**` | **Contained risk — additive kit data.** New kit ids are opt-in via contract `starter_kit`. The hard constraint: do **not** alter `app_shell` or `lead_form` output (they back live `static.site` and `appkit.leadgen` builds). New kits are validated against the reference HTML fixtures. |
| **5. Runtime tool layer LAST** | `artifact_*` façade router, `show_artifact_to_agent`/`show_artifact_to_user`, `present_artifact_for_download`, verifier `eval_js`, `read_skill_prompt` mounting, image tools, handoff package | **Highest risk — new executable surface that touches dispatch, routing, preview, and verifier scope.** Only after reliability hardening completes. Gated by the oracle fixtures (Phase 2) as acceptance tests and by adversarial review (§3). Each tool ships behind its own toggle, default-off for capable models per the gate-weak-model-assists discipline. |

## 2. Which main-campaign phases each piece accelerates

| Pack piece | Accelerates campaign phase | How |
| --- | --- | --- |
| Prompt-pack drafts + `PROMPT_PACK_REQUIREMENTS.md` | **P3 (prompt packs)** | Closes the `build_workflow_output` / `build_custom` GAPs and gives P3 a pinned section contract + draft text to harden. |
| Starter-kit catalog + reference HTML | **P7 (scaffold)** | Turns `scaffold_starter` from 2 kits into a full catalog; reference HTML is the acceptance target for each new kit. |
| `artifact_*` façade + `dc_set_props` mapping | **P8 (semantic manipulation)** | Defines the unifying edit surface over `file_*`/`app_*` so P8 has a single semantic-edit contract to implement. |
| TweakSpec mapping (`app_set_tweak`, grounding §7) | **P9 (TweakSpec)** | Documents the grounding-law (tweak controls behavior OR ≥1 field; text/color ≥2) so P9 validation has a spec to test against. |
| Export pipeline spec + `present_artifact_for_download` | **P10b (export)** | Builds on the live `DeliverableEvent` bytes flow P10b proved; specifies the per-file download affordance on top. |
| Tool parity matrix + oracle fixtures | **CD-TOOLS** | Confirms CD-TOOLS 1..10 close all P0 lifecycle gaps; oracle fixtures become regression guards for governed routing + verifier-only scope. |

## 3. What MUST be reviewed by Codex/GPT adversarially before merge

Per the bias-free dual-investigation discipline, the following get an adversarial Codex/GPT
pass **before** any runtime step (Phase 5), and the oracle/fixture wiring (Phase 2) gets one
too:

1. **`artifact_*` façade router** — prove it cannot shadow or mis-dispatch an existing
   kind-specific tool (see §5 collision points). Adversary tries to make the façade route an
   appkit edit to a `file_*` tool and vice-versa.
2. **New finalizers** (`build_workflow_output` / `build_custom` finalizer strings) — prove
   they still match `^ready_for_[a-z0-9_]+_verification$` and route host-side, not as
   model-callable builtins.
3. **Oracle fixtures** — adversary validates each fixture's `_meta` expected verdict against
   the *real* evidence-classifier output shape, not an assumed one.
4. **Starter-kit additions** — adversary diffs `app_shell`/`lead_form` output before/after to
   prove zero behavior drift.
5. **Verifier `eval_js`** — adversary attempts scope escape (build role calling the
   verifier-only probe; in-page script reaching outside the artifact).
6. **`present_artifact_for_download` / handoff package** — adversary checks for secret/path
   leakage in the emitted bundle (provenance must not embed provider keys).

## 4. What NOT to merge blindly

- **The `artifact_*` façade could collide with existing kind-specific tool dispatch.** Do not
  register façade names that the `ToolExecutor` could resolve ambiguously against `file_*` /
  `app_*` / `slides_generate` / `deck_patch`. The façade must be a *thin router that delegates
  to the contract-selected concrete tool*, never a second independent implementation.
- **New finalizers must keep the regex contract.** Any new `ready_for_*_verification` string
  must match `^ready_for_[a-z0-9_]+_verification$` and be host-routed via
  `VerificationContract.finalizer` — never exposed as an ordinary builtin in `files.py`.
- **New starter kits must not change `app_shell` / `lead_form` behavior.** Those two kits
  back live builds. Additions are purely additive; a byte/structure diff of the two existing
  kits' scaffold output must be empty post-merge.
- **Oracle fixtures must be validated against the real evidence-classifier shape before
  wiring.** Do not assume the verdict schema; confirm against the production classifier's
  input/output contract first, then wire. A fixture with the wrong shape silently passes
  while testing nothing.
- **Do not enable any Phase-5 tool on capable models by default.** Per the
  gate-weak-model-assists rule, new compensating surfaces are toggle-gated, default-off for
  capable models.

## 5. Possible conflicts with existing CD-TOOLS work

CD-TOOLS already landed **governed artifact routing** and **verifier-only read scope**
(grounding §9). Concrete collision points and avoidance:

| Collision point | Conflict with CD-TOOLS | How to avoid |
| --- | --- | --- |
| `artifact_*` façade vs governed artifact routing | Façade could route an entrypoint edit around the CD-TOOLS governed path (raw write of `index.html`/`appspec.json`) | Façade MUST delegate to the same governed routing CD-TOOLS installed; add an oracle fixture asserting entrypoint edits still go through edit tools, not `file_write`. |
| `show_artifact_to_agent` / verifier `eval_js` vs verifier-only read scope | New read/inspect surfaces could widen the build role's read scope that CD-TOOLS narrowed | Keep agent-private show + `eval_js` under the *same* verifier-only scope; build role gets render output but not raw file reads outside the artifact root. |
| New finalizers vs finalizer regex contract | A new pack's finalizer that doesn't match the regex breaks host routing | Validate every new finalizer string against `^ready_for_[a-z0-9_]+_verification$` in CI before merge. |
| `safe_write_file` clobber guard vs new starter kits | A new kit could declare an entrypoint that `safe_write_file` then refuses to write during scaffold | Scaffold path must use the scaffold writer, not `safe_write_file`; confirm `scaffold_starter` is exempt from the entrypoint-clobber guard for first-write only. |
| `exact_replace` fresh-read guard vs `artifact_*_str_replace` façade | Façade str-replace could bypass the fresh-read guard and reintroduce Mode-B thrash | Façade str-replace MUST call `exact_replace`/`file_str_replace` underneath so the fresh-read guard + no-elision discipline still apply. |

## 6. Per-deliverable merge checklist

| Deliverable | Merge phase | Risk | Reviewer | Blocking dependency |
| --- | --- | --- | --- | --- |
| `_GROUNDING.md` | 1 (docs) | none | Disco maintainer | — |
| `DISCO_ARTIFACT_VM_SPEC.md` | 1 (docs) | none | Disco maintainer | grounding |
| `TOOL_PARITY_MATRIX.md` | 1 (docs) | none | Disco maintainer | grounding |
| `ARTIFACT_TOOL_CONTRACTS.md` | 1 (docs) | none | Codex adversarial (façade) | matrix |
| `PROMPT_PACK_REQUIREMENTS.md` | 1 (docs) | none | Disco maintainer | grounding §5 |
| `STARTER_KIT_CATALOG.md` | 1 (docs) | none | Disco maintainer | grounding §6 |
| `EXPORT_PIPELINE_SPEC.md` | 1 (docs) | none | Disco maintainer | grounding §8 |
| `CONTEXT_AND_ITERATION_SPEC.md` | 1 (docs) | none | Disco maintainer | — |
| `DIRECT_MANIPULATION_SPEC.md` | 1 (docs) | none | Codex adversarial | TweakSpec §7 |
| `RESOURCE_AND_PROVENANCE_SPEC.md` | 1 (docs) | none | Codex adversarial (leak) | — |
| `CONTENT_AND_DESIGN_DISCIPLINE_SPEC.md` | 1 (docs) | none | Disco maintainer | — |
| Artifact fixtures (7) | 2 (fixtures) | low (test data) | Codex adversarial | evidence-classifier shape validated |
| Oracle fixtures (14) | 2 (fixtures) | low (test data) | Codex adversarial | real evidence-classifier shape |
| Starter-kit reference HTML (10) | 2 (fixtures) | low | Disco maintainer | — |
| `build_workflow_output.md` / `build_custom.md` packs | 3 (prompt) | low (fail-closed loader) | Codex adversarial (finalizer regex) | `PROMPT_PACK_REQUIREMENTS.md` |
| 12 new starter kits (`kits/starter.py`) | 4 (data) | contained | Codex adversarial (no app_shell/lead_form drift) | starter-kit fixtures + catalog |
| `artifact_*` façade router | 5 (runtime) | high | Codex adversarial | reliability campaign done; oracle fixtures wired |
| `show_artifact_to_agent` / `show_artifact_to_user` | 5 (runtime) | high | Codex adversarial (scope) | verifier-only scope preserved |
| `present_artifact_for_download` / handoff | 5 (runtime) | high | Codex adversarial (leak) | P10b DeliverableEvent flow |
| verifier `eval_js` | 5 (runtime) | high | Codex adversarial (scope escape) | verifier-only scope |
| `read_skill_prompt` skills mounting | 5 (runtime) | medium | Disco maintainer | prompt assembly |
| image tools (`image_metadata`/`view_image`/`image_slot`) | 5 (runtime) | medium | Disco maintainer | image pipeline |

**Hard gate:** no Phase-5 row merges until (a) the reliability/hardening campaign is
complete, (b) Phases 1–4 are landed and green, and (c) the adversarial Codex/GPT review in
§3 has signed off. See [`README.md`](./README.md) for the pack index.
