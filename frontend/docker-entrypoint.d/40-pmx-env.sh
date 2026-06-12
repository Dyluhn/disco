#!/bin/sh
# Generate /env.js from container env at startup.
#
# Explicit URLs (PMX_PUBLIC_API_BASE / PMX_PUBLIC_AGENT_BASE) win when set.
# When empty (the zero-config default), the emitted JS falls back IN THE BROWSER
# to the page's own protocol+hostname plus the published server ports, so
# localhost AND LAN access work without rebaking the image.
set -eu

API_PORT="${PMX_APP_PORT:-8800}"
AGENT_PORT="${PMX_AGENT_PORT:-8000}"

# Note: ${PMX_PUBLIC_*} expand to "" when unset/empty, yielding a falsy JS
# string literal so the `|| location...` branch fires in the browser.
cat > /usr/share/nginx/html/env.js <<EOF
window.__PMX_ENV = {
  API_BASE:   "${PMX_PUBLIC_API_BASE:-}"   || (location.protocol + "//" + location.hostname + ":${API_PORT}"),
  AGENT_BASE: "${PMX_PUBLIC_AGENT_BASE:-}" || (location.protocol + "//" + location.hostname + ":${AGENT_PORT}"),
};
EOF

echo "40-pmx-env: wrote /usr/share/nginx/html/env.js (API_PORT=${API_PORT} AGENT_PORT=${AGENT_PORT})"
