# Context & Iteration Spec — Artifact-VM Support Pack

> **Status:** SPEC ONLY. No runtime behavior is described as implemented. Read
> [`_GROUNDING.md`](./_GROUNDING.md) first for the real Disco names. The export
> pipelines that *consume* the durable context defined here (`resource_manifest.json`,
> ContextPack, handoff packages, version snapshots) are specified in
> [`EXPORT_PIPELINE_SPEC.md`](./EXPORT_PIPELINE_SPEC.md).
>
> **Purpose.** Define how the Artifact VM keeps a long-running build session coherent: how
> durable project memory is stored on disk under `.disco/`, who reads/writes each piece,
> how a "snip" of frozen context is resolved back to fresh source before an exact-text
> edit (the CD-TOOLS fresh-read guard), when a major revision gets a new version snapshot
> versus an in-place targeted edit, and how the verifier's diagnostics are kept *out* of
> the main agent's working context.

---

## 0. Vocabulary anchor (from grounding)

| Concept | Real name / status |
| --- | --- |
| AppKit version snapshot tool | `app_snapshot_version` (REAL — `appkit.py`, in `appkit.leadgen` edit pack) |
| AppKit tweak persistence | `.disco/tweaks.json` (REAL — grounding §7); values in `AppSpec.tweaks` |
| AppKit appspec | `.disco/appspec.json` (REAL — required file for `appkit.leadgen`) |
| Fresh-read guard before exact-text edits | CD-TOOLS 1 (DONE — grounding §9) |
| Atomic exact replacement | `exact_replace` (REAL — CD-TOOLS) |
| Safe writes (no entrypoint clobber) | `safe_write_file` (REAL — CD-TOOLS) |
| Verifier-only read scope | CD-TOOLS (DONE — grounding §9) |
| No elision markers in editable source | CD-TOOLS tool-prompt discipline (DONE) |
| `context_policy` pack section | REQUIRED prompt-pack section (grounding §5) |
| `context_memory` tool | REAL (grounding §4) |
| Human-visible preview (GAP) | `show_artifact_to_user` / `show_artifact_to_agent` (grounding §10) |
| ContextPack / `todo.md` / `decisions.md` / `direct_edits.json` / `overrides.css` / `resource_manifest.json` / `context_pack.json` | **(GAP)** — proposed durable-context files this spec defines |
| comment anchors / screen labels | **(GAP)** — proposed addressing scheme this spec defines |

> Only `app_snapshot_version`, `.disco/tweaks.json`, `.disco/appspec.json`, and the
> CD-TOOLS guards are REAL today. Every other artifact named below is **(GAP)** — proposed
> new durable-context surface. The CD-TOOLS work it builds on is DONE; do not re-spec it.

---

## 1. The `.disco/` context directory — file layout

A long-running project's durable context lives in a single host-owned directory at the
project root: `.disco/`. It is **never** part of a public deploy payload (stripped by
pipelines 1/2 in `EXPORT_PIPELINE_SPEC.md`) but **is** included in owner/developer handoff
packages (pipelines 6/7).

```
<project-root>/
├── index.html                      # (kind-dependent) the artifact entrypoint
├── report.md                       # (document kind) authored source
├── deck.authored.json              # (deck kind) AuthoredDeck sidecar (REAL)
├── assets/                         # images, fonts, css referenced by the artifact
│   └── logo.png
└── .disco/                         # host-owned durable context (this spec)
    ├── appspec.json                # (appkit) AppSpec (REAL)
    ├── tweaks.json                 # (appkit) tweak values (REAL, grounding §7)
    ├── context_pack.json           # (GAP) ContextPack: the resumable session memory index
    ├── todo.md                     # (GAP) the living plan / outstanding work
    ├── decisions.md                # (GAP) append-only decision log (why, not just what)
    ├── direct_edits.json           # (GAP) record of user/agent targeted edits + anchors
    ├── overrides.css               # (GAP) user style overrides layered over the artifact
    ├── resource_manifest.json      # (GAP) authoritative asset inventory (export reads this)
    ├── anchors.json                # (GAP) comment-anchor + screen-label registry
    ├── snapshots/                  # (GAP) version snapshots (cross-ref app_snapshot_version)
    │   ├── index.json              #   (GAP) ordered snapshot index w/ labels + timestamps
    │   ├── v0001/                  #   (GAP) a frozen copy of the artifact at a revision
    │   └── v0002/
    └── verifier/                   # (GAP) verifier-only scratch — NEVER read by main agent
        ├── screenshots/            #   (GAP) verify_web_app frames
        └── diagnostics.json        #   (GAP) eval/screenshot findings (isolated)
```

