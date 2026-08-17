# syntax=docker/dockerfile:1

# ---- Stage 1: build the Vite SPA ----
FROM node:22-alpine AS build
WORKDIR /app/frontend

# Install deps from lockfile for reproducible builds.
COPY current/frontend/package.json current/frontend/package-lock.json ./
RUN npm ci

# Build the SPA. No VITE_* env is baked in — backend URLs are injected at
# RUNTIME via /env.js (window.__PMX_ENV), so one image works at any host/port.
#
# Gate the bundle on a real type-check of the SHIPPING sources
# (tsconfig.build.json = the app config minus test files), then produce the
# artifact with `vite build` (esbuild). A type error fails the image build.
COPY current/frontend/ ./
COPY current/packages/agent-server/src/disco/agent_server/selection_agent.js \
    /app/packages/agent-server/src/disco/agent_server/selection_agent.js
COPY current/packages/agent-server/src/disco/agent_server/element_mention_picker.js \
    /app/packages/agent-server/src/disco/agent_server/element_mention_picker.js
RUN npm run typecheck:build && npx vite build

# ---- Stage 2: serve with nginx ----
FROM nginx:alpine AS serve

# Static SPA assets.
COPY --from=build /app/frontend/dist /usr/share/nginx/html

# SPA-aware server block (client-side routing + /env.js no-store). The official
# nginx entrypoint renders this after 10-disco-resolver.envsh discovers the
# running container network's DNS server. That keeps request-time service-name
# re-resolution portable across Docker and Podman.
COPY current/frontend/nginx.conf /etc/nginx/templates/default.conf.template
COPY current/frontend/docker-entrypoint.d/10-disco-resolver.envsh /docker-entrypoint.d/10-disco-resolver.envsh
ENV NGINX_ENVSUBST_FILTER=^DISCO_NGINX_RESOLVER$

# Runtime env injection: nginx:alpine runs /docker-entrypoint.d/*.sh before
# starting nginx. This writes /env.js from container env on every start.
COPY current/frontend/docker-entrypoint.d/40-disco-env.sh /docker-entrypoint.d/40-disco-env.sh
RUN chmod +x \
    /docker-entrypoint.d/10-disco-resolver.envsh \
    /docker-entrypoint.d/40-disco-env.sh

EXPOSE 80
