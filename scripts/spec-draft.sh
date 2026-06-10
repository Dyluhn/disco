#!/usr/bin/env bash
# scripts/spec-draft.sh — MiniMax M3 drafts a live spec; deterministic checks gate it.
#
# Implements the Job 3 split from the bake-off (bakeoff/RESULTS-spec.md): MiniMax M3
# was the strongest spec drafter (one pattern beat the Claude baseline), but ALL
# three candidates false-failed every real passing run on the shell-vs-shell_exec
# tool-name fact. Drafting moves to MiniMax; the iterate-against-reality loop and
# final sign-off stay with Claude. This script makes the draft and then runs the
# checks that are CHEAPER THAN A LIVE RUN, so a ~25-minute real build is never spent
# on a draft that would false-fail mechanically.
#
# Usage:
#   scripts/spec-draft.sh <brief.md> <name>          draft via MiniMax, then gate
#   scripts/spec-draft.sh --check <file.spec.ts>     gate an existing draft only
#
# Artifacts:
#   agent-projects/spec-candidates/<name>.spec.ts        (the draft)
#   agent-projects/spec-candidates/<name>.checklist.txt  (gate results)
#   agent-projects/spec-candidates/<name>.raw.txt        (raw model output)
#
# Exit: 0 all gates pass (worth Claude's iteration time);  2 gates failed (fix the
# draft or the brief BEFORE any live run);  1 pipeline error.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
CAND="$REPO_ROOT/agent-projects/spec-candidates"
LIVE_DIR="$REPO_ROOT/frontend/e2e-live"
mkdir -p "$CAND"

if [ "${1:-}" = "--check" ]; then
  spec="${2:?usage: spec-draft.sh --check <file.spec.ts>}"
  [ -s "$spec" ] || { echo "spec missing/empty: $spec" >&2; exit 1; }
  name="$(basename "$spec" .spec.ts | tr -cd 'a-zA-Z0-9._-')"
  brief="(none — check-only)"
  checklist="$CAND/$name.checklist.txt"
else
  brief="${1:?usage: spec-draft.sh <brief.md> <name> | --check <file.spec.ts>}"
  name="${2:?candidate name required}"
  [ -s "$brief" ] || { echo "brief missing/empty: $brief" >&2; exit 1; }

  raw="$CAND/$name.raw.txt"
  spec="$CAND/$name.spec.ts"
  checklist="$CAND/$name.checklist.txt"

  # ---- 1. draft via MiniMax M3 (pi) --------------------------------------------
  pi --provider openrouter --model minimax/minimax-m3 -p --no-session -nt -ne -ns -nc \
    "$(cat "$brief")" 2>/dev/null | grep -v '^Warning: No models match' >"$raw" || true
  [ -s "$raw" ] || { echo "DRAFT ERROR: MiniMax returned nothing" >&2; exit 1; }

  # extract the first fenced code block (```typescript / ```ts / bare ```)
  awk '/^```(typescript|ts)?[[:space:]]*$/ { if (!inb) { inb=1; next } else exit } inb' "$raw" >"$spec"
  [ -s "$spec" ] || { echo "DRAFT ERROR: no fenced code block in model output ($raw)" >&2; exit 1; }
fi

# ---- 2. deterministic gates (each encodes a paid-for bake-off lesson) -----------
pass=0 fail=0
gate() { # <PASS|FAIL> <label> <detail>
  printf '%-4s %s — %s\n' "$1" "$2" "$3" >>"$checklist"
  if [ "$1" = PASS ]; then pass=$((pass+1)); else fail=$((fail+1)); fi
}
: >"$checklist"
echo "candidate: $spec   brief: $brief   $(date -Is)" >>"$checklist"

# (a) shell-vs-shell_exec: the bake-off's shared false-fail. The draft must accept
#     tool name "shell" (standalone, not as a prefix of shell_exec/shell_view/...)
#     wherever it matches HTTP probes.
if grep -E "['\"]shell['\"]" "$spec" >/dev/null; then
  gate PASS "shell-tool-acceptance" "accepts plain 'shell' tool name (RESULTS-spec.md shared false-fail)"
else
  gate FAIL "shell-tool-acceptance" "matches only shell_exec — would false-fail real runs whose curl arrives via 'shell'"
fi

# (b) host coverage in probe matching: localhost-only literals miss 127.0.0.1/0.0.0.0
if grep -E '127\\?\.0\\?\.0\\?\.1|0\\?\.0\\?\.0\\?\.0' "$spec" >/dev/null; then
  gate PASS "probe-host-coverage" "probe matching covers non-localhost host literals"
else
  gate FAIL "probe-host-coverage" "probe matching appears localhost-only (DeepSeek candidate defect)"
fi

# (c) snapshot-cid discovery: known-ids-before-submit, not newest-first limit=1
if grep -Ei 'known|seen|existing|snapshot' "$spec" | grep -qi 'id'; then
  gate PASS "snapshot-cid-discovery" "snapshots known conversation ids before submitting (MiniMax pattern, beats newest-first)"
else
  gate FAIL "snapshot-cid-discovery" "no known-ids snapshot found — newest-first limit=1 can mis-bind on a busy server"
fi

# (d) skip-not-crash on a down backend
if grep -q 'test.skip' "$spec" && grep -Eq 'try[[:space:]]*\{|\.catch\(' "$spec"; then
  gate PASS "health-skip" "backend probe guarded + test.skip on unhealthy"
else
  gate FAIL "health-skip" "down backend would crash the test instead of skipping (Qwen candidate defect)"
fi

# (e) parse gate: the draft must list cleanly under the live Playwright config
tmp="$LIVE_DIR/__cand_$name.spec.ts"
cp "$spec" "$tmp"
if (cd "$REPO_ROOT/frontend" && npx playwright test --list \
      --config=playwright.live.config.ts --project=firefox 2>&1 | grep -q "__cand_$name"); then
  gate PASS "playwright-parse" "lists cleanly under playwright.live.config.ts"
else
  gate FAIL "playwright-parse" "does not parse/list under the live config"
fi
rm -f "$tmp"

echo "----" >>"$checklist"
echo "gates: $pass pass, $fail fail" >>"$checklist"
cat "$checklist"
if [ "$fail" -gt 0 ]; then
  echo "CANDIDATE GATED: fix the draft (or the brief) before spending a live run." >&2
  exit 2
fi
echo "candidate ready for Claude's iterate-against-reality loop: ${spec#"$REPO_ROOT"/}"