> `app_snapshot_version` (REAL) is the AppKit-specific writer for `snapshots/`; this spec
> generalizes the `snapshots/` directory to **all** kinds as **(GAP)**.

---

## 2. Per-item specs

Each item: **what it is · where it lives · who reads/writes · lifecycle · failure modes ·
tests required.**

---

### 2.1 ContextPack — `.disco/context_pack.json` **(GAP)**

- **What it is.** The resumable session-memory index: a compact JSON pointing at the other
  context files, the current artifact kind/contract, the active version snapshot, the open
  `todo.md` items, and a pointer into the event log. It is what an owner-handoff (pipeline
  7) ships so a *future* session resumes with full memory. The realization of the grounding
  `context_policy` pack section and the `context_memory` tool as a durable on-disk file.
- **Where it lives.** `.disco/context_pack.json`.
- **Who reads/writes.** *Writes:* the host (on plan updates, snapshots, deliveries).
  *Reads:* the loop on session resume; the owner_handoff export (pipeline 7); never the
  public deploy payload.
- **Lifecycle.** Created at first build action → updated on every snapshot/decision/delivery
  → frozen into owner/developer handoff bundles → re-hydrated on resume.
- **Failure modes.** Stale pointer to a deleted snapshot; pointer to a `todo.md` that
  drifted from reality; corruption (unparseable JSON) wedging resume; bloat (embedding full
  source instead of pointers).
- **Tests required.**
  - [ ] Resume from a ContextPack reconstructs kind/contract/active-snapshot/todo correctly.
  - [ ] A dangling pointer (snapshot deleted) is detected and reported, not silently
        followed.
  - [ ] Corrupt JSON fails closed on resume with a structured "context unreadable" message,
        not a crash.
  - [ ] ContextPack stays under a size bound (pointers, not inlined source).

---

### 2.2 `todo.md` — `.disco/todo.md` **(GAP)**

- **What it is.** The living plan: outstanding and completed work for the project, in
  human-readable markdown. The durable counterpart to the in-loop `plan_step` /
  `update_plan_progress` tools (grounding §4). One source of truth for "what's left".
- **Where it lives.** `.disco/todo.md`.
- **Who reads/writes.** *Writes:* the host on plan-progress updates (capable model uses
  declarative `update_plan_progress`; small model uses NL + done-at-finish — per the
  plan-progress redesign). *Reads:* the loop's finish gate; the owner-handoff summary.
- **Lifecycle.** Seeded from the initial plan → items checked off as work lands → emptied of
  open items is a precondition the finish gate consults (but does not solely trust — finish
  is decoupled from bookkeeping per the plan-progress unify work).
- **Failure modes.** State-drift (todo says done, artifact disagrees); incremental
  per-step checklist thrash (the failure the plan-progress redesign killed — do NOT
  reintroduce per-step writes for small models); orphaned items after a copy-vs-edit.
- **Tests required.**
  - [ ] Plan-progress update is reflected in `todo.md` for both capable and small model
        modes.
  - [ ] Finish gate reads effective plan-progress from ONE shared reader (no `plan_step`-only
        path that ignores `update_plan_progress`).
  - [ ] A done-claim in `todo.md` that contradicts artifact state does NOT alone satisfy the
        finish gate.

---

### 2.3 `decisions.md` — `.disco/decisions.md` **(GAP)**

- **What it is.** An append-only log of *why* choices were made (template chosen, library
  picked, a feature deferred and the reason). Durable rationale that survives context
  condensation, so a resumed/handed-off session does not re-litigate settled decisions.
- **Where it lives.** `.disco/decisions.md`.
- **Who reads/writes.** *Writes:* host appends on each material decision. *Reads:* the agent
  on resume; the owner/developer handoff summary; the user (it is human-readable).
- **Lifecycle.** Append-only; never rewritten in place (rewriting loses the audit value).
  Carried into all handoff packages.
