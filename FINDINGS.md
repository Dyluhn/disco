# Provider Keys Findings

## What changed

- Added persisted provider objects with encrypted `provider_<id>` secrets, server-side presets, kind-specific `/models` probing, TTL cache + singleflight, and enable/disable routes that create/remove ordinary catalogue entries.
- Added normalized catalogue support for OpenAI-compatible, Anthropic, and Gemini shapes.
- Added the generic Providers UI: preset + key add flow, inline browse/search, toggles, unknown-pricing display, and manual model-ID add when `/models` is unavailable.
- Kept the existing dedicated OpenRouter section/routes intact and visible, including the existing image-pricing enrichment path.

## Verification

- `PYTHONPATH=$(ls -d $PWD/packages/*/src | tr "\n" ":") /var/home/dylan/projects/disclaude/.venv/bin/python -m pytest packages/app-server/tests -q` — passed.
- `cd frontend && npm ci` — completed.
- `cd frontend && npx vitest run src/components/settings/` — passed.
- `cd frontend && npm run typecheck:build` — passed.

## Deviations / Code-Reality Notes

- Persistence is backed by an inert `RouterConfig.providers` field so provider definitions survive the existing `ConfigStore` load/save cycle. The agent/runtime still only consumes enabled catalogue models.
- The existing model catalogue schema has no explicit label field and no first-class “unknown pricing” state. The browse UI shows `pricing unknown`; enabled models use the existing `ModelUpsert` path with price `0` when unknown and can be edited afterward.
- The generic OpenRouter preset exists, but the old OpenRouter section remains in the Settings Providers area because the spec explicitly said not to break the existing OpenRouter section/routes this pass.
- `ProviderPresetDTO` includes `requires_base_url` for the Custom preset so the frontend does not hardcode which preset needs a URL.
- `ImageGenSection.test.tsx` had a brittle `findByRole("status")` assertion that failed because the component can render both the not-configured warning and the offline-test status. The assertion now targets the specific not-configured text.
