# Direct Manipulation Spec — the `data-disco-*` grammar

> **Read `_GROUNDING.md` first.** This spec is **documentation only** — no runtime
> behavior is implemented or claimed. It formalizes the attribute grammar that makes
> Disco artifacts *directly manipulable*: click a rendered element → resolve to a source
> location → make a **targeted** edit (never a rewrite) → preserve comment anchors and
> user direct-edits.
>
> **Where this sits relative to what already exists:**
> - **P8 "Semantic Direct Manipulation" is COMPLETE** in the main campaign — so a real
>   foundation exists (AppKit semantic tools, the selection bridge, the `data-oid`
>   stamper). This spec does **not** re-invent that; it *formalizes the attribute
>   grammar* on top of it.
> - The shipped substrate (see `docs/A1-click-to-edit-design.md`) stamps
>   `data-oid="{relpath}:{sourceline}"` and resolves it via `resolveRef()` →
>   `SourceRef{kind:'source', oid, file, line}`. That is the **coarse, file+line**
>   resolution layer. It exists and works.
> - The `data-disco-*` family specified here is the **(GAP)** richer, *semantic*
>   resolution layer that sits **alongside** `data-oid`: it names *what* an element is
>   (a field, a section, a metric, a media slot) rather than only *where* its source is
>   (file+line). `data-oid` answers "which file/line"; `data-disco-*` answers "which
>   semantic target + how to edit it without a rewrite".
>
> Cross-references: `_GROUNDING.md` (real Disco names), `RESOURCE_AND_PROVENANCE_SPEC.md`
> (media slots / `data-disco-media-slot`), `CONTENT_AND_DESIGN_DISCIPLINE_SPEC.md`
> (content tags surfaced on fields), and the oracle fixtures named throughout
> (`fixtures/oracles/*.json`).

---

## 0. Status legend

| Marker | Meaning |
| --- | --- |
| **(EXISTS)** | Real today (P8 / CD-TOOLS / A2 substrate). Referenced, not re-specced. |
| **(GAP)** | New surface proposed by this pack. Not implemented. |

`data-oid` is **(EXISTS)**. Every `data-disco-*` attribute below is **(GAP)** unless
stated otherwise.

---

## 1. Design goals (the contract this grammar must honor)

1. **Rendered element → source location** must be deterministic and reversible.
2. **Targeted edits only.** A click-to-edit must produce an `exact_replace` (CD-TOOLS,
   atomic) on the smallest enclosing source span — **never** a whole-file rewrite.
   `EditContract.rewrite_allowed` is `False` for every direct-manipulable kind (only
   `custom` allows rewrite — see `_GROUNDING.md §3`).
3. **Anchors survive.** Comment anchors and version markers MUST be preserved verbatim
   across any targeted edit. Duplication or loss is a **failure** (oracle:
   `comment_anchor_preserved_pass.json` / `comment_anchor_lost_fail.json`).
4. **User direct edits are not clobbered.** Edits the *user* makes by direct manipulation
   (persisted to `direct_edits.json` / `overrides.css`) reconcile with subsequent *agent*
   edits without silent loss (oracle: `direct_edit_clobbered_fail.json`).
5. **No false affordance.** An element gets an edit affordance **only** if it carries a
   resolvable `data-disco-field` (or an ancestor `data-oid` pointing at a real workspace
   file). Non-stamped → walk-up select still works, edit affordance is gated off
   (mirrors `docs/A1-click-to-edit-design.md §"No false affordance"`).

---

## 2. The attribute family — overview table