- **Failure modes.** In-place edits destroying history; decisions that contradict
  `todo.md`/artifact (drift); secrets accidentally logged (must be redacted before any
  outbound handoff — ties to EXPORT FC-6).
- **Tests required.**
  - [ ] Appends are append-only (a write that truncates prior entries is rejected).
  - [ ] No secret-shaped content survives into a handoff copy of `decisions.md`.
  - [ ] Resume surfaces prior decisions to the agent's working context (no re-litigation).

---

### 2.4 `direct_edits.json` — `.disco/direct_edits.json` **(GAP)**

- **What it is.** A structured record of targeted edits applied to the artifact — each entry
  ties a change to a **comment anchor** / **screen label** (see §2.8/§2.9) and the tool that
  made it (`exact_replace`, `file_replace_lines`, an `app_*` semantic op). It lets the host
  replay/audit edits and is the bridge between human "change this bit" requests and the
  exact-text edit tools.
- **Where it lives.** `.disco/direct_edits.json`.
- **Who reads/writes.** *Writes:* the host after each successful targeted edit. *Reads:* the
  copy-vs-edit decision rule (§3); the verifier (to know what changed); developer handoff.
- **Lifecycle.** Appended per edit; an entry references the snapshot it applied to; reset
  semantics: a new version snapshot starts a fresh edit run scoped to that snapshot.
- **Failure modes.** Anchor drift (an edit recorded against an anchor that later moved);
  recording an edit that did not actually apply (must be written only after the tool
  confirms the exact replacement landed — fresh-read guard, §4); divergence from the actual
  file.
- **Tests required.**
  - [ ] An entry is written ONLY after the underlying exact-text edit verifiably applied.
  - [ ] Replaying recorded edits over the referenced snapshot reproduces current state.
  - [ ] An edit against a stale anchor is rejected and re-resolved (no blind apply).

---

### 2.5 `overrides.css` — `.disco/overrides.css` **(GAP)**

- **What it is.** A user/agent style-override layer applied *over* the artifact without
  mutating its authored source — so visual tweaks survive a major-revision copy and can be
  toggled. The CSS analog of `AppSpec.tweaks` for non-appkit (HTML) kinds.
- **Where it lives.** `.disco/overrides.css`; linked last in the artifact's `<head>` at
  preview/export time (precedence over authored styles).
- **Who reads/writes.** *Writes:* the host on a style-override request. *Reads:* preview;
  export prepare/copy (it is **inlined/copied** into the bundle by pipelines 1/2 — it is
  real output, not internal-only); the verifier renders with it applied.
- **Lifecycle.** Persists across version snapshots (carried forward, since it is a layer);
  can be promoted into authored source by an explicit "bake in" action.
- **Failure modes.** Override silently masking a real bug (the artifact looks right only
  because of overrides — verifier must test WITH and note WITHOUT); override not carried
  into a snapshot copy; override referencing a missing asset.
- **Tests required.**
  - [ ] `overrides.css` is included in static/cloudflare export bundles and listed in the
        `resource_manifest.json`.
  - [ ] Overrides survive a copy-to-new-version snapshot.
  - [ ] Removing overrides reveals (does not hide) underlying artifact state to the verifier.

---

### 2.6 `resource_manifest.json` — `.disco/resource_manifest.json` **(GAP)**

- **What it is.** The authoritative inventory of every asset the artifact depends on
  (images, fonts, css, generated slide images, included data files) with a relative path and
  a content hash per entry. The **export pipelines' fail-closed FC-3 input** — every bundle
  must contain a manifest and it must be consistent (no orphans, no dangling refs). See
  `EXPORT_PIPELINE_SPEC.md` §3 FC-3.
- **Where it lives.** `.disco/resource_manifest.json` (source of truth); **copied into the
  bundle root** at export so the delivered artifact carries its own manifest.
- **Who reads/writes.** *Writes:* the host whenever an asset is added/removed (image_generate,
  file_write of an asset, deck image embed). *Reads:* EVERY export pipeline's preflight +
  validate; the verifier.
- **Lifecycle.** Continuously maintained during the build; snapshotted with each version;
  validated at export and shipped inside the bundle.
