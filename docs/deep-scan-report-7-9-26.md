# Deep-Scan Report — 2026-07-09

Four systematic scans, run back-to-back: refusal/error census, system-prompt
audit, observation diet, and UI stream staleness. Method: read everything
personally, grade against the house standards the campaign established
(diagnosis+recipe refusals, progressive valves, state-version oracles,
evidence persistence), corroborate with the event-store traces from the
9-site gauntlet night.

---

## Scan 1 — Refusal/error census (152 sites)

~60% of sites meet the diagnosis+recipe standard — everything the campaign
touched propagated well. Four defect classes remain:

- **D1 Muted failures (~20 sites):** `browser` ×4, `verify_web_app`'s
  catch-all, `deck_patch` ×6, several `image_gen`/`slides`/`document` paths
  return `content=""` with the message only in `error=`. A shared dual-field
  `_fail()` helper already exists in `shell_sessions.py` (with a comment
  explaining exactly why) — sweep it across.
- **D2 Catch-alls without recipes:** `browser tool error: {e}`,
  `verify_web_app error: {e}`, `Deck generation failed: {err}` — high-traffic
  ones should say what to check (`preview_logs`/`preview_status`) and warn
  against blind same-call retries (the flags-attempt-5 killer pattern).
- **D3 Missing machine codes (~40% of sites):** error field is prose or
  absent; forensics tonight repeatedly needed `LIKE '%...%'` matching.
- **D4 Two `land_blocked` sites** (driver.py:247, engine.py:1154) need manual
  verification that their guidance strings are non-empty.

## Scan 2 — System prompts (~2,940 tokens/execution call)

- **Fresh contradiction:** the prompt still commands "Read a file immediately
  BEFORE editing it" — obsoleted by the edit-returns-region change the same
  day. Prompts must move in lockstep with tool-contract changes.
- **All four verify mandates teach manual browser-verification and never
  mention `verify_web_app`** — the structured verifier the tool surface
  prefers. Explains the browser-verify double-loop in traces (37 browser +
  45 verify calls).
- "Three distinct tools" header lists five bullets (accretion drift).
- The monolith rule is stated 3× in one prompt; `update_plan_progress`'s JSON
  schema is duplicated verbatim from its tool description (~90 tokens); the
  progress cadence should be "at step boundaries", not "as you work"
  (10 bookkeeping turns/run).
- Planning prompt never states that shell/write tools are locked (4 refusal
  round-trips in traces); one negative line fixes it.
- "pnpm and uv are pre-installed" — UNVERIFIED against the sandbox image; a
  wrong claim here costs failed installs. Verify before trusting.
- `shell` vs `shell_exec`: two overlapping tools, no when-to-use-which
  anywhere.
- Trim estimate: 500–700 tokens (~20%) with zero content loss.

## Scan 3 — Observation diet (4.9MB fed back across 5 runs)

- **file_read observations = 82% of all bytes** (~1M tokens/5 runs); 93% of
  read-feed bytes were REPEAT reads of already-read paths (worst: one path
  read 109×). Counterweight: the view layer sha-masks byte-identical repeats,
  so the wire cost is bounded — what evades masking is *windowed* re-reads
  (different slices), which the churn valve + edit-returns-region now
  suppress. The turn-count fix and the byte fix turned out to be the same fix.
- Fat tails needing caps: one `shell` observation hit 15.9KB (cap ~4KB with
  tail + "redirect to file" hint — the prompt already teaches redirection);
  `design_lint` p90 14.5KB (summarize to top-N findings + count);
  `browser` median 5.9KB/obs vs `verify_web_app`'s 1.3KB structured verdict
  (another argument for the scan-2 mandate fix).
- Pre-F1 line-edit observations were already 6–7KB median; the new ±10-line
  region views are *smaller* — F1 shrank both turns and bytes.

## Scan 4 — UI stream staleness (the oracle-v3 lesson, product side)

Both build and research streams share one reconnect path
(`subscribeLive` in api/agent.ts): exponential backoff, replay-then-dedup.
Good for clean closes. Two real gaps:

- **G1 Silent death is unhandled** — reconnect triggers only on `onclose`.
  A half-open socket / proxy-eaten stream never closes → frozen "Working…"
  UI forever with no signal. This is byte-for-byte the wedge that froze three
  gauntlet runs; the test oracle got a watchdog, the product UI never did.
  Fix: application-level staleness watchdog — no frame for N seconds while
  status is RUNNING → force-close the socket (existing reconnect machinery
  then heals via replay) + a subtle "reconnecting…" hint. The needed state
  (maxSeq, last-frame time) already exists.
- **G2 Retry exhaustion is permanent** — 6 attempts ≈ a 45-second outage
  window; a dev-server restart can exceed it, after which the stream never
  tries again and the user must know to reload. Fix: never stop retrying
  while the tab is open (cap the backoff at 15s as today); optionally reset
  the attempt counter on tab-visibility change.

---

## Consolidated fix batch (priority order)

| # | Fix | Source | Size |
|---|-----|--------|------|
| 1 | UI staleness watchdog + retry-forever (G1+G2) | Scan 4 | S — one file |
| 2 | Prompt surgery: F1 contradiction, verify mandates → verify_web_app, de-dup/trim, planning negative line, progress cadence | Scan 2 | S — prompts.py |
| 3 | Dual-field failure sweep (D1) + recipes for browser/verify catch-alls (D2) | Scan 1 | M — ~26 sites, mechanical |
| 4 | Observation caps: shell tail-cap, design_lint top-N | Scan 3 | S |
| 5 | shell vs shell_exec when-to-use lines; verify pnpm claim against sandbox image | Scan 2 | XS |
| 6 | Machine codes for keyed-on prose errors (D3), opportunistic | Scan 1 | ongoing |

Items 1–5 are one evening of codex work in two worktrees (frontend: #1;
backend: #2–5) with no overlap between them.