| Attribute | Granularity | Maps to | Emitted by | Unique? | Survives edits? |
| --- | --- | --- | --- | --- | --- |
| `data-disco-field` | one editable value | a source text span (file+span / appspec field path) | app render / starter kit | unique per artifact | **MUST** preserve |
| `data-disco-section` | a content block | an AppKit section / HTML region | app render / `app_add_section` | unique per artifact | **MUST** preserve |
| `data-disco-file` | element ↔ backing file | a workspace-relative file path | app render / stamper | not unique (many els/file) | **MUST** preserve |
| `data-disco-flow` | a multi-step user flow | a named flow in the appspec/flowspec | app render | unique per flow | **MUST** preserve |
| `data-disco-screen-label` | a named screen/route | a screen id (prototype/app) | app render | unique per screen | **MUST** preserve |
| `data-disco-comment-anchor` | a review/comment pin | a stable anchor id (no source line) | app render (persisted) | globally unique | **MUST** preserve verbatim |
| `data-disco-metric-id` | a single stat/number | a metric in the metrics registry / appspec | app render | unique per metric | **MUST** preserve |
| `data-disco-version` | artifact version stamp | a snapshot id (`app_snapshot_version`) | host render / snapshot | one per rendered doc | **MUST** preserve |
| `data-disco-media-slot` | an image/video slot | a `ResourceManifest` slot id | app render / starter kit | unique per slot | **MUST** preserve |

> **Relationship to `data-oid` (EXISTS):** `data-oid="{relpath}:{line}"` is the coarse
> fallback. When both are present, `data-disco-field` (semantic) wins for resolution;
> `data-oid` is used to *locate the file* and as the resolution fallback when no
> `data-disco-*` is found on the click target or its ancestors.

---

## 3. Per-attribute specifications

### 3.1 `data-disco-field`

- **Meaning.** Marks a single, independently-editable **value** (a headline, a paragraph,
  a CTA label, a price). The atomic unit of click-to-edit.
- **Value grammar (BNF):**
  ```
  field-ref     ::= source-field | appspec-field
  source-field  ::= relpath ":" span-start "-" span-end [ "#" field-name ]
  appspec-field ::= "appspec:" json-pointer
  relpath       ::= <workspace-relative POSIX path, no leading "/", no "..">
  span-start    ::= line ":" col
  span-end      ::= line ":" col
  line          ::= 1*DIGIT          ; 1-based
  col           ::= 1*DIGIT          ; 1-based, UTF-16 code units
  json-pointer  ::= "/" 1*( unreserved / "~0" / "~1" )   ; RFC 6901
  field-name    ::= 1*( ALPHA / DIGIT / "_" / "-" )
  ```
- **Value regex (validation):**
  ```
  ^(?:[^/\0][^\0]*:\d+:\d+-\d+:\d+(?:#[A-Za-z0-9_-]+)?|appspec:/[^\s]+)$
  ```
- **Example HTML:**
  ```html
  <!-- static.site / interactive.prototype (file+span form) -->
  <h1 data-disco-field="index.html:12:1-12:38#hero_headline"
      data-disco-file="index.html">Launch faster with Disco</h1>

  <!-- appkit.leadgen (appspec field-path form) -->
  <h1 data-disco-field="appspec:/sections/0/headline"
      data-disco-section="hero">Launch faster with Disco</h1>
  ```
- **Maps to.** *file+span form* → an exact source span in `relpath` (edited via
  `exact_replace`, CD-TOOLS atomic). *appspec form* → a field path in
  `.disco/appspec.json`, edited via `app_update_content` / `app_set_tweak` (AppKit
  semantic tools, **EXISTS**) — NOT raw file write (governed artifact routing, CD-TOOLS).
- **Emitted by.** App render (AppKit) for the appspec form; the **server-side stamper**
  (A1.1, committed `e561000`, **EXISTS**) for the file+span form. Starter kits seed the
  initial fields. The **agent never hand-writes** `data-disco-field` — it is host-emitted.
- **Uniqueness.** The `field-name` suffix (when present) is unique within its file. The
  full `field-ref` is unique within the artifact at a given version.
