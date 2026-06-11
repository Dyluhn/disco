#!/usr/bin/env bash
# scripts/screenshot-triage.sh — first-pass screenshot triage on a cheap vision model.
#
# Implements the Job 2 cut from the bake-off (bakeoff/RESULTS-vision.md: Gemini 3
# Flash 4/4). The cheap model judges every screenshot against an EXPLICIT expected
# state; the orchestrator sees only escalations. A fallback is MANDATORY, not
# optional — Flash's sibling Pro tier hit a hard quota wall mid-bake-off
# (TerminalQuotaError). Fallback = Sonnet (headless Claude Code, Read-only): the
# MiniMax-via-pi route was REVOKED 2026-06-10 (no paid OpenRouter models via pi;
# Dylan: "for vision, you can utilize sonnet").
#
# Usage:
#   scripts/screenshot-triage.sh <shot.png> "<expected-state sentence>" [--name slug]
#
# Exit codes:  0 = auto-passed (logged)   3 = ESCALATED to review-queue/screenshots/
#              1 = pipeline error (no model reachable / unreadable input)
#
# GENERAL ROUTING RULE (bakeoff/ROUTING.md): Fable 5 (2x) is ONLY for code
# verification + review of Opus-authored briefs. Screenshot triage is a VISION
# CHECK, not verification — it must NEVER ride Fable. Ladder: Gemini 3 Flash
# (primary, cheapest-capable) -> Sonnet 1x (fallback). DeepSeek is text-only
# and not in this ladder.
#
# Escalation triggers (any one):
#   - model verdict FAIL
#   - model confidence LOW
#   - verdict PASS but the model could name no expected-state cue it actually saw
#     ("PASS with no cues" is the rubber-stamp signature)
#   - output malformed (model didn't follow the 3-line contract)
# Everything else is auto-passed and appended to the triage log.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PROMPT_FILE="$REPO_ROOT/prompts/screenshot-triage.txt"
QUEUE="$REPO_ROOT/review-queue/screenshots"
TRIAGE_LOG="$QUEUE/triage-log.tsv"
mkdir -p "$QUEUE"

shot="${1:?usage: screenshot-triage.sh <shot.png> \"<expected state>\" [--name slug]}"
expected="${2:?expected-state string required — what the shot SHOULD show, per the spec author}"
shift 2
name="$(basename "$shot" | tr -cd 'a-zA-Z0-9._-')"
while [ $# -gt 0 ]; do case "$1" in
  --name) name="$2"; shift 2 ;;
  *) echo "unknown option: $1" >&2; exit 64 ;;
esac; done

[ -s "$shot" ] || { echo "screenshot missing/empty: $shot" >&2; exit 1; }
[ -s "$PROMPT_FILE" ] || { echo "prompt template missing: $PROMPT_FILE" >&2; exit 1; }

prompt="$(sed "s|{{EXPECTED}}|$(printf '%s' "$expected" | sed 's/[&|\\]/\\&/g')|" "$PROMPT_FILE")"
errf="$(mktemp)" out="" model=""
trap 'rm -f "$errf"' EXIT

# ---- primary: Gemini 3 Flash --------------------------------------------------
out="$(gemini -m gemini-3-flash-preview -p "@$shot $prompt" 2>"$errf" || true)"
model="gemini-3-flash-preview"

# ---- mandatory fallback: Sonnet via headless claude (quota wall is a proven event)
if [ -z "$(printf '%s' "$out" | tr -d '[:space:]')" ] \
   || grep -Eqi 'TerminalQuotaError|RESOURCE_EXHAUSTED|MODEL_CAPACITY_EXHAUSTED|status (429|5[0-9][0-9])' "$errf"; then
  out="$(claude --model sonnet --allowedTools Read -p \
        "Read the screenshot image at $shot, then answer exactly as instructed: $prompt" \
        2>"$errf" || true)"
  model="sonnet (fallback)"
fi

if [ -z "$(printf '%s' "$out" | tr -d '[:space:]')" ]; then
  echo "TRIAGE ERROR: neither Flash nor Sonnet returned output for $shot" >&2
  sed 's/^/  stderr: /' "$errf" >&2 || true
  exit 1
fi

# ---- parse the 3-line contract -------------------------------------------------
verdict="$(printf '%s\n' "$out" | grep -m1 -oE '^(PASS|FAIL):.*' || true)"
cues="$(printf '%s\n' "$out" | grep -m1 -oE '^CUES:.*' | sed 's/^CUES:[[:space:]]*//' || true)"
conf="$(printf '%s\n' "$out" | grep -m1 -oE '^CONFIDENCE:[[:space:]]*(HIGH|LOW)' | grep -oE '(HIGH|LOW)' || true)"

escalate_reason=""
if [ -z "$verdict" ] || [ -z "$conf" ]; then
  escalate_reason="malformed output (3-line contract not followed)"
elif printf '%s' "$verdict" | grep -q '^FAIL:'; then
  escalate_reason="model verdict FAIL"
elif [ "$conf" = "LOW" ]; then
  escalate_reason="model confidence LOW"
elif [ -z "$cues" ] || printf '%s' "$cues" | grep -qiE '^(none|n/a)\.?$'; then
  escalate_reason="PASS but no expected-state cue actually seen (rubber-stamp signature)"
fi

ts="$(date +%Y%m%dT%H%M%S)"
if [ -n "$escalate_reason" ]; then
  dir="$QUEUE/$ts-$name"
  mkdir -p "$dir"
  cp "$shot" "$dir/"
  {
    echo "shot:      $shot"
    echo "expected:  $expected"
    echo "model:     $model"
    echo "escalated: $escalate_reason"
    echo "--- raw model output ---"
    printf '%s\n' "$out"
  } >"$dir/verdict.txt"
  printf '%s\tESCALATED\t%s\t%s\t%s\t%s\n' "$ts" "$name" "$model" "$escalate_reason" "$expected" >>"$TRIAGE_LOG"
  echo "ESCALATED -> ${dir#"$REPO_ROOT"/} ($escalate_reason)"
  exit 3
fi

printf '%s\tAUTO-PASS\t%s\t%s\t%s\t%s\n' "$ts" "$name" "$model" "$verdict" "$expected" >>"$TRIAGE_LOG"
echo "AUTO-PASS [$model] $verdict"
exit 0
