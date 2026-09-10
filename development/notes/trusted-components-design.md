# Trusted Components — the verified vendored-component tier

**Status:** DESIGN (the WHY). The implementation-ready spec — data contracts,
tool behavior, verify semantics, pilot WOs — is `current/docs/trusted-components-spec.md`,
which supersedes this doc on mechanics. Origin: Dylan's proposal, 2026-07-10
session — "make the important pieces (auth, RBAC, Stripe) immutable and verified
in depth; let everything cosmetic stay customizable; verification just checks the
important piece still matches source, plus that dependent pieces exist together."
This doc pins that idea with enough precision to build a pilot.

Prior art this deliberately mirrors: shadcn/ui (copy-in components + eject-to-own),
WordPress "never hack core", npm lockfile integrity hashes, Terraform verified
modules, and the Claude design surface's starter-component doctrine
(`deck_stage.js`: use it, compose around it, never redraw it — see
current/docs/claude-design-playbook.md).

---

## 1. Why this tier should exist

Disco has two build tracks today, and both mis-spend verification effort for
security-critical features:

- **Starter kits** (free-form Build): copied starting files, zero verification.
  Great for game loops and app shells; unacceptable for auth — hand-rolled
  wiring varies per run, and "probably fine" is not a product answer.
- **AppKit** (full generation): specs are source of truth, everything validated.
  Strong guarantees, but the machinery must semantically understand *arbitrary
  generated app shapes* — which is exactly the bug class that produced the
  2026-07-10 `schema_sql_valid` false-FAIL (a lead-shaped insert judged a
  `records` app's `shift` table) and the repair spirals behind it.

The observation this tier is built on: **rigor should be proportional to blast
radius.** Nobody is breached by a hero section that scales badly. Auth, RBAC,
payments, and session handling are the ~10% where failure is catastrophic — so
make *those* deterministic and leave layout free.

| | Starter kits | **Trusted components** | AppKit |
|---|---|---|---|
| Files | copied, then yours | trusted core IMMUTABLE, rest yours | generated from specs |
| Verification | none | hash + requires-graph + per-component probe | generation + semantic checks |
| Customization | anything | config surface only (or eject) | spec mutations only |
| Right for | starting points | security-critical capabilities | cookie-cutter full apps |

Non-goal: replacing either existing tier. AppKit keeps cookie-cutter shapes
(lead capture in one validated call); free-form stays the escape hatch. The
fail-closed Stripe/webhook seams — implemented and merged on `main` as
`8f356c1e` / `ce349096` (spec-only `template_only` discipline, real verifiers,
mandatory live exploit runners; their pruned branches survive only as the
archived tips `refs/archive/disclaude/f41-stripe-seam` /
`refs/archive/disclaude/f33-webhook-seam`, both ancestors of `main`) — are the
natural graduates INTO this tier — they already practice exactly this
verification discipline.

## 2. Component anatomy

A trusted component is a versioned directory of files plus one manifest:

```
components/auth-kit/1.0.0/
  manifest.json
  GUIDE.md                    # the small how-to-include-and-use guide
  core/                       # IMMUTABLE — hash-pinned
    auth.ts
    middleware.ts
    routes.ts
    schema.sql
  config/                     # SANCTIONED customization surface
    auth.config.ts            # session TTL, providers, redirect paths…
  probe/
    probe.py                  # the per-component behavioral check (§4.3)
```

`manifest.json`:

```json
{
  "name": "auth-kit",
  "version": "1.0.0",
  "kind": "trusted_component",
  "summary": "Session auth: login/logout routes, middleware, session store.",
  "files": {
    "core/auth.ts":       "sha256:…",
    "core/middleware.ts": "sha256:…",
    "core/routes.ts":     "sha256:…",
    "core/schema.sql":    "sha256:…"
  },
  "config_surface": ["config/auth.config.ts"],
  "requires": ["database-kit>=1.0"],
  "provides": ["auth"],
  "mounts": { "routes_prefix": "/auth", "middleware": "core/middleware.ts" },
  "probe": "probe/probe.py",
  "guide": "GUIDE.md"
}
```

Field semantics:

- **files** — the immutable set, each hash-pinned. These are the ONLY files the
  integrity check covers; everything else in the project is the user's/model's.
- **config_surface** — files the model MAY edit freely (validated shape but not
  hash-pinned). All intended customization flows here: theme, TTLs, provider
  lists, copy. If a need isn't expressible in config, that is a component-design
  bug or an eject (§5) — never a silent core edit.
- **requires / provides** — the dependency graph, exactly Dylan's RBAC⇒database
  rule. Declarative, versioned, checked at verify time: `rbac-kit` requires
  `auth` and `database`; present-or-fail. No semantic inspection — a walk over
  installed manifests.
- **mounts** — the seam-ownership declaration (§3).
- **probe** — one bounded behavioral check for the wiring (§4.3).
- **guide** — returned to the MODEL at scaffold time (catalog-in-schema doctrine:
  information arrives before commitment, the single most effective lever of the
  2026-07 campaigns — starter catalog, app_create vocabulary).

## 3. The seam-ownership rule (the one hard design constraint)

The hash check proves the component is intact — not that it is *used* correctly.
Real auth failures live at the boundary: middleware mounted on 9 of 10 routes,
`is_admin` trusted from the client. Two mitigations, both mandatory for a
component to enter the registry:

1. **The component owns its seam.** The auth kit exports the route table and
   mounts its own middleware app-wide (opt-OUT public allowlist in config, not
   opt-in protection per route). Misuse must be hard to *express*, not merely
   detectable. A component whose safe use depends on the model remembering N
   call sites is not admissible.
2. **The probe checks the seam, not the internals.** Internals are hash-proven;
   the probe exercises the wiring (§4.3).

## 4. Verification semantics

Three deterministic checks, run by the existing host-verifier path (same dispatch
seam as `verify_appkit_app`; verdict shape identical so finish gates need no new
wiring):

### 4.1 Integrity
Hash every `files` entry against the manifest. Byte-match or flag. CANNOT
false-fail; CANNOT require understanding the code. This check alone dissolves
the 2026-07-10 verifier-bug class for this tier.

### 4.2 Requires-graph
For every installed component, every `requires` entry resolves to an installed,
version-satisfying component. RBAC without database ⇒ FAIL with the exact
missing edge named.

### 4.3 Probe
The component's own bounded behavioral check, run against the served app —
e.g. auth-kit: "every route outside the declared public allowlist returns
401/redirect unauthenticated; login with the seeded dev user succeeds; the
session cookie is HttpOnly." Per-component, written by the component author
(us), versioned WITH the component — so probe assumptions can never drift from
component reality the way `schema_sql_valid` drifted from records apps.

### Verdicts (all honest, none blocking free-form work)
- **verified** — all three pass for all installed components.
- **ejected(name)** — integrity mismatch: the user/model modified core. NOT a
  failure: the badge flips to "custom — you own this now" (no-false-affordances
  applied to trust labels: never claim verified when it isn't; never refuse
  people their own code). Recorded in the manifest lockfile so it never
  silently reverts.
- **broken-deps(edges)** — requires-graph failure. This one blocks finish for
  the component's feature claims (an "RBAC app" without a database is a false
  affordance).