- **Preservation.** On a targeted edit the *value text changes*; the **attribute and its
  ref are re-stamped by the host at render time** (serve-time only — the agent's actual
  source files stay unstamped, per A1.1b). Line/col drift after an edit is expected and
  re-derived on the next render; the **`#field-name` is stable** and is the preferred join
  key across versions.

### 3.2 `data-disco-section`

- **Meaning.** A content block / region (hero, features, pricing, footer). The unit that
  `app_add_section` / `app_remove_section` / `app_reorder_section` (**EXISTS**) operate on.
- **Value grammar (BNF):**
  ```
  section-ref ::= section-id
  section-id  ::= ALPHA *( ALPHA / DIGIT / "_" / "-" )
  ```
- **Regex:** `^[A-Za-z][A-Za-z0-9_-]*$`
- **Example:**
  ```html
  <section data-disco-section="pricing" data-disco-file="index.html"> … </section>
  ```
- **Maps to.** An AppKit section in `.disco/appspec.json` (`/sections/<i>` with matching
  `id`), or an HTML region delimited by stamped boundary comments for static sites.
- **Emitted by.** App render / `app_add_section`. Starter kit seeds initial sections.
- **Uniqueness.** Unique within the artifact. A reorder changes DOM order but **not** the
  `section-id`.
- **Preservation.** `section-id` is **immutable** for the life of the section; remove +
  re-add creates a *new* id (never reuse). Reorder/edit MUST keep ids stable.

### 3.3 `data-disco-file`

- **Meaning.** Names the workspace file backing this element. The coarse locator; the
  semantic peer of `data-oid`'s file half.
- **Value grammar:** a workspace-relative POSIX path.
  ```
  file-ref ::= relpath
  relpath  ::= segment *( "/" segment )
  segment  ::= 1*( unreserved-char )   ; no ".", no "..", no leading "/"
  ```
- **Regex:** `^(?!/)(?!.*(?:^|/)\.\.?(?:/|$))[^\0]+$`  (rejects absolute paths and
  `.`/`..` segments — see `RESOURCE_AND_PROVENANCE_SPEC.md` "no absolute host paths").
- **Example:** `data-disco-file="components/pricing.html"`
- **Maps to.** A real file in the conversation workspace. MUST be project-relative; an
  absolute host path is a hard failure (oracle: `absolute_host_path_fail.json`).
- **Emitted by.** Stamper / app render.
- **Uniqueness.** **Not** unique — many elements share one file. (It is a *grouping* key.)
- **Preservation.** Stable unless the file is renamed; a rename re-stamps all descendants.

### 3.4 `data-disco-flow`

- **Meaning.** Marks the root element of a named, multi-step user flow (signup flow,
  checkout flow). Lets a mention like "the checkout flow" resolve to a flow definition
  rather than a single element.
- **Value grammar:**
  ```
  flow-ref  ::= flow-id [ "#" step-id ]
  flow-id   ::= ALPHA *( ALPHA / DIGIT / "_" / "-" )
  step-id   ::= ALPHA *( ALPHA / DIGIT / "_" / "-" )
  ```
- **Regex:** `^[A-Za-z][A-Za-z0-9_-]*(?:#[A-Za-z][A-Za-z0-9_-]*)?$`
- **Example:**
  ```html
  <form data-disco-flow="checkout#payment" data-disco-screen-label="payment"> … </form>
  ```
- **Maps to.** A flow entry in the appspec/flowspec (**GAP** — flowspec is new; for
  `interactive.prototype` it maps to a sequence of `data-disco-screen-label` screens).
- **Emitted by.** App render.
- **Uniqueness.** `flow-id` unique per artifact; `step-id` unique within its flow.
- **Preservation.** Both ids immutable; reordering steps preserves ids.

### 3.5 `data-disco-screen-label`

- **Meaning.** Names a screen / route / view (esp. `interactive.prototype`). Lets "the
  payment screen" resolve to a screen, and lets the host build a screen index.
