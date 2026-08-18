#!/usr/bin/env bash
# Overnight soak lane runner (2026-07-09). One lane = a sequential playlist of
# scenarios, each a full live run via harness.build_soak.run. Lanes run in
# parallel (separate processes); this script is ONE lane.
#
#   lane.sh <lane_name> <out_root> <seed_base> <scenario> [<scenario> ...]
#
# Per run: a RAM guard (waits while MemAvailable < 12 GiB so lanes can never
# push the box into pressure), then the runner with --autonomous. Every result
# is appended to <out_root>/ledger.tsv (lane, scenario, exit code, run dir,
# seconds). Lanes CONTINUE past failures — triage happens between waves.
set -u
# This process is one of several host-concurrent lanes.  The runner must not
# attribute a host-global container delta to one conversation when its frozen
# event stream lacks an owner id.
export DISCO_BUILD_SOAK_OUTER_LANE=1
LANE="$1"; OUT="$2"; SEED="$3"; shift 3
REPO="${DISCO_REPO_ROOT:-$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/../../.." && pwd)}"
PY="$REPO/.venv/bin/python"
mkdir -p "$OUT"
LEDGER="$OUT/ledger.tsv"

ram_guard() {
  while :; do
    avail_kb=$(awk '/MemAvailable/{print $2}' /proc/meminfo)
    [ "$avail_kb" -gt $((12 * 1024 * 1024)) ] && return
    echo "$(date +%T) [$LANE] RAM guard: only $((avail_kb/1024/1024))G available — waiting" >&2
    sleep 30
  done
}

for SCEN in "$@"; do
  ram_guard
  start=$(date +%s)
  before=$(ls -1 "$OUT" 2>/dev/null | sort)
  "$PY" -m harness.build_soak.run --scenario "$SCEN" --autonomous --out "$OUT" \
    --seed-base "$SEED" \
    > "$OUT/$LANE.$SCEN.$start.log" 2>&1
  rc=$?
  end=$(date +%s)
  after=$(ls -1 "$OUT" 2>/dev/null | sort)
  rundir=$(comm -13 <(echo "$before") <(echo "$after") | grep -v "\.log$\|ledger" | tail -1)
  printf "%s\t%s\t%s\t%s\t%s\n" "$LANE" "$SCEN" "$rc" "${rundir:-?}" "$((end-start))" >> "$LEDGER"
  echo "$(date +%T) [$LANE] $SCEN -> rc=$rc dir=${rundir:-?} ($((end-start))s)"
  SEED=$((SEED + 1))
done
echo "[$LANE] playlist complete"
