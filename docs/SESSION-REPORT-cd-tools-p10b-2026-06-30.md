# Session Report — CD-TOOLS campaign (1–10) + P10b export-smoke

**Repo:** `~/projects/disclaude` (isolated Disco-Pi clone) · **Date:** 2026-06-30
**Driver model under test:** MiniMax-M3 (DIRECT, `api.minimaxi.chat`) · **Reviewer/gate:** Codex (gpt-5.5)
**Final state:** all work committed + pushed; local `HEAD` == `origin` (`bf1bc998`); working tree clean.

---

## 1. The problem this session solved — "Mode B"

**Mode B = edit-elision thrash.** Disco Build snips large tool-arg bodies out of the model's context
view (`_snip_args`, `_ARG_SNIP_CHARS = 1500`). So when a model is asked to make a *targeted* edit to a
*large* file, it can no longer reproduce the `old` text it must match → it loops on
`old_text_not_found`, or copies the literal elision marker (`<… N chars elided …>`) into the tool args
→ it gets STUCK and the build stalls.

**The campaign's thesis:** re-architect Disco Build into a *host-owned artifact runtime* so the host —
not the model's fragile context — owns the truth about file state, and the model is given guarded,
atomic, output-truthful edit tools. Then **prove on a real live model that Mode B no longer happens.**

---

## 2. What was accomplished (PR by PR)

Every PR followed the same loop: **plan → write to `disclaude.md` → Codex binding plan-gate → revise to
APPROVE → implement (one canonical writer) → tests (both `packages/tools` + `packages/core` suites) +
`basedpyright` → Codex binding code/evidence-gate → revise to APPROVE → commit/push → heartbeat.**

| PR | What shipped | Key commit |
|---|---|---|
| **CD-TOOLS-1** | **Fresh-edit guard** — blocks exact/line edits against stale or elided source. SHA-aware read records, size-gated at 1500 B; codes `FRESH_READ_REQUIRED` / `STALE_FILE_CONTEXT` / `ELISION_MARKER_REJECTED`. This is the core Mode-B fix. | `0037a8c6` |
| **CD-TOOLS-2** | **Atomic `exact_replace`** — validate-in-memory (incl. syntax) then a single write, so a partial/failed edit never lands. | `1e2447f0` |
| **CD-TOOLS-3** | **`safe_write_file`** — guarded whole-file writer: shrink guard, governed-artifact guard, elision guard, atomic commit (mkstemp + `os.replace`). | `7977a36b` |
| **CD-TOOLS-4** | **Artifact-aware writes** — uniform governed `.disco/` routing guard across **all 8 mutators** (not just 2), bound to the real symlink-resolved path. | `4a2dd661` |
| **CD-TOOLS-5** | **`serve` output-truth** — no deliverable handoff for a non-existent path. (Pivoted away from a redundant `show_to_user` after discovering `DefaultToolExecutor` drops `ToolOutcome.artifacts` — `serve`→`DeliverableEvent` is the real handoff.) | `ba5d30c7` |
| **CD-TOOLS-6** | **VERIFY phase = read-only diagnostics** — the verifier can inspect (`verify_web_app`, `file_read`, etc.) but not mutate; `VERIFIER_ONLY_TOOL_BLOCKED`. | `d00749ef` |
| **CD-TOOLS-7** | **`run_project_script`** — buffered, transactional batch of file transforms (read/ls/replace_text/save). All-or-nothing: every guard runs in memory first; any failure → nothing written. | `36a8a5b2` |
| **CD-TOOLS-8** | **Tool prompt-pack** — teaches the driver to *use* the new tools. `exact_replace`/`file_str_replace` named only behind the `ANCHORED_EDIT` capability (the same axis the tool surface withholds on); `run_project_script`/`safe_write_file` named all-tier; universal discipline (fresh-read, never-echo-elision, never-rewrite-whole-file-for-a-small-edit). | `db911e59` |
| **CD-TOOLS-9** | **LIVE proof Mode B is gone** — a real MiniMax-M3 build-then-edit run, hardened oracle, Codex evidence-APPROVE. | `a26c2ace` |
| **CD-TOOLS-10** | **Consecutive soak** — 10 live runs; report with dual metrics. | `db87be43`, `67d56d64` |
| **P10b** | **LIVE export-smoke** — real `DeliverableEvent` + real 6100-byte download via `GET /artifacts/{path}`. | `340d4378` |

### The live results (the part that matters)

