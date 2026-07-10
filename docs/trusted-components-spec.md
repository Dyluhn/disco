# Trusted Components — implementation spec (v0.2)

**Status:** SPEC — implementation-ready, nothing built beyond the fail-closed
seam (`disco.core.trusted_components` + `test_trusted_components_seam.py`).
This document turns `docs/trusted-components-design.md` (the WHY) into build
contracts (the WHAT, exactly). Where the design doc left a question open (§8),
this spec DECIDES it and marks the decision.

Scope reminder from the design doc: this is the third tier between starter
kits (copied, unverified) and AppKit (generated, semantically verified) —
**immutable hash-pinned security cores + free periphery**, verified by three
deterministic checks. We own only the component and its seam; everything else
in the workspace is the model's/user's.

---

## 0. Decisions made by this spec

Each of these is binding for the pilot. Rationale inline in the sections.

| # | Decision |
|---|----------|
| D1 | Hash pins live in the **host-side registry manifest only**. The workspace lockfile is *never* trusted for hashes — it records only (name, version, eject history). |
| D2 | Components ship as **literal files** in the registry (importlib.resources data), not string-builder functions. A CI tripwire asserts shipped bytes match manifest pins. |
| D3 | The probe is **never copied into the workspace**. It is host-authored, host-run, versioned with the component in the registry. |
| D4 | Integrity mismatch at verify time **auto-records an eject** in the lockfile (idempotent, host-written) and reports verdict `ejected` — honest relabel, not a failure loop. |
| D5 | `requires` edges name **components** (`database-kit>=1.0`), not capabilities. `provides` is reserved for labels/badges and a future capability-level resolver. |
| D6 | Ejected components still **satisfy** `requires` edges structurally (the files exist); eject taints only trust labels, and the taint propagates to dependents' labels. |
| D7 | Probes are **HTTP-level by default** (httpx against the served preview), browser-level only when a component genuinely needs DOM assertions. |
| D8 | Pilot advertises the tools in the **free-form Build scope only**. AppKit integration (generator must never regenerate over a component dir) is deferred to a follow-up WO. |
| D9 | Config-surface validation in the pilot is **behavioral only** (the probe exercises the configured app). Static config validation (JSON-schema / tsc) is deferred. |
| D10 | Version specs support exactly two operators: `>=X.Y` and `==X.Y.Z`. Nothing else parses. |
| D11 | A locked version missing from the registry (user upgraded Disco, old version dropped) verifies as `ejected` with reason `registry-version-missing` — we can no longer vouch, so we stop claiming to. |

---

## 1. Data contracts

### 1.1 Registry layout (host-side, ships with Disco)

```
packages/core/src/disco/core/trusted_components/
  __init__.py                  # the pydantic contracts (already committed)
  registry.py                  # TrustedComponentRegistry (WO-TC1)
  verify.py                    # the three checks (WO-TC3)
  registry_data/
    auth-kit/
      1.0.0/
        manifest.json
        GUIDE.md
        core/                  # IMMUTABLE set — every file hash-pinned
          auth.ts
          middleware.ts
          routes.ts
          schema.sql
        config/                # copied once as defaults, then free
          auth.config.ts
        probe/
          probe.py             # host-run; NEVER copied to the workspace (D3)
```

New versions are new sibling directories (`1.0.1/`). Old versions are kept in
the registry as long as feasible; a dropped version triggers D11 at verify
time, never a crash.

### 1.2 manifest.json

The committed pydantic model `TrustedComponentManifest` is the contract. Two
fields are ADDED by this spec (extend the model in WO-TC1; the seam test's
example must be updated in the same commit):

```json
{
  "name": "auth-kit",
  "version": "1.0.0",
  "kind": "trusted_component",
  "summary": "Session auth: login/logout routes, middleware, session store.",
  "when_to_use": "ANY app with accounts, logins, or per-user data. Not for public read-only sites.",
  "files": {
    "core/auth.ts":       "sha256:<hex>",
    "core/middleware.ts": "sha256:<hex>",
    "core/routes.ts":     "sha256:<hex>",
    "core/schema.sql":    "sha256:<hex>"
  },
  "config_surface": ["config/auth.config.ts"],
  "config_defaults_hashed": false,
  "requires": ["database-kit>=1.0"],
  "provides": ["auth"],
  "mounts": { "routes_prefix": "/auth", "middleware": "core/middleware.ts" },
  "probe": "probe/probe.py",
  "guide": "GUIDE.md"
}
```

