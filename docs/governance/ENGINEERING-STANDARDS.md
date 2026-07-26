# Engineering Standards

**Status: STABLE / CHANGE-CONTROLLED.** This file is sealed by
`docs/governance/PROTECTED.sha256` and enforced by
`scripts/check_governance_seal.py`. An agent may not edit it. Rebaselining
requires an explicit owner instruction and the procedure in
[`README.md`](./README.md).

This file states durable engineering standards only. It intentionally contains
no campaign status, branch name, commit, count, date, or temporary decision.
Those live in the mutable governance files.

---

## 1. Product philosophy

- Build Disco so that capable models can succeed **naturally**. Do not force
  success through one brittle recipe.
- Make the model's job easier with complete bounded context, clear tool
  contracts, useful examples, reliable state, truthful feedback, graceful
  capability fallback, and recovery paths that preserve progress.
- **A model action is a symptom.** "The model failed" is not a root cause. Trace
  the earliest broken product contract across context, provider adaptation,
  tools, lifecycle, authority, evidence, and recovery.
- Do not punish legitimate flexibility to make a benchmark green. Funnel toward
  success with typed capabilities and good affordances — never with
  scenario-specific answers, seed checks, expected-file hacks, hidden retries,
  or framework assumptions.
- Preserve user control and truthful behaviour in this self-hosted product.

## 2. Architecture and implementation

- Keep the system modular and target-neutral: web/Next.js first, with clean
  seams for user-added Build Libraries, references, component packages,
  deployment targets, mobile, and desktop.
- Express strictness as explicit **profiles, policies, capabilities, and
  authority**. Do not scatter Freeform/AppKit/web special cases through the
  loop.
- Keep Freeform flexible and AppKit strict. **Do not flatten one into the
  other.**
- Preserve append-only event authority. Derived views project state; do not
  create competing mutable truth.
- Prefer generic typed contracts and capability negotiation over exact
  error-text parsing.
- Keep one owner for lifecycle, state, effects, composition, completion,
  evidence, and status. **UI state cannot outrank live runtime authority.**
- **No false affordances.** Connected / Open / Ready / Finished mean the
  responsible live authority proves them *now*.
- Do not weaken sandboxing, auth, secret custody, cleanup, AppKit policy, or
  evidence truth to make a lane pass.

## 3. Root-cause and testing discipline

- **Reproduce before changing behaviour** whenever reasonably possible.
- Fix observed causal families, not every imaginable adversarial construction.
- A coherent fix requires:
  1. the narrow positive case,
  2. the meaningful negative control,
  3. relevant focused tests,
  4. broad regression gates proportional to risk,
  5. real live execution where mocks cannot prove the claim.
- Preserve truthful cleanup and inspect/model/provider evidence.
- **Source changes reset counted soak credit.** Never replay an unchanged
  product failure hoping for luck, and never reclassify it to green.
- Harness failures are fixed as *truth-measurement* failures; product failures
  are fixed at the *product contract*. Neither is "pre-existing therefore
  green."
- Verification is finite and practical. **One independent practical review per
  coherent package is enough.** If it finds a material defect, perform one
  coherent correction and recheck. If the package is still structurally wrong,
  redesign it — do not enter an infinite adversarial tail.
- **No ceremony as a substitute for reality.** Hashes identify bytes; tests,
  live behaviour, evidence, and cleanup establish correctness.

## 4. Autonomy

- Do not stop to ask which reversible technical option to choose.
- Record assumptions and decisions while proceeding.
- If infrastructure or one provider is unavailable: preserve the fact, advance
  provider-free work, repair or reroute the infrastructure, and resume. Do not
  claim completion and do not weaken acceptance.
- Do not leave work at "in progress" merely because a package or report ended.
- The current campaign's complete acceptance contract is the sole voluntary
  stop.

## 5. Evidence and reporting integrity

- Evidence generated during counted certification stays **outside** the
  repository, so evidence writing cannot mutate tested source.
- Do not invalidate a run with its own reporting.
- Never overwrite inconvenient history. Historical non-pass attempts are
  preserved honestly.
- Hashes identify bytes and nothing else. A hash is not a correctness claim.
