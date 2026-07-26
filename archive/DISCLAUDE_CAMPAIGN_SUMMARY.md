> **SUPERSEDED / HISTORICAL (as of 2026-07-07).** Old disclaude campaign summary, pinned to an experimental-branch HEAD.
> Historical only. Not a source of current status or operating instructions.

# Disclaude Campaign — Work Summary

**Repo:** `/home/dylan/projects/disclaude` (isolated experimental clone of Disco-Pi)
**Branch:** `disclaude/experimental-20260628T025508Z`
**Range:** `5d281625` (bootstrap) → `e3d03d1e` (HEAD) — **29 campaign commits**, 169 files touched.
**Process:** every PR ran through a binding **Codex read-only review gate** (plan → APPROVE → implement →
tests + `basedpyright` strict → code review → APPROVE → commit). The reviewer was `gpt-5.3-codex-spark`
through P4, then **`gpt-5.5`** from P5 onward (spark hit its usage limit; the `-codex` model names are
unsupported on this ChatGPT account — only plain `gpt-5.5`/`gpt-5.4` work).

**Thesis being built:** turn Disco Build into a *host-owned artifact runtime* —
context → contract → prompt-pack → starter/brand kit → specialized mutation tools → preview/show/deliver →
verification finalizer → export → product-harness proof. Chat = control plane, project FS = memory, the
artifact contract = working context, the verifier is separate, the event log is the audit trail.

**Hard constraint (carried throughout):** the final soak (P17) must use **MiniMax-M3 via the direct MiniMax
API only** — never OpenRouter, never any other model. Zero-OpenRouter proof is required.

---

## Phase status at a glance

| Phase | Title | Status |
|---|---|---|
| **P0** | Context Runtime | ✅ complete (CXT-1…7) |
| **P0B** | Lifecycle & Safety | ✅ complete (LIFE-3 built; LIFE-1/2/4/5 verified pre-hardened) |
| **P1** | Product Harness / Oracles | ✅ headless layer complete (HARN-1a/1b/2/3) |
| **P2** | Contract Runtime | ✅ complete + **live-enforced** (CONTRACT-1/2/3 + ENFORCE + ACTIVATE) |
| **P3** | WorkflowPromptPack | ✅ complete (WPP-1/2/3) |
| **P4** | Specialized Mutation Tools | ✅ AppKit set (TOOL-1) |
| **P5** | Preview / Show / Delivery | ✅ complete (P5-DELIVERY) |
| **P6** | Verification Finalizers | ✅ complete (P6-FINALIZERS) |
| **P7** | Starter / Brand / UI Kits | ⏳ implemented + tested, **committed CODE-GATE-PENDING** |
| **P8–P17** | (semantic direct-manip → soak) | ☐ not started |

---

## P0 — Context Runtime (CXT-1 … CXT-7)