**CD-TOOLS-9 (single deep proof, run `r3`):** MiniMax-M3 built a large `index.html`, then in the edit
phase did `file_read(index.html)` **first** (seq 28), then `file_edit` ×4 on `index.html` (hero / CTA /
footer, real `old`→`new`) all **succeeded** (seq 35+), `verify_web_app` passed, FINISHED. Served page had
the NEW text and the OLD text gone (output-truth, 17.7 KB). `old_text_not_found = 0`. Ledger: 35 calls,
all `api.minimaxi.chat` / MiniMax-M3, **0 OpenRouter, 0 post-terminal**.

**CD-TOOLS-10 soak (10 runs):**
- **Mode-B-gone: 8/8** runs that reached the edit phase had `old_text_not_found = 0`, no elision
  rejection, and edits applied. **100% — zero thrash in any run that edited.**
- **Clean-finish: 7/10** (one benign edit-timeout; two transient HTTP 409s where the build never ran).
- **0 OpenRouter and 0 post-terminal across all 10 runs.**

**P10b (export proof):** MiniMax-M3 built `export.html`, served it `kind="files"` →
`DeliverableEvent{artifact_kind:"files", path:"export.html"}` → `GET /artifacts/export.html` returned
**HTTP 200, 6100 real bytes** of the actual HTML (build-marker token present, re-verified independently).
Ledger 0 OpenRouter / 0 post-terminal.

---

## 3. Implementation — how it was built

### Source changes (the host-owned artifact runtime)
- **`packages/tools/src/disco/tools/builtin/files.py`** — the heart. Fresh-edit guard (`guard_fresh_edit`,
  `record_read`, sha-aware read records, elision detection), `ExactReplaceTool`, `_atomic_write`,
  `SafeWriteFileTool`, governed-routing guard (`_governed_guard` / `_route_for_governed`) called at the
  top of all 8 mutators.
- **`packages/tools/src/disco/tools/sandbox/{process,_container,session}.py`** — `atomic_write` (mkstemp
  random `O_EXCL` + `os.replace`; container variant via `put_archive` + `mv -fT` onto the resolved real
  target) and `resolve_relpath` (symlink-followed real path) on every sandbox backend.
- **`packages/tools/src/disco/tools/builtin/run_script.py`** (new) — `RunProjectScriptTool`: in-memory
  buffer keyed by the **real resolved path**, validate-all-then-atomic-commit, every CD-TOOLS guard
  reused.
- **`packages/core/src/disco/core/llm/{exec_policy,prompts}.py`** — withheld-tools axis
  (`exact_replace` withheld unless `anchored_edit`), and the prompt-pack (universal discipline +
  capability-gated anchored bullet).
- **`packages/core/src/disco/core/loop/turn_control.py`**, **`contract/{scopes,enforce}.py`** — serve
  output-truth + VERIFY read-only scope.

### Live harness (new, under `harness/product_build/`)
- **`targeted_edit_run.py`** — drives a real build-then-edit through the agent-server HTTP API
  (`POST /conversations` → `/messages` → `/followup` → poll `/state` → `GET /events`), classifies the
  Mode-B-gone oracle from **real `tool_call`/`tool_result` events**, verifies output-truth on the served
  page, and reads the provider ledger.
- **`cd_tools10_soak.sh`** — runs the driver N× consecutively, recording every verdict to
  `soak_summary.jsonl`.
- **`export_smoke_run.py`** — P10b: detects the `DeliverableEvent` and fetches the real download bytes.

### Infrastructure used (confirmed live before any run)
- agent-server `:8000` (`DISCO_CONFIG` → `default_model = driver-minimax` → `base_url
  http://localhost:8080/v1`) → relay `:8080` (`minimax_relay.py`, OpenAI→MiniMax passthrough) →
  **MiniMax-M3 direct** (`api.minimaxi.chat`). Provider ledger at `relay.jsonl` (host + model per call).
- Token loaded from env only (`opencode auth.json` → `minimax-coding-plan`), never echoed/committed.

### Tests
- New unit tests per PR (`test_fresh_edit_guard`, `test_exact_replace`, `test_safe_write_file`,
  `test_governed_routing`, `test_run_project_script`, `test_cd_tools8_prompt_pack`,
  `test_serve_output_truth`, `test_cd_tools6_verify_scope`), plus snapshot/contamination tests kept green.
  Both `packages/tools` + `packages/core` suites run each PR; `basedpyright` 0/0.

---

## 4. Snags hit (and how they were resolved)

The recurring discipline: **a green exit code is a hypothesis, not a verdict** — every "PASS" was
interrogated before being trusted.

