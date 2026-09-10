# Ledgerline

A small full-stack personal expense tracker, shipped as a docker compose app.

- **Backend** — Python FastAPI + SQLite (stdlib `sqlite3`, no ORM). REST API for
  listing, creating and deleting expenses, plus monthly totals grouped by
  category. A `/health` endpoint and a seed of 40 realistic sample expenses
  across six categories and three months on first start.
- **Frontend** — React + Vite + TypeScript. A dashboard with this month's total
  in large numerals, a category breakdown bar chart drawn in plain SVG (no chart
  library), a recent-expenses table with text search and category filter, and an
  add-expense form with validation and an optimistic update.
- **Serving** — nginx serves the built frontend and proxies `/api` to the api
  service. One named volume holds the SQLite database.

Everything runs offline: no external fonts, images, CDNs, or runtime network
calls.

## Run it

```bash
docker compose up -d --build
```

Then open <http://localhost:8080>.

- The web service maps host port **8080** to nginx.
- The api service is not published to the host; nginx proxies `/api` and
  `/health` to it over the compose network.
- The SQLite database lives on the `ledgerline-data` named volume at
  `/data/ledgerline.db` inside the api container.

## API

| Method | Path | Description |
| --- | --- | --- |
| `GET` | `/health` | Health check, returns `{"status":"ok"}` |
| `GET` | `/api/expenses` | List expenses. Optional `?q=` (text search on description) and `?category=` filters. |
| `POST` | `/api/expenses` | Create an expense. Body: `{description, category, amount_cents, spent_on}`. |
| `DELETE` | `/api/expenses/{id}` | Delete an expense. |
| `GET` | `/api/summary?month=YYYY-MM` | Total and per-category totals for a month. |

## Development

Backend (port 8000):

```bash
cd backend
pip install -r requirements.txt
uvicorn app.main:app --reload
```

Frontend (Vite dev server proxies `/api` to `localhost:8000`):

```bash
cd frontend
npm install
npm run dev
```

## Layout

```
backend/            FastAPI app (app/main.py, app/db.py)
frontend/           Vite React-TS app (src/App.tsx, src/components/, src/api.ts)
docker-compose.yml  api + web services, named volume for the DB
```
