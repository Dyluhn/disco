# Codex PLAN Review — P7-KITS (Starter/Brand Kits)

Read "## PR P7-KITS — Starter/Brand Kits — PLAN" in /home/dylan/projects/disclaude/disclaude.md.

Verifiable: contracts (contract/registry.py) set artifact.starter_kit = app_shell/lead_form/deck_stage and the
prompt packs say "scaffold from the <name> starter", but NO resolver/registry exists — they are bare strings
(false affordance). app_create (tools/builtin/appkit.py) hard-codes its hero+lead_form default. appkit has
DEFAULT_DESIGN tokens (core/appkit/models.py) but no named BrandKit. slides_generate exists; no deck_stage
starter.

PLAN: core/kits/starter.py (StarterKit + StarterKitRegistry: app_shell→index.html shell; lead_form→reuse
appkit AppSpec→appspec.json+index.html; deck_stage→deck.json) + core/kits/brand.py (named design-token presets
over DEFAULT_DESIGN) + a coherence test (every contract starter_kit resolves) + wire app_create to scaffold
from the lead_form starter (single source). UIKit + brand-as-a-tool scoped out/tracked.

Judge: (a) is the audit right — are starters genuinely just unresolved strings, and does the plan close the
false affordance without rebuilding existing infra (appkit/slides)? (b) is reusing the appkit AppSpec as the
lead_form starter (single source, app_create scaffolds from it) the right move, or scope-inventing? (c) is the
StarterKit shape (id → dict[path,text] file-map) right for spanning site/app/deck artifacts? (d) is the
BrandKit (named token presets over DEFAULT_DESIGN) the right minimal brand substrate, or is more essential?
(e) is scoping out a full UIKit + app_set_brand acceptable? Return APPROVE|REVISE|BLOCKED_CODEX_UNAVAILABLE +
REASONS + REQUIRED_REVISIONS.
