> **SUPERSEDED / HISTORICAL (as of 2026-07-07).** Design-primitives catalog — the design reference is durable; the “current honest inventory” section (9 directions, “image-gen never used in slides”) is stale (now 22 directions, always-on art direction) — see `docs/disco-status-and-remaining.md`.
> Current status of record: `docs/disco-project-state.md` (master), `docs/disco-status-and-remaining.md` (features + remaining), `docs/disco-security-state.md` (security). This file is kept for history and may contain stale claims.

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

---

# HARVEST APPENDIX — 5-agent research sweep, 2026-07-04

License discipline: every entry below verified against the actual repository
LICENSE. **Vendor-safe** = code may be adapted/vendored. **Techniques-only** =
study and clean-room reimplement; never copy files.

## H1. License verdicts (the table that governs everything)

VENDOR-SAFE (MIT/Apache/BSD, verified):
- Magic UI (magicuidesign/magicui, MIT) — THE workhorse: marquee, border-beam,
  shine-border, bento-grid, dot/grid patterns, particles, meteors, warp/retro
  backgrounds, noise-texture. Source read + verified.
- shadcn/ui (MIT), vaul (MIT), sonner (MIT), number-flow (MIT), cult-ui (MIT),
  HyperUI (MIT), daisyUI (MIT), eldoraui (MIT), ibelick/motion-primitives (MIT
  — the strong "new find"), tw-animate-css (successor to deprecated
  tailwindcss-animate).
- Motion/motion.dev (MIT), anime.js v4 (MIT), Lenis (MIT), tsParticles (MIT),
  three.js (MIT), OGL (MIT), @formkit/auto-animate (MIT), Barba.js (MIT).
- GSAP — proprietary but 100% free for commercial use incl. all plugins since
  Apr 2025 (Webflow); AI-GENERATED GSAP CODE EXPLICITLY PERMITTED. Sole
  prohibition: building a visual no-code animation-builder UI competing with
  Webflow. Disco's surfaces do not do this; re-review only if we ever ship a
  drag-timeline editor.
- Games: Phaser (MIT, v4.1 2026), KAPLAY (MIT, active), Excalibur (BSD-2),
  PixiJS (MIT), melonJS (MIT), litecanvas (MIT), ZzFX (MIT).
- Frames: picturepan2/devices.css (MIT, modern device set). Deck engines
  (study): reveal.js/impress.js/Slidev/Spectacle/Swiper all MIT.
- Glass: liquidGL (MIT), rizroze/liquid-glass (MIT), glasscn-ui (MIT),
  artyhoo/shadcn-glass-ui-library (Apache-2.0).

TECHNIQUES-ONLY (no/none-OSI/restrictive license — clean-room reimplement):
- react-bits (MIT + COMMONS CLAUSE — cannot redistribute/port the components
  themselves; consuming-in-an-app OK, but for a builder that emits components
  treat as inspiration only).
- Aceternity UI free tier (NO LICENSE FILE — implied-free is not a grant),
  ibelick/background-snippets (NO LICENSE FILE), css.glass + themesberg
  glass-ui (license unresolvable — re-verify before any use).
- hover.dev (paid + anti-competing-component-library clause), Preline (MIT +
  fair-use overlay: never repackage as a kit/generator), Preline Pro +
  Tailwind Plus + shadcnblocks (PAID — never copy).
- jsfxr (license varies per fork; pin + verify a fork before use; prefer ZzFX).
- callmenick/CSS-Device-Mockups (no license — avoid).

STALE/DEAD (don't adopt): AOS (unmaintained), split-type + Splitting.js (stale
— hand-roll our ~30-line splitter), vanta.js (dead), marvelapp/devices.css
(iPhone-8-era devices).

## H2. Glassmorphism — the A5 "glass" treatment set + generation rulebook

