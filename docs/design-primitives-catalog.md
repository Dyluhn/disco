# Design Primitives Catalog — the "fantastic output" campaign basis

Dylan's directive (2026-07-04): disco should build *fantastic-looking* sites and
slides — and stretch into games, phone apps, and more. PDF exports and the brand
templates are fine as-is. Slides work but are standardized, and **image
generation is never used in slides despite full image-gen capability**.

This catalog enumerates the primitives — reusable building blocks — across
three layers: **Foundations** (cross-medium design atoms), **Medium recipes**
(per-output-type playbooks composed from the foundations), and **Machinery**
(how primitives are delivered and enforced: prompt-pack text, starter
components, tools, lint rules, verify checks). Each entry says what it is, why
it matters, and its delivery vehicle(s).

Current honest inventory the catalog builds on: 5 thin prompt packs (mechanics
only — the static-site pack has ONE design-related line, the deck pack zero),
2 starter kits (app_shell, lead_form), 1 design_lint tool (contrast-class
checks), 6 export brand templates, image_generate with 4 backend tiers, deck +
PPTX/PDF machinery, sandboxed browser + verify_web_app.

---

## Layer A — Foundations (cross-medium design atoms)

**A1. Type-scale primitive.** Modular scales (ratios 1.2/1.25/1.333) with
per-medium presets committed as CSS custom properties *before any content is
written*: slides at 1920×1080 → `--type-title:64px / subtitle:44 / body:34 /
small:28`, web → fluid `clamp()` scale off a 16-18px base, print → 12pt floor,
mobile → 17px base. The commitment step is the point: it stops web-density
defaults leaking into slides. *Vehicle: pack text + design_lint per-medium
floor rule (already proven pattern: "validator errors under 24px").*

**A2. Spacing & rhythm primitive.** 4/8px grid; spacing tokens
(`--space-1..8`); per-medium section padding presets (slide frame 100/80px,
web hero 96-160px, card 16-24px); gap-based flex/grid layout only (no
margin-chains — survives direct edits and reads cleaner). *Vehicle: pack text
+ lint (token adherence advisory).*

**A3. Color-system primitive.** Seed color → full derived system in oklch:
hue-shifted neutrals (never pure gray), accent ramp (50–900), semantic tokens
(bg / surface / raised / text / muted / accent / positive / warn), automatic
AA contrast pairing, dark-mode derivation from the same seed. One seed in,
coherent system out — the model never invents ad-hoc hexes mid-build.
*Vehicle: pack recipe + a pure helper the model can run via code_exec, or a
palette section emitted by the direction picker (C1); lint already checks
contrast — extend to flag off-token colors.*

**A4. Font-pairing primitive.** A curated library of 10–14 vetted pairings
with vibe tags — editorial (serif display + humanist sans), technical
(grotesk + mono), luxury (didone + light sans), playful (rounded geometric),
brutalist (mono everywhere), etc. — each with self-hostable files or system
stacks, `@font-face` snippets, and explicit *avoid* list (Inter/Roboto/Arial
as defaults = AI-slop tell). *Vehicle: starter asset (fonts + pairings.json) +
pack table; core/brand/fonts already exists for exports — unify (C5).*

**A5. Surface & elevation primitive.** Shadow scale, radius tokens, surface
layering rules, and 4–5 *coherent* treatment sets (flat, soft-depth,
glass, neobrutalist, outlined) — pick ONE per project; mixing treatments is
the tell of unintentional design. *Vehicle: pack text + direction picker.*

**A6. Layout & grid primitive.** 12-col web grid with named breakpoints;
slide safe-areas and rule-of-thirds anchors; section archetypes as reusable
skeletons: hero (5 variants), feature triplet, split/zigzag, stat band,
testimonial, pricing, CTA band, footer. Archetypes are the anti-sameness
lever for sites just as slide archetypes are for decks. *Vehicle: pack
library + optional starter snippets.*

**A7. Iconography primitive.** A bundled permissive inline-SVG icon subset
(Lucide, ~100 curated glyphs) with stroke/sizing rules; no emoji unless the
brand uses them; never SVG-draw imagery by hand. *Vehicle: starter asset +
pack rule.*

