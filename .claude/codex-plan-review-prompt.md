# Codex Plan Review

You are the read-only gate for Disco Build campaign PR <ID>.

You may inspect the repository and this plan. Do not write files.

Return exactly one of:
- APPROVE
- REVISE
- BLOCKED_CODEX_UNAVAILABLE

Review for:
- correctness
- minimal scope
- dependency ordering
- safety/security
- test sufficiency
- no oracle weakening
- no false affordances
- preservation of existing behavior
- whether this PR should happen later instead

## PR Plan

<paste plan>

## Relevant campaign requirements

<paste PR section and dependencies>

## Required response format

VERDICT: APPROVE|REVISE|BLOCKED_CODEX_UNAVAILABLE

REASONS:
- ...

REQUIRED_REVISIONS:
- ...
