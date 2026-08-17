# BP-07 — Read-counter family rollback (surgical)

**Read `README.md` first. PRECONDITION: BP-06 merged AND its acceptance step 4 (live
EE-Quest build, no read-rut) passed. If that precondition is not recorded in
`test-record/bp-06/`, STOP — removing the bandaid before the cause-fix is verified is
exactly the failure mode this project documents.**

## Why

The read-streak guards (commits 905ae60, eec8377, ecffec9, 939ce6c, e48c5bc) penalize the
model for being familiar with the codebase and withhold read tools to force commits. No
primary source endorses read-capping; the controlled-evidence fix is BP-06. The guards
now actively harm: capped reads → worse edits. They come out surgically. Runaway safety
remains via `max_iterations`, StuckDetector, and the circuit breaker — none of which you
touch.

## Exact removal list — `packages/core/src/disco/core/loop/engine.py`

Locate by symbol (line hints are from 2026-06-09):

| symbol / block | hint |
|---|---|
| `_READ_STREAK_LIMIT` | ~132 |
| `_READ_NUDGE`, `_READ_NUDGE_PLANNING` | ~134–150 |
| `_HARD_READ_NUDGE_LIMIT`, `_READ_NUDGE_HARD`, `_READ_NUDGE_PLANNING_HARD` | ~152–162 |
| `_trailing_read_only_streak()` | ~1019 |
| `_read_nudges_since_user()` | ~1045 |
| `self._force_commit` flag (init + every read/write site) | ~679 |
| the withhold-read-tools filter inside `_tools_for_step()` (the `t.name not in _READ_ONLY_TOOLS` branch) | ~875 |
| the read-streak nudge block in the run loop | ~1673–1705 |
| `read_loop_break` and its contribution to `escape_temp` | ~1705, ~1721 |

Rules:

- `escape_temp` becomes `_STUCK_ESCAPE_TEMP if in_escape else None` — the stuck-escape
  path is KEPT (it is StuckDetector's, not the read counter's).
- `_READ_ONLY_TOOLS`: delete IF its only remaining consumer was the withhold filter; if
  another consumer exists, leave it and say so in the report.
- KEEP: plan-step lag nudge (~1645–1670), execution gate, `c430275` plan-step scoping,
  `56526be` snapshot-on-terminal, StuckDetector + escape, circuit breaker, microcompact.
- Delete the family's tests; do NOT delete neighboring tests that share files — split
  carefully.

## New regression tests (these pin the rollback)

`packages/core/tests/test_no_read_penalty.py`:

1. A scripted loop performing 12 consecutive `file_read` actions: assert ZERO
   system-reminder messages are appended and the tool list passed to the agent on step 13
   contains every read tool (capture via a recording fake agent — fakes are fine in unit
   tests).
2. Stuck-escape still works: 3 identical action→observation cycles → `stuck_escape`
   StatusEvent fires (proves you didn't over-delete).

## Acceptance

1. `make unit` green; grep proof pasted in the report:
   `grep -rn "_READ_STREAK\|_READ_NUDGE\|_force_commit\|read_loop_break\|_read_nudges" packages/` → no hits.
2. **Behavioral (live driver, process backend)**: re-run the BP-06 live build prompt
   (EE-Quest v2 iteration). Assert from the event log: the agent performed ≥4 consecutive
   reads at some point AND still committed edits and finished — familiarity is no longer
   punished, and the rut does not return. Save log → `test-record/bp-07/`.
3. **UI surface (live, Firefox)** — reuse the build from step 2 driven through the UI;
   screenshot the feed showing an uninterrupted read sequence with no "STOP reading"
   reminders → `test-record/screenshots/bp-07/no-nudges.png`, sent to user.

## Prohibitions

- No replacement heuristic, soft cap, or telemetry-with-teeth. The rollback leaves NO
  read-counting state behind.
- If the live run DOES regress into a read-rut, STOP, restore nothing, and report — the
  decision about next steps is the user's.