**A8. Imagery primitive — THE image-gen integration (Dylan's named gap).**
Makes generated images first-class in every medium:
- **Art-direction spec**: per-project image style contract (style keywords,
  palette-locked descriptors, lighting, medium — e.g. "grainy risograph,
  two-tone teal/sand") derived from the design direction, so every generated
  image in a project shares a look.
- **Slot types**: full-bleed hero (aspect-fill), aspect-fit
  diagram/screenshot, background texture/gradient field, spot illustration,
  avatar, section divider art.
- **Prompt synthesis**: slot + art direction + slide/section content → the
  image_generate prompt is composed automatically; the model never writes
  bare "an image of X" prompts.
- **Treatment rules**: text-over-image requires a protection layer (gradient
  scrim, card, or blur); tone/duotone images toward the palette; never
  overlay text on aspect-fit screenshots.
- **Budget + reuse**: N images per deck/site by default (e.g. cover + one
  per section), cached and re-used across edits — regenerate only on
  direction change.
*Vehicle: pack recipes (deck + site) + a small `image_slot` helper seam in
the deck/site pipelines that carries slot metadata; deck already embeds
images (C7 fix) — this feeds it systematically.*

**A9. Motion primitive.** Easing tokens (standard/decelerate/spring),
duration scale (120/200/320/500ms), entrance + scroll-reveal patterns,
hover micro-interactions, `prefers-reduced-motion` compliance, slide
transitions; a timeline animation engine starter for hero scenes and
animated explainers (harvest the design-doc `animations.jsx` concept —
Stage/Sprite/scrubber/Easing — as our own implementation). *Vehicle: pack
tokens + starter component (C2).*

**A10. Voice & content primitive.** Copywriting rules that outlast any one
medium: no filler content ("one thousand no's for every yes"), no data slop
(decorative stats/icons), ask before inventing material, title parallelism,
action-titles vs topic-titles chosen once per artifact, banned AI-tells
("It's not X. It's Y.", "The magic moment", punchline titles), text-density
budgets per medium. *Vehicle: pack text (shared include) + deck lint (C3).*

---

## Layer B — Medium recipes (compositions of the foundations)

**B1. Web-site recipe** (the "fantastic sites" core). Aesthetic-direction
commitment step (C1) → named direction from a library of 8–10 (editorial
magazine, Swiss/international, brutalist, soft-depth SaaS, terminal/mono,
luxury serif, playful geometric, retro-print, dark-glass, warm-craft), each
defining type pairing + palette seed + surface treatment + motion level →
section archetypes (A6) → imagery slots (A8) → responsive rules + favicon/OG
meta + performance floor (inline critical CSS, lazy images). *Vehicle:
rewrite build_static_site pack (~5× current depth) + direction library file
+ lint additions.*

**B2. Slide-deck recipe** (the "standardized slides" fix). Three levers:
- **Craft discipline** (design-doc harvest): full title sequence written
  FIRST in one grammatical style ("titles alone tell the story"), type scale
  committed as CSS vars before slide one, pt→px conversion (36pt → 48px),
  24px floor, parallelism of repeated elements, verification tips that name
  the web-reflexes to resist (bottom whitespace on slides is correct).
- **Archetype variety**: slide archetype library — title, section divider,
  big-number, full-bleed image + statement, quote, comparison table,
  timeline/roadmap, diagram, 2×2, photo-grid, closing — with a target mix
  ("no more than 2 consecutive bullet slides"; every section gets a divider
  or full-bleed).
- **Imagery slots** (A8): cover art + section dividers + content images by
  default, generated under the deck's art direction.
*Vehicle: rewrite build_deck pack + deck-pipeline image-slot seam + deck
lint rules (text budget, archetype mix, floor).*

**B3. Document/PDF recipe.** Per Dylan: mostly fine. Only additions: A8
imagery slots (cover art, section art) and the A10 shared content rules.
*Vehicle: light build_document pack additions only.*

**B4. Interactive prototype / web-app recipe.** App-shell patterns (nav
rail, topbar+sidebar, split view, mobile tabs), state-feel rules (hover/
active/focus on EVERY interactive element, skeleton loaders, empty states,
error states — no false affordances, per the standing house rule), baseline
component kit (buttons, inputs, cards, modals, tables, toasts) as a starter,
data-viz primitive (Chart.js per the RP decision: chart archetypes bound to
the palette ramp, no rainbow defaults). *Vehicle: fatten
build_interactive_prototype pack + `ui_kit` starter + chart archetype
snippets.*

**B5. Game recipe (new medium).** Tiered:
- **Tier 1 — vanilla canvas starter** (zero deps, works in the sandbox
  preview today): fixed-timestep game loop, entity/update/render, input map
  (keyboard/touch), sprite-sheet loader, WebAudio one-shot sounds, scene
  stack (menu/play/game-over). Archetype variants: platformer, top-down,
  puzzle-grid, endless runner, card/board game.
- **Tier 2 — engine builds**: Phaser 3 (MIT) via npm in the sandbox for
  bigger asks; scaffold + build + preview pipeline same as vite apps.
- **Game-juice primitives**: screen shake, particles, tweens, hit-stop,
  score pop — the difference between "works" and "feels great".
- **Asset pipeline**: A8 imagery for backgrounds/sprites (image_generate →
  sprite slots), procedural palettes for tiles.
*Vehicle: NEW build_game pack + game_loop starter kit + Phaser scaffold
path; verify = playthrough smoke via the browser tool (start, input, score
changes).*

**B6. Phone-app recipe (new medium).** PWA-first: manifest + service worker
+ installability scaffold; mobile design rules (44px hit targets, thumb-zone
placement, safe-area insets, tab-bar/stack-nav patterns, sheet modals);
device-frame starter (our own iOS/Android bezel components — harvest the
design-doc frame concept) so previews present as phones; platform-correct
patterns per OS look. Real-native path = handoff export (Expo/React Native
starter + handoff README) rather than in-sandbox native builds. *Vehicle:
NEW build_mobile_app pack + device-frame + pwa_shell starters + lint
(hit-target rule).*

**B7. Dashboard/data-app recipe.** Dense-UI tokens (tighter spacing scale),
KPI cards, table patterns (sticky headers, zebra option, numeric alignment),
chart grid layouts, filter bars. *Vehicle: pack section under B4 + chart
archetypes.*

**B8. Fixed-canvas graphics recipe.** Posters, social posts, OG images,
banners, simple infographics: explicit pixel-size root (PDF export
auto-sizes — already works), oversized type rules, A8 background art.
*Vehicle: small pack + reuse of existing PDF sizing.*

**B9. 3D/hero-scene recipe (stretch).** three.js starter for product spins,
particle heroes, generative backgrounds; strictly optional garnish on B1.
*Vehicle: starter + pack paragraph.*

**B10. Email template recipe (optional).** Table-based HTML email with
bulletproof-button patterns and inline styles; niche but requested often in
the wild. *Vehicle: small pack; low priority.*

---

## Layer C — Machinery (delivery + enforcement)

**C1. Design-direction picker (the keystone).** A pre-build commitment step:
pick direction (B1 library) + font pairing (A4) + palette seed (A3) + surface
treatment (A5) + motion level (A9) + image art direction (A8) — either asked
via questions_v2 (interactive) or chosen-and-stated (autonomous), then
WRITTEN to `.disco/context/design_direction.md` so it survives condensation
and recurs via the context pack. Prevents mid-build drift and cross-build
sameness. *Vehicle: pack step + a context-memory kind; the CXT machinery for
durability already exists.*

**C2. Starter-component library expansion.** From 2 kits to ~10: game_loop,
pwa_shell, ui_kit, device frames (ios/android), window chrome
(browser/macos), animation timeline engine, image-slot helper, chart
archetypes, deck theme starter. Each host-owned, dropped by scaffold_starter
with usage notes (existing mechanism — extend the catalog). The design-doc
upload carries ~6,400 lines of reference source for five of these; adapt as
our own implementations.

**C3. design_lint expansion — per-medium rule packs.** Slides: type floor,
text-per-slide budget, archetype monotony (3+ consecutive bullet slides),
missing image slots, parallelism drift. Web: contrast (exists), off-token
colors, missing hover/focus on interactive elements, hit-target floor
(mobile), banned-font defaults. Advisory severity except the hard floors.

**C4. Visual-verify prompts per medium.** verify_web_app's review prompt
gets medium-aware guidance: judge slides against slide composition (not web
instincts), judge games on a played frame (input → state change screenshot),
judge mobile in the device frame at mobile viewport. Screenshot evidence
stays mandatory (house rule).

**C5. One token registry.** core/brand tokens currently serve exports
(PDF/PPTX chrome); generated sites/decks invent their own. Bridge them: the
direction picker (C1) emits tokens in the same registry format, so exports
and artifacts share a single source and a deck's PPTX export matches its
on-screen design.

**C6. Image budget + slot plumbing.** The pipeline seam that makes A8 real:
deck/site generation declares slots → prompts synthesized from the art
direction → image_generate called with palette-locked prompts → assets cached
in the workspace and re-embedded on edit (the C7 bytes-on-Element path
already exists for decks — feed it).

---

## Sequencing recommendation (for ratification)

1. **Wave 1 — the named pains, cheapest first**: B2 deck recipe (craft +
   archetypes) + A8/C6 imagery-in-slides + C3 deck lint. Before/after live
   decks as proof.
2. **Wave 2 — sites**: C1 direction picker + B1 site recipe + A1–A7
   foundations in the packs + C3 web lint. Before/after live sites.
3. **Wave 3 — new mediums**: B5 games (tier 1 starter first) + B6 phone
   apps (PWA + frames) + C2 starter expansion.
4. **Wave 4 — coherence**: C5 token-registry bridge + C4 medium-aware
   verify + B7/B8/B9/B10 as demand dictates.

Proof standard throughout (house rules): every wave ends with live MiniMax
builds + Firefox screenshots sent to Dylan; no green-test-only claims.
