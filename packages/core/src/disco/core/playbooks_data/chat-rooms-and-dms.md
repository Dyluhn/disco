# Chat: public rooms and private threads

> A public forum room and pharmacist-to-pharmacist (or any role-gated) direct messages with attribution, timestamps, persistent history and live delivery. Builds on the realtime playbook.

## Schema (app migration)
```sql
CREATE TABLE conversations(id INTEGER PRIMARY KEY, kind TEXT NOT NULL CHECK(kind IN ('room','dm')),
  name TEXT, created_at TEXT NOT NULL);
CREATE TABLE conversation_members(conversation_id INTEGER NOT NULL, user_id INTEGER NOT NULL,
  PRIMARY KEY(conversation_id, user_id));
CREATE TABLE messages(id INTEGER PRIMARY KEY, conversation_id INTEGER NOT NULL, sender_id INTEGER NOT NULL,
  body TEXT NOT NULL CHECK(length(body) BETWEEN 1 AND 4000), created_at TEXT NOT NULL);
CREATE INDEX messages_conv_created ON messages(conversation_id, created_at);
```
Seed one `room` named `general` at startup (`INSERT OR IGNORE`). A DM is a `dm` conversation with exactly two members; look it up by the pair before creating (`SELECT c.id FROM conversations c JOIN conversation_members a ON … JOIN conversation_members b ON … WHERE c.kind='dm' AND a.user_id=? AND b.user_id=?`).

## Routes
- `GET /api/chat/rooms` — public rooms (any authenticated user; role-gate with rbac `roleRoutes` if the brief limits who may enter, e.g. `"/api/chat/rooms*": "staff"` is wrong when three roles qualify — check `currentUser(db, req).roles` in the handler instead).
- `GET /api/chat/conversations/:id/messages?before=<id>&limit=50` — newest-first page; the client prepends older pages on scroll-up. Membership check for `dm`; 403 otherwise.
- `POST /api/chat/conversations/:id/messages` `{ body }` — insert, then `broadcast("conv:"+id, "message", messageWithSender)`. The payload carries `sender: { id, name, role }` and `created_at` — attribution is server-assigned, never taken from the client.
- `GET /api/chat/users?q=` — searchable directory for starting a DM; restricted to the role the brief names (e.g. only pharmacists see pharmacists).
- `POST /api/chat/dm` `{ userId }` → finds or creates the pair conversation; both members are auto-joined to the socket room on connect (query `conversation_members` in the `connection` handler and `socket.join("conv:"+id)` for each).

## Client
- `ChatRoom` component: `useLiveList`-style hook keyed on the conversation id, `socket.on("message")` appends when `conversation_id` matches; scroll to bottom on new message unless the user scrolled up (keep a `pinnedToBottom` flag).
- Message row: name, role badge, `toLocaleTimeString`, body (render as text, never HTML).
- DM list: conversations of kind `dm` with the other member's name and the last message preview (`GET /api/chat/dms`).
- Unread badge: optional; store `last_read_message_id` per member if the brief asks.

## Prove it
Two users in two browser contexts post in `general`; each sees the other's message with the right name/role/time without reload; history survives an api restart; a user of the wrong role gets 403 on the DM directory; a DM between A and B is invisible to C (403 on the messages route).
