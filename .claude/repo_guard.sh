#!/usr/bin/env bash
set -euo pipefail
ROOT="$(git rev-parse --show-toplevel)"
BASE="$(basename "$ROOT")"
if [ "$BASE" != "disclaude" ]; then
  echo "REFUSING: not inside disclaude experimental repo: $ROOT" >&2
  exit 42
fi
ORIGIN="$(git remote get-url origin 2>/dev/null || true)"
# Refuse if origin points anywhere that looks like the source/backup mirror.
case "$ORIGIN" in
  *git-backups* | *Disco-Pi* | *projects/disco/* | *projects/disco.git*)
    echo "REFUSING: origin points at a source/backup mirror: $ORIGIN" >&2
    exit 43
    ;;
esac
