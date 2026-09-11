# RAG over uploaded documents (Chroma + embeddings + @-references)

> Upload/replace documents, chunk and embed them into Chroma, retrieve for each question, force-include @-referenced documents, and make the assistant cite them. Extends the ai-assistant playbook.

## Services and env
compose adds Chroma (durable volume) next to the api:
```yaml
  chroma:
    image: chromadb/chroma:1.0.0
    volumes: [ "chroma-data:/data" ]
    environment: { IS_PERSISTENT: "TRUE", ANONYMIZED_TELEMETRY: "FALSE" }
volumes: { app-data: {}, chroma-data: {} }
```
```
CHROMA_URL            # http://chroma:8000 inside compose
EMBEDDINGS_BASE_URL   # OpenAI-compatible /v1 base; default = LLM_BASE_URL
EMBEDDINGS_MODEL      # e.g. text-embedding-3-small, nomic-embed-text
```
`npm i chromadb@^3.0.0` in `api/`. Use one collection per app, metadata `{ doc_id, user_id, name, chunk }`. Do not use Chroma's built-in default embedder (it phones home); pass your own embeddings.

## Schema
```sql
CREATE TABLE documents(id INTEGER PRIMARY KEY, user_id INTEGER NOT NULL, name TEXT NOT NULL,
  filename TEXT NOT NULL, mime TEXT NOT NULL, bytes INTEGER NOT NULL, storage_path TEXT NOT NULL,
  chunks INTEGER NOT NULL DEFAULT 0, version INTEGER NOT NULL DEFAULT 1, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
  UNIQUE(user_id, name));
```
`name` is the @-handle (`Contract_2024`): derived from the filename (strip extension, non-word → `_`), editable, unique per user.

## Ingest
`POST /api/documents` (multipart, see media-uploads playbook for the upload handler; accept pdf/txt/md/docx, 25 MB cap):
1. Save the file under `data/documents/<user>/<uuid>`; extract text — `pdf-parse@^1.1.1` for PDF, `mammoth@^1.8.0` for DOCX, raw for txt/md.
2. Chunk: ~800 tokens ≈ 3000 chars with 300-char overlap, split on paragraph boundaries first.
3. Embed in batches of 32: `POST ${EMBEDDINGS_BASE_URL}/embeddings` `{ model, input: [chunks…] }` → `data[i].embedding`.
4. `collection.add({ ids: chunks.map((_,i)=>`${docId}:${version}:${i}`), embeddings, documents: chunks, metadatas })`.
5. Update `documents.chunks`. Replace (`PUT /api/documents/:id` with a new file): `collection.delete({ where: { doc_id } })`, bump `version`, re-ingest — never leave two versions retrievable.

## Retrieve
```js
export async function retrieve(collection, embed, { userId, question, forcedDocIds = [], k = 6 }) {
  const [qv] = await embed([question]);
  const res = await collection.query({ queryEmbeddings: [qv], nResults: k, where: { user_id: userId } });
  const hits = res.documents[0].map((text, i) => ({ text, meta: res.metadatas[0][i], distance: res.distances[0][i] }));
  const forced = forcedDocIds.length
    ? (await collection.get({ where: { doc_id: { $in: forcedDocIds } }, include: ["documents", "metadatas"] }))
        .documents.map((text, i) => ({ text, meta: res_meta_at(i), forced: true }))
    : [];
  return dedupe([...forced.slice(0, 12), ...hits]);                // forced docs first, capped so the prompt stays bounded
}
```
Scope retrieval to the user (`where: { user_id }`) unless the brief says the knowledge base is shared.

## @-references
Parse `@([A-Za-z0-9_\-]+)` in the user message; resolve each handle against `documents.name` for the user; unknown handles → reply with "I don't have a document called @X (you have: …)" without calling the model. Resolved ids go to `forcedDocIds`; store them in `context_json` so the history shows which documents were pinned.

## Prompting for citations
System prompt: "Answer from the provided sources. Quote or summarise briefly and cite each source as [name] after the sentence it supports. If the sources do not cover the question, say so. Sources pinned with @ must be addressed explicitly." Render sources as:
```
### Sources
[Contract_2024 §3] …chunk text…
[EmployeeHandbook §1] …
```
Combine with web search (ai-assistant playbook) when the brief asks: retrieved chunks first, web results second, both cited.

## Client
Documents page: upload/replace, list with name, size, chunk count, updated time; inline rename of the handle. In the chat input, typing `@` opens a picker over `documents.name`; chips show pinned documents.

## Prove it
Upload two documents; a question answered only by one returns that document's content and cites `[name]`; replacing a document changes the answer (old content gone); `@Handbook question` cites the handbook even when it is not the top hit; an unknown handle gets the friendly error; Chroma and SQLite both survive `compose restart`.

## Security
Files are private to the uploader (path under the user's dir, routes check ownership); never serve raw upload paths; store extracted text only in Chroma; reject files above the cap before reading them.