- `when_to_use` (NEW, required): the one-line catalog entry rendered into the
  tool schema — the catalog-in-schema doctrine from `scaffold_starter._CATALOG`.
- `config_defaults_hashed` (NEW, default false): always false in the pilot;
  reserved so a future component can opt its config *defaults* into integrity
  checking while still allowing edits (upgrade-diff support).
- `files` keys are workspace-relative paths **under the component's install
  dir** (see 1.4), values are `sha256:` + lowercase hex of the raw file bytes.
  No newline normalization, no encoding pass — byte-exact (D2).
- `requires` entries must match `^[a-z0-9-]+(>=\d+\.\d+|==\d+\.\d+\.\d+)$` (D10).

### 1.3 Workspace lockfile — `.disco/components.lock`

Written and updated ONLY by host-side code (the install tool and the verify
path). Lives beside `.disco/appspec.json` / `.disco/primitives/` — same
provenance-directory convention as WO-A3.

```json
{
  "lockfile_version": 1,
  "components": {
    "auth-kit": {
      "version": "1.0.0",
      "installed_at": "2026-07-12T18:04:11+00:00",
      "installed_by": "add_trusted_component",
      "ejected": false
    },
    "database-kit": {
      "version": "1.0.0",
      "installed_at": "2026-07-12T18:03:40+00:00",
      "installed_by": "add_trusted_component",
      "ejected": false
    }
  },
  "ejects": [
    {
      "name": "auth-kit",
      "at": "2026-07-13T02:11:09+00:00",
      "reason": "core-edit-detected",
      "diverged_files": ["core/middleware.ts"]
    }
  ]
}
```

**What the lockfile is NOT (D1):** a source of hash pins. Verification always
resolves (name, version) → the HOST registry manifest and hashes workspace
bytes against *that*. Threat analysis in §8.

