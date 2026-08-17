// database-kit 1.0.0 — core/health.js
//
// Owns the `/__health/db` seam (spec §3 seam-ownership): the kit mounts its
// own health route rather than relying on the model to remember to check it
// at N call sites. IMMUTABLE and hash-pinned — do not edit (see GUIDE.md).

import { migrationVersion } from "./db.js";

/**
 * A node:http request handler: `(req, res) => void`. Responds 200 with JSON
 * `{"ok": true, "migration_version": <int>}` when the database is reachable,
 * or 500 with `{"ok": false, "error": "<message>"}` when it is not — never
 * throws out to the caller.
 *
 * @param {import("node:sqlite").DatabaseSync} db
 * @returns {(req: import("node:http").IncomingMessage, res: import("node:http").ServerResponse) => void}
 */
export function healthHandler(db) {
  return (req, res) => {
    let body;
    let status;
    try {
      const version = migrationVersion(db);
      status = 200;
      body = JSON.stringify({ ok: true, migration_version: version });
    } catch (err) {
      status = 500;
      body = JSON.stringify({ ok: false, error: String(err && err.message ? err.message : err) });
    }
    res.writeHead(status, { "Content-Type": "application/json" });
    res.end(body);
  };
}
