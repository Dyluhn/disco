#!/usr/bin/env bash
# scripts/orchestrate.sh — deterministic worker-dispatch + commit-sequencing plumbing.
#
# Implements the bake-off routing decision (bakeoff/SUMMARY.md): "orchestration
# plumbing (commit sequencing, polling, worker salvage) moves to bash."
# This script calls NO model, ever. Pure coordination. On any worker failure it
# STOPS and writes a one-paragraph reason file — it never improvises.
#
# Subcommands:
#   dispatch <order> <worker> <brief-file> <artifact-path>
#            [--timeout-min N=45] [--stall-min N=8] [--watch]
#       Launch a worker in tmux session wkr-<order>, log to .orchestrate/logs/.
#       REFUSES if: order unknown; another ACTIVE order shares a manifest file;
#       any of the order's manifest files already has uncommitted changes that
#       belong to an OTHER order (the prompts.py-collision class); brief missing.
#   watch <order>
#       Block until the worker exits. Kills it on wall-clock timeout or stall
#       (no log growth for stall-min minutes), writes a reason file, exits 1.
#       On clean exit chains into `verify`.
#   verify <order>
#       Worker exited: check recorded exit code, then that the artifact EXISTS,
#       is NON-EMPTY and FRESH (mtime >= dispatch time). If the worker "claimed
#       success" but wrote nothing (the pi printed-to-stdout failure,
#       bakeoff/CONTEXT.md §4): salvage the tmux log to the artifact path and
#       FLAG it (exit 2). If nothing to salvage: reason file, exit 1.
#   commit <order> -m <message>
#       Stage ONLY the order's manifest files and commit. REFUSES if a shared
#       file carries another order's uncommitted changes, or anything outside
#       the manifest is already staged.
#   status
#       One line per active order: age, log size, last-growth, tmux liveness.
#
# Worker presets (model choice per bakeoff/SUMMARY.md):
#   flash       gemini -m gemini-3-flash-preview --yolo   (implementation)
#   pi-minimax  pi … minimax/minimax-m3                   (spec drafts / triage fallback)
#   pi-free     pi … openai/gpt-oss-120b:free             (recon)
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
MANIFEST="$REPO_ROOT/orders.yaml"
STATE="$REPO_ROOT/.orchestrate"
LOGS="$STATE/logs" ACTIVE="$STATE/active" DONE="$STATE/done" STOPD="$STATE/stop" FLAGS="$STATE/flags"
mkdir -p "$LOGS" "$ACTIVE" "$DONE" "$STOPD" "$FLAGS"
POLL_SECS=30

# ----------------------------------------------------------------------------- util
fail_reason() { # <order> <slug> <one-paragraph reason>  -> writes reason file, exits 1
  local order="$1" slug="$2" reason="$3"
  local f="$STOPD/${order}-${slug}-$(date +%Y%m%dT%H%M%S).txt"
  printf '%s\n' "$reason" >"$f"
  echo "STOP [$order/$slug] — reason written to ${f#"$REPO_ROOT"/}" >&2
  echo "$reason" >&2
  exit 1
}

order_files() { # <order> -> manifest paths on stdout
  awk -v target="$1" '
    /^[a-z0-9-]+:/ { ord = substr($1, 1, length($1)-1); next }
    /^[[:space:]]+-[[:space:]]/ { if (ord == target) print $2 }
  ' "$MANIFEST"
}

all_orders() {
  awk '/^[a-z0-9-]+:/ { print substr($1, 1, length($1)-1) }' "$MANIFEST"
}

# Orders not marked "committed" are live: their files are contested territory.
live_orders() {
  awk '/^[a-z0-9-]+:/ { if ($2 != "committed") print substr($1, 1, length($1)-1) }' "$MANIFEST"
}

order_known() { all_orders | grep -qx "$1"; }

# -uall: list untracked files INDIVIDUALLY (default collapses an untracked dir to
# "dir/", which can never match a manifest's exact file paths — evidence files in
# fresh test-record/ subdirs were silently unstageable).
dirty_files() { git -C "$REPO_ROOT" status --porcelain -uall | awk '{print $2}'; }

