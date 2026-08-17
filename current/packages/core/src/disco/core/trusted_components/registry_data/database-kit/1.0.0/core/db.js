// database-kit 1.0.0 — core/db.js
//
// Plain ESM, zero npm dependencies, node:sqlite (Node >= 22). This file is
// IMMUTABLE and hash-pinned (spec §1.2/§6) — edit config/db.config.json to
// customize, never this file. Editing it is an eject: the verified badge is
// removed (docs/trusted-components-spec.md §4.1).

import { DatabaseSync } from "node:sqlite";
import { mkdirSync, readFileSync } from "node:fs";
import { dirname } from "node:path";

const DEFAULT_CONFIG_PATH = "src/trusted/database-kit/config/db.config.json";

function ensureMigrationsTable(db) {
  db.exec(
    "CREATE TABLE IF NOT EXISTS _tc_migrations (" +
      "version INTEGER PRIMARY KEY, " +
      "applied_at TEXT NOT NULL" +
      ")"
  );
}

/**
 * Open (creating if absent) the SQLite database named by the JSON config at
 * `configPath` ({"path": "<db file, relative to the app's working dir>"}).
 * The directory holding the db file is created if missing. Both the config
 * path and the configured db path are resolved the same way node:fs always
 * resolves relative paths: against `process.cwd()` — i.e. the app's root,
 * where `node server.js` (or equivalent) is run from.
 *
 * @param {string} [configPath]
 * @returns {DatabaseSync}
 */
export function openDatabase(configPath = DEFAULT_CONFIG_PATH) {
  let raw;
  try {
    raw = readFileSync(configPath, "utf8");
  } catch (err) {
    throw new Error(`database-kit: could not read config at ${configPath}: ${err.message}`);
  }
  let config;
  try {
    config = JSON.parse(raw);
  } catch (err) {
    throw new Error(`database-kit: config at ${configPath} is not valid JSON: ${err.message}`);
  }
  if (typeof config.path !== "string" || config.path.length === 0) {
    throw new Error(`database-kit: config at ${configPath} is missing a string "path"`);
  }
  const dir = dirname(config.path);
  if (dir && dir !== "." && dir !== "/") {
    mkdirSync(dir, { recursive: true });
  }
  return new DatabaseSync(config.path);
}

/**
 * Apply every migration in `migrations` (`{version: int, sql: string}[]`)
 * that has not yet been recorded, in ascending version order, each inside its
 * own transaction. Creates the `_tc_migrations` bookkeeping table on first
 * use. Idempotent: re-running with the same list is a no-op once every
 * version is recorded.
 *
 * @param {DatabaseSync} db
 * @param {{version: number, sql: string}[]} migrations
 */
export function runMigrations(db, migrations) {
  ensureMigrationsTable(db);

  const applied = new Set();
  for (const row of db.prepare("SELECT version FROM _tc_migrations").all()) {
    applied.add(row.version);
  }

  const ordered = [...migrations].sort((a, b) => a.version - b.version);
  const recordStmt = db.prepare(
    "INSERT INTO _tc_migrations (version, applied_at) VALUES (?, ?)"
  );

  for (const migration of ordered) {
    if (applied.has(migration.version)) continue;
    db.exec("BEGIN");
    try {
      db.exec(migration.sql);
      recordStmt.run(migration.version, new Date().toISOString());
      db.exec("COMMIT");
    } catch (err) {
      db.exec("ROLLBACK");
      throw err;
    }
    applied.add(migration.version);
  }
}

/**
 * The highest applied migration version, or 0 if none have run yet (including
 * the case where `runMigrations` has never been called on this db).
 *
 * @param {DatabaseSync} db
 * @returns {number}
 */
export function migrationVersion(db) {
  ensureMigrationsTable(db);
  const row = db.prepare("SELECT MAX(version) AS v FROM _tc_migrations").get();
  const value = row ? row.v : null;
  return typeof value === "number" ? value : 0;
}
