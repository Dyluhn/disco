#!/bin/sh
# Generate /env.js from container env at startup.
#
# Explicit URLs (DISCO_PUBLIC_API_BASE / DISCO_PUBLIC_AGENT_BASE) win when set.
# When empty (the zero-config default), the emitted JS derives the API host from
# the BROWSER'S OWN hostname (location.hostname) + the configured ports — so
# localhost, LAN, and tailnet access all work with zero config (the promise the
# compose.yaml comment makes). This is also the cookie-safe choice: the session
# cookie is host-only and cookies ignore ports, so page-host == API-host keeps
# the cookie flowing for WHATEVER host the user browses from. (The old hardcoded
# `localhost` fallback was the opposite: it MIXED hosts whenever the page was
# not loaded from localhost, sending every API call to the visitor's own
# machine — found live 2026-07-09 on the first remote fresh-install test.)
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
  API_BASE:   "${PUBLIC_API}"   || (location.protocol + "//" + location.hostname + ":${API_PORT}"),
  AGENT_BASE: "${PUBLIC_AGENT}" || (location.protocol + "//" + location.hostname + ":${AGENT_PORT}"),
};
EOF

echo "40-disco-env: wrote /usr/share/nginx/html/env.js (API_PORT=${API_PORT} AGENT_PORT=${AGENT_PORT})"