The durable working-memory substrate: an event log + a `ContextPack` (the model's current view) + recoverable
compression instead of destructive elision.

- **CXT-1 — core context models.** `ledger.py` (ContextLedger), `pack.py` (ContextPack), `compaction.py`
  (CompactionPolicy + `context_mark_resolved` / `context_write_summary` / `context_compact_if_needed`),
  `source_priority.py` (documented precedence order), `artifact_memory.py`.
  - *Codex REVISE fixes:* pinned the enum base to match the repo house style `(str, Enum)`; pinned the exact
    `SourcePriority.default()` precedence sequence + asserted every member is ordered.
- **CXT-2 — durable `.disco/context/*` files + `context_memory` tool.** `ArtifactMemoryStore` (WorkspaceFS
  Protocol, `ReconstructResult`, `_MD_KINDS`/`_JSON_KINDS`/`_SINGLETON_KINDS`); verifier failures stored as
  `.json`. Tool registered in AGENT + ARTIFACT scopes.
- **CXT-3 — agent-driven deferred snip.** Reused the existing `CondensationEvent` as the tombstone (View.of
  omission + `recover_span`) rather than inventing a second forgetting path; added only the agent-intent layer
  (`ContextResolvedEvent` / `ContextSummaryEvent`, mirrored in `frontend/.../eventDisposition.ts` so the
  frontend event-kind contract test stayed green).
- **CXT-4 — `ContextPack` assembler + byte-stable renderer.** `build_context_pack(...)` + `render_context_pack`
  (byte-stable `<context-pack>` block) + `render_plan_as_todo_markdown`.
  - *Codex REVISE fix:* `failures or led.latest_verifier_failures` silently ignored an explicit empty tuple →
    changed to `failures: tuple | None = None` + `failures if failures is not None else …` + a regression test.
- **CXT-5 — recoverable compression (no destructive elision).** `recoverable_excerpt` +
  `DESTRUCTIVE_ELISION_MARKERS` + `scan_for_destructive_elision`, wired into the harness `OutputTruthOracle`.
  - *Codex REVISE fixes:* Codex caught a **missed destructive elision site** (`bootstrap.py` "… (truncated)")
    and that `OutputTruthOracle` already existed (so I wired the scan immediately instead of deferring) — fixed
    both + added harness regression tests.
- **CXT-6 — `todo.md` as live execution memory.** Plan approval seeds `current_goal.md` + `todo.md`.
- **CXT-7 — context runtime integration (the P0 gate).** `_seed_context_from_plan` called from BOTH the
  interactive `approve_plan()` AND the autonomous `_gate_planning_mode` path.
  - *Codex REVISE fix:* I had only hooked the interactive path; Codex caught the missing autonomous path →
    added the seed there + an autonomous test. Also narrowed a pre-existing `engine.py` `reportAssignmentType`
    in the DoD `file_preds` comprehension.

## P0B — Lifecycle & Safety (LIFE-3)

Audit found LIFE-1/2/4/5 were already hardened in the clone (verified green). The one real gap:
- **LIFE-3 — auto-suspend active-work guard.** `_has_active_work(conversation_id)` (a live run task in
  `self._rt._tasks` OR a live Pi sidecar session) guards both `_suspend` AND `sweep_idle_once` so an
  in-progress build/agent run is never reaped mid-work.

## P1 — Product Harness / Oracles (HARN-1a, 1b, 2, 3)

Headless, zero-opinion oracles + the MiniMax-only enforcement, all buildable without a live browser.

- **HARN-1a — provider-call ledger + `ProviderLedgerOracle`.** Tolerant relay-log parser; the oracle is
  OPT-IN via scenario assertions and **fail-closed** (required-but-absent / empty / malformed →
  `MISSING_REQUIRED_EVIDENCE` → `INVALID_RUN`); enforces forbidden-host (default `openrouter`) / required-host /
  model / after-terminal — the mechanism that will prove "zero OpenRouter" at P17.
  - *Codex REVISE fixes:* `None`-vs-`[]`-vs-malformed conflation collapsed into a fail-closed
    `MISSING_REQUIRED_EVIDENCE`; `classify_run_folder` made crash-proof (tolerant `_read_ledger`, malformed line
    → hostless record, never raises); expanded parser tests.
- **HARN-2 — 8 browser product-harness oracles.** `BrowserWS / Lifecycle / SidecarStop / PreviewOwnership /
  ShowToUser / VerificationGate / ExportDownload / Cleanup` over a `product_evidence` dict; each SKIPs without
  its slice; fail-closed via a safe `_int()` and explicit-positive reads (`owner=='platform'`,
  `workspace_released is True`, …).
  - *Codex REVISE fixes:* `product_evidence` wasn't wired into the live classify path (`run.py classify_dossier`
    + `classify_run_folder` now read `product-evidence.json`); an `int("n/a")` crash → safe `_int()`; a
    false-PASS on partial slices → explicit-positive reads.
- **HARN-3 — centralized product promotion policy.** `evaluate_product_promotion(classifications, *,
  product_harness, require_product_harness)` → `{eligible, checks}`.
- **HARN-1b (evidence) — validated product-evidence + ledger writer.** `validate_product_evidence` +
  `write_product_evidence(strict=True)` + `write_provider_ledger`. (The *live* Playwright harness is a tracked
  follow-up — it genuinely needs a running frontend+agent-server stack, so I deliberately did not write an
  unrunnable stub.)
- Added `DESTRUCTIVE_ELISION` (P1) + the P0 provider codes (`PROVIDER_FORBIDDEN` / `PROVIDER_WRONG_MODEL` /
  `PROVIDER_CALL_AFTER_TERMINAL`) + 8 browser codes to `failure_codes.py`.

## P2 — Contract Runtime (CONTRACT-1/2/3 + ENFORCE + ACTIVATE)

The Build Artifact Contract: every run declares what it produces, the tools it may use per phase, how it's
verified, and how it's exported — and that is now **hard-enforced at the tool-dispatch boundary**.