Eject is recorded twice on purpose: the boolean flips on the component entry
(fast lookup) and an append-only `ejects` entry keeps the history ("this copy
diverged at <date>", which files, why). An eject record never disappears; a
reinstall (§5) appends a fresh `components` entry with `ejected: false` and
leaves history intact.

### 1.4 Workspace install layout

```
<workspace>/
  src/trusted/auth-kit/        # core/ + config/ + GUIDE.md — probe excluded (D3)
    core/...
    config/auth.config.ts
    GUIDE.md
  .disco/components.lock
```

`src/trusted/<name>/` (no version segment — the lockfile carries the version).
Under `src/` so Vite/TS imports resolve without config gymnastics. The
manifest's `files` paths are relative to this dir.

### 1.5 Hashing spec

`sha256:` + `hashlib.sha256(raw_bytes).hexdigest()`. One canonical helper in
`trusted_components/verify.py`, used by BOTH the authoring script and the
verify path so they can never drift:

```python
def pin(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()
```

Authoring-time generation: `scripts/component_pins.py <component-dir>` rewrites
the manifest's `files` map from the bytes on disk. The CI tripwire
(`test_registry_pins_match_shipped_bytes`, WO-TC1) recomputes every pin for
every registry component and fails on any mismatch — pins cannot rot (D2).

---

## 2. Registry API (WO-TC1)

`disco.core.trusted_components.registry`, mirroring the shape of
`StarterKitRegistry` but backed by literal files:

```python
class TrustedComponentRegistry:
    def get(self, name: str, version: str | None = None) -> LoadedComponent | None:
        """version=None → highest available version. Missing → None, never raises."""
    def names(self) -> frozenset[str]: ...
    def versions(self, name: str) -> tuple[str, ...]: ...
    def catalog_lines(self) -> str:
        """'- <name> <ver> — <when_to_use>' per component, for the tool schema."""
    @classmethod
    def default(cls) -> TrustedComponentRegistry: ...

@dataclass(frozen=True)
class LoadedComponent:
    manifest: TrustedComponentManifest
    def read_file(self, relpath: str) -> bytes: ...        # core/, config/, GUIDE.md
    def install_tree(self) -> dict[str, bytes]: ...        # everything EXCEPT probe/
    def probe_source(self) -> bytes | None: ...            # host-side use only
```

Version comparison: parse `X.Y.Z` into an int tuple; `>=X.Y` compares on the
first two segments; `==X.Y.Z` is exact. Anything unparsable is a registry
authoring error caught by the tripwire, not a runtime branch.

---

## 3. Tool surface (WO-TC2)

Both tools registered in the **free-form Build scope only** (D8). Neither is
reachable in strict AppKit scopes in the pilot.

### 3.1 `add_trusted_component`

```python
class AddTrustedComponentArgs(BaseModel):
    name: str      # description carries registry.catalog_lines() — the full catalog
    version: str | None = None  # None → latest; description lists valid versions
```

Behavior, in order:
1. Resolve (name, version) in the registry. Unknown → refusal naming the
   valid catalog (the vocabulary-in-schema lesson: the refusal must teach).
2. **Requires preflight:** every `requires` edge must already be satisfied by
   the lockfile. Missing → refusal that names the exact missing edge AND the
   command that fixes it ("install database-kit first:
   add_trusted_component(name='database-kit')"). Fail-closed, but the refusal
   carries the next step.
3. **Collision check (never clobber — the `scaffold_starter` rule):** if any
   install-tree path already exists in the workspace with different bytes,
   refuse and list the collisions. Byte-identical existing files are fine
   (idempotent re-install).
4. Write the install tree to `src/trusted/<name>/`, write/update
   `.disco/components.lock`.
5. Return SUCCESS whose content is the component's **GUIDE.md verbatim**, plus
   one line naming the mounts ("routes live under /auth; middleware is mounted
   app-wide — public routes go in config/auth.config.ts `publicAllowlist`").
   Guidance lands exactly at decision time, same as `scaffold_starter` NOTES.md.

Re-install of an ejected component: allowed, but the refusal-free path requires
the diverged files to be gone or byte-identical to the fresh copy (rule 3
covers this automatically). History stays in `ejects`.

### 3.2 `eject_trusted_component`

```python
class EjectTrustedComponentArgs(BaseModel):
    name: str
    reason: str    # free text, recorded in the eject entry
```

Marks the eject in the lockfile (reason `explicit`), appends a divergence
banner to the workspace copy of GUIDE.md ("⚠ ejected <date>: this copy is
yours now — upgrades and the verified badge no longer apply"), returns a
SUCCESS that states the same. Never refuses for an installed component; eject
is a right, not a request (design §5: one-way door, loudly labeled, never
punished).

### 3.3 What the model sees

The build system prompt gains one paragraph (same funnel slot as the starter
catalog): trusted components exist for auth/RBAC/database/payments-class
features; prefer them over hand-rolling those features; everything outside
`src/trusted/` is yours. The per-component *when to use* guidance lives in the
tool schema, not the prompt (catalog-in-schema).

---

## 4. Verification (WO-TC3)

One pure-core entrypoint plus wiring into the existing finish-gate seam:

```python
def verify_trusted_components(
    tree: Mapping[str, bytes],        # workspace file tree (same projection the finish gates read)
    registry: TrustedComponentRegistry,
    served_base_url: str | None,      # None → probe checks SKIP (recorded, not passed)
) -> TrustedComponentsVerdict
```

Called from `run_finish_verify_gates()` — both finish paths converge there
(see docs on the finish-path drift fix) — **whenever `.disco/components.lock`
exists in the tree**. No lockfile → zero checks, zero cost, free-form builds
unaffected. This is the same fold-in shape WO-A3 used for
`primitive_verify:<id>` checks: per-item checks appended to the existing
W-45-compatible verdict, so the finish gate consumes them with no new plumbing.

Per installed, non-ejected component, three checks land in the verdict:

### 4.1 `component_integrity:<name>`
Resolve the locked version's manifest from the HOST registry (D1). For every
`files` entry, hash the workspace bytes at `src/trusted/<name>/<relpath>`.
- All match → PASS.
- Any mismatch or missing file → **auto-eject** (D4): host code appends the
  eject record (reason `core-edit-detected`, with `diverged_files`), flips the
  boolean, and the check reports `ejected` — a distinct status, not FAIL. The
  summary line says exactly what the design demands: "auth-kit core was
  modified — it is now custom code you own; the verified badge is removed."
  Idempotent: the next verify sees `ejected: true` and skips.
- Locked version absent from registry → same eject flow, reason
  `registry-version-missing` (D11).

Rationale for auto-eject over report-only: report-only re-detects the same
divergence every verify forever — a nagging false state. Eject-on-detect makes
the lockfile converge to the truth in one pass, and reinstall is always
available.

### 4.2 `component_deps:<name>`
Walk the component's `requires` edges against the lockfile (presence +
version, D5/D10). Ejected dependencies satisfy the edge (D6) but downgrade
this check to PASS-with-taint: `"rbac-kit depends on auth-kit, which is
ejected — RBAC's guarantees now rest on custom code."` Missing or
version-unsatisfied edge → FAIL naming the exact edge (design §4.2), and this
FAIL **blocks finish** — an "RBAC app" without a database is a false
affordance.

### 4.3 `component_probe:<name>`
Host-side execution of the registry's probe (D3) against the served preview:

```
subprocess: python probe.py --base-url <url> --workspace <mounted-tree-path>
stdout:     one JSON object {"passed": bool, "checks": [{"name","passed","detail"}], "summary": str}
exit != 0 or malformed stdout → probe INFRA error (FAIL, "probe could not run")
timeout: 60s hard
```

Probes are HTTP-level by default (D7): httpx against `base_url`, no browser.
The probe contract file (`ProbeVerdict` pydantic model) lives in
`trusted_components/verify.py`; probes import nothing from Disco (plain
stdlib + httpx) so they stay runnable in any harness.

`served_base_url=None` (no preview running in this gate context) → the probe
check records SKIPPED with "probe requires a served app — run verify with the
preview up." SKIPPED is not PASS: the finish gate treats a skipped probe on a
non-ejected component as FAIL for the *finish* decision (fail-closed), while
mid-build verifies may proceed. This mirrors how the existing browser checks
already need the preview.

### Verdict → gate mapping (design §4 verdicts)

| Design verdict | Spec realization | Blocks finish? |
|---|---|---|
| `verified` | all three checks PASS for all installed components | — |
| `ejected(name)` | integrity check status `ejected` (auto-recorded) | **No** — labels change, work continues |
| `broken-deps(edges)` | `component_deps` FAIL | **Yes** |
| `probe-fail(detail)` | `component_probe` FAIL (or SKIPPED at finish) | **Yes** — routes to REPAIR with the probe's detail |

---

## 5. Eject / reinstall lifecycle

```
installed ──(core edit detected §4.1)──► ejected(core-edit-detected)
installed ──(explicit tool §3.2)──────► ejected(explicit)
installed ──(version dropped §D11)────► ejected(registry-version-missing)
ejected ───(add_trusted_component, collisions resolved)──► installed (fresh entry; history kept)
```

Upgrades: `add_trusted_component(name, version="1.0.1")` on an installed
component = collision check against the NEW install tree; byte-identical files
copy over silently, diverged CONFIG files are left alone (config is the user's),
diverged CORE files refuse with the list (the user chooses: revert the core
edits or stay on the old version/eject). Ejected copies get a WARNING on
upgrade availability, never a forced upgrade (design §6).

---

## 6. Authoring & admissibility (the gate on US)

A component enters `registry_data/` only through this checklist, enforced by
review (and the mechanical parts by the tripwire):

1. **Seam ownership (design §3):** the component mounts itself. Protection is
   opt-OUT (public allowlist in config), never opt-in per call site. If safe
   use requires the model to remember N call sites, the component is
   inadmissible — redesign it.
2. **Probe checks the seam, not the internals:** internals are hash-proven;
   the probe must exercise wiring (unauthenticated request → 401/redirect;
   seeded login → success; cookie flags). A probe that re-tests internals is
   rejected in review.
3. **Config surface is complete:** every anticipated customization is
   expressible in `config_surface` files. "Edit core to change the redirect
   path" is a design bug.
4. **Pins generated, never hand-written:** `scripts/component_pins.py` +
   the CI tripwire.
5. **GUIDE.md ≤ 60 lines**, structured: what you got / how to use it / what
   you may edit (config surface, spelled out) / what you must not edit (core,
   with the eject consequence stated honestly).

---

## 7. Pilot components (WO-TC4/5/6, strictly in this order)

Order is the design doc's: each component proves one new mechanism.

### 7.1 `database-kit 1.0.0` — proves install + integrity (no deps, trivial probe)
- core: `db.ts` (typed sqlite/D1 client + migration runner), `migrations/0001_init.sql`
- config: `db.config.ts` (database name/path)
- requires: none; provides: `["database"]`
- probe: served app responds; a `/__health/db` route (mounted by core) returns
  the migration version. (Health route is part of the seam the kit owns.)

### 7.2 `auth-kit 1.0.0` — proves the seam-ownership rule + a real probe
- core: `auth.ts`, `middleware.ts` (app-wide mount, opt-out allowlist),
  `routes.ts` (/auth/login, /auth/logout, /auth/session), `schema.sql`
  (users/sessions tables via database-kit's migration runner)
- config: `auth.config.ts` — `publicAllowlist: string[]`, `sessionTtlHours`,
  `devSeedUser: {email, password} | null`, redirect paths
- requires: `["database-kit>=1.0"]`; provides: `["auth"]`
- probe (HTTP): (a) every probed route outside the workspace config's
  allowlist → 401/302 unauthenticated; (b) login with the dev seed user → 200
  + Set-Cookie; (c) cookie is HttpOnly + SameSite; (d) /auth/session with the
  cookie → the user; without → 401.
- The probe READS the workspace's `auth.config.ts` allowlist (that's why
  `--workspace` is in the probe contract): config is free, so the probe must
  honor it — the check is "the allowlist is enforced," not "the allowlist is
  what we shipped."

### 7.3 `rbac-kit 1.0.0` — proves the requires-graph on a 2-edge chain
- requires: `["auth-kit>=1.0", "database-kit>=1.0"]`; provides: `["rbac"]`
- core: `roles.ts`, `guard.ts` (route-level role guard exported through
  auth-kit's middleware chain), `schema.sql` (roles/user_roles)
- probe: seeded admin passes an admin-guarded route; seeded member gets 403;
  role checks happen server-side (member token + forged client claim → 403).

### 7.4 Graduate the parked Stripe seam (f41)
`disclaude/f41-stripe-seam` (`60436fa8`) becomes `payments-kit` — its
fail-closed `template_only, verify=None` scaffold already matches this tier's
shape; its security-fill spec (`docs/wo-f41-stripe-security-spec.md`, on that
branch) becomes the probe's check list. This is the first component whose
probe needs secret custody (host-side Stripe test key) — it composes the S-W2
substrates (`secret_refs`, `host_egress`) rather than new machinery.

### 7.5 Lane measurement
New build_soak scenario `auth_app_autonomous`: autonomous free-form build,
prompt requires accounts + a members-only page; asserts
`.disco/components.lock` exists, `verify` verdict = verified, probe PASS, and
`terminal_status_in: [FINISHED, VERIFIED]`. Run before/after funnel changes —
same discipline as the appkit lane.

---

## 8. Adversarial analysis (what the model/user can do, and what happens)

| Attack / accident | Outcome | Why it's safe |
|---|---|---|
| Model edits `core/middleware.ts` to "fix" a build error | Next verify: auto-eject, honest relabel, badge gone | D4; the finish summary says the app's auth is now custom |
| Model edits `.disco/components.lock` to claim auth-kit installed | Integrity check hashes `src/trusted/auth-kit/` against the HOST manifest → files missing → eject(`core-edit-detected`) | D1 — the lockfile never supplies pins |
| Model edits the lockfile to mark a component ejected | That IS an eject — honest state, labels follow | Eject is not a privilege |
| Model deletes the lockfile | Zero components installed → zero checks → no verified badge anywhere | Absence of state = absence of claims, fail-closed |
| Model copies core files elsewhere and hand-wires them | Outside `src/trusted/` = ordinary model code, unverified, no badge | The tier never claims what it didn't install |
| Model edits config to add every route to `publicAllowlist` | Probe honors config (7.2) → PASSES, and that is CORRECT | Config is the sanctioned surface; a fully-public app is a user choice, not a breach of OUR guarantee. The GUIDE states this tradeoff. |
| Model "fixes" the probe | Impossible — the probe is not in the workspace | D3 |
| Registry authoring typo in a pin | CI tripwire fails before it ships | D2 |
| AppKit generator regenerates over `src/trusted/` | Deferred (D8): pilot doesn't advertise the tools in AppKit scope, so the collision cannot occur yet; the follow-up WO adds a generator guard before scopes merge |

The `publicAllowlist` row is the honest edge of the design: we verify that the
component *enforces what the config says*, not that the config is wise. Trust
labels must mean exactly what they check — no more (no-false-affordances,
applied to ourselves).

---

## 9. Test plan

- **Unit (core):** pin helper; version-spec parser (D10, including rejects);
  requires-graph walk (missing edge / version miss / ejected-taint D6);
  lockfile round-trip + eject idempotence; registry loading + `catalog_lines`.
- **Tripwires:** existing `test_seam_is_fail_closed_nothing_advertises_it`
  stays until WO-TC2 registers the tools — then it INVERTS into
  `test_tools_advertised_only_in_freeform_scope`. `test_registry_pins_match_shipped_bytes`
  from WO-TC1 onward. `test_manifest_contract_parses_the_design_doc_example`
  updated for the two new fields in the same commit that adds them.
- **Tool tests:** collision refusal lists paths; requires-preflight refusal
  names edge + fix; install is idempotent; GUIDE.md returned verbatim; eject
  banner appended.
- **Verify tests:** each row of the §8 table as a fixture (tree in, verdict
  out); probe subprocess contract (good JSON / bad JSON / timeout / exit≠0);
  SKIPPED-at-finish is FAIL.
- **Live (mandatory, per the standing rule — fixtures are regression only):**
  the 7.5 soak scenario end-to-end on the dev stack, plus one manual run where
  the operator hand-edits `middleware.ts` mid-build and screenshots the
  eject relabel in the UI.

## 10. Work-order breakdown

| WO | Contents | DoD |
|---|---|---|
| WO-TC1 | Registry (`registry.py`, `registry_data/` layout, pins script, manifest model +2 fields) | tripwires green; `registry.default().names()` returns pilot set as they land |
| WO-TC2 | `add_trusted_component` + `eject_trusted_component`, free-form scope registration, prompt paragraph | tool tests green; inverted scope tripwire green |
| WO-TC3 | `verify_trusted_components` + finish-gate wiring + probe subprocess contract | §8 fixture matrix green; gate consumes checks with no loop changes |
| WO-TC4 | database-kit + auth-kit (files, probes, guides) | probes pass against a scaffolded app on the dev stack, live |
| WO-TC5 | rbac-kit + the 2-edge graph proof | broken-deps FAIL demonstrated live, then fixed by installing the dep |
| WO-TC6 | payments-kit graduation (f41) + `auth_app_autonomous` soak scenario | scenario green in a sequential lane run |

Dependencies: TC1 → TC2 → TC3 → TC4 → TC5 → TC6, strictly. No WO starts
before the previous one's DoD is met and committed (the wave discipline).

---

*Companion: `docs/trusted-components-design.md` (rationale, prior art, market
evidence). This spec supersedes the design doc wherever they disagree on
mechanics; the design doc remains authoritative on intent.*
