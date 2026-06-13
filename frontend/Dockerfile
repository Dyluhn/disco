# syntax=docker/dockerfile:1

# ---- Stage 1: build the Vite SPA ----
FROM node:22-alpine AS build
WORKDIR /app

# Install deps from lockfile for reproducible builds.
COPY package.json package-lock.json ./
RUN npm ci

# Build the SPA. No VITE_* env is baked in — backend URLs are injected at
# RUNTIME via /env.js (window.__PMX_ENV), so one image works at any host/port.
#
# Gate the bundle on a real type-check of the SHIPPING sources
# (tsconfig.build.json = the app config minus test files), then produce the
# artifact with `vite build` (esbuild). A type error fails the image build.
COPY . .
RUN npm run typecheck:build && npx vite build

# ---- Stage 2: serve with nginx ----
FROM nginx:alpine AS serve

# Static SPA assets.
COPY --from=build /app/dist /usr/share/nginx/html

# SPA-aware server block (client-side routing + /env.js no-store).
COPY nginx.conf /etc/nginx/conf.d/default.conf

# Runtime env injection: nginx:alpine runs /docker-entrypoint.d/*.sh before
# starting nginx. This writes /env.js from container env on every start.
COPY docker-entrypoint.d/40-pmx-env.sh /docker-entrypoint.d/40-pmx-env.sh
RUN chmod +x /docker-entrypoint.d/40-pmx-env.sh

EXPOSE 80