- **Value grammar:**
  ```
  screen-ref ::= screen-id
  screen-id  ::= ALPHA *( ALPHA / DIGIT / "_" / "-" )
  ```
- **Regex:** `^[A-Za-z][A-Za-z0-9_-]*$`
- **Example:** `<main data-disco-screen-label="dashboard"> … </main>`
- **Maps to.** A screen id in the prototype's screen registry / a route entry.
- **Emitted by.** App render.
- **Uniqueness.** Unique per artifact.
- **Preservation.** Immutable for the screen's life.

### 3.6 `data-disco-comment-anchor`

- **Meaning.** A **stable pin** for a review comment / annotation. Critically, it carries
  **no source line** — it is a content-independent identity so a comment stays attached to
  "this element" even as surrounding source shifts. This is the anchor whose preservation
  is load-bearing.
- **Value grammar:**
  ```
  anchor-ref ::= "ca_" 22*22( base62 )      ; ULID-like, fixed length
  base62     ::= ALPHA / DIGIT
  ```
- **Regex:** `^ca_[A-Za-z0-9]{22}$`
- **Example:**
  ```html
  <p data-disco-field="index.html:40:1-40:60#cta_sub"
     data-disco-comment-anchor="ca_01HZX4P7Q2R8S3T9V6W1Y0">Try it free for 14 days</p>
  ```
- **Maps to.** A row in the comment store keyed by anchor id (host-side). It does **not**
  map to a source line — that is the point.
- **Emitted by.** App render, **persisted**. Created when a user (or agent) first leaves a
  comment on an element. The host writes it into the source as a stable id and re-emits it
  on every render.
- **Uniqueness.** **Globally unique** (across the artifact and across versions). Reuse is
  forbidden; a lost anchor must NOT be regenerated under a new id (that orphans the
  comment).
- **Preservation rules (HARD):**
  1. A targeted edit on an element MUST carry its `data-disco-comment-anchor` through
     **verbatim** — same id, exactly once.
  2. **Duplication is failure** — two elements with the same anchor id (oracle:
     `comment_anchor_duplicated_fail.json`).
  3. **Loss is failure** — an anchor present in version N missing in version N+1 with no
     explicit delete (oracle: `comment_anchor_lost_fail.json`).
  4. A *passing* edit preserves every pre-existing anchor unchanged (oracle:
     `comment_anchor_preserved_pass.json`).
  5. Anchors are preserved even when the element's **text** is fully replaced — the anchor
     is on the element identity, not the text.

### 3.7 `data-disco-metric-id`

- **Meaning.** Marks a single rendered **statistic / number** (e.g. "10,000 users",
  "99.9% uptime"). Lets the design-discipline lint and provenance checks find every stat,
  and lets "change the uptime number" resolve precisely.
- **Value grammar:**
  ```
  metric-ref ::= "m_" metric-name
  metric-name ::= ALPHA *( ALPHA / DIGIT / "_" )
  ```
- **Regex:** `^m_[A-Za-z][A-Za-z0-9_]*$`
- **Example:**
  ```html
  <span data-disco-metric-id="m_uptime_pct"
        data-disco-field="index.html:88:14-88:19#uptime">99.9%</span>
  ```
- **Maps to.** A metric entry in the metrics registry (**GAP**) carrying its
  **provenance tag** (`provided_by_user` / `derived_from_research` /
  `generated_placeholder` / `requires_user_input` — see
  `CONTENT_AND_DESIGN_DISCIPLINE_SPEC.md`). A `generated_placeholder` metric is a
  **fake-stat** lint finding unless backed.
- **Emitted by.** App render.
- **Uniqueness.** Unique per artifact.
- **Preservation.** `metric-id` immutable; the displayed value may change via targeted
  edit, but the provenance tag MUST travel with it.

### 3.8 `data-disco-version`

