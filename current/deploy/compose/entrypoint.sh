#!/bin/sh
# Container entrypoint for both disco servers (app-server + agent-server).
# Prepares the shared /data volume, ensures an app secret exists (so encrypted
# secrets survive restarts), seeds a first-run config, then execs the server CMD.
set -e

# DISCO_* preferred; legacy PMX_* honored (the rename compat path).
DATA="${DISCO_DATA_DIR:-${PMX_DATA_DIR:-/data}}"
mkdir -p "$DATA" "$DATA/skills" "$DATA/projects" "$DATA/cache/tts" "$DATA/cache/fastembed"

# App secret (Fernet KDF for the at-rest secret store). If the operator pinned
# DISCO_SECRET_KEY (or legacy PMX_SECRET_KEY) in .env, that wins. Otherwise generate
# ONCE into the data volume so stored API keys survive restarts — but NOT volume loss
# (document: pin it to survive that). The keyfile path is unchanged, so a deployment
# that generated a key under the old name keeps decrypting its existing ciphertext.
SECRET="${DISCO_SECRET_KEY:-${PMX_SECRET_KEY:-}}"
if [ -z "$SECRET" ]; then
  KEYFILE="$DATA/.secret_key"
  if [ ! -f "$KEYFILE" ]; then
    head -c 32 /dev/urandom | base64 | tr -d '\n' > "$KEYFILE"
    chmod 600 "$KEYFILE"
    echo "[entrypoint] generated a new DISCO_SECRET_KEY into $KEYFILE (pin it in .env to survive volume loss)"
  fi
  SECRET="$(cat "$KEYFILE")"
fi
# Export under BOTH names so the app's DISCO_-first reader and any legacy PMX_ reader resolve.
export DISCO_SECRET_KEY="$SECRET"
export PMX_SECRET_KEY="$SECRET"

# First-run model/config seed — self-skips when the config already exists (the
# Settings UI owns the catalogue after the first write).
python /app/development/scripts/seed_config.py || echo "[entrypoint] seed_config skipped/failed (non-fatal)"

exec "$@"