- **Failure modes.** Orphan (file present, not in manifest) → export FC-3 block; dangling ref
  (manifest entry, no file) → FC-3 block; stale hash (file changed, hash not updated) →
  idempotency/verification mismatch; absolute host path in a manifest entry → FC-2 block.
- **Tests required.**
  - [ ] Adding an asset updates the manifest; removing one removes the entry.
  - [ ] Manifest with an orphan or a dangling ref is rejected at export validate.
  - [ ] Manifest entries are all relative paths (no absolute host paths) and hashes match
        file contents.
  - [ ] The manifest is present inside every files/app export bundle.

---

### 2.7 Snip / resolved context — `.disco/snapshots/` + fresh-read **(GAP dir; CD-TOOLS guard REAL)**

- **What it is.** "Snip" = a *frozen* excerpt of source that was placed into the model's
  working context earlier in the session (e.g. the host showed lines 40–60 of `index.html`).
  Because the file may have changed since, the snip is **stale by construction**. Resolved
  context = the act of re-reading the *current* file region before relying on it. The
  fresh-read guard (CD-TOOLS 1, DONE) enforces this before any exact-text edit.
- **Where it lives.** Snips are transient (in the model's context window / condensation
  records); the *authoritative* source is always the live file under the project root.
  Version snapshots in `.disco/snapshots/` are the durable frozen copies (distinct from
  transient snips).
- **Who reads/writes.** *Reads/resolves:* the host before dispatching `exact_replace` /
  `file_replace_lines`. *Writes:* none — resolution is a read that refreshes; it never
  trusts the snip.
- **Lifecycle.** Snip enters context → time passes / edits land → fresh-read resolves snip to
  current bytes → exact-text edit operates on current bytes (never on the frozen snip).
- **Failure modes.** Editing against a frozen snip whose text no longer matches (the Mode-B
  edit-elision thrash CD-TOOLS killed); elision markers (`// ... unchanged`) treated as real
  source (forbidden by CD-TOOLS tool-prompt discipline); resolving to the wrong file region.
- **Tests required.**
  - [ ] An `exact_replace` whose `old` text comes from a now-stale snip is forced to
        re-read fresh source first and either matches current bytes or fails closed (never
        edits a phantom region).
  - [ ] An elision marker present in a snip never reaches executed edit args.
  - [ ] Fresh-read resolves the correct file + region for the anchor.

#### Worked example — snip resolves frozen context before an exact-text edit

1. **Snip enters context.** Earlier the host showed the agent:
   ```
   <snip file="index.html" lines="40-44">
   40  <h1 class="hero">Welcome</h1>
   41  <p class="sub">Old tagline</p>
   </snip>
   ```
   This text is now *frozen* in the agent's context.
2. **Time passes.** A prior targeted edit changed line 41 to
   `<p class="sub">A fresher tagline</p>`. The snip in context is now stale.
3. **Agent requests an edit.** It calls `exact_replace(file="index.html",
   old="<p class=\"sub\">Old tagline</p>", new="<p class=\"sub\">Welcome aboard</p>")`,
   using the **frozen** `old` text.
4. **Fresh-read guard fires (CD-TOOLS 1).** Before applying, the host RE-READS the current
   bytes of `index.html` around the anchor. It finds `Old tagline` is no longer present
   (the live line says `A fresher tagline`).
5. **Fail closed, re-resolve.** The host does NOT edit a phantom region. It returns a
   structured mismatch: `"exact_replace target not found — the file changed since you last
   saw it. Current line 41 is: '<p class=\"sub\">A fresher tagline</p>'. Re-issue the edit
   against current text."` The agent re-reads, then issues `old="A fresher tagline"` and
   the atomic replacement lands on real current bytes.
6. **Record.** Only after the replacement verifiably applied does the host append an entry to
   `direct_edits.json` (§2.4) tied to the comment anchor (§2.8).

> This is the durable-context realization of CD-TOOLS' fresh-read guard + atomic
> `exact_replace` + no-elision discipline (grounding §9). The export side relies on the same
> property: prepare/copy resolves snip/frozen context to fresh source before snapshotting
> (`EXPORT_PIPELINE_SPEC.md` stage glossary, "prepare/copy").

---

### 2.8 Comment anchors — `.disco/anchors.json` **(GAP)**

