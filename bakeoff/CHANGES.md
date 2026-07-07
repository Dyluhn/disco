> **SUPERSEDED / HISTORICAL (as of 2026-07-07).** Dated model-routing change log (Pi / MiniMax-via-Pi era) that no longer reflects current practice.
> Current status of record: `docs/disco-project-state.md` (master), `docs/disco-status-and-remaining.md` (features + remaining), `sec-work-remaining/disco-security-state.md` (security). This file is kept for history and may contain stale claims.

# Routing changes — 2026-06-11 (each committed separately)

1. Vision triage: Gemini 3 Flash primary → Sonnet fallback, NEVER Fable (`47dcef0`; RESULTS-vision: Flash 4/4, triage ≠ verification).
2. Drafting: DeepSeek V4 Pro primary, Flash secondary, Claude never first-drafts; two shared-bug facts injected into every drafting prompt (`8523b20`; RESULTS-spec: candidates all repeated both bugs).
3. Diff pre-filter: DeepSeek V4 Flash (direct, cache-priced) emits UNVERIFIED LEADS narrowing Fable's context — never verdicts (`8645af9`; RESULTS-verifier: cheap reviewers 0/3, confidently wrong).
4. Orchestration: Opus 4.8 for spec-iteration / brief-authoring / deviation judgment; Fable 5 (2x) ONLY for verification verdicts + Opus-brief review, via pinned `model: fable` subagents (RESULTS-orchestration: Opus matched Fable except verification).
5. Discipline guard in ROUTING.md: Opus never renders verification verdicts; verification path refuses non-Fable.

Honest expectation: the real saving depends on DeepSeek holding drafting quality
well enough to keep the escalation-to-Fable rate flat — if drafts bounce more,
Fable load goes UP, the opposite of the goal. loop-report.md will show the
actual session-hour number after one full run on this wiring.
