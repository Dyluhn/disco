# Realtime updates and presence

> Live-updating boards, feeds and status with socket.io on the same Node server: server-authoritative state, rooms, presence, reconnection and conflict-free concurrent edits.

## Rule
The database is the source of truth; sockets only announce what the server has already saved. A client never applies its own change until the server broadcasts it (or use optimistic UI that reconciles on the broadcast).

## Server
`npm i socket.io@^4.8.1` in `api/`.
```js
// api/src/realtime.js
import { Server } from "socket.io";
import { getSession } from "./trusted/auth-kit/core/auth.js";
export let io;
const online = new Map();                       // userId → Set(socketId)

export function attachRealtime(server, db) {
  io = new Server(server, { path: "/socket.io", serveClient: false });
  io.use((socket, next) => {                    // authenticate the handshake with the same cookie session
    const cookie = socket.handshake.headers.cookie ?? "";
    const token = cookie.split(";").map(s => s.trim()).find(s => s.startsWith("tc_session="))?.slice(11);
    const session = getSession(db, token);
    if (!session) return next(new Error("unauthorized"));
    socket.data.user = { id: session.userId, email: session.email };
    next();
  });
  io.on("connection", (socket) => {
    const uid = socket.data.user.id;
    if (!online.has(uid)) online.set(uid, new Set());
    online.get(uid).add(socket.id);
    io.emit("presence", { online: [...online.keys()] });
    socket.on("join", (room) => socket.join(String(room)));   // e.g. "unit:ICU", "listing:42"
    socket.on("leave", (room) => socket.leave(String(room)));
    socket.on("disconnect", () => {
      online.get(uid)?.delete(socket.id);
      if (online.get(uid)?.size === 0) online.delete(uid);
      io.emit("presence", { online: [...online.keys()] });
    });
  });
}
export function broadcast(room, event, payload) { (room ? io.to(room) : io).emit(event, payload); }
```
In every write handler, after the SQL commit: `broadcast("board", "bed:updated", row)`. Emit the full updated row (not a diff) so late joiners and reconnects converge.

## Concurrent edits without conflicts
- Every editable table has `version INTEGER NOT NULL DEFAULT 0` and `updated_at`.
- `PATCH` carries the version the client last saw: `UPDATE beds SET status=?, notes=?, version=version+1, updated_at=? WHERE id=? AND version=?`; if `changes === 0` answer 409 with the current row — the client refreshes and retries. This is last-writer-wins with detection, enough for boards and dashboards.
- Field-level edits (notes textarea) → send the whole field on blur, not per keystroke; debounce 300 ms.

## Client
`npm i socket.io-client@^4.8.1` in `web/`.
```ts
// web/src/realtime.ts
import { io } from "socket.io-client";
export const socket = io({ path: "/socket.io", withCredentials: true, autoConnect: false });
export function connectRealtime() { if (!socket.connected) socket.connect(); }
```
Connect after login (`AuthProvider`), disconnect on logout. A hook per feature:
```ts
export function useLiveList<T extends { id: number }>(url: string, events: { updated: string; removed?: string }) {
  const [rows, setRows] = useState<T[]>([]);
  useEffect(() => {
    let alive = true;
    const load = () => fetch(url).then(r => r.json()).then(d => { if (alive) setRows(d); });
    load();
    const onUpdated = (row: T) => setRows(prev => prev.some(r => r.id === row.id) ? prev.map(r => r.id === row.id ? row : r) : [...prev, row]);
    const onRemoved = ({ id }: { id: number }) => setRows(prev => prev.filter(r => r.id !== id));
    socket.on(events.updated, onUpdated); if (events.removed) socket.on(events.removed, onRemoved);
    socket.io.on("reconnect", load);                    // resync after a drop
    return () => { alive = false; socket.off(events.updated, onUpdated); if (events.removed) socket.off(events.removed, onRemoved); socket.io.off("reconnect", load); };
  }, [url]);
  return rows;
}
```
Presence: `socket.on("presence", ({ online }) => setOnline(online))` and render a dot next to users whose id is in the set; the user list comes from `GET /api/users` (or the board's assignments).

## One-way feeds (prices, news)
When the server is the only writer (a poller), the same `broadcast("feed", "price", tick)` works; keep a bounded in-memory `lastTicks` map and send it to a socket on `connection` so a new tab is populated immediately.

## Scaling note
One api container, in-memory presence and rooms: correct for this deployment. If the brief demands multiple api replicas, add `@socket.io/redis-adapter` and move presence to Redis — do not pretend to support it otherwise.

## Prove it
Open two browser contexts (two users). A change made in one appears in the other within a second with no reload; both show each other online; closing one tab clears its presence; edit the same record in both — the second save gets 409 and refreshes. Use `verify_web_app` or Playwright with two `browser.newContext()` sessions.
