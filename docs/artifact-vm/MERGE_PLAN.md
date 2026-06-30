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
