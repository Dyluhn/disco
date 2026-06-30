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
