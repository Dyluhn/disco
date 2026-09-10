# sota-scan, vendored

Upstream: https://github.com/MerlijnW70/sota-scan at `a3fbf07dc4e277852517ff29cf112e664134093d`
(2026-06-22), MIT. Vendored on 2026-09-10 on the owner's instruction.

What lives where:

- `.claude/skills/sota-scan/SKILL.md` — the skill, project-scoped, so `/sota-scan`
  works in any Claude Code session opened in this repository.
- `development/sota-scan/workflows/sota-scan-fanout.js` — the fan-out workflow `exhaustive` mode uses; `.claude/workflows/sota-scan-fanout.js` is a relative symlink to it so Claude Code finds it.
- `development/sota-scan/{lib,scripts,test}` — the deterministic clustering core and the
  report exporter, with upstream's tests (`cd development/sota-scan && node --test`).
- `.sota/` (repository root) — the scan record the skill writes: `rubric.<domain>.json`,
  `last-scan.json`, and the exported `report.<domain>.md`.

Re-run: `/sota-scan` (standard, 5–10 comparators), `/sota-scan quick`, or
`/sota-scan exhaustive` (10+ comparators, delegates to the workflow). Then export the
shareable report: `node development/sota-scan/scripts/sota-report.mjs --report`, and
`--check` flags a stale one (the report is a pure function of `last-scan.json`).

No upstream file is modified. The exporter looks for `.sota/` one directory above its
own `scripts/`, so `development/sota-scan/.sota` is a relative symlink to the root `.sota/`.
