#!/usr/bin/env bash
# P1B-LIVE-STABILITY orchestrator — run the single-build stability spec N times CONSECUTIVELY.
#
# Each run is one isolated build (no retry-to-pass). EVERY run is recorded (run-<i>.json +
# run-<i>.log). The orchestrator STOPS on the first non-PASS so the failure can be classified
# (product / harness / model / infra) before the count is restarted — it never hides a flaky run.
# Strict sequencing (one build at a time) is what makes the ledger-native sidecar boundary sound.
#
# Usage: run_stability_matrix.sh <target_consecutive> <report_dir> [scenario_label]
# Requires the live stack up (relay :8080, agent-server :8000) + env:
#   PMX_RELAY_LOG, PMX_VENV_PY, PMX_REPO_ROOT  (passed through to the spec)
set -uo pipefail

TARGET="${1:-10}"
REPORT_DIR="${2:?report dir required}"
LABEL="${3:-static_smoke}"
REPO_ROOT="${PMX_REPO_ROOT:?PMX_REPO_ROOT required}"
mkdir -p "$REPORT_DIR"

streak=0
run=0
echo "[stability] target=$TARGET label=$LABEL report=$REPORT_DIR"
while [ "$streak" -lt "$TARGET" ]; do
  run=$((run + 1))
  echo "[stability] === run $run (streak $streak/$TARGET) ==="
  STAB_RUN_INDEX="$run" STAB_REPORT_DIR="$REPORT_DIR" \
    npx --prefix "$REPO_ROOT/frontend" playwright test \
      --config "$REPO_ROOT/frontend/playwright.live.config.ts" stability-run \
      > "$REPORT_DIR/run-$run.log" 2>&1
  ec=$?
  status=$("$PMX_VENV_PY" -c "import json,sys;print(json.load(open('$REPORT_DIR/run-$run.json'))['verdict'].get('status','NO_RECORD'))" 2>/dev/null || echo "NO_RECORD")
  echo "[stability] run $run: exit=$ec verdict=$status"
  if [ "$status" = "PASS" ]; then
    streak=$((streak + 1))
  else
    echo "[stability] STOP at run $run — verdict=$status (classify before restarting the count)"
    break
  fi
done

echo "[stability] FINAL: consecutive PASS streak = $streak / $TARGET (total runs = $run)"
[ "$streak" -ge "$TARGET" ] && echo "[stability] GREEN" || echo "[stability] NOT GREEN"