- **Meaning.** Stamps the rendered document with the artifact version it was produced
  from. One per rendered doc (on `<html>` or `<body>`). Lets the host detect a
  stale-preview / fresh-read mismatch (CD-TOOLS fresh-read guard).
- **Value grammar:**
  ```
  version-ref ::= "v_" snapshot-id
  snapshot-id ::= 1*( ALPHA / DIGIT / "_" / "-" )
  ```
- **Regex:** `^v_[A-Za-z0-9_-]+$`
- **Example:** `<body data-disco-version="v_2026_06_30_0007"> … </body>`
- **Maps to.** A snapshot produced by `app_snapshot_version` (**EXISTS**) or the host's
  per-render version counter.
- **Emitted by.** Host render only (never the agent, never the kit).
- **Uniqueness.** Exactly one per rendered document; monotonically increasing.
- **Preservation.** Re-stamped on every render (it *must* change when content changes).
  A click-to-edit resolution MUST verify the page's `data-disco-version` matches the
  current source version before editing — a mismatch triggers a **fresh-read** (CD-TOOLS)
  instead of editing a stale span.

### 3.9 `data-disco-media-slot`

- **Meaning.** Marks an image/video placeholder or filled media element. The DOM peer of a
  `ResourceManifest` slot. Used when real assets are missing (honest placeholder, never a
  fake logo/photo).
- **Value grammar:**
  ```
  slot-ref ::= "slot_" slot-name
  slot-name ::= ALPHA *( ALPHA / DIGIT / "_" )
  ```
- **Regex:** `^slot_[A-Za-z][A-Za-z0-9_]*$`
- **Example (empty placeholder):**
  ```html
  <figure data-disco-media-slot="slot_hero_image"
          data-disco-field="appspec:/media/hero">
    <div class="disco-media-placeholder" aria-label="Image slot: hero_image (no asset yet)"></div>
  </figure>
  ```
- **Maps to.** A `ResourceManifest` entry (`RESOURCE_AND_PROVENANCE_SPEC.md`) by
  `slot-name`. When filled, the manifest's `copied_path` (project-relative only) is the
  src. A passing empty-slot render is the oracle `media_slot_placeholder_pass.json`.
- **Emitted by.** App render / starter kits (`image_slot`, `metrics_overlay` kits are
  **GAP**, see `_GROUNDING.md §6`).
- **Uniqueness.** Unique per artifact.
- **Preservation.** `slot-name` immutable; filling/emptying a slot MUST keep the id (it is
  the join key to the manifest).

---

## 4. Resolution algorithm (rendered element → source location)

> Inputs: a clicked/mentioned DOM element `E` and the current artifact version `V`.
> Output: a **resolved edit target** `{kind, file?, span?, appspec_pointer?, anchors[],
> media_slot?, metric_id?}` or `NO_TARGET` (→ no edit affordance).

```
resolve(E, V):
  1. STALE GUARD:
     doc_version = nearest_ancestor(E, "data-disco-version")?.value
     if doc_version != V:                       # preview is stale
        return FRESH_READ_REQUIRED              # CD-TOOLS fresh-read; do NOT edit
  2. SEMANTIC FIELD (preferred):
     f = E.attr("data-disco-field") or nearest_ancestor(E, "data-disco-field")
     if f present and validates(field-ref):
        if f starts with "appspec:":
           target.kind = "appspec"; target.appspec_pointer = json_pointer(f)
        else:
           target.kind = "source"; (target.file, target.span, name) = parse(f)
        goto 6
  3. COARSE FALLBACK (data-oid, EXISTS):
     o = nearest_ancestor(E, "data-oid")        # resolveRef() walk-up, EXISTS
     if o present and o.file is a real workspace file:
        target.kind = "source"; (target.file, target.line) = parse_oid(o)
        target.span = whole_line(target.line)   # coarse; edit still exact_replace
        goto 6
  4. NO resolvable field/oid:
     return NO_TARGET                           # walk-up select ok; NO edit affordance
  6. ATTACH CONTEXT (collected from E and ancestors, nearest-wins):
     target.section     = nearest_ancestor(E, "data-disco-section")?.value
     target.file        = target.file or nearest_ancestor(E, "data-disco-file")?.value
     target.flow        = nearest_ancestor(E, "data-disco-flow")?.value
     target.screen      = nearest_ancestor(E, "data-disco-screen-label")?.value
     target.metric_id   = E.attr("data-disco-metric-id")
     target.media_slot  = nearest_ancestor(E, "data-disco-media-slot")?.value
  7. COLLECT ANCHORS TO PRESERVE (load-bearing):
     target.anchors = all "data-disco-comment-anchor" on E and on every descendant
                      that lies inside target.span      # must round-trip verbatim
  8. VALIDATE file path:
     if target.file is absolute or contains "..":
        return ERROR(absolute_host_path)        # RESOURCE_AND_PROVENANCE_SPEC
  9. return target
```