- **CONTRACT-1 — core models.** `ContractKind`, `VerificationLevel`, `ToolPack`, `EditContract`,
  `VerificationContract` (finalizer regex `^ready_for_[a-z0-9_]+_verification$`), `ExportContract`,
  `ArtifactContract`, `BuildContract` (+ `minimal()` factory).
  - *Codex REVISE fix:* `BuildContract` didn't enforce `kind == artifact.kind` → added a `model_validator`
    (`_kind_coherent`) + the finalizer-convention `field_validator` + tests.
- **CONTRACT-2 — `BuildContractRegistry`.** Per-kind built-ins + `get_for_brief(brief, *, strict_kind=True)`
  (missing kind → CUSTOM, present-but-malformed → `ValueError`, opt-out via `strict_kind=False`).
  - *Codex REVISE fixes:* the appkit contract referenced **non-existent `app_*` tools** (a false affordance) →
    scoped it to only registered tools until P4; `get_for_brief` could `KeyError` on a partial registry → a
    `_custom()` fallback; added `test_all_builtin_contract_tools_are_registered` (no contract may scope a tool
    that doesn't ship).
- **CONTRACT-3 — Contract→ToolScope compiler.** `compile_tool_scopes(contract)` → hard per-phase allowlists
  (bootstrap/edit/repair/verify/export). Proves the invariant: a tool absent from a phase pack is hard-excluded.
  - *Codex REVISE fixes:* added a `shell` hard-exclusion test (negative + paired positive); explicitly gated
    the PR as **compiler-only** with the executor-enforcement follow-up recorded in the spine (not silently
    dropped).
- **CONTRACT-ENFORCE — executor-side per-phase scope guard (mechanism).** `enforce.py`:
  `decide_tool_in_scope` + `ContractScopeGuard`; the universal `executor.execute()` chokepoint denies an
  out-of-phase tool before it runs. **Metadata-driven** — a non-`read_only` tool is governed whatever its name
  (no name-list bypass); the verify finalizer always passes (it's a control signal).
  - *Codex REVISE fixes (2 blocking):* (1) hardcoded `DANGEROUS_TOOLS` list could be bypassed by an unlisted
    writer → switched to deriving "mutating" from the tool's `read_only` flag (`is_mutating = not read_only`),
    with the name list only a fallback for metadata-less callers; (2) "not wired in production" → the honest
    resolution wasn't to fake a phase but to **build the phase substrate** (next PR). Codex agreed the
    deferral was the only correct way to avoid fake enforcement.
- **CONTRACT-ACTIVATE — build-phase substrate + LIVE wiring.** `BuildPhaseTracker` (a deterministic state
  machine: bootstrap-tool success → EDIT, finalizer → VERIFY, verifier verdict → EXPORT/REPAIR); executor
  gains an `on_tool_success` callback; the runtime resolves a per-conversation `BuildContract` + tracker and,
  at the artifact-mode executor construction, passes `scope_guard` + `on_tool_success` — so a contract-governed
  run now HARD-blocks out-of-phase tools (no raw `file_write` during the edit phase). `forget_conversation`
  evicts the build state.
  - *Codex REVISE fixes (2):* `note_build_verify_result` entry point added for the verify edge; `_build_kind`/
    `_build_trackers` added to `forget_conversation` cleanup. The `verify_web_app`→tracker wire was correctly
    scoped out (artifact mode uses `ARTIFACT_TOOLS`, which excludes `verify_web_app` — that edge belongs to the
    build surface; Codex verified this against the code and approved).

## P3 — WorkflowPromptPack (WPP-1/2/3)

The mode-specific operating manual both kernels run inside.

- **WPP-1 — pack format + 5 bundled packs.** `PromptPack` (frozen, slugged sections) + 11 `REQUIRED_SECTIONS`
  + `parse_prompt_pack` (fence-aware, rejects duplicate sections) + `PromptPackRegistry` (importlib.resources).
  Packs: `build_static_site` / `build_appkit_leadgen` / `build_deck` / `build_document` /
  `build_interactive_prototype`. Coherence enforced: every contract's `prompt_pack` resolves to a complete
  pack that names that contract's exact finalizer.
  - *Codex REVISE fixes:* wheel wouldn't ship the `.md` (pyproject `artifacts` + importlib.resources loader +
    a packaging test); `interactive.prototype` was pointed at `build_static_site` → gave it its **own** pack
    (finalizer mismatch fixed); the appkit pack advertised P4-only `app_*` tools → restricted to registered
    tools; parser hardened (fence-aware + duplicate-section raise); finalizer-coherence test added.
- **WPP-2 — kernel-neutral prompt assembly.** `assemble_workflow_prompt(...)` builds the shared, deterministic
  message list both kernels send: stable prefix → WorkflowPromptPack → ContextPack → recent turns; each part
  once; tool schema left to the driver. (APPROVED clean; the "both kernels call this in the live path" wiring
  is a tracked follow-up.)
- **WPP-3 — skill mount policy (no global skill soup).** `BuildContract.skills` (additive) +
  `resolve_mounted_skills` / `is_skill_mountable` (base default EMPTY → a contract gets only its declared
  skills). A registry invariant test asserts only `appkit.leadgen` declares skills.
  - *Codex REVISE fix:* the required follow-up (live mount-path enforcement) had to be *written* in the spine,
    not just claimed → recorded it explicitly.

## P4 — Specialized Mutation Tools (TOOL-1: AppKit)

An app is a structured `AppSpec` (sections + design tokens + tweaks) rendered deterministically to a
self-contained `index.html`; 8 semantic tools edit the spec, not raw HTML — so every change is targeted.

- `app_create` / `app_update_content` / `app_add_section` / `app_remove_section` / `app_reorder_section` /
  `app_set_design` / `app_set_tweak` / `app_snapshot_version`. Registered + scoped; the appkit contract now
  uses these real tools (raw `file_write` demoted to repair-only).
  - *Codex REVISE fixes (High + Mediums):* **CSS injection** — `render_html` interpolated design tokens into
    `<style>` unescaped → added `_css_safe` sanitization (no style/script breakout); **non-deterministic
    snapshot** — used Python's randomized `hash()` → switched to content-addressed `sha256`; **duplicate
    section ids** — `AppSpec` had no uniqueness check → added a `model_validator` (create persists nothing on
    a dup); **fragile tweak coercion** — only exact lowercase `true`/`false` → robust `_coerce_scalar`
    (case-insensitive bool, int, else str). The dispatch-time edit-vs-repair enforcement was deferred to
    CONTRACT-ENFORCE/ACTIVATE (which then delivered it).

## P5 — Preview / Show / Delivery (P5-DELIVERY)

Audit found the preview + `serve`→`DeliverableEvent` substrate already present and hardened — so this ties
**delivery to the contract**.

- `delivery_mode_for_kind` + `ArtifactContract.delivery_mode` (derived property: `app` = open-in-preview for
  appkit/site/prototype; `files` = download for deck/document/workflow/custom) + `deliverable_kind_matches_
  contract` validator (no wrong-shape handoff — a deck can't be delivered as a runnable app) + runtime
  `expected_delivery_mode(conversation_id)` accessor (build runs only; never fabricates a contract for a plain
  chat).
  - *Codex REVISE fixes:* reworded the scope (the validator *makes shape enforceable*; runtime enforcement is
    a tracked `handle_serve` follow-up — no overclaim); two real coherence gaps it caught are now tracked
    follow-ups — the `serve`/`finish` tool descriptions hardcode port `8000` (conflicts with host-owned
    preview), and `lifecycle._maybe_synthesize_app_deliverable` stamps `artifact_kind="app"` for any
    `index.html` regardless of contract.

## P6 — Verification Finalizers (P6-FINALIZERS)

Fixed a **live false affordance**: the per-kind `ready_for_*_verification` finalizers were named in every
contract/pack but were not real tools. They are now recognized + advertised as a per-kind **alias of the
`finish` virtual tool**, routed through the existing host-truth finish gate (no self-certification, no new
verify logic). `is_finish_tool_name` centralized in `boundaries.py` (single source across dispatch /
advertisement / requery / planning / agent batched-selection); the runtime advertises the alias only for a
resolved non-CUSTOM declared contract.

- *Codex REVISE fixes (3 real bugs it caught):* (1) **planning-gate leak** — the alias name would reach
  `_gate_planning_mode` and emit an `ActionEvent` before the dispatch normalized it, leaking the alias into the
  event log that `signals.py`/stuck-detection read; (2) **batched-call discard** — a batched
  `[ready_for_app_verification, shell]` would let the finalizer shadow and *discard* the real `shell` action;
  (3) **CUSTOM fabrication** — `set_build_kind("custom")` would fabricate `ready_for_artifact_verification`.
  Fix for (1)+(2) was the same elegant move: the **Agent** canonicalizes the alias → `"finish"` in
  `_pick_tool_call` *before any engine processing*, so the real action is preserved and the alias name never
  reaches the planning gate / dispatch / signals / event log. Fix for (3): gate on the *resolved non-CUSTOM*
  contract. Pi finalizer wiring scoped to P15 (documented, not silent).

## P7 — Starter / Brand Kits (P7-KITS) — IMPLEMENTED, CODE-GATE PENDING

Another **false affordance**: the `starter_kit` names (`app_shell` / `lead_form` / `deck_stage`) and the
BrandKit/DesignSpec the packs reference were bare strings with nothing resolving them.

- `core/kits/starter.py` — `StarterKit.scaffold(title)` → `{rel_path: text}`, **path-safe** (rejects
  absolute/`..`); built-ins `app_shell` (renderable inline-CSS shell) + `lead_form` (the AppKit default
  AppSpec, byte-identical to what `app_create` writes — single source).
- `core/kits/brand.py` — `brand_to_appkit_tokens` / `appkit_brand` — a **projection over the existing
  `disco.core.brand` THEMES** into AppKit's `{primary, accent, bg, fg, font}` keys (not a parallel catalog).
- `scaffold_starter` tool — materializes the **active contract's** starter (`ctx.starter_kit`, threaded by the
  runtime) into the workspace; writes only missing files (never clobbers).
- `app_create` now scaffolds its default from the `lead_form` starter (single source, byte-equivalent).
- **Deck reconcile:** the deck contract said `deck.json` but the slides tooling writes the `AuthoredDeck`
  sidecar `deck.authored.json` → aligned the contract `required_files` + the `build_deck` pack to reality;
  removed the unresolvable `deck_stage` starter (slides_generate is the deck materializer).
  - *Codex plan REVISE fixes folded in:* parameterized + path-safe scaffold; lead_form single-source
    byte-equivalence; the brand projection (Codex caught the existing `disco.core.brand` engine — don't
    duplicate it); the deck naming reconcile (Codex caught the real `deck.json` vs `.authored.json`
    inconsistency).
  - **Status:** plan was Codex-APPROVED; implementation is complete + **76 tests green** + basedpyright clean,
    but the Codex *code* gate had not run when the workstation GPU memory leak crashed the desktop. The commit
    is flagged `[CODEX CODE GATE PENDING]` — on resume, run the `gpt-5.5` CODE review of the `e3d03d1e` diff
    before treating P7 as accepted.

---

## Cross-cutting follow-ups (tracked in the spine, none silently dropped)

- HARN-1b **live Playwright** product harness + live provider-ledger population (needs the running stack; P17).
- CXT-4 ContextPack **live prompt-wiring** into the loop; `context_compact_if_needed` live firing.
- WPP-2 **kernel prompt-wiring** (both kernels call `assemble_workflow_prompt` in the live path).
- WPP-3 **skill mount-path enforcement** in the live SkillStore/MCP loader.
- **P5-PORT** — reconcile the `serve`/`finish` port-`8000` guidance with host-owned preview.
- The deliverable **reject-wire** (enforce `deliverable_kind_matches_contract` in `handle_serve` + the
  synthetic-deliverable path).
- **VerificationLevel** STRICT escalation; the build-surface `verify_web_app`→tracker verify edge.
- **Pi finalizer** wiring (P15); the executor-enforcement is live for artifact-mode, build-surface wiring next.

## Engineering notes

- Every PR was gated by an independent adversarial Codex read-only review; the review caught **real** bugs on
  the majority of PRs (CSS injection, non-deterministic snapshots, fail-closed evidence holes, a finalizer
  false-affordance, planning-gate event-log leaks, batched-action discard, CUSTOM-finalizer fabrication,
  packaging gaps, a latent deck data-contract bug, and a "would-be parallel BrandKit catalog").
- `basedpyright` strict ran at **0 new errors** throughout (two pre-existing errors — `ScheduleService.
  fire_now` and `driver.py:291` — were confirmed pre-existing via `git stash` and ignored).
- Deferrals were always **built or explicitly tracked**, never faked — e.g. CONTRACT-ENFORCE shipped the
  mechanism and CONTRACT-ACTIVATE then built the phase substrate to wire it live, rather than hardcoding a
  fake phase.
