# Content & Design Discipline Spec — `ContentStyle`, content provenance, design-slop lint

> **Read `_GROUNDING.md` first.** Documentation only — no runtime behavior implemented or
> claimed. This spec defines (1) `ContentStyle` as a value object, (2) the content-
> provenance tag system that keeps every word/number sourced, and (3) the design-slop lint
> that catches the visual tells of AI filler — plus the rationale override mechanism that
> lets an intentional design decision downgrade a finding with an audit trail.
>
> Cross-references: `DIRECT_MANIPULATION_SPEC.md` (`data-disco-metric-id` carries a
> provenance tag; `data-disco-field` surfaces it in UI), `RESOURCE_AND_PROVENANCE_SPEC.md`
> ("no fake logos/photos/testimonials", media slots for missing assets), `_GROUNDING.md`
> (TweakSpec §7, brand registry, real names), and oracle fixtures in `fixtures/oracles/`.

---

## 0. Status legend

| Marker | Meaning |
| --- | --- |
| **(EXISTS)** | Real today. Referenced, not re-specced. |
| **(GAP)** | New surface proposed by this pack. Not implemented. |

`ContentStyle`, the content-provenance tags, the design-slop lint, and the
`DesignSpec`-rationale downgrade are all **(GAP)**. They build on **(EXISTS)** substrate:
TweakSpec (`tweaks.py`, `_GROUNDING.md §7`), the brand registry (`kits/brand.py`), and
the `data-disco-*` grammar (`DIRECT_MANIPULATION_SPEC.md`).

---

## 1. Content-tag table (read this first)

Every user-visible content unit (a `data-disco-field`, a `data-disco-metric-id`, a media
slot) carries exactly one **provenance tag**. Tags are stored per-field in
`.disco/content_provenance.json` (keyed by `data-disco-field` `#field-name` /
`data-disco-metric-id`) and surfaced in the editor UI as a small badge on each field.

| Tag | Semantics | Where stored | Surfaced in UI as | Export gate |
| --- | --- | --- | --- | --- |
| `provided_by_user` | Verbatim or lightly-edited user-supplied content. | `content_provenance.json[field]` | green "from you" badge | always allowed |
| `derived_from_research` | Synthesized from a cited, retrievable source (`search`/`extract`, `_GROUNDING.md §4`); MUST carry a `source_ref`. | `content_provenance.json[field].source_ref` | blue "researched" badge w/ source link | allowed if `source_ref` resolves |
| `generated_placeholder` | Model-written stand-in (lorem-ish, sample copy, sample stat). | `content_provenance.json[field]` | amber "placeholder" badge | **flagged** at export; blocked for stats/logos/testimonials |
| `requires_user_input` | A field/slot the user must fill (no honest content possible yet). | `content_provenance.json[field]` | red "needs you" badge | **blocked** from a "final" export; allowed as draft |

**Rules:**
1. Every `data-disco-metric-id` (a stat/number) MUST be `provided_by_user` or
   `derived_from_research`. A `generated_placeholder` stat is a **fake stat** (lint
   `fake-stats`, §3) — never ship it as real.
2. Testimonials/logos follow `RESOURCE_AND_PROVENANCE_SPEC.md §R10`: fabricated → must be
   `generated_placeholder`/`requires_user_input`, never presented as real.
3. No **filler content** (`generated_placeholder` text that adds nothing) survives a final
   export without being flagged.
4. The tag travels with the field across targeted edits
   (`DIRECT_MANIPULATION_SPEC.md §3.7` preservation).

---

## 2. `ContentStyle` value object

A frozen value object (proposed Pydantic v2, `extra="forbid"`, mirroring the contract
value objects in `_GROUNDING.md §2`) capturing the intended voice of an artifact's copy.
Stored at `.disco/content_style.json`; referenced by the prompt-pack assembler
(`workflows/assembly.py`, **EXISTS**) so the model writes copy in-style.

| Field | Type | Required | Allowed values / meaning |
| --- | --- | --- | --- |
| `tone` | enum | yes | `professional` \| `friendly` \| `playful` \| `authoritative` \| `technical` \| `minimal` |
| `density` | enum | yes | `terse` \| `balanced` \| `detailed` — words-per-section budget. |
| `voice` | enum | yes | `first_person` \| `second_person` \| `third_person` \| `brand` (brand-registry voice, `kits/brand.py`). |
| `audience` | string | yes | Free-text audience descriptor, e.g. "small-business owners", "developers". |
| `reading_level` | enum | no | `general` \| `expert` (default `general`). |
| `banned_phrases` | tuple<string> | no | Phrases to avoid (e.g. "unlock", "seamless", "game-changer"). |

**JSON example:**
```json
{
  "tone": "friendly",
  "density": "balanced",
  "voice": "second_person",
  "audience": "small-business owners",
  "reading_level": "general",
  "banned_phrases": ["unlock", "seamless", "supercharge"]
}
```

**Invariants:** `tone`/`density`/`voice` required; `audience` non-empty; `banned_phrases`
de-duplicated. `ContentStyle` constrains generation but does **not** override provenance
rules — style never licenses a fake stat.