# Which OTHER LIVE order (active dispatch, or sharing this dirty file) claims <file>?
other_claimant() { # <file> <this-order>
  local f="$1" me="$2" o
  for o in $(live_orders); do
    [ "$o" = "$me" ] && continue
    if order_files "$o" | grep -qx "$f"; then
      # claimant if that order has an active dispatch, or the shared file is
      # dirty (the hunks cannot be attributed mechanically)
      if [ -e "$ACTIVE/$o.env" ] || dirty_files | grep -qx "$f"; then
        echo "$o"; return 0
      fi
    fi
  done
  return 1
}

worker_cmd() { # <preset> <brief-file> -> command string on stdout
  local brief="$2"
  case "$1" in
    flash)
      printf 'gemini -m gemini-3-flash-preview --yolo -p "$(cat %q)"' "$brief" ;;
    pi-minimax)
      printf 'pi --provider openrouter --model minimax/minimax-m3 -p --no-session -nt -ne -ns -nc "$(cat %q)"' "$brief" ;;
    pi-free)
      printf 'pi --provider openrouter --model openai/gpt-oss-120b:free -p --no-session -nt -ne -ns -nc "$(cat %q)"' "$brief" ;;
    *) return 1 ;;
  esac
}

# ----------------------------------------------------------------------------- dispatch
cmd_dispatch() {
  local order="${1:?usage: dispatch <order> <worker> <brief> <artifact> [--timeout-min N] [--stall-min N] [--watch]}"
  local worker="${2:?worker preset required}" brief="${3:?brief file required}" artifact="${4:?artifact path required}"
  shift 4
  local timeout_min=45 stall_min=8 do_watch=0
  while [ $# -gt 0 ]; do case "$1" in
    --timeout-min) timeout_min="$2"; shift 2 ;;
    --stall-min)   stall_min="$2";   shift 2 ;;
    --watch)       do_watch=1;       shift ;;
    *) echo "unknown option: $1" >&2; exit 64 ;;
  esac; done

  order_known "$order" || fail_reason "$order" "unknown-order" \
    "Dispatch refused: '$order' is not in orders.yaml. Every dispatch must be backed by a manifest entry listing the files the order touches — add the entry (source of truth: the order's surgical brief) and retry."
  [ -s "$brief" ] || fail_reason "$order" "missing-brief" \
    "Dispatch refused: brief file '$brief' is missing or empty. A worker without a brief improvises, and improvising workers are exactly what this script exists to prevent."
  [ -e "$ACTIVE/$order.env" ] && fail_reason "$order" "already-active" \
    "Dispatch refused: $order already has an active dispatch (.orchestrate/active/$order.env). Finish or stop it first — two workers on one order would race each other in the same files."

  # collision rules (the by-hand prompts.py dance, now data-driven)
  local f o
  while IFS= read -r f; do
    if o=$(other_claimant "$f" "$order"); then
      fail_reason "$order" "file-collision" \
        "Dispatch refused: '$f' is in $order's manifest but is currently claimed by order '$o' (active dispatch or uncommitted changes). Dispatching both would entangle their diffs — the exact prompts.py collision class this gate encodes. Commit or finish '$o' first."
    fi
  done < <(order_files "$order")

  local cmd; cmd=$(worker_cmd "$worker" "$brief") || fail_reason "$order" "unknown-worker" \
    "Dispatch refused: unknown worker preset '$worker'. Valid presets: flash, pi-minimax, pi-free."

  local log="$LOGS/$order.log" sess="wkr-$order"
  : >"$log"
  tmux new-session -d -s "$sess" \
    "cd $(printf %q "$REPO_ROOT") && $cmd 2>&1 | tee $(printf %q "$log"); echo \"EXIT=\$?\" >> $(printf %q "$log")"

  # Values QUOTED — this file is `source`d, and the repo path contains a space
  # ("perpleximanus build"); unquoted values word-split and break every consumer.
  cat >"$ACTIVE/$order.env" <<EOF
ORDER="$order"
WORKER="$worker"
BRIEF="$brief"
ARTIFACT="$artifact"
SESSION="$sess"
LOG="$log"
START_EPOCH=$(date +%s)
TIMEOUT_MIN=$timeout_min
STALL_MIN=$stall_min
EOF
  echo "dispatched $order -> tmux:$sess worker:$worker log:${log#"$REPO_ROOT"/} (timeout ${timeout_min}m, stall ${stall_min}m)"
  # NOT bare `[ ... ] && cmd_watch` — as the function's last command that makes a
  # plain (non-watch) dispatch return exit 1.
  if [ "$do_watch" = 1 ]; then cmd_watch "$order"; fi
}

