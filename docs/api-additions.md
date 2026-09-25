# API additions: saved search and workflow schedules

These additions extend the [sealed API endpoint reference](contracts/api-endpoints.md).
The sealed reference retains its accepted bytes; this addendum documents the
new owner-scoped endpoints and persistence behavior.

### Saved Standard Search answers

A Standard Search conversation uses `surface: "research"`. Its browser URL is
`/research/{conversation_id}` after the first completed answer. Opening this URL
or its History row reads saved data; it does not submit another query.

`GET /conversations/{conversation_id}/research` (agent server) is owner-scoped and
returns `{conversation_id, turns, latest_request}` with `Cache-Control: private,
no-store`. Each completed turn contains its original structured `answer` and
`scope`, including `request_id`, `parent_request_id`, sources, denied domains,
and model/thinking settings. Citation, block and artifact filenames are retained.
`latest_request` also records an interrupted request without claiming it finished.

The current UI includes a fresh `request_id` in every explicit `/ws/research`
submission, follow-up, retry or re-scope. The server atomically saves the complete
answer and FINISHED status before sending the final frame. Repeating an already
completed identity and identical scope replays that result without model or
retrieval work. A changed scope under an existing identity, an interrupted
identity, or concurrent work in that conversation is rejected with an error
frame. Explicit retries use fresh identities and reference the last completed
parent, never an unfinished request. Imported conversations remain read-only;
workspace/agent conversations cannot be run through this persistence path.
Older websocket clients without a request identity retain their stateless
streaming contract; previously unsaved answers cannot be recovered retroactively.

### Workflow schedule management

`GET /api/workflows/schedules` returns the authenticated owner's saved schedules.
The workflow review UI filters that list by `spec.instance_id` and displays the
cron, timezone, active/paused state and next run. Saving an identical owner,
instance digest, cron and timezone returns the existing schedule; it does not
create another timer or implicitly resume a paused one.

`PATCH /api/workflows/schedules/{schedule_id}` accepts only `{ "enabled": boolean }`.
It requires the session owner and normal CSRF protection. Unknown, unsafe,
foreign-owner or unclaimed legacy IDs return 404; invalid bodies return 422.
The response is the updated schedule row. Pausing retains its definition digest,
ID, creation time and history, clears `next_run`, and prevents future automatic
or fire-now dispatch. An already-started run continues. Resuming calculates the
next future occurrence in the saved timezone, without replaying missed runs.

New workflow run records persist their schedule owner's ID so history ownership
survives a missing schedule file. Legacy records without ownership still require
a surviving owner-scoped schedule; ownership is never inferred from an arbitrary
run conversation ID. There is no destructive schedule-delete control.