**Edit dispatch from a resolved target:**

| `target.kind` | Tool used | Rewrite allowed? |
| --- | --- | --- |
| `source` (static.site / prototype / document) | `exact_replace` on `target.file` at `target.span` (CD-TOOLS atomic) | **No** — `EditContract.rewrite_allowed=False` |
| `appspec` (appkit.leadgen) | `app_update_content` / `app_set_tweak` at `appspec_pointer` (AppKit, EXISTS) | **No** |
| `media_slot` | manifest update + render; see `RESOURCE_AND_PROVENANCE_SPEC.md` | n/a |

The host always wraps the edit instruction so it stays **targeted**, e.g.
`"In {file} at {span} (field {name}; preserve anchors {anchors}): {instruction}"` —
mirrors the shipped `steer()` wire (`docs/A1-click-to-edit-design.md §A1.4`).

---

## 5. Comment-anchor preservation (the load-bearing invariant)

Anchors are the one attribute whose loss is silent and expensive (orphaned review
comments). Rules, restated as enforceable checks:

| # | Rule | Detect | Oracle |
| --- | --- | --- | --- |
| C1 | Every anchor in `target.span` before the edit is present after, exactly once. | diff anchor-set(before) vs anchor-set(after) | `comment_anchor_preserved_pass.json` |
| C2 | No anchor appears twice in the whole artifact. | global anchor multiset count == 1 each | `comment_anchor_duplicated_fail.json` |
| C3 | No anchor present at version N disappears at N+1 without an explicit delete event. | set-difference across versions | `comment_anchor_lost_fail.json` |
| C4 | Anchor id format is `^ca_[A-Za-z0-9]{22}$`; never regenerated for the "same" comment. | regex + stable-id ledger | (covered by C2/C3) |
| C5 | Text replacement that empties an element still keeps its anchor. | edit oracle: empty-text + anchor present | `comment_anchor_preserved_pass.json` |

> A correct `exact_replace` (CD-TOOLS) that swaps **only the inner text** of a span
> trivially preserves attributes including the anchor. Anchors break when an edit is
> actually a **rewrite** of the enclosing element/file — which is exactly what
> `targeted_edit_rewrite_fail.json` captures.

---

## 6. Direct-edit override reconciliation

Users can edit *directly* in the preview (drag, inline-type, recolor). Those edits are
**not** agent edits and are persisted out-of-band so a later agent edit cannot silently
erase them.

- **Where user direct edits live:**
  - `direct_edits.json` — structured per-field overrides:
    `{ "<field-ref or #field-name>": { "value": "...", "ts": "...", "by": "user" } }`
  - `overrides.css` — user style overrides keyed by a stable selector
    (`[data-disco-field="…"]` / `[data-disco-section="…"]`).