# ----------------------------------------------------------------------------- watch
cmd_watch() {
  local order="${1:?usage: watch <order>}"
  local env="$ACTIVE/$order.env"
  [ -f "$env" ] || { echo "no active dispatch for $order" >&2; exit 64; }
  # shellcheck disable=SC1090
  source "$env"
  local last_size=-1 last_growth now size
  last_growth=$(date +%s)
  while :; do
    if ! tmux has-session -t "$SESSION" 2>/dev/null; then
      cmd_verify "$order"; return $?
    fi
    now=$(date +%s)
    size=$(stat -c %s "$LOG" 2>/dev/null || echo 0)
    if [ "$size" != "$last_size" ]; then last_size="$size"; last_growth="$now"; fi
    if [ $((now - START_EPOCH)) -ge $((TIMEOUT_MIN * 60)) ]; then
      tmux kill-session -t "$SESSION" 2>/dev/null || true
      rm -f "$env"
      fail_reason "$order" "wall-timeout" \
        "Worker for $order killed after exceeding the ${TIMEOUT_MIN}-minute wall-clock budget. Log preserved at ${LOG#"$REPO_ROOT"/} ($size bytes). No salvage was attempted and nothing was committed — inspect the log, fix the brief or the budget, and re-dispatch deliberately."
    fi
    if [ $((now - last_growth)) -ge $((STALL_MIN * 60)) ]; then
      tmux kill-session -t "$SESSION" 2>/dev/null || true
      rm -f "$env"
      fail_reason "$order" "stalled" \
        "Worker for $order killed: its log produced no output for ${STALL_MIN} minutes (stall detector). Stalls in this setup have meant interactive prompts the worker cannot answer or a hung model call. Log preserved at ${LOG#"$REPO_ROOT"/} — inspect before re-dispatching."
    fi
    sleep "$POLL_SECS"
  done
}

# ----------------------------------------------------------------------------- verify
cmd_verify() {
  local order="${1:?usage: verify <order>}"
  local env="$ACTIVE/$order.env"
  [ -f "$env" ] || { echo "no active dispatch for $order" >&2; exit 64; }
  # shellcheck disable=SC1090
  source "$env"

  local wexit
  wexit=$(grep -oE '^EXIT=[0-9]+' "$LOG" | tail -1 | cut -d= -f2 || true)
  if [ -n "$wexit" ] && [ "$wexit" != 0 ]; then
    rm -f "$env"
    fail_reason "$order" "worker-failed" \
      "Worker for $order exited with code $wexit. Per policy this is a full stop, not a retry: a failed worker's partial writes are untrusted until a human-or-orchestrator reads the log (${LOG#"$REPO_ROOT"/}) and decides whether the brief, the model, or the environment is at fault."
  fi

  # the artifact must exist, be non-empty, and be FRESH (claims are not evidence)
  if [ -s "$ARTIFACT" ] && [ "$(stat -c %Y "$ARTIFACT")" -ge "$START_EPOCH" ]; then
    mv "$env" "$DONE/$order.env"
    echo "VERIFIED $order: artifact ${ARTIFACT#"$REPO_ROOT"/} present, non-empty, fresh."
    return 0
  fi

  # the pi failure mode (CONTEXT.md §4): claimed success, wrote nothing → salvage
  if [ -s "$LOG" ]; then
    mkdir -p "$(dirname "$ARTIFACT")"
    cp "$LOG" "$ARTIFACT"
    local flag="$FLAGS/$order.salvaged-$(date +%Y%m%dT%H%M%S)"
    printf 'artifact %s was missing/empty/stale after worker exit; contents salvaged verbatim from %s\n' \
      "$ARTIFACT" "$LOG" >"$flag"
    mv "$env" "$DONE/$order.env"
    echo "SALVAGED $order: worker did not write its artifact; tmux log copied to ${ARTIFACT#"$REPO_ROOT"/} and flagged (${flag#"$REPO_ROOT"/}). Treat as a worker CLAIM, not a deliverable." >&2
    return 2
  fi

  rm -f "$env"
  fail_reason "$order" "no-artifact-no-log" \
    "Worker for $order exited leaving neither its expected artifact ($ARTIFACT) nor any log output to salvage. There is nothing to verify and nothing to salvage — the dispatch produced no evidence of work. Stop here; re-dispatch only after understanding why the worker emitted nothing."
}

