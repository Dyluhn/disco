#!/bin/sh
# Generate /env.js from container env at startup.
#
# Explicit URLs (DISCO_PUBLIC_API_BASE / DISCO_PUBLIC_AGENT_BASE) win when set.
# When empty (the zero-config default), the emitted JS falls back to localhost.
# The API session cookie is host-only, so keep the API servers on the canonical
# loopback host and never derive 127.0.0.1 or a LAN host into the cookie flow.
# DISCO_* preferred; legacy PMX_* honored as a fallback (the rename compat path).
set -eu

API_PORT="${DISCO_APP_PORT:-${PMX_APP_PORT:-8800}}"
AGENT_PORT="${DISCO_AGENT_PORT:-${PMX_AGENT_PORT:-8000}}"
PUBLIC_API="${DISCO_PUBLIC_API_BASE:-${PMX_PUBLIC_API_BASE:-}}"
PUBLIC_AGENT="${DISCO_PUBLIC_AGENT_BASE:-${PMX_PUBLIC_AGENT_BASE:-}}"

# Note: empty PUBLIC_* yields a falsy JS string literal so the `|| ...` branch
# fires in the browser (zero-config localhost default).
cat > /usr/share/nginx/html/env.js <<EOF
window.__DISCO_ENV = {
  API_BASE:   "${PUBLIC_API}"   || (location.protocol + "//localhost:${API_PORT}"),
  AGENT_BASE: "${PUBLIC_AGENT}" || (location.protocol + "//localhost:${AGENT_PORT}"),
};
EOF

echo "40-disco-env: wrote /usr/share/nginx/html/env.js (API_PORT=${API_PORT} AGENT_PORT=${AGENT_PORT})"
