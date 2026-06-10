#!/usr/bin/env bash
# scripts/diff-prefilter.sh — cheap-model claim-vs-diff pre-filter. LEADS, NEVER VERDICTS.
#
# Implements the Job 1 SUPPLEMENT from the bake-off (bakeoff/RESULTS-verifier.md):
# cheap reviewers went 0/3 on real defects and were CONFIDENTLY WRONG (two models
# produced the identical false catch from the same commit-message bait) — so no
# cheap output may ever gate, approve, or reject anything. The one thing they all
# did find was a claim-vs-diff discrepancy (a /health endpoint the diff never adds).
# This script harvests exactly that class of lead and nothing else; the verification
# VERDICT stays with Claude, with these leads appended as labeled hints.
#
# Usage:
#   scripts/diff-prefilter.sh <commit-ish | diff-file> [--name slug]
#
# Output: review-queue/prefilter/<name>.leads.txt, headed by an UNVERIFIED banner.
# Exit:   0 leads file written;  1 no model reachable / bad input.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PROMPT_FILE="$REPO_ROOT/prompts/diff-prefilter.txt"
OUTDIR="$REPO_ROOT/review-queue/prefilter"
QWEN_URL="http://127.0.0.1:18080/v1/chat/completions"
mkdir -p "$OUTDIR"

target="${1:?usage: diff-prefilter.sh <commit-ish | diff-file> [--name slug]}"
shift
name=""
while [ $# -gt 0 ]; do case "$1" in
  --name) name="$2"; shift 2 ;;
  *) echo "unknown option: $1" >&2; exit 64 ;;
esac; done

# resolve input: a diff file, or a commit-ish resolved via git show
if [ -f "$target" ]; then
  diff_text="$(cat "$target")"
  [ -n "$name" ] || name="$(basename "$target" | tr -cd 'a-zA-Z0-9._-')"
elif git -C "$REPO_ROOT" rev-parse --verify --quiet "$target^{commit}" >/dev/null; then
  diff_text="$(git -C "$REPO_ROOT" show "$target")"
  [ -n "$name" ] || name="$(git -C "$REPO_ROOT" rev-parse --short "$target")"
else
  echo "not a readable diff file or resolvable commit: $target" >&2
  exit 1
fi
[ -n "$diff_text" ] || { echo "empty diff for $target" >&2; exit 1; }

prompt="$(cat "$PROMPT_FILE")
$diff_text"

errf="$(mktemp)"; trap 'rm -f "$errf"' EXIT
export PMX_PREFILTER_PROMPT="$prompt"   # consumed by the local-Qwen fallback

# ---- primary: gpt-oss-120b:free via pi (paid pi models REVOKED 2026-06-10) -----
leads="$(pi --provider openrouter --model openai/gpt-oss-120b:free -p --no-session -nt -ne -ns -nc \
        "$prompt" 2>"$errf" | grep -v '^Warning: No models match' || true)"
model="gpt-oss-120b:free"

# ---- fallback: local Qwen 27B (keyless, llama-server :18080) --------------------
if [ -z "$(printf '%s' "$leads" | tr -d '[:space:]')" ]; then
  leads="$(python3 - "$QWEN_URL" <<'PY' 2>>"$errf" || true
import json, sys, urllib.request, os
url = sys.argv[1]
body = json.dumps({
    "model": "local", "temperature": 0.2,
    "messages": [{"role": "user", "content": os.environ["PMX_PREFILTER_PROMPT"]}],
}).encode()
req = urllib.request.Request(url, body, {"Content-Type": "application/json"})
print(json.load(urllib.request.urlopen(req, timeout=600))["choices"][0]["message"]["content"])
PY
)"
  model="qwen-27b-local (fallback)"
fi

if [ -z "$(printf '%s' "$leads" | tr -d '[:space:]')" ]; then
  echo "PREFILTER ERROR: neither gpt-oss-120b:free nor local Qwen returned output" >&2
  sed 's/^/  stderr: /' "$errf" >&2 || true
  exit 1
fi

out="$OUTDIR/$name.leads.txt"
{
  echo "================== UNVERIFIED PRE-FILTER LEADS (cheap model) =================="
  echo "Leads only — NEVER verdicts. Bake-off ground truth (bakeoff/RESULTS-verifier.md):"
  echo "cheap reviewers scored 0/3 on real defects and were confidently wrong; nothing"
  echo "below may gate, approve, or reject anything. Each lead must be independently"
  echo "verified against the actual diff/code before being believed or acted on."
  echo "model: $model   target: $target   generated: $(date -Is)"
  echo "==============================================================================="
  printf '%s\n' "$leads"
} >"$out"
echo "leads written -> ${out#"$REPO_ROOT"/} (model: $model)"