- **What it is.** Stable, human-meaningful handles embedded as comments in the artifact
  source (e.g. `<!-- @anchor:hero -->`) that name a region independently of line numbers, so
  edits and the verifier can refer to "the hero section" even after lines shift. The registry
  maps anchor → current location.
- **Where it lives.** Anchors live inline in source; the registry is `.disco/anchors.json`.
- **Who reads/writes.** *Writes:* host when a region is named (scaffold, app_add_section).
  *Reads:* `direct_edits.json` recording; the fresh-read resolver (an anchor resolves to a
  current region); user-facing "change the hero" requests.
- **Lifecycle.** Created with a region; updated when the region moves; removed with the
  region. Carried into snapshots.
- **Failure modes.** Anchor drift (registry points at a moved/deleted region); duplicate
  anchor ids; anchor comments leaking into a public bundle (acceptable in HTML comments but
  should be stripped for production polish — flag, do not fail).
- **Tests required.**
  - [ ] An anchor resolves to the correct current region after intervening edits shift lines.
  - [ ] A deleted region's anchor is removed/invalidated, not left dangling.
  - [ ] Duplicate anchor ids are rejected at write time.

---

### 2.9 Screen labels + human 1-based indexing **(GAP)**

- **What it is.** Human-facing labels for screens/sections/slides ("Screen 1: Landing",
  "Slide 3") using **1-based** indexing (humans count from 1), distinct from any 0-based
  internal array index. The mapping is recorded alongside anchors in `.disco/anchors.json`.
- **Where it lives.** `.disco/anchors.json` (label ↔ internal index ↔ anchor).
- **Who reads/writes.** *Writes:* host as screens/slides are created/reordered. *Reads:*
  every user-facing message ("Slide 3 rendered blank" — see `EXPORT_PIPELINE_SPEC.md`
  pipeline 5 copy); the verifier; reorder operations (`app_reorder_section`).
- **Lifecycle.** Labels track creation order but are re-derived on reorder so "Slide 3"
  always means the third slide as the human sees it.
- **Failure modes.** Off-by-one (showing a 0-based index to a human); label not updated after
  a reorder; mismatch between the label in a user message and the actual screen.
- **Tests required.**
  - [ ] Every user-facing index is 1-based; the internal index is converted exactly once at
        the boundary.
  - [ ] After a reorder, labels are re-derived so the displayed Nth == the actual Nth.
  - [ ] A user message referencing "Slide N" maps to the same slide the verifier inspects.

---

### 2.10 Version snapshots — `.disco/snapshots/` (cross-ref `app_snapshot_version`) **(GAP dir; tool REAL for appkit)**

- **What it is.** Frozen, restorable copies of the whole artifact at a labeled revision.
  `app_snapshot_version` (REAL, in the `appkit.leadgen` edit pack) is the AppKit writer;
  this spec generalizes snapshots to **all** kinds. Each snapshot has an id (`v0001`), a
  label, a timestamp, and a copy of the artifact + `.disco/` context at that point.
- **Where it lives.** `.disco/snapshots/v0001/...`, indexed by `.disco/snapshots/index.json`.
- **Who reads/writes.** *Writes:* host on a major revision (see §3 copy-vs-edit rule) or an
  explicit `app_snapshot_version` call. *Reads:* restore/rollback; owner-handoff version
  history; the copy-vs-edit decision.
- **Lifecycle.** Created at a major-revision boundary → never mutated (a snapshot is
  immutable) → referenced by ContextPack as the active baseline → shipped (index + latest, or
  all, per handoff policy) in owner handoff.
- **Failure modes.** Snapshot bloat (snapshotting on every tiny edit); mutating a snapshot in
  place (breaks immutability/audit); ContextPack pointing at a pruned snapshot; snapshot that
  omits `.disco/` context (un-resumable).
- **Tests required.**
  - [ ] A snapshot is immutable after creation (writes into a snapshot dir are rejected).
  - [ ] Restore from a snapshot reproduces both artifact AND `.disco/` context exactly.
  - [ ] Snapshots are created at major-revision boundaries, NOT on every small edit
        (anti-bloat — ties to §3).
  - [ ] `app_snapshot_version` (appkit) writes into the same `snapshots/` layout this spec
        defines.

---

