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
