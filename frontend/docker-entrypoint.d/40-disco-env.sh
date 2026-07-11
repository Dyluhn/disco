#!/bin/sh
# Generate /env.js from container env at startup.
#
# Single front door (2026-07-09): by DEFAULT the API bases are RELATIVE paths on
# the page's own origin — nginx (this container) proxies /svc/app → app-server
# and /svc/agent → agent-server. The browser never needs to discover backend
# hosts/ports, CORS never applies, and ONE cookie/pairing covers everything, for
# WHATEVER host the user browses from (localhost, LAN IP, tailnet, domain).
#
# Explicit absolute URLs (DISCO_PUBLIC_API_BASE / DISCO_PUBLIC_AGENT_BASE) still
# win when set — the split-origin escape hatch for deployments that terminate
# the backends elsewhere. DISCO_* preferred; legacy PMX_* honored as a fallback.
set -eu

PUBLIC_API="${DISCO_PUBLIC_API_BASE:-${PMX_PUBLIC_API_BASE:-}}"
PUBLIC_AGENT="${DISCO_PUBLIC_AGENT_BASE:-${PMX_PUBLIC_AGENT_BASE:-}}"

# Note: empty PUBLIC_* yields a falsy JS string literal so the `|| ...` branch
# fires in the browser (the zero-config same-origin default).
cat > /usr/share/nginx/html/env.js <<EOF
window.__DISCO_ENV = {
  API_BASE:   "${PUBLIC_API}"   || "/svc/app",
  AGENT_BASE: "${PUBLIC_AGENT}" || "/svc/agent",
};
EOF

echo "40-disco-env: wrote /usr/share/nginx/html/env.js (API_BASE=${PUBLIC_API:-/svc/app} AGENT_BASE=${PUBLIC_AGENT:-/svc/agent})"
