# Alerts and broadcast banners

> Admin-created alerts that appear instantly for everyone, stay until each user dismisses them, and are still shown to users who connect later.

## Schema
```sql
CREATE TABLE alerts(id INTEGER PRIMARY KEY, title TEXT NOT NULL, message TEXT NOT NULL,
  severity TEXT NOT NULL DEFAULT 'info' CHECK(severity IN ('info','warning','critical')),
  category TEXT, created_by INTEGER NOT NULL, created_at TEXT NOT NULL, resolved_at TEXT);
CREATE TABLE alert_dismissals(alert_id INTEGER NOT NULL, user_id INTEGER NOT NULL, dismissed_at TEXT NOT NULL,
  PRIMARY KEY(alert_id, user_id));
```
"Active for me" = `resolved_at IS NULL AND NOT EXISTS (dismissal by me)`. Dismissal is per user; an admin may also resolve an alert for everyone.

## Routes
- `POST /api/admin/alerts` (rbac: admin) `{ title, message, severity?, category? }` → insert, `broadcast(null, "alert:created", alert)`.
- `GET /api/alerts/active` → the current user's undismissed, unresolved alerts (called on load and on reconnect).
- `POST /api/alerts/:id/dismiss` → insert dismissal, respond 204; only affects the caller.
- `POST /api/admin/alerts/:id/resolve` → sets `resolved_at`, `broadcast(null, "alert:resolved", { id })`.

## Client
`AlertBanner` mounted at the app root (above routing): state from `GET /api/alerts/active`; `socket.on("alert:created")` prepends; `socket.on("alert:resolved")` removes; reload the list on `reconnect`. Each banner: severity colour, title, message, category chip, a Dismiss button (optimistic remove, then POST). Stack multiple banners; critical first.

## Prove it
Admin creates an alert in context A; context B shows the banner within a second; B dismisses — it stays visible in A; a third user who logs in afterwards still sees it; resolving as admin clears it everywhere.
