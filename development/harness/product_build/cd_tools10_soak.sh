#!/usr/bin/env bash
# CD-TOOLS-10 — consecutive live MiniMax-M3 targeted-edit soak. Runs the targeted_edit_run.py driver
# N times, records EVERY verdict (PASS / build-stuck / Mode-B-fail), and appends a one-line summary
# per run to soak_summary.jsonl. MiniMax DIRECT only (relay :8080 → MiniMax); the driver's own oracle
# enforces 0 OpenRouter + 0 post-terminal + the hardened Mode-B-gone checks.
set -u
RUN_DIR="${RUN_DIR:-/tmp/disco-cd-tools10-soak}"
N="${1:-10}"
REPO="${DISCO_REPO_ROOT:-$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/../../.." && pwd)}"
PY="${DISCO_PY:-$REPO/.venv/bin/python3}"
SUMMARY="$RUN_DIR/soak_summary.jsonl"
mkdir -p "$RUN_DIR"
: > "$SUMMARY"
echo "CD-TOOLS-10 soak: $N runs -> $SUMMARY"
for i in $(seq 1 "$N"); do
  tag="s$i"
  echo "=== soak run $i/$N (tag $tag) — $(date -u +%H:%M:%S) ==="
  "$PY" development/harness/product_build/targeted_edit_run.py --tag "$tag" > "$RUN_DIR/cd9_$tag.log" 2>&1
  rc=$?
  # extract the one-line verdict summary from the dossier
  "$PY" - "$RUN_DIR/cd9_dossier_$tag.json" "$tag" "$rc" >> "$SUMMARY" <<'PYEOF'
import json, sys
path, tag, rc = sys.argv[1], sys.argv[2], sys.argv[3]
try:
    v = json.load(open(path))["verdict"]
    c = v["checks"]
    print(json.dumps({
        "tag": tag, "PASS": v["PASS"], "built_ok": v["built_ok"],
        "build_status": v["build_status"], "edit_status": v["edit_status"],
        "edit_tools": c["targeted_edit_tools_called"],
        "edit_on_index": c.get("successful_edit_on_index_html"),
        "fresh_read_before_edit": c.get("fresh_read_before_first_edit"),
        "old_text_not_found": c["old_text_not_found_count"],
        "no_elision_rej": c["no_elision_marker_rejected"],
        "edits_applied": c["edits_applied_new_present_old_absent"],
        "openrouter": c["openrouter_count"], "post_terminal": c["provider_calls_after_terminal"],
        "hosts": c["ledger_hosts"],
    }))
except Exception as e:
    print(json.dumps({"tag": tag, "PASS": False, "error": f"driver/dossier failure rc={rc}: {e}"}))
PYEOF
  tail -1 "$SUMMARY"
done
echo "=== SOAK DONE — summary: ==="
cat "$SUMMARY"
