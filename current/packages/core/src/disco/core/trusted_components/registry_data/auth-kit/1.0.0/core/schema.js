// auth-kit 1.0.0 — schema migrations (database-kit contract: migrations =
// [{version: int, sql: string}], applied ascending, tracked in
// _tc_migrations by runMigrations()).
//
// Versions start at 900001 so an auth-kit migration can never collide with an
// app's own migration numbering.

export const AUTH_MIGRATIONS = [
  {
    version: 900001,
    sql: `
      CREATE TABLE IF NOT EXISTS users (
        id INTEGER PRIMARY KEY,
        email TEXT NOT NULL UNIQUE,
        password_hash TEXT NOT NULL,
        created_at TEXT NOT NULL
      );

      CREATE TABLE IF NOT EXISTS sessions (
        token TEXT PRIMARY KEY,
        user_id INTEGER NOT NULL REFERENCES users(id),
        expires_at TEXT NOT NULL
      );
    `,
  },
];