# ----------------------------------------------------------------------------- commit
cmd_commit() {
  local order="${1:?usage: commit <order> -m <message>}"; shift
  local msg=""
  while [ $# -gt 0 ]; do case "$1" in
    -m) msg="$2"; shift 2 ;;
    *) echo "unknown option: $1" >&2; exit 64 ;;
  esac; done
  [ -n "$msg" ] || { echo "commit message required (-m)" >&2; exit 64; }
  order_known "$order" || fail_reason "$order" "unknown-order" \
    "Commit refused: '$order' is not in orders.yaml, so there is no manifest to bound the staging set. Unbounded staging is how unrelated orders' changes leak into one commit."

  local f o
  while IFS= read -r f; do
    dirty_files | grep -qx "$f" || continue
    for o in $(live_orders); do
      [ "$o" = "$order" ] && continue
      if order_files "$o" | grep -qx "$f"; then
        fail_reason "$order" "shared-file-uncommitted" \
          "Commit refused: '$f' has uncommitted changes and sits in both $order's and live order $o's manifests — the hunks cannot be attributed to one order mechanically. Commit '$o' first (or mark it committed in orders.yaml if it already is) before committing $order."
      fi
    done
  done < <(order_files "$order")

  # never inherit a pre-staged index — the staging set must equal the manifest set
  if [ -n "$(git -C "$REPO_ROOT" diff --cached --name-only)" ]; then
    fail_reason "$order" "index-not-empty" \
      "Commit refused: the git index already has staged files. This script stages exactly one order's manifest files and nothing else; a pre-populated index means some other staging plan is in progress. Commit or unstage that first."
  fi

  local staged=0
  while IFS= read -r f; do
    if dirty_files | grep -qx "$f"; then
      git -C "$REPO_ROOT" add -- "$f"
      staged=$((staged + 1))
    fi
  done < <(order_files "$order")
  [ "$staged" -gt 0 ] || fail_reason "$order" "nothing-to-commit" \
    "Commit refused: none of $order's manifest files have uncommitted changes. Either the order is already committed or the worker never wrote — check .orchestrate/flags/ and the worker log before assuming this is fine."

  # flip the order to committed in the manifest, inside the same commit
  sed -i "s/^$order:.*/$order: committed/" "$MANIFEST"
  git -C "$REPO_ROOT" add -- "$MANIFEST"

  # paranoia: staged set must be a subset of the manifest (+ the manifest itself)
  local outside
  outside=$(git -C "$REPO_ROOT" diff --cached --name-only \
    | grep -vxF -f <(order_files "$order"; echo "orders.yaml") || true)
  if [ -n "$outside" ]; then
    git -C "$REPO_ROOT" reset -q
    fail_reason "$order" "staged-outside-manifest" \
      "Commit refused and index reset: staging $order's files pulled in paths outside its manifest ($outside). The manifest is wrong or the working tree is entangled; fix orders.yaml or untangle by hand."
  fi

  git -C "$REPO_ROOT" commit -m "$msg"
  echo "committed $order ($staged files + manifest state flip)"
}

# ----------------------------------------------------------------------------- status
cmd_status() {
  local env now size last
  now=$(date +%s)
  shopt -s nullglob
  for env in "$ACTIVE"/*.env; do
    # shellcheck disable=SC1090
    source "$env"
    size=$(stat -c %s "$LOG" 2>/dev/null || echo 0)
    last=$(stat -c %Y "$LOG" 2>/dev/null || echo "$START_EPOCH")
    printf '%-8s worker=%-10s age=%3dm log=%7dB log-idle=%3dm tmux=%s\n' \
      "$ORDER" "$WORKER" $(((now - START_EPOCH) / 60)) "$size" $(((now - last) / 60)) \
      "$(tmux has-session -t "$SESSION" 2>/dev/null && echo alive || echo gone)"
  done
  [ -z "$(ls -A "$ACTIVE" 2>/dev/null)" ] && echo "no active dispatches"
}

# ----------------------------------------------------------------------------- main
case "${1:-}" in
  dispatch) shift; cmd_dispatch "$@" ;;
  watch)    shift; cmd_watch "$@" ;;
  verify)   shift; cmd_verify "$@" ;;
  commit)   shift; cmd_commit "$@" ;;
  status)   shift; cmd_status ;;
  *) sed -n '2,30p' "$0"; exit 64 ;;
esac