- **probe-fail(detail)** — wiring broken; routes to REPAIR with the probe's
  actionable output.

## 5. Eject semantics

`eject <component>` (or any detected core edit at verify time) moves the
component's files out of the trusted set: hashes dropped from the lockfile,
badge changes, guide amended with "this copy diverged at <date>". Upgrades stop
applying. One-way door, loudly labeled, never punished. This is the shadcn
model, and it is what keeps immutability from fighting the hobbyist who needs
one weird change.

## 6. Delivery mechanics

- **Scaffold**: extend the `scaffold_starter` catalog pattern — a
  `add_trusted_component` tool whose schema renders the registry (name +
  one-line when-to-use each, the proven catalog-in-schema shape). On success it
  returns the component's GUIDE.md verbatim — guidance exactly at decision time.
- **Sharing across kits**: components are identified by (name, version, hash) —
  any starter kit or appkit recipe may *reference* one; verification stays
  per-component. What cannot exist is a kit-locally-modified variant without a
  new version. (This answers the "couldn't share components across kits"
  concern in the original proposal: sharing survives; silent divergence dies.)
- **Lockfile**: `.disco/components.lock` — installed set, versions, hashes,
  eject records. The verify checks read ONLY this + manifests.
- **Central patching**: a new component version (CVE fix) ⇒ upgrade = re-copy
  core + re-verify. Ejected copies get a WARNING, not a forced upgrade.

