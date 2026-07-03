# Changelog

## v0.1.0 - 2026-07-03

### Added

- Reliability engine: the REL ladder is complete, with REL-1e making host
  verification authoritative by default. `DISCO_HOST_VERIFY_AUTHORITATIVE=off`
  restores the earlier shadow posture.
- Build workspace versioning: Build projects now keep restorable workspace
  versions, expose list/restore APIs, serve historical preview bytes with
  `?version=`, and show rollback controls in the preview UI.
- AppKit engine: generated apps use React, Vite, TypeScript, Cloudflare Worker,
  D1, and Drizzle schema output. The strict AppKit verifier covers the original
  seven app checks plus Drizzle schema validation and Cloudflare export readiness.
- Owner-gated Cloudflare deploy: AppKit deploy routes are owner-only, dry-run by
  default, and require explicit confirmation before real Cloudflare mutations.
- `questions_v2` structured intake: planning can ask one bounded pre-plan
  clarification round, while autonomous runs skip it and record assumptions.
- Watch-it-write: streaming file-write/edit deltas now reach the frontend as
  `file_stream` frames so users can see generated file bodies assemble live.
- HMR preview proxying: live preview WebSockets, including Vite HMR frames, pass
  through the agent-server preview routes.
- Verifier context split: model verification now runs through the `VERIFIER`
  role with a bounded seed of contract, deliverable paths, check results, and
  screenshot; the builder context receives only the typed verdict summary.

### Changed

- Release packaging now includes a tag-triggered GitHub Actions workflow that
  runs the four deterministic fitness gates, unit suites, and frontend build
  before creating or updating a draft-only GitHub release from this changelog.