---

## 3. Design-slop lint

A static linter over rendered HTML/CSS + the `data-disco-*` grammar. Each rule has a
detection heuristic, severity, and a `downgradable?` flag (whether a `DesignSpec`
rationale may lower it — §4). Run in the verify stage (alongside `verify_web_app`,
**EXISTS**, `_GROUNDING.md §4`).

### 3.1 Lint-rule table

| Rule id | Heuristic | Severity | Downgradable? |
| --- | --- | --- | --- |
| `generic-gradients` | CSS `linear-gradient(...)` matching the cliché palettes (purple→blue `#667eea`/`#764ba2`-family, "indigo→violet") used on hero/background; ≥1 occurrence. | warn | yes |
| `emoji-as-icons` | An emoji char (Unicode `Emoji_Presentation`) used as a UI affordance/icon inside a button/link/list-marker (not in prose). | warn | yes |
| `left-border-cards` | A card/box styled with only `border-left: Npx solid …` as its accent (the "callout bar" cliché), ≥2 instances. | info | yes |
| `fake-stats` | A `data-disco-metric-id` tagged `generated_placeholder`, OR a number-bearing element with no metric-id and no provenance, esp. round/implausible ("10,000+", "99.9%"). | **error** | **no** |
| `fake-testimonials` | A testimonial-pattern block (quote + attributed name/role/company) whose attribution has no `ResourceManifest`/provenance backing. | **error** | **no** |
| `overused-generic-fonts` | Body/heading font stack relying on a default system/Google cliché ("Inter"/"Roboto"/"Poppins") AND no brand-registry font declared. | info | yes |
| `inline-spacing-not-gap` | Layout spacing done via per-child `margin`/`<br>`/`&nbsp;` runs or empty spacer divs instead of `flex`/`grid` + `gap`. | warn | yes |

### 3.2 Per-rule detail

#### `generic-gradients` (warn, downgradable)
- **Detect.** Parse CSS; match `linear-gradient`/`radial-gradient` whose stops fall in the
  flagged cliché set; weight higher on full-bleed hero/background.
- **Example violation.**
  ```css
  .hero { background: linear-gradient(135deg, #667eea 0%, #764ba2 100%); }
  ```
- **Example fix.** Use brand-registry colors (`kits/brand.py`) or a solid/duotone tied to
  the brand: `background: var(--brand-surface);` or a gradient built from brand tokens.

#### `emoji-as-icons` (warn, downgradable)
- **Detect.** Emoji code points inside interactive/affordance elements or as list markers.
- **Example violation.** `<button>🚀 Get started</button>`,
  `<li>✅ Fast</li>`.
- **Example fix.** Use a real icon set / inline SVG: `<button><svg …/> Get started</button>`.

#### `left-border-cards` (info, downgradable)
- **Detect.** Cards whose only visual accent is `border-left`, ≥2 instances.
- **Example violation.** `.card { border-left: 4px solid #6366f1; padding: 1rem; }`
- **Example fix.** Use real card structure (subtle shadow/border-radius/background) or a
  brand accent that isn't the default left-bar cliché.

#### `fake-stats` (error, NOT downgradable)
- **Detect.** Any `data-disco-metric-id` with content tag `generated_placeholder`, or a
  conspicuous statistic with no provenance.
- **Example violation.**
  ```html
  <span data-disco-metric-id="m_users">10,000+ happy customers</span>
  <!-- content_provenance: generated_placeholder -->
  ```
- **Example fix.** Tag from a real source (`provided_by_user`/`derived_from_research` with
  `source_ref`), or convert to a `requires_user_input` field with an honest placeholder.

#### `fake-testimonials` (error, NOT downgradable)
- **Detect.** Quote + attribution pattern with no manifest/provenance backing.
- **Example violation.**
  ```html
  <blockquote>"Disco changed our business!" — Jane D., CEO, Acme</blockquote>
  ```
- **Example fix.** Remove, or replace with a `requires_user_input` testimonial slot
  (honest "add a real customer quote here").

#### `overused-generic-fonts` (info, downgradable)
- **Detect.** Cliché default font stack with no brand font declared.
- **Example violation.** `body { font-family: Poppins, sans-serif; }` with no brand font.
- **Example fix.** Declare the brand-registry font, or a deliberate pairing justified in
  the `DesignSpec`.

#### `inline-spacing-not-gap` (warn, downgradable)
- **Detect.** Spacer divs / `<br><br>` / per-child margins / `&nbsp;` runs used for layout
  instead of `flex`/`grid` `gap`.
- **Example violation.**
  ```html
  <div style="margin-bottom:8px"></div><div style="margin-bottom:8px"></div>
  ```
- **Example fix.** `display:flex; flex-direction:column; gap:8px;` on the container.

---

## 4. `DesignSpec` rationale downgrade (the override mechanism)

A finding on a **downgradable** rule (§3.1) may be lowered (e.g. `warn → info`, or
suppressed to `ack`) when an intentional design decision is recorded in the artifact's
`DesignSpec`. **`error` rules (`fake-stats`, `fake-testimonials`) are NEVER downgradable**
— honesty is not a style choice.