Canonical recipe (tested ranges): backdrop-filter blur(8-20px, default 14)
saturate(140-180%, default 160 — saturate IS the premium ingredient; its
absence = "gray mud", the #1 bad-glass tell) + fill lanes (light glass
rgba(255,255,255,.08-.20) on dark/colorful; dark glass rgba(17,25,40,.45-.65)
on light/colorful) + MANDATORY 1px border highlight (bright on light glass
.25-.35, dim on dark .10-.15 — the refractive-edge fake that separates glass
from blurred-div) + soft large shadow (0 8px 32px rgba(0,0,0,.15-.30)) +
radius 12-24px + @supports fallback to near-opaque fills + optional
feTurbulence grain (baseFrequency .6-.9, opacity .03-.06, hero surfaces only)
+ specular gradient edge via mask-composite ring.

THE 12 GENERATION RULES (enforce in packs + lint): (1) backdrop gate — glass
only over colorful/imagery backdrops, never flat color; (2) never blur
without saturate; (3) one fill lane per view; (4) border highlight mandatory;
(5) soft shadow mandatory; (6) max 3 glass layers/screen, content surfaces
(tables/forms/code) NEVER glass; (7) no glass-on-glass nesting; (8) fallback
+ prefers-reduced-transparency always emitted; (9) scrim under text over
unpredictable imagery, verify worst-case contrast; (10) SVG-displacement
"liquid glass" (feDisplacementMap, Chromium-only) at most ONE hero element
with blur fallback — never WebGL by default; (11) grain on marketing
surfaces only; (12) mobile ≤3-5 blurred layers, never animate
backdrop-filter itself.

Nav variant: 200%-height element + linear-gradient mask so the blur samples
scrolling content. Modal variant: blur 18-24px + separate darkening scrim.

## H3. Motion — A9 decisions + taste defaults

ARCHITECTURE (C2): adopt Motion (MIT) as the tween/spring substrate + a thin
FIRST-PARTY policy layer (taste presets, reduced-motion gating, deck
build-in state machine, text splitter). GSAP = opt-in high-power tier
(ScrollTrigger/SplitText). NATIVE-FIRST default: scroll-driven animations API
+ @starting-style + view-transitions for the 80% case; a library must earn
its bytes (choreographed timelines, spring physics, scroll-scrubbed JS
state). Lenis (MIT) when "premium scroll feel" is asked.

TASTE DEFAULTS (the anti-AI-slop table): hero choreography 900ms-1.4s total,
easing cubic-bezier(.16,1,.3,1), per-element travel VARIES (24/16/8px — the
uniform-40px-fade-up is THE AI tell), stagger 60-100ms; scroll reveals =
small travel (12-20px), fire once, ≥1/3 of sections get NO animation;
word-level (not char) splitting, 30-50ms/word, cap total <800ms; marquee =
constant px/sec + pause-on-hover; parallax subtle (60-80% speed) max 2
layers; magnetic hover ≤8-12px pointer-fine only; counters ease-out scaled
to magnitude. Reduced-motion: gate INIT (not just duration), disable
parallax/marquee/ambient entirely; remove motion never outcomes.

DECKS: click-advance build-in state machine (imperative step index, not
scroll); startViewTransition for slide swaps + crossfade fallback; PPTX/video
export REQUIRES a deterministic seekable JS timeline (native APIs don't
survive export) — keep one.

## H4. Decks (B2 harvest — feeds Wave 1)

From Slidev/reveal/impress (all MIT, study-safe): (1) click-indexed atomic
reveal units — multiple elements share a click index, composable modifiers
(fade+direction+scale via data attrs); (2) named-layout-as-frontmatter —
cover/quote/statement/two-cols/image-left/image-right/full are LAYOUT
variants built around the content (the real "image-first" mechanism), not
decorations on a generic slide; (3) decoupled speaker-notes channel (second
surface, postMessage-synced — also fixes notes-leak-into-export class);
(4) theme = accent PALETTE (several named accents for per-section
derivation) + font triplet + light/dark token pairs — one hardcoded blue +
one mode = the generic-deck tell; (5) fragment-flattening print/export
architecture (expand all builds statically, re-linearize, then paginate);
stretch: impress-style 3D camera zoom-to-detail as ONE optional transition.

