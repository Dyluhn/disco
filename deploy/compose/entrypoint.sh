#!/bin/sh
# Container entrypoint for both disco servers (app-server + agent-server).
# Prepares the shared /data volume, ensures an app secret exists (so encrypted
# secrets survive restarts), seeds a first-run config, then execs the server CMD.
set -e

DATA="${PMX_DATA_DIR:-/data}"
mkdir -p "$DATA" "$DATA/skills" "$DATA/projects" "$DATA/cache/tts" "$DATA/cache/fastembed"

# App secret (Fernet KDF for the at-rest secret store). If the operator pinned
# PMX_SECRET_KEY in .env, that wins. Otherwise generate ONCE into the data volume so
# stored API keys survive restarts — but NOT volume loss (document: pin it to survive that).
if [ -z "${PMX_SECRET_KEY:-}" ]; then
  KEYFILE="$DATA/.secret_key"
  if [ ! -f "$KEYFILE" ]; then
    head -c 32 /dev/urandom | base64 | tr -d '\n' > "$KEYFILE"
    chmod 600 "$KEYFILE"
    echo "[entrypoint] generated a new PMX_SECRET_KEY into $KEYFILE (pin it in .env to survive volume loss)"
  fi
  PMX_SECRET_KEY="$(cat "$KEYFILE")"
  export PMX_SECRET_KEY
fi

# First-run model/config seed — self-skips when PMX_CONFIG already exists (the
# Settings UI owns the catalogue after the first write).
python /app/scripts/seed_config.py || echo "[entrypoint] seed_config skipped/failed (non-fatal)"

exec "$@"
