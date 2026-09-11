# Multiplayer game loop: rooms, lobby, turns, hidden information, replay

> A server-authoritative realtime game (Skribbl-style drawing and guessing): public rooms with capacity, a live lobby, strict turn order, secrets only the right player sees, scoring, and recorded rounds replayable in sync.

## State lives on the server
One `GameRoom` object per room in the api process (Map by id); the database keeps accounts, scores and finished-round replays. Clients send intents (`join`, `stroke`, `guess`, `start`); the server validates against the room state and broadcasts the resulting state. Use `scaffold_starter game_loop_vanilla` only for the drawing surface if you want its canvas helpers; the loop here is server-side.

## Rooms and lobby
```js
const rooms = new Map();   // id → { id, name, capacity, players: Map<userId,{name,score,socketId}>, phase:'lobby'|'playing'|'ended', round, drawerId, word, wordHint, roundEndsAt, strokes:[], guessedIds:Set }
export function lobbySnapshot() { return [...rooms.values()].map(r => ({ id: r.id, name: r.name, players: r.players.size, capacity: r.capacity, phase: r.phase })); }
```
Lobby route `GET /api/rooms` = `lobbySnapshot()`; socket room `lobby` receives `lobby` events on every join/leave/create/phase change; a full room is `players === capacity`, "in progress" is `phase === 'playing'`. `join` is refused with `{ error: "room full" }` server-side even if a stale lobby showed a seat.

## Turn system and hidden word
- On `start` (needs ≥ 2 players): `phase='playing'`, `round=1`, `drawerId` = first player; each turn picks `word` from the bundled list, sends `turn` `{ drawerId, hint: "_ _ _ _", endsAt }` to the room and `word` `{ word }` **only** to the drawer's socket (`io.to(drawer.socketId).emit`). Never include the word in room-wide payloads (graders check that guessers cannot see it, including via devtools).
- Only the drawer's `stroke` events are accepted (`if (socket.data.user.id !== room.drawerId) return`).
- Turn ends on timer (server `setTimeout`) or when all guessers have guessed; then the server reveals the word, stores the round (below), advances `drawerId` round-robin; after N rounds `phase='ended'` with the final scoreboard; `game_end` event, then `lobby` update.

## Words
Bundle `api/src/words.json`: ~300 concrete, elementary-level nouns/verbs (cat, apple, jump, rainbow, bicycle, penguin…) — write the list yourself, review it for age-appropriateness, no brands, no violence. Pick without repeats within a game.

## Guessing and scoring
`guess` `{ text }` → normalise (trim, lowercase, collapse spaces); if it equals the word: mark the guesser, award `100 + remainingSeconds × 2` to the guesser and `+25` to the drawer, broadcast `guess:correct` `{ userId }` (not the text), and echo other guesses to the room chat as normal messages (`guess`) with the sender name. Close guesses (Levenshtein ≤ 1) get a private "close!" hint. Persist final scores per game to `game_results(user_id, game_id, score)` for the leaderboard and "persist player score data".

## Strokes: order and timing
Client sends `stroke` `{ t: performance-relative ms, points: [[x,y],…], color, width, erase }` batched every 50 ms (normalise x/y to 0..1 so canvas sizes differ safely). Server stamps `seq` and `ts = Date.now() - room.turnStartedAt`, appends to `room.strokes`, and broadcasts `stroke` to everyone else. `clear` is a stroke with `points: []` and `erase: 'all'`.

## Replay
At turn end: `INSERT INTO round_replays(game_id, round, drawer_id, word, strokes_json, duration_ms, created_at)` (strokes in `seq` order with `ts`). `GET /api/games/:id/replays` lists rounds; `GET /api/replays/:id` returns the strokes. Player: a `ReplayPlayer` component redraws strokes whose `ts <= elapsed` on each animation frame with play / pause / restart and a scrubber. Synchronised replay: a player emits `replay:control` `{ replayId, action, atMs, serverTime }`; the server rebroadcasts with its own timestamp; clients compute `elapsed = atMs + (Date.now() - serverTime)` so everyone watching is within network jitter of each other. Keep replays for at least the session (rows persist anyway).

## Client structure
`Lobby` (rooms list from `GET /api/rooms` + `lobby` events, create/join), `Game` (canvas, tools palette for the drawer, hint/word bar, timer, players + scores, chat/guess box, round transitions), `Replay`. Redraw the whole canvas from `strokes` on join (server sends `state` with the current turn's strokes) so late joiners see the picture.

## Prove it
Three browser contexts: lobby shows the room with `2/8` then `3/8`; a fourth cannot join a full room of capacity 3; only the drawer sees the word (inspect other clients' socket frames — no word); strokes appear on all clients as they are drawn; a correct guess updates scores everywhere and the turn advances; after the round, all three can watch the replay and pause/restart together; scores survive an api restart.
