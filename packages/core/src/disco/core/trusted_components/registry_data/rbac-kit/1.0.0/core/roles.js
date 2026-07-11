// rbac-kit 1.0.0 — role storage + assignment. Pure db-in/db-out; the HTTP seam
// lives in guard.js. Plain ESM, Node >= 22, zero npm deps.
//
// rbac-kit REQUIRES auth-kit: seedRoleUsers reuses auth-kit's createUser so a
// seeded role-holder is a real auth user (same password/session machinery),
// which is exactly the 2-edge requires graph this kit exercises.

import { createUser } from "../../auth-kit/core/auth.js";

// Idempotent: create the role if absent, return its id. INSERT OR IGNORE keeps
// the UNIQUE(name) invariant without a race window.
export function ensureRole(db, name) {
  if (typeof name !== "string" || name.length === 0) {
    throw new TypeError("rbac-kit: role name must be a non-empty string");
  }
  db.prepare("INSERT OR IGNORE INTO roles (name) VALUES (?)").run(name);
  return db.prepare("SELECT id FROM roles WHERE name = ?").get(name).id;
}

// Grant `roleName` to `userId` (idempotent — the composite PK dedupes).
export function assignRole(db, userId, roleName) {
  const roleId = ensureRole(db, roleName);
  db.prepare("INSERT OR IGNORE INTO user_roles (user_id, role_id) VALUES (?, ?)").run(userId, roleId);
}

export function revokeRole(db, userId, roleName) {
  const row = db.prepare("SELECT id FROM roles WHERE name = ?").get(roleName);
  if (!row) return;
  db.prepare("DELETE FROM user_roles WHERE user_id = ? AND role_id = ?").run(userId, row.id);
}

// Every role name held by `userId` (empty list for a non-numeric id or no roles).
export function userRoles(db, userId) {
  if (typeof userId !== "number") return [];
  return db
    .prepare(
      `SELECT roles.name AS name
         FROM user_roles
         JOIN roles ON roles.id = user_roles.role_id
        WHERE user_roles.user_id = ?`,
    )
    .all(userId)
    .map((r) => r.name);
}

// The one authorization predicate the guard calls. Type-guards both inputs so a
// forged/non-string role or a bad id is a clean `false`, never a thrown query.
export function hasRole(db, userId, roleName) {
  if (typeof userId !== "number" || typeof roleName !== "string") return false;
  const row = db
    .prepare(
      `SELECT 1 AS ok
         FROM user_roles
         JOIN roles ON roles.id = user_roles.role_id
        WHERE user_roles.user_id = ? AND roles.name = ?
        LIMIT 1`,
    )
    .get(userId, roleName);
  return Boolean(row);
}

// Dev/probe convenience (idempotent), mirroring auth-kit's seedDevUser: create
// each seed as a real auth user if absent, then grant its roles. Runs ONLY when
// config.devSeedRoleUsers is a non-empty array — it ships null, so no accounts
// and no roles exist by default.
export function seedRoleUsers(db, config) {
  const seeds = config && config.devSeedRoleUsers;
  if (!Array.isArray(seeds)) return;
  for (const seed of seeds) {
    if (!seed || typeof seed.email !== "string" || typeof seed.password !== "string") continue;
    let user = db.prepare("SELECT id FROM users WHERE email = ?").get(seed.email);
    if (!user) user = createUser(db, seed.email, seed.password);
    const roles = Array.isArray(seed.roles) ? seed.roles : [];
    for (const roleName of roles) {
      if (typeof roleName === "string" && roleName.length > 0) assignRole(db, user.id, roleName);
    }
  }
}
