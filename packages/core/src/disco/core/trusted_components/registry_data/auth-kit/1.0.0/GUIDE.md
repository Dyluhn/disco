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

Run this server module from the app's root with Node 22 or later. Supply your
`yourAppHandler` request handler and `port` before starting the server.

```js
import http from "node:http";
import { openDatabase, runMigrations } from "./src/trusted/database-kit/core/db.js";
import { AUTH_MIGRATIONS } from "./src/trusted/auth-kit/core/schema.js";
import { seedDevUser } from "./src/trusted/auth-kit/core/auth.js";
import { createAuthApp } from "./src/trusted/auth-kit/core/middleware.js";
import config from "./src/trusted/auth-kit/config/auth.config.json" with { type: "json" };

const db = openDatabase(); // reads the installed database-kit config
runMigrations(db, AUTH_MIGRATIONS);
seedDevUser(db, config);

const app = createAuthApp({ db, config, handler: yourAppHandler });
const server = http.createServer(app);
server.listen(port);
```

Add every one of YOUR public pages/assets to `publicAllowlist` (exact path,
or a `prefix*` wildcard). Everything not listed requires a valid
`tc_session` cookie. The shipped default already includes `"/__health/*"` so
database-kit's `/__health/db` seam stays reachable when both kits are mounted.

The guard canonicalizes the request path itself (collapsing `//`, `\`, and
`/../`) and rewrites the URL to that form before your handler sees it, so a
crafted target like `//admin` can't read as public here and resolve to a
protected route in your router. A malformed login body (wrong types, oversized)
is answered `401`/`413`, never allowed to throw the process down.

## What you may edit

Only `config/auth.config.json`. The sanctioned customization surface:

- `publicAllowlist` — routes reachable without a session (opt-out protection).
- `sessionTtlHours` — session lifetime.
- `devSeedUser` — a local-dev/test account, or `null` (the default: no account).
- `maxBodyBytes` — request-body cap on `/auth/login` (default 65536).
- `loginRateLimit` — `{ windowMs, max }` per-IP login throttle (default 10/60s).
- `cookieSecure` — omit for auto (Secure over a direct TLS socket, or via
  `x-forwarded-proto` only when `trustProxy` is on), or set `true`/`false` to
  force. Leave it omitted unless you have a reason.
- `trustProxy` — `false` by default. Turn it on ONLY when the app sits behind a
  reverse proxy you control that sets `X-Forwarded-For`/`X-Forwarded-Proto`. Off,
  those client-settable headers are ignored (the rate limiter keys on the real
  socket peer and Secure is decided by the real transport) so a caller cannot
  spoof them to dodge the login throttle.

## What you must not edit

Anything under `core/`. Editing a core file diverges it from the registry
pin — the next verify auto-ejects auth-kit: the verified badge is removed
and it becomes ordinary code you own. That's always allowed, never punished,
but it's one-way (reinstall requires reverting the edit).

## Security note

`devSeedUser` ships **`null`** — no account exists by default. Set it to
`{ "email": ..., "password": ... }` to seed a local dev/test account, then
remove it before shipping. The seam probe **fails closed** if you leave the
well-known example credential (`dev@example.com` / `dev-password-123`) live,
so you cannot reach the verified badge carrying that backdoor.

Session tokens are stored **hashed** (sha256) in the `sessions` table — a read
of that table yields hashes, not usable bearer tokens. The raw token lives only
in the client's `tc_session` cookie.
