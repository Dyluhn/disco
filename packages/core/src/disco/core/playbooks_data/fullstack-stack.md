# Full-stack reference stack (api + web + compose)

> The default shape for a multi-user app: Node 22 API on the trusted database/auth/RBAC kits, a Vite + React front end behind nginx, one compose file, env var NAMES only. Read this first; every other pack assumes it.

## When to use
Any brief that needs accounts, durable data, realtime, or third-party services. A static page or a single-file prototype does not need this — use `scaffold_starter` there.

## Layout
```
api/          Node 22 ESM service (node:http + Express 5), SQLite via database-kit
  package.json  src/server.js  src/routes/*.js  src/realtime.js  src/migrations.js
  src/trusted/  ← installed by add_trusted_component (database-kit, auth-kit, rbac-kit)
web/          Vite + React + TypeScript SPA, served by nginx in production
  package.json  src/  nginx.conf  Dockerfile
compose.yaml  api + web (+ chroma / mailpit when a playbook needs them)
.env.example  NAMES only, one line per variable, comment says what it is for
data/         SQLite file + uploads (a compose volume; never committed)
```

## Install order
1. `add_trusted_component` → `database-kit`, then `auth-kit`, then `rbac-kit` (each returns its GUIDE; follow it — do not hand-roll sessions or role checks).
2. `api/package.json` (ESM, pinned majors):
```json
{ "name": "api", "type": "module", "private": true,
  "scripts": { "start": "node src/server.js", "dev": "node --watch src/server.js" },
  "dependencies": { "express": "^5.1.0", "socket.io": "^4.8.1" } }
```
Add per-pack dependencies only when that playbook is used (stripe, nodemailer, chromadb, busboy…).
3. `web/`: `npm create vite@latest web -- --template react-ts`, then `npm i socket.io-client@^4.8.1`.

## api/src/server.js — the one composition root
```js
import http from "node:http";
import express from "express";
import { openDatabase, runMigrations } from "./trusted/database-kit/core/db.js";
import { healthHandler } from "./trusted/database-kit/core/health.js";
import { AUTH_MIGRATIONS } from "./trusted/auth-kit/core/schema.js";
import { createAuthApp } from "./trusted/auth-kit/core/middleware.js";
import { RBAC_MIGRATIONS } from "./trusted/rbac-kit/core/schema.js";
import { createRbacApp } from "./trusted/rbac-kit/core/guard.js";
import authConfig from "./trusted/auth-kit/config/auth.config.json" with { type: "json" };
import rbacConfig from "./trusted/rbac-kit/config/rbac.config.json" with { type: "json" };
import { APP_MIGRATIONS } from "./migrations.js";
import { attachRealtime } from "./realtime.js";
import { registerRoutes } from "./routes/index.js";

const db = openDatabase(process.env.DB_CONFIG ?? "./src/trusted/database-kit/config/db.config.json");
runMigrations(db, [...AUTH_MIGRATIONS, ...RBAC_MIGRATIONS, ...APP_MIGRATIONS]);

const app = express();
app.use(express.json({ limit: "1mb" }));
registerRoutes(app, db);                        // your routes, all under /api/...
app.get("/__health/db", (req, res) => healthHandler(db)(req, res));

// Guard chain: auth-kit (who are you → 401) wraps rbac-kit (may you → 403) wraps the app.
const guarded = createAuthApp({ db, config: authConfig,
  handler: createRbacApp({ db, config: rbacConfig, handler: app }) });
const server = http.createServer(guarded);
attachRealtime(server, db);                     // socket.io on the same server (realtime playbook)
server.listen(Number(process.env.PORT ?? 3000), "0.0.0.0");
```
Auth-kit protects every route unless it is in `config/auth.config.json` `publicAllowlist`; list registration, health and the SPA assets there (`"/api/auth/register"`, `"/__health/*"`). Inside a handler, identify the user with `getSession(db, token)` where `token` is the `tc_session` cookie (see auth-and-roles playbook).

## Migrations
`api/src/migrations.js` exports `APP_MIGRATIONS = [{ version: 1, sql: "..." }, …]`, ascending, never edited once shipped — add a new version instead. Keep app versions below 900000 (the kits own 900001+). Use `TEXT` ISO-8601 timestamps and `INTEGER PRIMARY KEY` ids.

## web ↔ api wiring
- Dev: Vite proxy in `web/vite.config.ts` → `server.proxy = { "/api": "http://localhost:3000", "/socket.io": { target: "http://localhost:3000", ws: true } }`.
- Prod: `web/nginx.conf` serves `/usr/share/nginx/html` with SPA fallback and proxies `/api/` and `/socket.io/` (with `Upgrade`/`Connection` headers) to `http://api:3000`.
- Same-origin cookies mean no CORS work; do not enable `cors` unless the brief demands a split origin.

## compose.yaml
```yaml
services:
  api:
    build: ./api
    environment:
      PORT: "3000"
      PUBLIC_URL: "${PUBLIC_URL:-http://localhost:8080}"
      # per-pack keys: NAMES only, values come from the host's .env
    volumes: [ "app-data:/app/data" ]
    healthcheck: { test: ["CMD", "node", "-e", "fetch('http://127.0.0.1:3000/__health/db').then(r=>process.exit(r.ok?0:1))"], interval: 10s, timeout: 5s, retries: 6 }
  web:
    build: ./web
    ports: [ "${BIND:-127.0.0.1}:${WEB_PORT:-8080}:80" ]
    depends_on: { api: { condition: service_healthy } }
volumes: { app-data: {} }
```
`api/Dockerfile`: `FROM node:22-bookworm-slim`, `npm ci --omit=dev`, `CMD ["node","src/server.js"]`. `web/Dockerfile`: build stage `node:22-alpine` → `nginx:alpine`.

## Env vars
Every variable the app reads appears in `.env.example` with a comment and is declared with `release_declare`. Read them once at startup; fail at startup with a message naming the variable when a required one is missing. Never write a value into the repository.

## Delivery checklist
Registration → login → the feature → logout works in a real browser (`verify_web_app`); `compose up` from a clean checkout reaches healthy; `README.md` states the env vars and where the data lives.
