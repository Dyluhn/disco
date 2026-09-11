# Accounts, sessions and roles

> Registration, login/logout, HttpOnly cookie sessions, per-user roles chosen at signup, admin user management and deactivation — on top of the auth-kit and rbac-kit trusted components.

## What the kits already give you
- auth-kit owns `POST /auth/login`, `POST /auth/logout`, `GET /auth/session`, scrypt password hashing, the `sessions` table and the `tc_session` HttpOnly cookie. Every route is protected unless listed in `publicAllowlist`.
- rbac-kit owns `roles` / `user_roles` and enforces `config/rbac.config.json` `roleRoutes` (path pattern → required role) server-side, 403 otherwise.
You add: registration, role assignment at signup, the current-user helper, and admin user management.

## Registration (public route)
`config/auth.config.json` → `"publicAllowlist": ["/", "/index.html", "/assets/*", "/__health/*", "/api/auth/register"]`.
```js
// api/src/routes/auth.js
import { createUser, createSession } from "../trusted/auth-kit/core/auth.js";
import { ensureRole, assignRole } from "../trusted/rbac-kit/core/roles.js";
export const ALLOWED_SIGNUP_ROLES = ["patient", "pharmacist"];   // from the brief; never "admin" via self-signup

export function registerAuthRoutes(app, db) {
  app.post("/api/auth/register", (req, res) => {
    const { email, password, name, role } = req.body ?? {};
    if (typeof email !== "string" || !/^[^@\s]+@[^@\s]+$/.test(email)) return res.status(400).json({ error: "valid email required" });
    if (typeof password !== "string" || password.length < 8) return res.status(400).json({ error: "password: 8+ characters" });
    if (!ALLOWED_SIGNUP_ROLES.includes(role)) return res.status(400).json({ error: "role must be one of " + ALLOWED_SIGNUP_ROLES.join(", ") });
    if (db.prepare("SELECT 1 FROM users WHERE email = ?").get(email)) return res.status(409).json({ error: "email already registered" });
    const user = createUser(db, email, password);                     // {id, email}
    db.prepare("INSERT INTO profiles(user_id, name, active) VALUES (?, ?, 1)").run(user.id, name ?? email);
    ensureRole(db, role); assignRole(db, user.id, role);
    const token = createSession(db, user.id, 24);
    res.setHeader("Set-Cookie", `tc_session=${token}; HttpOnly; Path=/; SameSite=Lax; Max-Age=86400`);
    res.status(201).json({ id: user.id, email, name, role });
  });
}
```
Profile columns (name, active, avatar…) live in your own `profiles` table keyed by `user_id`; do not alter the kit's `users` table.

## The current user, in any handler
```js
import { getSession } from "../trusted/auth-kit/core/auth.js";
import { userRoles } from "../trusted/rbac-kit/core/roles.js";
export function currentUser(db, req) {
  const token = (req.headers.cookie ?? "").split(";").map(s => s.trim()).find(s => s.startsWith("tc_session="))?.slice(11);
  const session = getSession(db, token);              // null → auth-kit already sent 401 for guarded routes
  if (!session) return null;
  const profile = db.prepare("SELECT name, active FROM profiles WHERE user_id = ?").get(session.userId);
  return { id: session.userId, email: session.email, name: profile?.name, active: profile?.active !== 0, roles: userRoles(db, session.userId) };
}
```
Deactivated users: check `active` in `currentUser` and answer 403 `{ error: "account deactivated" }`; also `DELETE FROM sessions WHERE user_id = ?` when an admin deactivates.

## Role gates
- Whole path families: `rbac.config.json` → `"roleRoutes": { "/api/admin*": "admin", "/api/pharmacy*": "pharmacist" }`.
- Per-record rules (a nurse edits only their unit) stay in the handler: load the record, compare to `currentUser`, 403 on mismatch. Never trust a role or user id sent by the client.
- The first admin: seed from env at startup — `ADMIN_EMAIL` / `ADMIN_PASSWORD` (names in `.env.example`); create only when no admin exists.

## Admin user management
`GET /api/admin/users` (id, email, name, roles, active), `POST /api/admin/users` (same validation as register, any role), `PATCH /api/admin/users/:id` (name, roles via assign/revoke, `active`), never hard-delete a user with history — deactivate.

## Front end
- `web/src/auth.tsx`: an `AuthProvider` that calls `GET /auth/session` on load, exposes `{ user, login, register, logout }`; `fetch` with `credentials: "same-origin"`.
- Register form shows the role choice the brief allows; login form; a `Logout` button that `POST /auth/logout` then clears state; route guard component that redirects to `/login` when `user` is null and hides role-gated navigation (the server still enforces).

## Prove it
1. Register two users with different roles; `GET /auth/session` returns the email after login; after logout it returns 401.
2. A role-gated route answers 403 for the wrong role and 200 for the right one — test with `curl -b cookies.txt`.
3. Deactivate a user as admin; their next request is 403 and their session is gone.
4. Restart the api container: users and sessions survive (SQLite on the volume).

## Security notes
Passwords never logged; registration rate-limited like login (reuse the pattern from auth-kit's `loginRateLimit`); `SameSite=Lax` + `HttpOnly` cookie; behind TLS add `Secure` (auth-kit does this when `trustProxy` and `x-forwarded-proto` say https).
