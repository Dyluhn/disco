# Build-Agent Friction Audit — 2026-07-09

Personal scan of every finished build run from the final gauntlet night (5 runs,
1,463 actions, ~258 min of agent wall-clock) plus a tool-by-tool review of all
45 agent-scope tool definitions. Focus: friction that *doesn't fail* — turns
spent on ceremony that still produce good output ("trash that works").

## Part 1 — Trace findings, ranked by wall-clock cost

### F1. The post-edit re-read tax — THE speed fix (~15-20% of turns)
- **43.5% of ALL agent actions are `file_read`** (637 of 1,463).
- **77 times, an edit was immediately followed by a re-read of the same file**
  (46% of the 167 edits). Each is a full LLM round-trip (median 9.4s) ≈ 13+
  minutes across 5 runs — pure ceremony.
- The agent is TRAINED to do this by us: `file_replace_lines`' description
  says "re-read the file IMMEDIATELY before each call", and the freshness
  gates (`FRESH_READ_REQUIRED` ×4, `STALE_FILE_CONTEXT` ×2 even in passing
  runs) punish it when it doesn't.
- **Fix (structural, biggest win available):** mutating file tools
  (file_edit / file_replace_lines / file_insert_lines / file_write) should
  RETURN the updated region in their observation — ±10 lines around the change
  with FRESH line numbers — and that observation should GRANT grounding
  (set the read-since-write bit) for the next edit to the same file. This
  eliminates both the re-read turn and the freshness refusals in one move.
  The line-shift warnings in tool descriptions then change from "re-read
  first" to "your last edit's observation shows the current numbering".

### F2. Bookkeeping turns: update_plan_progress ×52 (~10 per run)
Each progress update is a full LLM round-trip that does no work (~100s/run).
Options, cheapest first: (a) prompt guidance "update at step BOUNDARIES only,
not per action"; (b) let the finish/verify tools infer step completion from
done_conditions so mid-run updates become optional; (c) piggyback progress on
other tool calls (schema change — probably not worth it).

### F3. code_exec is criminally underused (1 use vs 45 `shell python3 -c ...`)
The persistent-IPython-kernel tool sat idle while the model paid process
startup and lost state 45 times. Pure guidance gap — the description never
says "prefer this over shell python3". (Fixed in this commit.)

### F4. Planning-mode shell refusals (×4)
Models consistently want `ls`/`grep`-class inspection during planning and get
refused. The refusal text names the allowed set, so the cost is bounded (one
round-trip each), but consider allowing a read-only shell subset in planning
if this grows.

### F5. The :8000 confusion is partly self-inflicted
One run navigated `browser` to `http://localhost:8000` (nothing of the
build's there). The build prompts hammer "there is NO fixed :8000" — but
`server_status`'s own description said "8000 user-visible", directly
contradicting the doctrine. (Fixed in this commit.) `browser`'s description
also never says "for pass/fail checking use verify_web_app instead" — 37
browser calls include verification loops verify_web_app does in one shot.
(Fixed in this commit.)

### F6. High variance between comparable runs (141 vs 400 actions)
The seo run took 400 actions where blog took 141 for a same-shape site.
The distribution is dominated by F1's read tax + shell debugging of preview
crashes (×4 in the analytics run). No single new mechanism — F1 + the
already-shipped valves are the treatment.

## Part 2 — Tool-guidance audit (all 45 agent-scope tools)

**Strong (use as templates):** `run_project_script` (inline example),
`shell_exec` (anti-patterns named), `update_plan_progress` (example + snapshot
semantics), `submit_plan` (workflow + done_condition example), `verify_web_app`
("call ONCE"), `file_write`/`file_append` (caps + split recipe),
`delegate_explore` (bounded, when-to-use).

**Defects found and FIXED in this commit:**
| Tool | Defect | Fix |
|---|---|---|
| `think` | Claimed "never persisted" — FALSE (it persists as an action event) and it now counts toward bookkeeping limits | Corrected; added "don't chain thinks" warning |
| `server_status` | "8000 user-visible" contradicted the no-:8000 doctrine | Reworded to platform-URL language |
| `code_exec` | Never said when to prefer it over shell python | "Prefer over `shell python3 -c`; state persists" |
| `browser` | No cross-ref to verify_web_app for checks | Added "for pass/fail verification use verify_web_app" |
| `app_set_tweak` | key/value args had NO descriptions | Documented |
| `scaffold_starter` | title arg had NO description | Documented |
| `search` | limit arg undocumented; no when-vs-extract | Documented + cross-ref |
| `extract` | No when-vs-search guidance | Cross-ref added |
| `file_read` | Didn't say whole-file reads are cheap/preferred under the cap | Added (mirrors the churn-nudge advice) |
| `file_list` | Didn't state depth semantics | Stated (single level) |

**Structural recommendations (not description fixes):**
1. **Six overlapping edit tools** (`file_edit`, `file_str_replace`,
   `exact_replace`, `file_replace_lines` + `file_insert_lines`,
   `run_project_script`, `safe_write_file`, plus `find_and_edit`) with no
   decision tree. Every description now cross-references at least one sibling,
   but the surface itself invites choice-paralysis and inconsistent error
   surfaces (three different not-found error codes existed until this week).
   Recommend: pick a blessed trio (anchor edit / line edit / transactional
   batch), fold the rest into aliases or retire them from the agent scope.
2. **`plan_step` is retired but still shipped** in the tool surface — dead
   context weight on every request. Remove from agent scope.
3. **F1's edit-returns-region** change (above) — one spec, touches the four
   mutating file tools + the grounding bit + two description updates.

## Bottom line

The engine no longer fails builds — the valves closed that. What remains is
**ceremony**: roughly a quarter of the agent's turns are reads and bookkeeping
that a better-shaped tool contract would make unnecessary. F1 alone is worth
an estimated 15-20% wall-clock reduction on build mode, F2/F3 a further ~5%.