1. **`_snip_args` was *not* the build-loop driver (over-attribution).** Earlier work flagged it as the
   "keystone"; unbiased review showed the macOS build trace had *zero* elision markers. CD-TOOLS kept the
   `_snip_args` execution-guard scope honest (it's the targeted-edit / Mode-B fix, not a cure-all).

2. **Aliasing bugs (recurred 6× in CD-TOOLS-4).** Every one was a check computed from one path
   representation and an effect from another. The crystallized lesson: *"a guard is only as sound as the
   key it's bound to"* — bind the check, the buffer, and the write to **one real resolved path**.

3. **CD-TOOLS-5 false affordance.** `DefaultToolExecutor` silently drops `ToolOutcome.artifacts`, so a
   `show_to_user` tool would have looked wired but done nothing. Pivoted to hardening the existing `serve`
   handoff instead.

4. **CD-TOOLS-8 prompt contamination trap.** The prompt-pack made the prompt *contain* the tool names —
   so a naive test that substring-matched the prompt would always "pass." Forced real-event parsing and
   gated `exact_replace` strictly on the capability the tool surface actually withholds on.

5. **CD-TOOLS-9 first live run was a harness bug, not a model success.** `POST /files` creates an *upload
   reference*, not a workspace file → `file_list(".")` was empty → **MiniMax correctly *refused*** ("the
   workspace is empty"). That honest refusal exposed three harness bugs: (a) upload ≠ workspace, (b) the
   build surface starts in PLANNING mode, (c) the classifier substring-matched prompt text. Rewrote to the
   campaign's true **build-then-edit** scenario with real-event parsing and served-page output-truth.

6. **CD-TOOLS-9 second run crashed:** `POST /conversations` returns `conversation_id`, not `id`. One-line
   fix.

7. **CD-TOOLS-9 oracle was looser than the proof claim (Codex hardened it twice).** Added: read-*before*-
   edit *ordering* (not just "a read happened"), OLD-token *absence* on the served page (real in-place
   replace, not an append), edit on the *exact* `index.html`, and pre-edit sentinel *presence* (so "old
   absent" isn't vacuous). r3 still passed under the full hardened oracle.

8. **CD-TOOLS-10 two non-PASS runs were *not* regressions.** `s5` edited before its phase-2 read yet
   succeeded with `old_text_not_found = 0` — because it was already grounded from the build phase, i.e. the
   guard *correctly* didn't force a redundant read (plus a finish-rate timeout). `s2`/`s9` were transient
   HTTP 409s. Reading the event seqs — not the headline — separated finish-rate (Mode A) from edit-thrash
   (Mode B); the report keeps them distinct rather than inflating the result.

9. **P10b `/artifacts/{path}` serves files, not directories/`.zip`** (Codex caught). Pinned the deliverable
   to a concrete fetchable `export.html` with a build-marker token, and re-verified the token in the full
   downloaded body (it sat past the 2000-char head snippet I first checked).

---

## 5. Current status

| Item | Status |
|---|---|
| CD-TOOLS-1 … 8 (the tool layer + prompts) | **SHIPPED, gated, committed, pushed** |
| CD-TOOLS-9 (live proof Mode B gone) | **PROVEN** — `docs/cd-tools-9-10-report.md` |
| CD-TOOLS-10 (10× soak) | **DONE** — 8/8 edit-phase non-thrashing, 0 OpenRouter |
| P10b (export-smoke) | **DONE, live-proven** — `docs/p10b-export-smoke-evidence.md` |
| Repo sync | local `HEAD` == `origin` `bf1bc998`; tree clean |

**Non-negotiables held throughout:** MiniMax-direct only (0 OpenRouter, 0 post-terminal across every
live run); provider ledger recorded every run; output-truth (edits/downloads verified against real
served bytes, never a fixture claim); one canonical writer; no faked pass; no oracle weakened.

### Carried forward (future sessions — recorded, **not** started; no actionable spec was queued)
- **P11** — Resource Import / Provenance.
- **P1B-LIVE-STABILITY** browser-harness gaps (e.g. G1 SIDECAR slice capture).
- **CD-TOOLS follow-ups** (honestly deferred): 5b (`ready_for_verification` finalizer-tool + verifier
  fork), 4b (runtime manifest-fold), §6 full ToolScope phase wiring, soak 409-retry + inter-run
  kill/sleep.

### Evidence / docs to read
- `docs/cd-tools-9-10-report.md` — the live soak report (per-run table + dual-metric verdict).
- `docs/p10b-export-smoke-evidence.md` — the export proof.
- `disclaude.md` — the full append-only campaign ledger (plans, gates, heartbeats).