- **Reconciliation algorithm (agent edit lands on a field with a user override):**
  ```
  on agent_edit(field f, new_value):
    u = direct_edits.json[key_of(f)]
    if u is None:
       apply exact_replace(f, new_value)                 # no conflict
    else if u.value == current_source_value(f):
       # user override already reflected; agent edit supersedes intentionally
       apply exact_replace(f, new_value); record supersede(u)
    else:
       # CONFLICT: user changed it, agent also wants to change it
       DO NOT clobber. Emit a reconciliation record:
         keep user value in source; attach agent's proposed value as a pending
         suggestion on the field; surface both in UI.
       NEVER overwrite u.value silently.
  ```
- **Failure being guarded:** an agent edit overwrites a user's direct edit with no
  conflict record → oracle `direct_edit_clobbered_fail.json` (FAIL verdict). A passing
  reconcile keeps the user value and records the supersede/conflict.
- **CSS overrides** are layered **after** generated CSS at render time (last-wins) and are
  never rewritten by the agent; an agent style change that would contradict
  `overrides.css` is recorded as a conflict, not applied over it.

---

## 7. Targeted-edit oracle expectations (passing vs rewrite)

| Property | Passing targeted edit (`targeted_edit_pass.json`) | Rewrite (FAIL) (`targeted_edit_rewrite_fail.json`) |
| --- | --- | --- |
| Tool | `exact_replace` (CD-TOOLS, atomic) | `file_write` / full `safe_write_file` of entrypoint |
| Bytes changed | only the resolved span | whole file / whole element subtree |
| Lines outside span | **byte-identical** | changed/reflowed |
| Comment anchors | all preserved verbatim (C1–C5) | dropped or duplicated |
| `data-disco-*` ids on untouched elements | unchanged | re-numbered / lost |
| Unrelated sections | unchanged | reformatted |
| `rewrite_allowed` honored | yes (False) | violated |
| Verdict | **PASS** | **FAIL** |

> A passing edit is defined operationally: **the post-edit file equals the pre-edit file
> with exactly one contiguous span replaced.** Anything broader is a rewrite, even if the
> rendered output "looks the same".

---

## 8. Worked end-to-end example

**Scenario:** user clicks the hero headline in a built `static.site` preview and changes
it to "Ship faster with Disco".

1. **Render (host).** The preview-edit route (`A1.1b`, server-stamped) serves:
   ```html
   <body data-disco-version="v_2026_06_30_0007">
     <section data-disco-section="hero" data-disco-file="index.html">
       <h1 data-disco-field="index.html:12:1-12:24#hero_headline"
           data-disco-comment-anchor="ca_01HZX4P7Q2R8S3T9V6W1Y0">Launch faster with Disco</h1>
     </section>
   </body>
   ```
2. **Click.** User clicks the `<h1>`. The selection overlay (A2, **EXISTS**) captures `E`.
3. **Resolve** (`resolve(E, V=v_2026_06_30_0007)`):
   - Stale guard: page `data-disco-version` == current `V` → proceed.
   - `data-disco-field` present + valid → `kind=source`,
     `file=index.html`, `span=12:1-12:24`, `name=hero_headline`.
   - Context: `section=hero`, `file=index.html`.
   - Anchors in span: `["ca_01HZX4P7Q2R8S3T9V6W1Y0"]`.
   - Path check: relative, no `..` → ok.
4. **Affordance.** Target is resolvable → inline edit input appears (no false affordance).
5. **Edit instruction → steer** (A1.4 wire, **EXISTS**):
   `"In index.html at 12:1-12:24 (field hero_headline; preserve anchor
   ca_01HZX4P7Q2R8S3T9V6W1Y0): change to 'Ship faster with Disco'"`.
6. **Override check.** `direct_edits.json["#hero_headline"]` is absent → no conflict.
7. **Apply.** `exact_replace(index.html, "Launch faster with Disco" @12:1-12:24,
   "Ship faster with Disco")` — atomic, CD-TOOLS. Only that span changes.
