# auth-kit 1.0.0

## What you got

Session auth, integrity-pinned:

- `core/schema.js` — `AUTH_MIGRATIONS` (users + sessions tables, versions
  starting at 900001 so they never collide with app migrations).
- `core/auth.js` — password hashing (scrypt), session create/get/destroy,
  `seedDevUser`.
- `core/middleware.js` — `createAuthApp({ db, config, handler })`. It OWNS
  `POST /auth/login`, `POST /auth/logout`, `GET /auth/session`, and guards
  every other route app-wide. Protection is **opt-out**: routes are public
  only if listed in `config/auth.config.json`'s `publicAllowlist` — there is
  no way to "forget" to protect a route.

## How to mount it

```js
import { openDatabase, runMigrations } from "../database-kit/core/db.js";
import { AUTH_MIGRATIONS } from "./core/schema.js";
import { seedDevUser } from "./core/auth.js";
import { createAuthApp } from "./core/middleware.js";
import config from "./config/auth.config.json" with { type: "json" };

const db = openDatabase("./data/app.db.config.json"); // your database-kit config
runMigrations(db, AUTH_MIGRATIONS);
seedDevUser(db, config);

const app = createAuthApp({ db, config, handler: yourAppHandler });
http.createServer(app).listen(port);
```

Add every one of YOUR public pages/assets to `publicAllowlist` (exact path,
or a `prefix*` wildcard). Everything not listed requires a valid
`tc_session` cookie.

## What you may edit

Only `config/auth.config.json`: `publicAllowlist`, `sessionTtlHours`,
`devSeedUser`. This is the sanctioned customization surface.

## What you must not edit

Anything under `core/`. Editing a core file diverges it from the registry
pin — the next verify auto-ejects auth-kit: the verified badge is removed
and it becomes ordinary code you own. That's always allowed, never punished,
but it's one-way (reinstall requires reverting the edit).

## Security note

`devSeedUser` creates a known-password account (`dev@example.com` /
`dev-password-123`) for local development and the probe. **Set it to `null`
in `config/auth.config.json` before shipping anything real.**
