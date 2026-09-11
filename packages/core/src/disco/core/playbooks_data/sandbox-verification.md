# Proving integrations inside the sandbox

> How to verify each capability where the build runs: seeded roles, cookie flows with curl, two-user browser checks, Mailpit, signed webhooks, and what to do when the sandbox has no route to a third-party API.

## What the sandbox gives you
Node 22, Python 3, Playwright + Chromium, curl, sqlite3. Network egress is `filtered` by default (package registries only). The api you are building listens on a workspace port; the browser tools (`browser`, `verify_web_app`) reach it. Two conditions decide what you can prove live: whether a key is present (env var) and whether egress reaches the provider.

## Seed for verification, not for production
`api/src/seed.js` runs when `SEED_DEMO=1`: one user per role (`admin@example.test`, `nurse@…`, …, password from `SEED_PASSWORD`), a handful of records (units/beds, listings, medications), and prints the credentials once. Never seed when the flag is unset; document the flag in `.env.example`.

## Cookie flows with curl
```bash
curl -s -c c.txt -H 'content-type: application/json' -d '{"email":"nurse@example.test","password":"…","role":"nurse"}' localhost:3000/api/auth/register
curl -s -b c.txt localhost:3000/auth/session                      # {"email":"nurse@example.test"}
curl -s -b c.txt -o /dev/null -w '%{http_code}\n' localhost:3000/api/admin/units   # 403 for a nurse
curl -s -b c.txt -X POST localhost:3000/auth/logout && curl -s -b c.txt -o /dev/null -w '%{http_code}\n' localhost:3000/auth/session   # 401
```

## Two users, live
Playwright script (`node`, `playwright` is installed): `const a = await browser.newContext(); const b = await browser.newContext();` log in as different users, perform the action in `a`, `await b.getByText(...).waitFor({ timeout: 3000 })` — no reload in `b`. Use this for boards, chat, alerts, order status, lobby, strokes (compare canvas pixel diff or count received `stroke` frames via `page.on('websocket')`). Save screenshots of both contexts to `.pmx/screenshots/` as evidence.

## Provider by provider
| Capability | Needs egress? | Live check | Without egress / key |
|---|---|---|---|
| LLM assistant | yes (or a host-side model via `host.docker.internal` when egress is `public`) | streamed reply, persisted | 503 with the var name; UI shows "not configured" — do not fake replies |
| Web search | yes | cited URLs in the answer | same as above |
| RAG (Chroma) | no — Chroma is a compose service; embeddings need the endpoint | upload, ask, cite | verify chunking + `@` resolution with a deterministic fake embedder (`EMBEDDINGS_FAKE=1`: hashed bag-of-words vectors) and say so |
| STT | yes | `curl -F audio=@sample.wav /api/stt` | browser fallback path; state it |
| Email | no — Mailpit | `GET http://mailpit:8025/api/v1/messages` | outbox rows exist and drain later |
| Stripe | yes for cards | `4242…` test card + `stripe listen` | signed test event to the webhook (payments playbook); state that live confirmation was not exercised |
| Market data / news | yes | prices change, news arrives | 503 + "not configured"; the chart still renders from `history` fixtures under `MARKET_FIXTURES=1` — say so |
| Uploads, realtime, chat, alerts, booking, game | no | full live check | — |

Ask the operator for keys the brief requires (the brief says to); put the NAMES in `.env.example` and `release_declare`. If the operator wants a live third-party check inside the build, they set `DISCO_BUILD_EGRESS=public` on the agent-server; you cannot change it from inside.

## Delivery notes (always)
A `VERIFICATION.md` in the project root: what was checked live (with the evidence file names), what was checked with a fake/fixture and why, what needs a key the operator did not provide. Graders read this; an honest gap costs one rubric line, a hidden one costs trust in the whole delivery.
