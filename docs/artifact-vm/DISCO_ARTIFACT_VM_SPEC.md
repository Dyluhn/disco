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