8. **Re-render.** Host re-stamps; new `data-disco-version="v_2026_06_30_0008"`; the
   `<h1>` now reads "Ship faster with Disco" and **still carries**
   `data-disco-comment-anchor="ca_01HZX4P7Q2R8S3T9V6W1Y0"` (C1 satisfied).
9. **Oracle check.** Post-file == pre-file with one span replaced; anchor preserved →
   matches `targeted_edit_pass.json` + `comment_anchor_preserved_pass.json`. A run that
   instead rewrote `index.html` would match `targeted_edit_rewrite_fail.json` /
   `comment_anchor_lost_fail.json`.

---

## 9. Failure modes

- [ ] **Stale resolution** — editing against a span from an older `data-disco-version`
  (no fresh-read guard) → wrong bytes edited.
- [ ] **Rewrite masquerading as edit** — whole-file `file_write` instead of
  `exact_replace`; passes visual check but fails the byte-diff oracle.
- [ ] **Comment-anchor loss** — anchor dropped during edit → orphaned review comment.
- [ ] **Comment-anchor duplication** — same `ca_…` on two elements → ambiguous comment.
- [ ] **Anchor regeneration** — a lost anchor re-created under a new id → comment still
  orphaned, now silently.
- [ ] **Direct-edit clobber** — agent overwrites a user's `direct_edits.json` value with
  no conflict record.
- [ ] **Override drift** — `overrides.css` rewritten/ignored by the agent.
- [ ] **Absolute / `..` path in `data-disco-file`** — escapes the workspace.
- [ ] **False affordance** — edit UI shown on a non-resolvable element (no
  `data-disco-field`, no real-file `data-oid`).
- [ ] **Agent-emitted `data-disco-*`** — the agent hand-writing stamper-owned attributes
  (must be host/render-emitted only).
- [ ] **Mutated immutable id** — a `section-id` / `metric-id` / `slot-id` / anchor changed
  by an edit.
- [ ] **appspec vs source confusion** — an appkit field edited via raw `file_write`
  instead of `app_update_content` (violates governed artifact routing, CD-TOOLS).

---

## 10. Tests required

- [ ] **Grammar validators** — for each `data-disco-*`, a property test that the regex/BNF
  accepts the valid examples here and rejects: absolute paths, `..`, empty ids, wrong
  prefix, and (for anchors) wrong length.
- [ ] **Resolution unit tests** — `resolve(E, V)` returns the correct target for: direct
  field hit, ancestor field hit, `data-oid` fallback, no-target, stale-version
  (FRESH_READ_REQUIRED), absolute-path error.
- [ ] **Anchor preservation** — golden oracles: `comment_anchor_preserved_pass.json`,
  `comment_anchor_lost_fail.json`, `comment_anchor_duplicated_fail.json` (C1–C5).
- [ ] **Targeted-vs-rewrite** — `targeted_edit_pass.json` (one-span byte-diff) vs
  `targeted_edit_rewrite_fail.json` (whole-file change); assert verdicts.
- [ ] **Direct-edit reconciliation** — `direct_edit_clobbered_fail.json` (FAIL) plus a
  passing reconcile fixture (user value kept + conflict recorded).
- [ ] **Path safety** — any `data-disco-file` resolving outside the workspace →
  `absolute_host_path_fail.json` verdict (shared with
  `RESOURCE_AND_PROVENANCE_SPEC.md`).
- [ ] **No false affordance** — render a non-stamped element; assert no edit affordance,
  walk-up select still available.
- [ ] **Idempotent stamping** — re-stamping a stamped doc is a no-op (reuses A1.1 stamper
  unit-test property, **EXISTS**).
- [ ] **End-to-end (live, host-driven)** — the §8 round-trip on a real static site:
  click → resolve → `exact_replace` → re-render → anchor preserved (a *live model* run is
  the only proof the feature works; oracle fixtures prove "didn't regress").
