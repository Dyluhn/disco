# database-kit 1.0.0

## What you got

A verified SQLite persistence core for this app, mounted at
`src/trusted/database-kit/`:

- `core/db.js` — `openDatabase(configPath?)`, `runMigrations(db, migrations)`,
  `migrationVersion(db)`. Plain ESM, `node:sqlite`, zero npm dependencies.
- `core/health.js` — `healthHandler(db)`, a node:http `(req, res) => void`
  handler. The kit OWNS the `/__health/db` seam — mount it exactly there.
- `config/db.config.json` — `{"path": "./data/app.db"}`.

## How to mount it

```js
import { openDatabase, runMigrations } from "./src/trusted/database-kit/core/db.js";
import { healthHandler } from "./src/trusted/database-kit/core/health.js";

const db = openDatabase(); // reads config/db.config.json, mkdirs the dir
runMigrations(db, [
  { version: 1, sql: "CREATE TABLE notes(id INTEGER PRIMARY KEY, body TEXT NOT NULL)" },
  // add your app's migrations here, one entry per schema change, ascending
  // version numbers — already-applied versions are skipped automatically.
]);

// in your node:http request router:
if (req.url === "/__health/db") return healthHandler(db)(req, res);
```

Call `runMigrations` once at startup, before serving traffic. Every call is
safe to repeat — already-recorded versions are no-ops.

## What you may edit

- `config/db.config.json` — change `path` to move the database file
  (relative paths resolve against the app's working directory; the directory
  is created for you if missing). This is the ONLY sanctioned edit surface.

## What you must not edit

- `core/db.js`, `core/health.js` — these are integrity-pinned. Editing either
  file is detected at verify time and auto-**ejects** the component: the
  verified badge is removed, upgrades stop applying, and the files become
  ordinary code you own from then on. If you need different behavior, write
  your own module elsewhere in the app instead of modifying these — or eject
  explicitly with `eject_trusted_component` if you genuinely want to own it.
