> **SUPERSEDED / HISTORICAL (as of 2026-07-07).** Dated model-routing wiring notes (Pi / MiniMax-via-Pi era) that no longer reflect current practice.
> Historical only. Not a source of current status or operating instructions.

# Model routing — the wiring in force (2026-06-11)

Basis: bakeoff/SUMMARY.md, RESULTS-verifier.md, RESULTS-vision.md,
RESULTS-orchestration.md. Ratified by Dylan 2026-06-11 ("no testing, no
relitigating"). Each row is implemented in the named surface.

## GENERAL RULE
**Fable 5 (2x) is used ONLY for (a) code/work verification verdicts and
(b) review of Opus-authored briefs.** Any other job needing a Claude-tier
model falls back to Opus 4.8 / Sonnet (1x) first — NEVER Fable.
DeepSeek V4 (Flash/Pro) is text-only — never route vision to it.

## The table

| Job | Primary | Fallback | Never | Surface |
|---|---|---|---|---|
| Vision / screenshot triage | Gemini 3 Flash | Sonnet (1x) | Fable, DeepSeek (text-only) | scripts/screenshot-triage.sh |
| Writing / drafting (specs, briefs' first drafts) | DeepSeek V4 Pro (direct) | Gemini 3 Flash | Claude as first-drafter | scripts/spec-draft.sh |
| Diff pre-filter (UNVERIFIED LEADS, never verdicts) | DeepSeek V4 Flash (direct, cache-priced) | gpt-oss-120b:free → local Qwen | any model rendering a VERDICT | scripts/diff-prefilter.sh |
| Implementation worker rounds | DeepSeek V4 Pro (pro-direct preset) | Gemini Flash/Pro (free) when capacity | Sonnet (worker set removal 06-10) | scripts/orchestrate.sh presets |
| Orchestration: spec iteration, surgical-brief authoring, deviation/escalation judgment | Opus 4.8 (1x) | — | Fable (matched Opus on these; 2x waste) | the main Claude Code session |
| Verification verdicts + review of Opus-authored briefs | **Fable 5 (2x) ONLY** | none | Opus, Sonnet, any cheap model | Fable subagent (Agent tool `model: fable`), context = diff + pre-filter LEADS |

## Vision fallback ladder (item 1, in force in screenshot-triage.sh)
Gemini 3 Flash → Sonnet (1x). Judge against the spec's EXPLICIT expected-state
string, never "looks ok". Escalate to the Claude review queue ONLY on: FAIL,
PASS-with-no-named-cues, or verdict-disagrees-with-expected. Everything else
auto-passes and logs.

## DISCIPLINE GUARD (the rule that makes the saving real)
**Opus must NEVER run a verification verdict.** Its confident-wrong failure
mode costs more than the 1x saving (RESULTS-orchestration: 0/3 on
verification; Fable 3/3). Where dispatch allows, the verification path
refuses any non-Fable model: verification subagents are pinned
`model: fable`; the pre-filter emits LEADS with an explicit NOT-A-VERDICT
header; no script routes a verdict to a cheap model.

Session-rule reconciliation (supersedes the 2026-06-11 morning "reviews
inline, subagents only for web research" note): when the main session runs
on Opus, verification MUST go to a pinned-Fable subagent — that is the
prescribed exception, not a violation.

## Shared-bug facts (injected into every drafting prompt; spec-draft.sh)
(a) HTTP-probe matching must accept tool name `shell`, not just `shell_exec`.
(b) Conversation-id discovery = snapshot known ids, then poll for NEW ones.
