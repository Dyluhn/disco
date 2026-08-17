"""Agent-surface fixture — a minimal FastAPI web app for the self-host E2E.

The Agent surface IS the Build machinery, so its release shape flows through the
exact same detector/overlay path as a free-form Build. The app binds the $PORT
contract (the generated compose sets PORT) and answers GET / with a meaningful
body so the compose healthcheck can observe a real 200.
"""

from __future__ import annotations

import os

from fastapi import FastAPI
from fastapi.responses import HTMLResponse

app = FastAPI(title="agent-fastapi")


@app.get("/", response_class=HTMLResponse)
def root() -> str:
    return (
        "<!doctype html><meta charset=utf-8><title>agent-fastapi</title>"
        "<h1>agent-fastapi</h1><p>Disco self-host E2E fixture is running.</p>"
    )


@app.get("/api/health")
def health() -> dict[str, object]:
    return {"service": "agent-fastapi", "ok": True, "port": os.environ.get("PORT")}
