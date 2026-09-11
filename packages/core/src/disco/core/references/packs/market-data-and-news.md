# Live market data, news feed and range-selectable charts

> Real-time quotes and price history from a market-data API relayed through your server, a self-refreshing finance-only news feed, and an interactive chart whose selected date range feeds the assistant.

## Provider and env
Finnhub covers quotes, candles, websocket trades and company/market news on one free key:
```
FINNHUB_API_KEY     # https://finnhub.io — required for real data
MARKET_SYMBOLS      # optional default watchlist, e.g. "AAPL,MSFT,NVDA"
```
Alternative single-key providers: Alpha Vantage (quotes/daily history, no websocket) or Polygon. Keep one adapter module `market.js` exporting `quote(symbol)`, `history(symbol, from, to, resolution)`, `search(q)`, `news()`, and `subscribeTrades(symbols, onTrade)` so the provider can change without touching the UI.

## Server relay (the browser never holds the key)
```js
// api/src/market.js
const BASE = "https://finnhub.io/api/v1", KEY = process.env.FINNHUB_API_KEY;
const get = async (p, q) => { const r = await fetch(`${BASE}${p}?${new URLSearchParams({ ...q, token: KEY })}`); if (!r.ok) throw new Error(`finnhub ${r.status}`); return r.json(); };
export const quote = (s) => get("/quote", { symbol: s });                      // {c,d,dp,h,l,o,pc,t}
export const history = (s, from, to, res = "D") => get("/stock/candle", { symbol: s, resolution: res, from, to }); // {t[],o[],h[],l[],c[],v[]}
export const search = (q) => get("/search", { q });                            // {result:[{symbol,description}]}
export const news = () => get("/news", { category: "general" });               // [{headline,summary,url,source,datetime,related}]
export function subscribeTrades(symbols, onTrade) {                            // wss relay → socket.io fan-out
  const ws = new WebSocket(`wss://ws.finnhub.io?token=${KEY}`);
  ws.onopen = () => symbols.forEach(s => ws.send(JSON.stringify({ type: "subscribe", symbol: s })));
  ws.onmessage = (m) => { const d = JSON.parse(m.data); if (d.type === "trade") for (const t of d.data) onTrade({ symbol: t.s, price: t.p, ts: t.t }); };
  ws.onclose = () => setTimeout(() => subscribeTrades(symbols, onTrade), 5000);   // reconnect
  return () => ws.close();
}
```
Node 22 has a global `WebSocket` client. Outside US market hours the trade stream is quiet — also poll `quote()` every 15 s for each watched symbol and broadcast it, so "continuous updates" holds at any time. Cache `history` responses for 60 s per (symbol, range) and rate-limit search (Finnhub free tier: 60 calls/min).

Routes: `GET /api/market/quote/:symbol`, `GET /api/market/history/:symbol?from&to&res`, `GET /api/market/search?q=`, `GET /api/market/news`. Socket events: `price` `{symbol, price, ts}` to room `market`, `news` `{items}` every 60 s (see below).

## Watchlist and multiple symbols
`watchlist(user_id, symbol, position)` table; the client joins `market` and renders a ticker card per symbol from the last known quote, updated by `price` events. Adding a symbol validates it through `search` (exact symbol match) before insert.

## News feed, finance only
Poll `news()` every 60 s server-side; keep the last 200 by id in memory + a `news_items` table for durability; filter to finance with a keyword allowlist over headline+summary (`stock|shares|earnings|market|Fed|inflation|bond|IPO|Nasdaq|S&P|Dow|dividend|revenue|merger|acquisition|guidance|SEC|ETF|crypto|yield|rates`) or by `related` symbols ∈ watchlist; broadcast only new ids. Client prepends new items with a subtle highlight; each item links to the real article URL and shows source + time.

## Chart with range selection
`npm i recharts@^2.15.0` in `web/`. `<LineChart data={candles}>` with `<Brush dataKey="date" onChange={({startIndex,endIndex}) => setRange([candles[startIndex].date, candles[endIndex].date])}/>`; highlight the selection with `<ReferenceArea x1={range[0]} x2={range[1]} />`. Resolution: 1 y → "D", 1 m → "60", 1 d → "5". On a settled selection (debounce 400 ms) call `assistant.setContext({ chart_range: { symbol, from, to, summary } })` where `summary` is computed client-side from the candles in range (open, close, high, low, % change) — the assistant then has numbers to reason with, and the ai-assistant pack makes the reply reference the dates explicitly.

## Prove it
Two symbols show prices that change without reload (watch the socket frames); the search box resolves a real symbol and rejects a fake one; history renders for 1d/1m/1y; selecting a brush range highlights it and the assistant reply mentions the from/to dates and the range's move; the news list gains items over a few minutes and never shows a sports headline. Without a key the market routes answer 503 with the env var name, and the UI shows "Market data not configured".