### 2.11 Verifier context isolation — `.disco/verifier/` **(GAP dir; verifier-only read scope REAL)**

- **What it is.** A hard boundary: the verifier's diagnostics (screenshots from
  `verify_web_app`, eval/LLM-judge findings) are written to `.disco/verifier/` and **never
  enter the main agent's working context** unless promoted as an explicit, structured
  finding. Builds on CD-TOOLS' verifier-only read scope (DONE, grounding §9). Prevents the
  verifier's raw output (large screenshots, internal eval chatter) from polluting/condensing
  the builder's context.
- **Where it lives.** `.disco/verifier/screenshots/`, `.disco/verifier/diagnostics.json`.
- **Who reads/writes.** *Writes:* the verifier only. *Reads:* the host, which decides what (if
  anything) to surface to the main agent as a typed finding; the verifier on a re-check. The
  **main agent never reads `.disco/verifier/` directly.**
- **Lifecycle.** Written per verify pass → consulted by the host's gate → pruned/rotated;
  promoted findings become structured messages, not raw dumps.
- **Failure modes.** Leakage (raw screenshot bytes or verifier reasoning landing in the
  builder's context — the exact pollution this isolates); the builder editing to satisfy a
  *raw* diagnostic it should not have seen; verifier reading the builder's scratch and
  conflating roles.
- **Tests required.**
  - [ ] Main-agent context after a verify pass contains NO raw verifier diagnostic content
        (only promoted, typed findings).
  - [ ] The verifier writes only under `.disco/verifier/` and reads only its allowed scope
        (CD-TOOLS verifier-only read scope).
  - [ ] A promoted finding is a structured message (anchor + screen label + verdict), not a
        screenshot blob.

---

### 2.12 Handoff packages as durable context — (cross-ref `EXPORT_PIPELINE_SPEC.md` 6/7) **(GAP)**

- **What it is.** The developer-handoff and owner-handoff exports (pipelines 6 and 7) are the
  *durable, portable* form of all the context above: they bundle `.disco/` (ContextPack,
  decisions, todo, snapshots index, manifest) with the artifact so a future session — or a
  different model, or a human — can resume with full memory.
- **Where it lives.** Produced by the export pipelines; the source is `.disco/`.
- **Who reads/writes.** *Writes:* pipelines 6/7 (`EXPORT_PIPELINE_SPEC.md`). *Reads:* a
  resumed session re-hydrating from a delivered handoff; the receiving human/developer.
- **Lifecycle.** Snapshot-of-context at delivery time → portable → re-hydrated on resume.
- **Failure modes.** Handoff missing the ContextPack (un-resumable — owner_handoff FC,
  `missing_context_pack_fail.json`); secrets carried into a handoff (FC-6); stale manifest.
- **Tests required.**
  - [ ] An owner handoff contains a parseable ContextPack + decisions + todo + snapshot index.
  - [ ] Re-hydrating from a handoff reconstructs a working session (kind/contract/active
        snapshot/open todos).
  - [ ] No secrets survive into a handoff (shared with `EXPORT_PIPELINE_SPEC.md` FC-6).

---

## 3. Copy-vs-edit decision rule (checklist)

When the user/agent wants to change the artifact, the host decides between an **in-place
targeted edit** (cheap, exact-text via `exact_replace`/`file_replace_lines`/`app_*`) and a
**copy to a new version snapshot** (major revision). Use this checklist; **default to
in-place edit** — snapshot only when a trigger fires (anti-bloat, §2.10).

**Take an IN-PLACE targeted edit when ALL of these hold:**
- [ ] The change is localized (one or a few anchors/regions).
- [ ] The artifact's structure/identity is preserved (same screens, same intent).
- [ ] The change is reversible by another small edit.
- [ ] No risk of clobbering an entrypoint (otherwise route through `safe_write_file`).
- [ ] The fresh-read guard can resolve the target to current bytes (§2.7).

**Create a NEW VERSION SNAPSHOT (copy) when ANY of these hold:**
- [ ] **Major revision:** a structural rewrite, new layout, or a "make me a different
      version / try another direction" request (the user wants to compare/keep the old one).
- [ ] **Destructive/irreversible** change where the prior state must be restorable.
- [ ] **Branch point:** the user wants A/B variants kept side by side.
- [ ] An `app_snapshot_version` call (appkit) is made explicitly.
- [ ] Pre-export checkpoint of a milestone the owner should be able to return to.

**Never:**
- [ ] Snapshot on every small edit (bloat — §2.10 failure mode).
- [ ] Rewrite the whole artifact to make a localized change (the rewrite-thrash the build-loop
      fixes target; in-place targeted edit is the law for editable kinds — only `custom` has
      `rewrite_allowed=True`, grounding §3).
- [ ] Mutate an existing snapshot in place.

**Tests required.**
- [ ] A localized change takes the in-place path and does NOT create a snapshot.
- [ ] A "make another version" request creates a new snapshot and preserves the prior one.
- [ ] A rewrite of an editable (non-`custom`) kind to achieve a localized change is rejected
      in favor of a targeted edit.

---

## 4. Verifier context isolation (rule restated)

- **Rule.** Verifier-only diagnostics (screenshots, eval/LLM-judge output) live in
  `.disco/verifier/` and are read by the host gate only. The main agent's working context
  receives, at most, **promoted, typed findings** (anchor + screen label + verdict), never
  raw verifier artifacts. (CD-TOOLS verifier-only read scope, DONE — grounding §9.)
- **Why.** Raw verifier output is large and noisy; letting it into the builder's context
  causes condensation pressure and tempts the builder to "fix the screenshot" rather than the
  artifact. Isolation keeps the builder's context about the *artifact and the plan*.
- **Boundary checklist.**
  - [ ] Verifier writes ONLY under `.disco/verifier/`.
  - [ ] Main agent NEVER reads `.disco/verifier/` directly.
  - [ ] Only typed, structured findings cross the boundary (no blobs).
  - [ ] Findings reference human screen labels (§2.9) and comment anchors (§2.8).

---

## 5. Tests required (global checklist)

- [ ] **CT-1 ContextPack round-trip.** Build → owner_handoff → resume reconstructs
      kind/contract/active-snapshot/open-todos.
- [ ] **CT-2 Plan-progress single reader.** Finish gate and `todo.md` read effective
      plan-progress from ONE shared reader (no `plan_step`-only path).
- [ ] **CT-3 Decisions append-only + redacted.** Appends never truncate; no secrets in a
      handoff copy.
- [ ] **CT-4 direct_edits write-after-verify.** An edit is recorded only after the exact-text
      replacement verifiably applied.
- [ ] **CT-5 overrides survive + export.** `overrides.css` carries across snapshots and is
      bundled + manifested by static/cloudflare export.
- [ ] **CT-6 manifest consistency.** Orphan/dangling/abs-path manifest entries are rejected
      at export validate (shared with `EXPORT_PIPELINE_SPEC.md` FC-2/FC-3).
- [ ] **CT-7 snip fresh-read.** An exact-text edit from a stale snip is forced to re-read
      fresh source and fails closed on mismatch (CD-TOOLS 1); no elision marker reaches edit
      args.
- [ ] **CT-8 anchors stable under drift.** Anchors resolve to correct current regions after
      line shifts; duplicates rejected; deleted-region anchors invalidated.
- [ ] **CT-9 1-based indexing.** Every user-facing index is 1-based; reorder re-derives
      labels; user "Slide N" == verifier's slide N.
- [ ] **CT-10 snapshot immutability + restore.** Snapshots are immutable and restore artifact
      + `.disco/` exactly; created at major-revision boundaries only.
- [ ] **CT-11 copy-vs-edit routing.** Localized → in-place (no snapshot); "another version" →
      snapshot (prior preserved); rewrite-for-localized rejected for editable kinds.
- [ ] **CT-12 verifier isolation.** No raw verifier diagnostic enters main-agent context;
      only typed findings cross; verifier confined to `.disco/verifier/`.
- [ ] **CT-13 handoff durability.** Owner handoff is re-hydratable; missing ContextPack fails
      closed (`missing_context_pack_fail.json`, GAP).

> Cross-references: real export consumption of this context is in
> [`EXPORT_PIPELINE_SPEC.md`](./EXPORT_PIPELINE_SPEC.md); real names + CD-TOOLS DONE work in
> [`_GROUNDING.md`](./_GROUNDING.md) §7, §9, §10.