- **Where stored.** `.disco/design_spec.json`, an array of override records.
- **Override record schema:**

  | Field | Type | Required | Meaning |
  | --- | --- | --- | --- |
  | `rule_id` | enum | yes | A downgradable rule id (§3.1). |
  | `selector` | string | yes | The element/CSS selector or `data-disco-*` ref it applies to. |
  | `rationale` | string | yes | Why this is intentional (non-empty, ≥1 sentence). |
  | `downgrade_to` | enum | yes | `info` \| `ack` (cannot raise severity). |
  | `by` | enum | yes | `user` \| `agent` — who decided. |
  | `ts` | string (RFC3339) | yes | When. |

- **Example.**
  ```json
  {
    "rule_id": "generic-gradients",
    "selector": ".hero",
    "rationale": "Brand guidelines specify a purple→blue hero gradient; matches the logo.",
    "downgrade_to": "ack",
    "by": "user",
    "ts": "2026-06-30T12:30:00Z"
  }
  ```
- **Audit trail.** Every applied downgrade is logged with the originating record; the
  lint report shows the original severity, the downgrade, and the rationale. A downgrade
  with an empty/auto-generated rationale is itself a finding (no rubber-stamping). An
  attempted downgrade of an `error` rule is rejected and recorded as a violation.

---

## 5. How content tags reach the UI

- Each `data-disco-field` / `data-disco-metric-id` renders with a small provenance badge
  (§1 table) read from `content_provenance.json`.
- `requires_user_input` fields render the red "needs you" badge AND block a *final*
  export (`RESOURCE_AND_PROVENANCE_SPEC.md §6` / content gate); they are allowed in draft.
- `generated_placeholder` fields render amber and are listed in the export report so the
  user can replace them before shipping.

---

## 6. Failure modes

- [ ] **Fake stat shipped** — a `generated_placeholder`/unsourced number presented as real
  (`fake-stats`, error, non-downgradable).
- [ ] **Fake testimonial shipped** — unbacked quote+attribution (`fake-testimonials`,
  error, non-downgradable).
- [ ] **Fabricated logo/photo as real** — see `RESOURCE_AND_PROVENANCE_SPEC.md §R10`.
- [ ] **Filler content** — `generated_placeholder` prose that conveys nothing, unflagged.
- [ ] **Missing provenance tag** — a content field with no tag in
  `content_provenance.json`.
- [ ] **Real asset missing → no honest slot** — content rendered without a media slot when
  the asset is absent (should be `requires_user_input`, R7).
- [ ] **Improper downgrade** — an `error` rule downgraded; or a downgrade with an empty
  rationale; or a downgrade missing its audit record.
- [ ] **Design slop unaddressed** — generic gradient / emoji-icons / left-border cards /
  cliché fonts / inline spacing with no fix and no DesignSpec rationale.
- [ ] **Style overrides provenance** — `ContentStyle` used to justify fabricated content.
- [ ] **Tag lost on edit** — provenance tag dropped during a targeted edit
  (`DIRECT_MANIPULATION_SPEC.md §3.7`).

---

## 7. Tests required

- [ ] **`ContentStyle` schema** — accepts the §2 example; rejects bad enums, empty
  `audience`, duplicate `banned_phrases`.
- [ ] **Content-tag store** — every content field has exactly one valid tag;
  `derived_from_research` requires a resolvable `source_ref`.
- [ ] **`fake-stats` (error)** — a `generated_placeholder` metric → FAIL; the same number
  tagged `derived_from_research` with a valid source → PASS. Assert NON-downgradable
  (DesignSpec override of `fake-stats` is rejected).
- [ ] **`fake-testimonials` (error)** — unbacked testimonial → FAIL; `requires_user_input`
  slot → PASS. Assert non-downgradable.
- [ ] **Each downgradable rule** — a golden violation fixture trips the rule at its stated
  severity; a matching DesignSpec rationale downgrades it (with audit record); an
  empty-rationale override is itself flagged.
- [ ] **Downgrade audit** — every applied downgrade carries `rule_id`/`selector`/
  `rationale`/`downgrade_to`/`by`/`ts`; report shows original + downgraded severity.
- [ ] **Export gates** — `requires_user_input` blocks a final export but allows draft;
  `generated_placeholder` appears in the export report.
- [ ] **Tag preservation** — a targeted edit preserves the field's provenance tag
  (shared with `DIRECT_MANIPULATION_SPEC.md` targeted-edit oracles).
- [ ] **Honest-placeholder integration** — missing asset → media slot +
  `requires_user_input`, never a fabricated substitute (shared with
  `RESOURCE_AND_PROVENANCE_SPEC.md` `media_slot_placeholder_pass.json`).
- [ ] **End-to-end (live, host-driven)** — a real build that uses `ContentStyle`, sources
  one stat from research, leaves one testimonial as `requires_user_input`, and trips +
  fixes one downgradable slop rule (live run proves it works; fixtures prove no
  regression).
