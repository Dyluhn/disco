# Architecture Roadmap — after the current campaign

**Status: MUTABLE.** This is the post-core architectural sequence. It is *not*
authority for the current campaign, and none of it may be pulled into the active
reliability branch before certification — doing so moves the target and resets
the soak.

## How to read the classifications

Every item is tagged with how much it is actually worth:

| Tag | Meaning |
|-----|---------|
| **[OBSERVED]** | Verified present in current code with tests. Do not reimplement it. |
| **[ACCEPTED-DESIGN]** | Design decided and recorded; implementation not verified present. |
| **[PLANNED]** | Intended work, not yet designed in detail. |
| **[RESEARCH-GATE]** | Open question that must be answered before it can be scheduled. |
| **[OWNER-DECISION]** | Not an engineering call. Requires Dylan. |

**Verify code and tests before calling any item open.** Some Tier-A substrate
already exists from accepted Phase 3; blindly reimplementing a planning document
would destroy working code.

---

## Tier A — first-release platform completion

- **Reconcile/freeze the neutral delivery/entry/preview/readiness vocabulary.**
  **[OBSERVED]** — the neutral vocabulary exists as open namespaced identifiers
  and opaque adapter-owned descriptors, and is sealed in
  [`ARCHITECTURE-BOUNDARIES.md`](./ARCHITECTURE-BOUNDARIES.md) §3.
  *Today: `build_platform/contracts.py`; tests
  `test_build_platform_contracts.py`.*

- **Freeze the single composition owner, capability intersection, and identity
  scopes.** **[OBSERVED]** — one deterministic composition constructor,
  layered capability intersection that can only narrow, and the
  `build-composition.v1` / `run-admission.v1` identity schemes bound to a
  durable run-intent event.
  *Today: `build_platform/resolver.py`, `build_platform/identity.py`; tests
  `test_build_platform_resolver.py`, `test_build_platform_admission.py`,
  `test_build_platform_authority_boundary.py`.*

- **Finish truthful status / no-false-affordance behaviour.** **[PLANNED]** —
  the invariant is sealed (BOUNDARIES §7), but full end-to-end truthful status
  projection across every surface is not proven complete.

- **Treat backup/restore/upgrade/uninstall as product behaviour**, not an ops
  script. **[ACCEPTED-DESIGN]** — backup/restore has accepted Phase-2 evidence;
  upgrade/uninstall as first-class product behaviour is not verified.

- **Certify Linux.** **[PLANNED]** — Linux execution availability is the only
  universal one (BOUNDARIES §11).

- **Keep Freeform and AppKit distinct.** **[OBSERVED]** — separate profile,
  engine, prompt, and verifier component identities exist for each; AppKit has
  its own strict verifier.
  *Today: `build_platform/builtin_profiles.py`; tests
  `test_build_platform_freeform_engine.py`,
  `test_build_platform_appkit_engine.py`.*

- **Non-web architectural neutrality proof.** **[OBSERVED]** — a synthetic
  non-web conformance target exists specifically to prove Core is not web-bound.
  It is deliberately *not* a shipped user target.
  *Today: `build_platform/conformance.py`; test
  `test_build_platform_nonweb_conformance.py`.*

## Tier B — web product expansion

- **Next.js target profiles/adapters.** **[PLANNED]** — the currently registered
  web target is the legacy web target. A first-class Next.js target adapter is
  not present.
- **Self-host and Vercel deployment connectors.** **[PLANNED]** — Vercel is a
  connector, never a prerequisite (BOUNDARIES §11).
- **User-authored Build Libraries and guided library authoring.**
  **[ACCEPTED-DESIGN]** — the three input kinds and their binding rules are
  sealed (BOUNDARIES §12); the authoring product is not built.
- **WSL2 and macOS host certification.** **[PLANNED]** — hosts expose
  capabilities; they must not leak platform checks through Core.

The first release may be **web + Next.js** as Dylan authorised, provided the
target adapter seams remain capable of later non-web registration.

## Tier C — later targets

- Expo / React Native mobile. **[PLANNED]**
- Tauri / Electron desktop. **[PLANNED]**
- Optional GPU / marketplace capabilities. **[PLANNED]**

All three arrive as **adapters**, not as another lifecycle.

## Research and owner gates carried forward

These are carried forward deliberately. **None of them is an excuse to stop or
weaken the current Build reliability campaign.**

| Gate | Class |
|------|-------|
| Soak-matrix digest continuity | **[RESEARCH-GATE]** |
| Vercel prebuilt `.env` behaviour | **[RESEARCH-GATE]** |
| Downgrade write-safety / schema-version guard | **[RESEARCH-GATE]** |
| Latent-executable content-scan lexicon for user recipes | **[RESEARCH-GATE]** |
| Image signing / SBOM / SLSA target | **[OWNER-DECISION]** |
| Secret-key rotation | **[RESEARCH-GATE]** |
| Fresh-device count and RPO/RTO | **[OWNER-DECISION]** |

## Carried from campaign findings

- **Type-level separation of raw event rows from normalized events.**
  **[ACCEPTED-DESIGN]** — `collect_events()` returns raw SQLite rows
  (`{seq, kind, source, id, created_at, payload}`, payload an unparsed JSON
  string) typed identically to a normalized event: both `dict[str, Any]`. That
  collision produced pattern **P11** twice, once fatally (a freeze that could
  never fire). Giving the row shape a distinct type — e.g.
  `NewType("RawEventRow", dict[str, Any])` — lets **basedpyright**, already a
  required zero-error gate, refuse the confusion at every call site. Stronger
  than a lint pattern, which cannot distinguish the correct payload-aware reader
  from the broken one because they are syntactically identical. Deferred from
  the acceleration insertion because it touches many signatures.

- **Live focused replay from a failure capsule.** **[RESEARCH-GATE]** — capsules
  bind a safe boundary and restore the exact immutable workspace today
  (`development/harness/build_soak/capsule.py`), but a *live* re-drive cannot be built
  without crossing a boundary the campaign forbids. A replay must be a new
  isolated run that leaves the original verdict byte-stable, and there is no
  product API to seed prior conversation history into a **new** conversation:
  `restore_workspace_version` targets an existing one, and no
  `import_events`/`fork_conversation` route exists. The three available routes —
  a new product API, writing events behind the product, or re-driving the
  original conversation — are respectively a new API, fabricated internal state
  with a second lifecycle owner, and mutation of the original dossier. The open
  question is whether a *first-class, product-owned* "fork a conversation at an
  accepted event horizon" capability is worth having on its own merits; if it
  is, replay follows for free. It should not be reverse-engineered to serve a
  diagnostic convenience.

- **Architecture-budget debt.** **[OBSERVED]** — resolved. The historical 26
  violations on `stable-main` were worked off rather than rebaselined:
  `development/architecture/debt.json` is empty (`[]`), and
  `development/scripts/check_arch_budget.py` passes on `main` — 0 active debt
  rows, 0 violations across 2222 Python and 592 TypeScript modules (verified
  2026-08-19 at `f724188a`). The check remains a landing gate; new debt rows
  need the same honest-decomposition-or-owner-visible-rebaseline treatment.

## Accepted planning backbone

```text
/var/home/dylan/disclaude-campaigns/post-core-design/2026-07-21/outputs/INDEX.md
```

This is **design evidence, not current implementation status.** Use its linked
deliverables selectively and verify every claim against current code before
acting on it.