## H5. Sites (B1/A6 harvest)

Premium-craft checklist (apply per direction): hairline border-white/10 on
dark (never solid gray) or inset glow shadow; cursor-tracked radial
spotlight on cards (CSS custom props + mousemove — highest feel-per-line);
noise/grain overlay on gradients (kills banding); gradient text via
bg-clip; subtle 3D tilt clamped ±6-10°; staggered entrances (50-100ms);
edge-fade masks on all marquees; GROUP-hover choreography (icon shrinks
while CTA slides up); near-black #0a0a0a never #000, muted text = fg at
60-70% opacity never hardcoded gray; transform-gpu on continuous animation;
border-radius must match mask radius on glow borders (clipped corners = the
sloppy-clone tell). Section archetype references: Magic UI bento (pure CSS
grid, trivial), HyperUI static sections (zero-JS), marquee-composite
testimonial walls, Aceternity-style hero backgrounds reimplemented
(aurora/threads/silk shader family — MUST ship static CSS-gradient
fallbacks; the sources don't).

## H6. Games + frames + audio (B5/B6 verdicts)

Games: tier-1 = vanilla canvas + rAF fixed-timestep + ZzFX (MIT, ~1KB
synthesized SFX, no assets) for single-screen games; tier-2 DEFAULT =
KAPLAY (MIT — flattest API, fewest lines to playable+juicy, ideal codegen
target); tier-2 FALLBACK = Phaser (MIT — deepest doc corpus = fewest
hallucinated APIs, built-in tilemap/physics for platformer/top-down).
NEVER default PixiJS (renderer-only). Juice helpers (~30 lines, both
tiers): decaying-random screen shake, squash/stretch tween, 40-80ms
hit-stop, particle bursts, lerp camera follow.

Frames: picturepan2/devices.css (MIT) for modern iPhone/iPad/Watch/MacBook
bezels; window chrome (traffic lights, address bar) hand-rolled first-party
(~25 lines CSS, no maintained lib exists).

## H7. reactbits technique index (Commons Clause — reimplement only)

Trivial-easy reimplements: Star Border (pure CSS orbiting gradient), Click
Spark (~80-line canvas emitter), Gooey Nav (blur+contrast filter trick),
Letter Glitch (Canvas2D fillText grid), Decrypted Text (interval scramble —
NEEDS aria-hidden + visually-hidden real text), Tilted Card (center-offset
rotate + ~20-line spring). Moderate: Aurora/Threads/Silk (OGL-style
fragment shaders ~150 lines w/ simplex noise — palette-lockable to A3
seeds), Magic-Bento glow grid, Dither. Hard (skip or three.js-only): Grid
Distortion (velocity-injected displacement field), Fluid Glass
(MeshTransmissionMaterial refraction). A11y binding rules: every WebGL
backdrop ships a static gradient fallback; flicker effects capped <3Hz;
cursor-replacement effects never sole affordance, disabled on touch;
full-viewport shader motion pauses off-screen + respects reduced-motion.

## H8. Heroes + landing composition (round-2 sweep)

13 hero archetypes with composition rules (split product-shot, full-bleed
video/shader w/ 30-50% scrim, kinetic typography [animate ONCE, reduced-motion
off switch], bento-hero [container queries per tile; +23% scroll depth],
terminal/code, centered manifesto [text-wrap:balance earns its keep], editorial
magazine [serif display + overlap grid], 3D object, anti-grid brutalist,
marquee band, data-viz/live-metric, cursor-parallax, illustrated blob
[OVERUSED — vary palette or avoid]). Narrative orders per business type (SaaS
baseline hero→proof-band-HIGH→problem-narrative features→how-it-works→
testimonials→pricing→FAQ→distinct closing CTA; deviations for portfolio/
restaurant/event/personal/e-commerce). CSS-only compendium: scroll-driven
animations, :has() mode-switching heroes, container queries, text-wrap
balance/pretty, subgrid, clamp(), accent-color, color-mix(), light-dark(),
view()-timeline. BAN LIST (the template tells): purple-blue gradient default,
three identical feature cards, Inter-everything, indigo-600 CTA + reflexive
hover:scale-105, same-treatment elevated middle pricing tier, isometric-people
art, flat feature lists, kinetic type with CLS, padded logo strips, generic
FAQs, five-column footer soup, verbatim identical section skeletons.

## H9. Data-viz (round-2 sweep)

Chart.js stays the standard (MIT, best codegen reliability); PREMIUM DEFAULTS
now mandatory: token palette bound by series index (desaturated, never RGB
primaries), rounded bar tops + maxBarThickness 40, vertical dark→light
gradients (≥15% bottom stop), x-gridlines OFF + y at 5-8% opacity + axis
border off, global font via Chart.defaults, styled tooltip (padding 12,
radius 8, leading-space labels, locale formatter), 400ms re-render animation
(1000ms first-paint only), circle legend swatches, dark mode from CSS custom
props (rebuild not recolor — gradients bake canvas colors). Archetype table:
comparison→bar (horizontal when >7 cats), trend→line (area only when
magnitude matters; NEVER dual-axis by default), composition→ranked stacked
bar (donut ≤5 slices, labeled on-slice), distribution→histogram
(Freedman-Diaconis bins), relationship→scatter (alpha .5 past 200 pts),
KPI→big-number composite. SLIDE CHART RULES (structural, not advisory): max
3 series, ≥14-16px ticks, labels on marks, near-zero gridlines, ONE takeaway
per chart, annotate the point (plugin-annotation), titles state the FINDING
not the axis. Numbers: tabular-nums everywhere, delta badges are
DIRECTION-AWARE (lower-is-better metrics flip green/red; neutral gray for
sub-noise deltas), sparklines = shape not chart (last-point dot only).
Reliable SVG diagram math for generators: timelines (alternate labels
above/below), flowcharts (grid layering + Manhattan routing + one <marker>
def), 2×2 (label axis ENDS), funnels (trapezoid widths on shared scale +
conversion %), org charts (parent-bus-child routing). Flags: Recharts 3.x
got heavy (Redux/Immer inside); TradingView lightweight-charts needs live
attribution (keep attributionLogo or footer credit); frappe-charts +
chartjs-plugin-datalabels stale; uPlot = zero-dep escape hatch for large-N.

## H10. Forms + onboarding (round-2 sweep)

Stack verdict for the ui_kit starter: Radix Primitives or Base UI (MIT) as
unstyled base + react-hook-form + zod (valibot when bundle-critical:
1.4kB vs 17.7kB) + react-aria-components (Apache-2.0) fallback for
date/complex widgets + input-otp for segmented OTP. Formik = legacy, skip.
Craft: TOP-ALIGNED labels win (floating only when space-scarce, never
placeholder-as-label); 8 input states incl read-only ≠ disabled; validate
on-blur first then on-change after first error; error text instructive +
aria-describedby + top error summary on long forms; 40-44px input height,
~440px max width; steppers linear 3-6 steps w/ back+persist; autosave =
"Saving…→Saved 1m ago" AND keep the explicit button; type+inputmode+
autocomplete triad (one-time-code for OTP). Micro: 120-200ms state
animations, APG combobox keyboard contract, drag handles not draggable rows
(+ keyboard pickup/move/drop + live region), skeletons for content
spinners for actions nothing under 1s, optimistic UI w/ request identity.
A11y floor: real labels, aria-invalid only post-validation, placeholder
contrast 4.5:1.

## H11. App shells + dashboards (round-2 sweep)

Shell archetypes w/ dimensions: sidebar-rail 56-64px collapsed / 240-272px
expanded (shadcn sidebar-07 = reference impl; ⌘B toggle; tinted-pill active
state; <1024px auto-rail, <768px overlay drawer), topbar 56px dense/64px
roomy (breadcrumb left, fake-input ⌘K trigger right-center, bell+avatar
right), double-drawer inspector 320-420px resizable (opens on explicit
selection; nav-left ≠ detail-right, never conflate), mobile tab shell (56-64px
+ safe-area), palette-first minimal chrome (power-user opt-in only, keep a
4-6-item fallback sidebar). DENSITY TOKEN TABLE (compact mode as a token
swap, user-preference not hardcode): 13-14px base, 32px rows (40 comfortable/
48 spacious), 8px inline gaps, 12-16px card padding, 5-8% opacity dividers,
1.2-1.3 data line-height, 16-18px inline icons, 4-6px radii, tint-not-shadow
hover, tabular-nums. Tables: sticky header w/ explicit bg, dividers default
(zebra only for wide sparse-border tables), numeric right-align, hover-reveal
inline actions (max 3, keyboard-reachable), paginate for shareable position
vs virtualize for exploration (never both), skeleton rows not spinner
overlay, keep stale data on refetch error. cmdk (MIT, Radix-based) = the
palette primitive: ⌘K, fuzzy scored, grouped results, Recent-on-empty,
inline shortcut hints, nested actions. react-resizable-panels (MIT, Vaughn)
for splits — PERSIST pane sizes; 8-12px grab targets. Inspector conventions
for generated apps: identity header + close, actions → grouped collapsible
metadata → danger zone pinned bottom, Escape closes. TRAPS: AG Grid
Enterprise features hard-paywall at runtime (stay Community-scoped);
Tremor = Apache-2.0 w/ NOTICE (not MIT); dnd-kit stable-but-unsupported.

## H12. Mobile + PWA (round-2 sweep)

vaul EXPLICITLY UNMAINTAINED → vendor its snap/rubber-band physics (MIT,
small) or use Base UI drawer; `motion` package (NOT frozen framer-motion) =
one dep covering drag/spring/layout; embla (headless) vs swiper (batteries);
Konsta = instant native-look Tailwind chrome; Capacitor = the native handoff
(wraps the PWA, near-zero rework). Sheets: snap fractions [.25,.6,.95] +
velocity-weighted release (>0.5-0.7 px/ms flick dismisses), 1:1 finger
tracking on transform only, iOS rubber-band resistance curve, backdrop
opacity COUPLED to sheet position (the tell of real physics), touch-action:
none on handles; iOS card-stack peek vs Material edge-to-edge. Nav: tab bar
49px + env(safe-area-inset-bottom) on the BAR, filled/outline icon swap,
16-18px badges w/ bg ring, max 5 tabs; swipe-back = 20px edge zone +
two-layer transform (outgoing 0→100%, incoming -30%→0) + 35%/velocity
commit; pull-to-refresh only where pull-down means nothing else. TOUCH
CRAFT: 44px floor (pad hit area not icon), thumb-zone = primary CTAs
BOTTOM (top-right CTA = the desktop-web tell), :active states instantly
(scale .96-.98) + hover gated behind (hover:hover), touch-action:
manipulation global, overscroll-behavior-y: contain on inner scrollers,
haptics = enhancement-only (iOS Safari has none). PWA: display standalone,
theme_color mirrored in meta, SEPARATE any + maskable icons (content in
central 65-70%), 192+512; iOS STILL needs generated apple-touch-startup-image
per device (bake into export) + apple-mobile-web-app metas; SW = precache
shell + cache-first hashed assets + network-first HTML/API, Workbox
(Apache-2.0) fine; install prompts deferred until value-proof, iOS =
coach-mark (no beforeinstallprompt). WEBPAGE-PRETENDING tells+fixes:
visible scrollbars, hover-dependent UI, desktop density, missing safe
areas, no :active feedback, centered-fade modals instead of sheets,
pinch-zoomable text (maximum-scale=1 + viewport-fit=cover).
