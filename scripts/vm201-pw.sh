#!/usr/bin/env bash
# Run a Node Playwright script on VM 201 (sandbox, 100.81.82.115) — the dedicated browser
# host (no memcap). Usage: vm201-pw.sh <local-script.cjs> [arg ...]
# Sends the script + any local file args (paths under the workspace), runs on VM 201,
# pulls back anything written to ~/pwhost/work/out/. Screenshots/artifacts land in ./vm201-out/.
set -euo pipefail
VM=100.81.82.115; SCRIPT="$1"; shift
ssh "$VM" 'mkdir -p ~/pwhost/work/out ~/pwhost/work/in'
scp -q "$SCRIPT" "$VM:~/pwhost/run.cjs"
ssh "$VM" 'cd ~/pwhost && node run.cjs '"$*"' 2>&1'
mkdir -p ./vm201-out
scp -q -r "$VM:~/pwhost/work/out/." ./vm201-out/ 2>/dev/null || true
echo "[vm201-pw] outputs in ./vm201-out/"
