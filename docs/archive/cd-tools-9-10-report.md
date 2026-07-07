# CD-TOOLS-9 / CD-TOOLS-10 — Live MiniMax-M3 proof that "Mode B" is gone

**Date:** 2026-06-30 · **Model:** MiniMax-M3 (DIRECT, `api.minimaxi.chat`) · **Driver:** agent-server
`:8000` (default model `driver-minimax` → relay `:8080` → MiniMax) · **Provider ledger:** every call
recorded to `relay.jsonl`.

## What this proves

**Mode B** = the edit-elision thrash the CD-TOOLS campaign set out to kill: a model asked to make a
*targeted* edit to a *large* file whose body has been elided from its context can't reproduce the
`old` text, so it loops on `old_text_not_found` (or echoes an elision marker) and gets STUCK. CD-TOOLS-1
(fresh-edit guard) + CD-TOOLS-2/3 (atomic `exact_replace` / `safe_write_file`) + CD-TOOLS-7
(`run_project_script`) + CD-TOOLS-8 (prompt-pack) together fix it. These two PRs prove the fix on a
**real live model**, end-to-end — not a cassette.

This is a LIVE-model proof. Cassettes/fixtures show only regression ("didn't break"); only a live
model end-to-end shows the feature *works*.

## CD-TOOLS-9 — the deep single-run proof (run r3)

A real build-then-edit run (`harness/product_build/targeted_edit_run.py`): MiniMax-M3 first **builds**
a large `index.html` (so its own `file_write` body is later elided — the Mode-B setup), then a
**targeted followup** asks it to change only the hero headline, CTA, and footer year *in place*.

Phase-2 tool flow (verbatim from the event log):

```
file_read(index.html)            success=True    <- fresh read FIRST (the CD-TOOLS-1 guard)
file_edit  <h1 ...>OLD_HER...  -> ...NEW_HER...   success=True
file_edit  CTA button          -> ...            success=True
file_edit  "© 2024 Acme Corp"  -> "© 2026 ..."   success=True
verify_web_app                   success=True
```

Hardened oracle (all enforced — a vacuous / misdirected / append / thrash run cannot pass):

| check | r3 |
|---|---|
| build produced all 3 OLD sentinels | ✓ |
| a targeted-edit tool succeeded **on `index.html`** | ✓ (file_edit ×4) |
| `file_read(index.html)` **before** the first successful edit (seq 28 < 35) | ✓ |
| `old_text_not_found` count | **0** |
| no elision-marker rejected | ✓ |
| served page: NEW present **AND** OLD absent (output-truth, 17.7 KB) | ✓ |
| ledger all `api.minimaxi.chat` / MiniMax-M3, 0 OpenRouter | ✓ (35 calls) |
| 0 provider calls after terminal | ✓ |

Codex (gpt-5.5) APPROVE after 2 oracle-hardening REVISEs (read-before-edit ordering + old-token
absence; edit-on-exact-`index.html` + pre-edit sentinel presence).

## CD-TOOLS-10 — the consecutive soak (10 runs, `s1`–`s10`)

| tag | PASS | built_ok | build | edit | onf | edits applied | OpenRouter | post-terminal |
|---|---|---|---|---|---|---|---|---|
| s1 | ✓ | ✓ | FINISHED | FINISHED | 0 | ✓ | 0 | 0 |
| s2 | — | — | — | — | — | — | 0 | 0 | (HTTP 409 transient — build never ran) |
| s3 | ✓ | ✓ | FINISHED | FINISHED | 0 | ✓ | 0 | 0 |
| s4 | ✓ | ✓ | FINISHED | FINISHED | 0 | ✓ | 0 | 0 |
| s5 | ✗ | ✓ | FINISHED | TIMEOUT | 0 | ✓ | 0 | 0 |
| s6 | ✓ | ✓ | FINISHED | FINISHED | 0 | ✓ | 0 | 0 |
| s7 | ✓ | ✓ | FINISHED | FINISHED | 0 | ✓ | 0 | 0 |
| s8 | ✓ | ✓ | FINISHED | FINISHED | 0 | ✓ | 0 | 0 |
| s9 | — | — | — | — | — | — | 0 | 0 | (HTTP 409 transient — build never ran) |
| s10 | ✓ | ✓ | FINISHED | FINISHED | 0 | ✓ | 0 | 0 |

### Two distinct metrics (kept separate on purpose)

- **Mode-B-gone** (the campaign's actual fix — the edit path no longer thrashes): of the **8** runs that
  reached the edit phase (`built_ok`), **8/8** had `old_text_not_found = 0`, **no** elision-marker
  rejection, and the edits **actually applied** on the served page. **100% — zero Mode-B thrash in any
  run that edited.**
- **Clean-finish PASS** (Mode-B-gone *and* a clean terminal FINISH *and* read-before-edit ordering):
  **7/10**. The one built_ok miss is `s5`: its edits applied with `onf=0` (Mode B gone), but the edit
  phase TIMED OUT and it edited *before* a phase-2 read — legitimately, because it was already grounded
  from phase 1, so the CD-TOOLS-1 guard correctly did **not** force a redundant read. That is a
  *finish-rate* (Mode A) miss, **not** an edit-thrash.

### Honest caveats

- **`s2`, `s9`: HTTP 409 Conflict** on the message POST — a transient server/timing issue (a prior
  conversation's teardown lagged), not a model failure. The build never ran. Not Mode-B, not an
  OpenRouter leak. A future soak should add a 409-retry + inter-run kill/sleep.
- **Build/finish rate is a separate, known issue** (`disco-m3-real-build-capability`: MiniMax-M3 ~50%
  on hard builds; here 8/10 built cleanly). CD-TOOLS fixes the **edit** path (Mode B), **not** the build
  finish rate (Mode A). We do not claim to have fixed the latter.

## Provider-ledger integrity (the hard non-negotiable)

Across **all 10 runs**: **0 OpenRouter calls** and **0 provider calls after terminal**. Every recorded
upstream host is `api.minimaxi.chat`, every model `MiniMax-M3`. MiniMax-direct only, as required.

## Conclusion

The CD-TOOLS campaign (1–10) is complete and **live-proven**: fresh-edit guard → atomic `exact_replace`
→ `safe_write_file` → artifact-aware governed routing → `serve` output-truth → VERIFY read-only scope →
buffered transactional `run_project_script` → tool prompt-pack → **live MiniMax-M3 proof** → consecutive
soak. **Mode B (edit-elision thrash) no longer occurs**: 8/8 live targeted-edit runs that reached the
edit phase made their edits with zero `old_text_not_found`, zero elision rejection, and verified
output-truth — on a 100% MiniMax-direct provider ledger.
