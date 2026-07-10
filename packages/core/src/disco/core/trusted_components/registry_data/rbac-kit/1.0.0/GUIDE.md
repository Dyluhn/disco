# rbac-kit 1.0.0

## What you got

Role-based access control, integrity-pinned. **Requires auth-kit and
database-kit** — it reuses auth-kit's session and users, and stores roles in the
database-kit database.

- `core/schema.js` — `RBAC_MIGRATIONS` (`roles` + `user_roles` tables, versions
  starting at 910001 so they never collide with auth-kit's 900001 or your app).
- `core/roles.js` — `ensureRole`, `assignRole`, `revokeRole`, `userRoles`,
  `hasRole`, and `seedRoleUsers` (dev/probe convenience).
- `core/guard.js` — `createRbacApp({ db, config, handler })`. For every route in
  `config.roleRoutes`, it requires the current user to hold the mapped role
  (looked up **server-side** from `user_roles`) before your handler runs — else
  `403`. The required role is never read from the request, so a forged client
  claim (`x-role: admin`, a cookie, a body field) is ignored.

## How to mount it

Mount rbac-kit **inside** auth-kit's chain — auth-kit proves *who you are*
(session, else 401); rbac-kit proves *what you may do* (role, else 403):

```js
import { openDatabase, runMigrations } from "./src/trusted/database-kit/core/db.js";
import { AUTH_MIGRATIONS } from "./src/trusted/auth-kit/core/schema.js";
import { seedDevUser } from "./src/trusted/auth-kit/core/auth.js";
import { createAuthApp } from "./src/trusted/auth-kit/core/middleware.js";
import { RBAC_MIGRATIONS } from "./src/trusted/rbac-kit/core/schema.js";
import { seedRoleUsers } from "./src/trusted/rbac-kit/core/roles.js";
import { createRbacApp } from "./src/trusted/rbac-kit/core/guard.js";

const db = openDatabase();
runMigrations(db, AUTH_MIGRATIONS);
runMigrations(db, RBAC_MIGRATIONS);       // roles/user_roles reference users
seedDevUser(db, authConfig);
seedRoleUsers(db, rbacConfig);

const guarded = createRbacApp({ db, config: rbacConfig, handler: yourAppHandler });
const app = createAuthApp({ db, config: authConfig, handler: guarded });
http.createServer(app).listen(port);
```

Grant roles programmatically with `assignRole(db, userId, "admin")` (idempotent;
the role row is created on first use).

## What you may edit

Only `config/rbac.config.json`:

- `roleRoutes` — a map of route → required role. Keys are exact paths or
  `prefix*` wildcards (e.g. `"/admin*": "admin"`). A request to a matched route
  must carry a session whose user holds that role.
- `devSeedRoleUsers` — a list of `{ email, password, roles: [...] }` seeded as
  real auth users with those roles, or `null` (the default: no accounts). For
  local dev/testing only — remove before shipping.

## What you must not edit

Anything under `core/`. Editing a core file diverges it from the registry pin —
the next verify auto-ejects rbac-kit (the verified badge is removed and it
becomes ordinary code you own). Always allowed, never punished, but one-way.

## Security note

The role decision is made **only** from the `user_roles` table via the
authenticated session's user id. Nothing a client sends (headers, cookies, body)
can grant a role. `devSeedRoleUsers` ships `null`, so no privileged account
exists by default.
