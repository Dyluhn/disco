# AI assistant chat (OpenAI-compatible, streaming, tools, context injection)

> An in-app assistant on any OpenAI-compatible endpoint: streamed replies, per-user persistent conversations, optional web search as a tool, and structured context (a selected chart range, a document) injected into the turn.

## Env vars (names only)
```
LLM_BASE_URL      # e.g. https://api.openai.com/v1 or http://host.docker.internal:11434/v1 (Ollama) — no trailing slash
LLM_API_KEY       # may be empty for a local endpoint
LLM_MODEL         # e.g. gpt-4o-mini, qwen3:8b
TAVILY_API_KEY    # only if the brief asks for web search
```
Fail at startup with the variable name when `LLM_BASE_URL` or `LLM_MODEL` is missing. Never expose these to the browser — the browser talks to your api only.

## Schema
```sql
CREATE TABLE assistant_conversations(id INTEGER PRIMARY KEY, user_id INTEGER NOT NULL, title TEXT, created_at TEXT NOT NULL);
CREATE TABLE assistant_messages(id INTEGER PRIMARY KEY, conversation_id INTEGER NOT NULL,
  role TEXT NOT NULL CHECK(role IN ('system','user','assistant','tool')), content TEXT NOT NULL,
  context_json TEXT, created_at TEXT NOT NULL);
```
`context_json` stores what was injected with a user turn (e.g. `{"chart_range":{"symbol":"AAPL","from":"2025-01-02","to":"2025-03-01"}}`) so the history shows it and re-sends are faithful.

## Server: one streaming endpoint
```js
// api/src/llm.js
const base = process.env.LLM_BASE_URL, key = process.env.LLM_API_KEY ?? "", model = process.env.LLM_MODEL;
export async function chatStream(messages, { tools, onToken, signal }) {
  const res = await fetch(`${base}/chat/completions`, { method: "POST", signal,
    headers: { "content-type": "application/json", ...(key ? { authorization: `Bearer ${key}` } : {}) },
    body: JSON.stringify({ model, messages, stream: true, ...(tools?.length ? { tools } : {}) }) });
  if (!res.ok) throw new Error(`llm ${res.status}: ${(await res.text()).slice(0, 300)}`);
  let text = "", toolCalls = [];
  for await (const line of lines(res.body)) {                    // SSE: "data: {...}" lines, "data: [DONE]" ends
    if (!line.startsWith("data: ") || line === "data: [DONE]") continue;
    const delta = JSON.parse(line.slice(6)).choices?.[0]?.delta ?? {};
    if (delta.content) { text += delta.content; onToken?.(delta.content); }
    for (const tc of delta.tool_calls ?? []) {                   // accumulate streamed tool-call fragments by index
      toolCalls[tc.index] ??= { id: tc.id, type: "function", function: { name: "", arguments: "" } };
      if (tc.id) toolCalls[tc.index].id = tc.id;
      toolCalls[tc.index].function.name += tc.function?.name ?? "";
      toolCalls[tc.index].function.arguments += tc.function?.arguments ?? "";
    }
  }
  return { text, toolCalls: toolCalls.filter(Boolean) };
}
async function* lines(body) { const dec = new TextDecoder(); let buf = "";
  for await (const chunk of body) { buf += dec.decode(chunk, { stream: true }); let i;
    while ((i = buf.indexOf("\n")) >= 0) { yield buf.slice(0, i).trimEnd(); buf = buf.slice(i + 1); } } }
```
Route `POST /api/assistant/conversations/:id/messages` `{ content, context? }`:
1. Save the user message (+ `context_json`).
2. Build `messages`: a system prompt that names the app and states how to use context ("When a chart range is given, analyse that period explicitly and say the dates."), then the last ~30 stored turns, then the new user turn with the context rendered as text at the top of the content (`[Selected range: AAPL 2025-01-02 → 2025-03-01]\n\n<question>`).
3. Respond as `text/event-stream`; forward each token as `data: {"token":"…"}`; on finish save the assistant message and send `data: {"done":true,"messageId":…}`.
4. Tool loop (web search): pass `tools=[searchTool]`; if the model returns `toolCalls`, run them (below), append the assistant tool-call message and one `role:"tool"` message per call with the JSON result, then call `chatStream` again (max 3 rounds). Stream only the final answer's tokens.

## Web search tool (Tavily)
```js
export const searchTool = { type: "function", function: { name: "web_search", description: "Search the web for current information.",
  parameters: { type: "object", properties: { query: { type: "string" } }, required: ["query"] } } };
export async function webSearch(query) {
  const r = await fetch("https://api.tavily.com/search", { method: "POST", headers: { "content-type": "application/json" },
    body: JSON.stringify({ api_key: process.env.TAVILY_API_KEY, query, max_results: 5, include_answer: false }) });
  if (!r.ok) throw new Error(`tavily ${r.status}`);
  return (await r.json()).results.map(x => ({ title: x.title, url: x.url, content: x.content.slice(0, 1200) }));
}
```
Tell the model in the system prompt to cite the URLs it used. Brave (`BRAVE_SEARCH_API_KEY`, `GET https://api.search.brave.com/res/v1/web/search?q=`) is a drop-in alternative; keep one adapter interface `search(query) → [{title,url,content}]`.

## Client
`AssistantPanel` is always mounted in the main layout (the brief usually says "on-screen at all times"): message list (persisted history loaded from `GET …/messages`), a textarea, a send button, and a `context` slot. Streaming via `fetch` + `ReadableStream` reader (not `EventSource`, which cannot POST): append tokens to the pending assistant bubble. Context injection from elsewhere in the app (a chart brush selection) calls `assistant.setContext({ chart_range })` from a small shared store and shows a chip ("Using AAPL Jan 2 – Mar 1"); the next send includes it.

## Prove it
A message gets a real streamed reply (`curl -N` shows tokens); the reply is persisted and reappears after reload; with a chart range selected the answer names the dates; with web search on, a question about today's news returns cited URLs. With no key configured the endpoint answers 503 `{ error: "LLM_BASE_URL not configured" }` — visible in the UI, never a silent empty bubble.