## 7. Pilot plan (after the appkit lane goes green)

1. **auth-kit** first — it forces the seam-ownership question immediately and
   is the highest-value single component. Deliverables: component dir + probe +
   guide, `add_trusted_component` tool with 1-entry catalog, the three verify
   checks wired into the host-verifier dispatch, lockfile.
2. **database-kit** second (auth-kit requires it — proves the requires-graph on
   a real edge).
3. **rbac-kit** third (requires auth+database — proves a 2-edge graph).
4. Graduate the parked **stripe-seam** (f41) into the registry as the first
   externally-risky component; its fail-closed template is already
   component-shaped.
5. Lane measurement: an `auth_app_autonomous` soak scenario asserting
   verdict=verified + probe pass — before/after numbers, same discipline as the
   appkit lane.

## 8. Open questions

- Probe sandboxing: probes run host-side against the served app (no model in
  the loop) — reuse the build_soak preview probing or the verifier session?
- Config validation: JSON-schema per config file, or TypeScript-typecheck as
  the gate?
- Upgrade UX when core AND config schema both change between versions.
- Whether appkit apps may also install trusted components (likely yes — the
  requires-graph is tier-agnostic — but the generator must never regenerate
  over a component dir).

---

*Provenance: Dylan's design instinct, 2026-07-10 (~04:30), verbatim core: "have
the parts that are important be immutable while the things like how it scales or
fits to a page are customizable; verification checks it matches the source file
still; things dependent on one another get verified together; with each piece a
small guide." The only additions in this doc are the seam-ownership rule, the
per-component probe, and eject semantics.*

---

## 9. Prior art & market evidence (researched 2026-07-10)

The pattern this doc proposes is not novel — it is the essential architecture
of the enterprise low-code tier, and the segment that skipped it produced the
breaches this tier prevents.

**Spec-driven generation ships at scale (the appkit-shaped precedent):**
- Power Apps Copilot emits `.pa.yaml` + Power Fx specs, validated + auto-fixed —
  never raw code. Documented ceiling: "complexity is hidden, not eliminated."
- Salesforce Agentforce (Spring '26): typed metadata bundles, schema-validated
  params, published-vs-source divergence detection.
- Retool AI: ToolScript specs; generated apps "automatically inherit enterprise
  security, SSO, RBAC, and data-level permissions"; queries "parameterized and
  sanitized automatically" — the guarantee-inheritance pitch verbatim.
- Glide: constrained specs + per-task model routing took formula error rates
  30-40% → 10-15% (Q1 2026) — funnel-grinding works.
- OSS: Wasp MAGE (LLM emits .wasp DSL, compiler generates; 10-40x token
  efficiency; "constraints are the feature"); app.build (arXiv 2509.03310)
  ablations: "improving the environment often matters more than scaling the
  model."

**The free-form segment's bill (the trusted-components motivation):**
- Lovable CVE-2025-48757: generated Supabase schemas with RLS DISABLED by
  default — 170+ production apps exposed, 13k users' PII leaked. Root cause
  phrasing: "the AI had no structural enforcement to add it." The component
  (Supabase) was fine; THE SEAM was the breach — exactly §3's argument.
- Bolt.new: live Stripe secret keys in frontend bundles. Replit: public .env
  on default deploys. VibeEval 2026: ~45% of AI-generated samples carry OWASP
  Top-10 vulns.
- Manus: confirmed free-form sandbox codegen, no validation layer.

**The honest counterexample:**
- Vercel v0 launched CONSTRAINED (shadcn-only) and widened to free-form in 2025
  ("constrained generation bottlenecked ambition"), replacing constrain-before
  with verify-after (AutoFix). Lesson taken here: constrained-ONLY products die
  at the expressiveness ceiling; constrained TIERS beside a free-form escape
  hatch (this doc, §1 non-goals) do not have that failure mode.

**Survival rules every long-lived implementation shares** (all already in this
design): narrow catalog of repeatable archetypes; an escape hatch, always
(free-form Build / eject §5); semantics verified separately from structure
(probes §4.3 — Builder.io's own docs concede schemas guarantee shape, not truth).
