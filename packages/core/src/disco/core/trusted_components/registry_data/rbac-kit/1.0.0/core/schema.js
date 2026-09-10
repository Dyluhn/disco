// rbac-kit 1.0.0 — schema migrations (database-kit contract: migrations =
// [{version: int, sql: string}], applied ascending, tracked in _tc_migrations
// by runMigrations()).
//
// Versions start at 910001 so rbac-kit migrations never collide with auth-kit's
// (900001+) or an app's own numbering. `user_roles.user_id` references the
// users table auth-kit owns (900001) — rbac-kit REQUIRES auth-kit, so those
// migrations always run first.

export const RBAC_MIGRATIONS = [
  {
    version: 910001,
    sql: `
      CREATE TABLE IF NOT EXISTS roles (
        id INTEGER PRIMARY KEY,
        name TEXT NOT NULL UNIQUE
      );

      CREATE TABLE IF NOT EXISTS user_roles (
        user_id INTEGER NOT NULL REFERENCES users(id),
        role_id INTEGER NOT NULL REFERENCES roles(id),
        PRIMARY KEY (user_id, role_id)
      );
    `,
  },
];
