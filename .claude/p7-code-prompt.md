# Codex CODE Review — P7-KITS (Starter/Brand Kits) — IMPLEMENTED

You APPROVED this plan. Inspect:
NEW core/kits/{__init__,starter.py,brand.py}:
- starter.py: lead_form_appspec(title) (the single-source AppKit default); StarterKit.scaffold(title) →
  {rel_path: text}, PATH-SAFE (_safe_rel rejects absolute/'..'); StarterKitRegistry built-ins app_shell
  (renderable inline-CSS index.html) + lead_form (.disco/appspec.json + index.html, byte-identical to
  AppSpecStore.write).
- brand.py: brand_to_appkit_tokens(theme) + appkit_brand(name,mode) — a PROJECTION over the EXISTING
  disco.core.brand THEMES/resolve_theme into appkit {primary,accent,bg,fg,font}; BRAND_NAMES from THEMES. No
  parallel catalog.
NEW tools/builtin/scaffold_starter.py: materializes ctx.starter_kit's StarterKit into the workspace (writes
ONLY missing files — never clobbers); errors no_starter / unknown_starter / invalid_starter. Registered +
in AGENT_TOOLS + ARTIFACT_TOOLS.
CHANGED:
- tools/anatomy.py ToolContext + starter_kit; executor.py threads starter_kit param → ctx (like driver_llm);
  runtime.py _starter_kit_for(conversation_id) = the contract's artifact.starter_kit (build runs only),
  passed at executor construction — active-contract-bound.
- tools/builtin/appkit.py app_create: sections=None now scaffolds from lead_form_appspec (single source;
  byte-equivalent — tested).
- DECK RECONCILE: contract/registry.py deck required_files ("deck.json"→"deck.authored.json") + starter_kit
  removed (None) — slides_generate is the materializer (the tooling writes the AuthoredDeck sidecar, not
  deck.json); build_deck.md pack updated to match (no deck_stage starter).

Tests: 76 green. Key: starter registry + path-safety; app_shell renderable; lead_form byte-identical to
app_create default; EVERY contract starter_kit resolves (coherence); deck no longer claims an unresolvable
starter; brand projects into the appkit keys from the real THEMES; scaffold_starter materializes/skips-
existing/errors; registered+scoped; AGENT_TOOLS snapshot updated. basedpyright: 0 new errors (runtime 1
pre-existing fire_now).

Judge: (a) does this close the starter false affordance with REAL consumers (app_create + scaffold_starter),
no decorative registry? (b) is the lead_form single-source byte-equivalence correct + the app_create branch
refactor sound? (c) is the brand a clean projection over disco.core.brand (no parallel catalog)? (d) is the
deck reconcile right (align contract/pack to the real AuthoredDeck, slides_generate as materializer)? (e) is
scaffold_starter safe (path-safe, never clobbers, active-contract-bound via ctx)? (f) test sufficiency.
Return APPROVE|REVISE|BLOCKED_CODEX_UNAVAILABLE + REASONS + REQUIRED_REVISIONS.
