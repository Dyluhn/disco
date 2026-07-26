# Architecture Boundaries

**Status: STABLE / CHANGE-CONTROLLED.** This file is sealed by
`docs/governance/PROTECTED.sha256` and enforced by
`scripts/check_governance_seal.py`. An agent may not edit it. Rebaselining
requires an explicit owner instruction and the procedure in
[`README.md`](./README.md).

This file states **stable architectural invariants** only. It contains no
implementation schedule, no sequencing, and no status. Planned work lives in
[`ARCHITECTURE-ROADMAP.md`](./ARCHITECTURE-ROADMAP.md).

Each invariant below was reconciled against the current code before being
sealed. Symbol and path references are **non-normative navigation aids** — the
invariant is the property, not the filename.

---

## 1. The layering

```text
Build Platform Core
  → Build Profile            (user-facing composition + explicit strictness)
  → Construction Engine      (Freeform flexible | AppKit strict/governed)
  → Target Adapter
  → Preview / Verify / Package / Deploy policies
  → Reference / Starter / Library inputs
```

Dependencies point one way, downward. A lower layer never reaches up into a
higher one to obtain authority.

The repository-wide package layering rule is separate and also binding:

```text
core  ←  retrieval  ←  tools  ←  { agent_server | app_server }
```

`agent_server` and `app_server` are independent top siblings. Dependencies point
downward only; this is machine-enforced, not merely documented.

## 2. Plans are values, never handles

**This is the structural choke point that keeps Core target-neutral.**

The Build Platform contract objects are pure immutable values. They expose no
runtime, store, sandbox, secret, executor, revision-writer, or terminal-status
handle. Engines, adapters, exporters, and connectors may *describe* requested
work; only existing host authorities may execute it or publish success.

Consequences that must remain true:

- A component intent is **evidence of a request**, never an execution handle.
- An adapter cannot smuggle execution authority through its returned plan.
- Therefore a target adapter registers behaviour **without creating a second
  agent loop**. There is one loop.

*Today: `packages/core/src/disco/core/build_platform/contracts.py`.*

## 3. Neutral, open, adapter-owned vocabulary

Core assigns no filename, transport, framework, or platform semantics to a
target's delivery.

- **Delivery shape** is an *open namespaced identifier* carried on the delivery
  intent — not a closed enum. A new target registers a shape without editing
  Core.
- **Entry descriptor** is **opaque and adapter-owned**: a `kind`, a `reference`,
  and deterministic scalar parameters. Core does not interpret the reference.
- **Preview modality** is an *open namespaced identifier* and explicitly
  includes `none`. The `none` modality must not declare entry or readiness work
  — a preview that does nothing must not claim readiness machinery.
- **Readiness signal** is neutral: a `kind` plus deterministic scalar
  parameters. It is not an HTTP-, port-, or browser-specific concept.

Openness is deliberate. Closing any of these into a fixed enum would make Core
non-neutral and is a boundary violation.

*Today: `DeliveryIntent.shape`, `EntryDescriptor`, `PreviewPlan.modality`,
`ReadinessSignal` in `build_platform/contracts.py`.*

## 4. One declared entry per revision

One declared entry for one revision is used consistently by **Open, preview,
verify, export, and deploy**. These surfaces must not each re-derive their own
notion of "the thing we built."

## 5. One composition owner; capabilities only narrow

- There is exactly **one deterministic constructor** of an inspectable Build
  composition. Composition is not assembled ad hoc at call sites.
- Capability resolution is an **intersection across explicit layers**. Each
  layer is a ceiling. A component may *request* capability but may never
  **widen** the host-enforced result.
- A denial at any layer is final. One decision per policy key.

*Today: `resolve_build_composition()` in `build_platform/resolver.py`;
`EffectiveCapabilityPolicy` / `CapabilityLayer` in `contracts.py`.*

## 6. Identity is scoped and event-bound

Distinct identity namespaces exist for release-candidate, run/composition, and
package/artifact identity. They are not interchangeable strings.

- Composition identity is a digest over the validated composition's canonical
  bytes.
- Run-admission identity is derived from that composition digest **bound to a
  durable run-intent event**. Run identity therefore cannot be minted without
  existing event authority, and it introduces no new mutable state.
- Package/artifact identity is carried as an explicit digest reference with a
  declared identity scheme.

*Today: `build_platform/identity.py`
(`build-composition.v1`, `run-admission.v1`).*

## 7. Host owns verdicts and evidence

- A verifier verdict is a **host-owned result**. It is never a field of engine
  or adapter plan output.
- Status projection is truthful: a surface may only report what the responsible
  live authority currently proves.
- Evidence must be bound to the exact immutable state it describes. A mutable
  head, "latest", or a symlink is not a substitute for exact event-bound
  immutable state, and must fail closed rather than silently substitute.

## 8. Append-only event authority

State is a **projection of an append-only event log**, never mutated in place.

- Events are appended; the view folds them into current state. Nothing reaches
  around the projection to mutate state.
- A returned view and the event list it was built from must describe **one
  horizon**. Two different horizons in one response is a defect, not a nuance.
- Derived views project state. They never become a competing mutable truth.

## 9. Host authority survives context handling

- Host-authored typed runtime constraints are host authority. Model prose
  cannot create, extend, or ratify host authority.
- A host capability fact must not silently disappear from model-facing context
  because of summarization or condensation.
- Provider protocol markup and tool-call protocol output are **not ordinary
  prose** and must never be accepted as summary content.
- A transient error must not become a permanent constraint.

## 10. Freeform and AppKit are different compositions

- **Freeform** is permissive and repairable.
- **AppKit** is strict, generated, policy-bound, and must remain so.

Neither may be flattened into the other, and strictness is expressed through
profile/policy/capability/authority — not through special cases scattered in the
loop.

## 11. Targets and hosts

- Next.js is a **first-class target**, not the universal internal architecture.
- Vercel is a **deployment connector**, not a prerequisite. Self-host and Vercel
  consume the same built artifact when using the strict provenance path.
- Only Linux execution availability is universal. WSL2 and macOS hosts **expose
  capabilities**; they do not leak platform checks through Core.
- Mobile and desktop arrive as later **adapters**, not as another lifecycle.

## 12. Build Libraries

Build Libraries eventually contain three distinct input kinds:

- **Reference Packs** — inert knowledge/design/reference material.
- **Starter Recipes** — deterministic initialization.
- **Library Recipes** — declared capabilities and optional authorized code.

Binding rules for all of them:

- progressive disclosure;
- deterministic precedence;
- explicit trust tiers;
- capability intersection (a library may never widen the host policy);
- provenance;
- an eject / no-live-pointer rule.

**User-added references and components must not become ambient prompt soup.**
