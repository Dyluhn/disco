> **SUPERSEDED / HISTORICAL (as of 2026-07-07).** Messaging-bridge setup notes whose one-shot bot instructions hit `POST /conversations` etc. directly.
> Historical only. Not a source of current status or operating instructions.
> ⚠ post-auth (S-W1) these direct-POST instructions likely require a session/pairing token — VERIFY before use; see `sec-work-remaining/disco-security-state.md`.

# Disco messaging bridge

Start and observe Disco tasks from a chat channel. `disco_bot.py` is a **thin external
process** — it speaks the agent-server's public REST surface and imports nothing from
the Disco packages, so it needs **zero server-side changes**.

```
your message ──▶ Telegram ──▶ disco_bot ──▶ POST /conversations
                                          ──▶ POST /conversations/{cid}/messages  (kicks the loop)
                                          ──▶ GET  /conversations/{cid}/state     (poll to done)
                                          ──▶ GET  /conversations/{cid}/events     (pull the result)
                              ◀── reply ◀──  ✓ Done + summary + a deep link
```

## Run it

**One-shot (no chat channel — the easiest way to confirm it works):**

```bash
python disco_bot.py --base http://localhost:8000 --once "What is reciprocal rank fusion?"
```

It runs one task and prints the result. Exit code 0 on `FINISHED`, non-zero otherwise.

**Telegram bridge:**

```bash
export DISCO_TELEGRAM_TOKEN="123456:ABC..."     # from @BotFather
python disco_bot.py \
  --base http://localhost:8000 \
  --app-base http://localhost:8088 \
  --telegram-token "$DISCO_TELEGRAM_TOKEN" \
  --allow 12345678 87654321 \
  --surface research
```

Only deps: `httpx` (already in the workspace). The bridge long-polls Telegram; on a
message from an allow-listed chat it runs a task and replies with the result + a deep
link into the UI.

## Security (read this)

Disco v1 has **no authentication**, so this bridge *is* the access control:

- **`--allow` is mandatory in practice.** Only the listed Telegram chat ids may start
  tasks. An un-listed chat is silently ignored.
- **Per-chat isolation.** Each chat runs as its own owner (`tg:<chat_id>`), so one
  person's History never mixes with another's.
- The agent-server it talks to should stay bound to loopback / a trusted network — the
  bridge does not add TLS or authenticate to the agent-server (there's nothing to
  authenticate to in v1). Put it next to the server, not on the open internet.

## Surfaces

`--surface research` (default) is the safest for an unattended bridge: read-only, no
confirmation gates, so a task runs straight to `FINISHED`. `build`/`agent` tasks can
pause for a confirmation the bridge can't answer — it reports that honestly
(`Paused — this task needs your input in the app`) rather than hanging.

## Tests

`test_disco_bot.py` drives the whole create→kick→poll→result loop and the Telegram relay
against an `httpx.MockTransport` (no network): `uv run pytest test_disco_bot.py`. The
real Telegram token round-trip is the one thing not covered there — use `--once` against
a running agent-server to confirm the end-to-end path on your box.
