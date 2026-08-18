#!/usr/bin/env python3
"""Dump the FULL trace of a disco conversation the way the UI renders it.

Usage:
  trace.py                      # latest conversation, last 40 events
  trace.py <conv_id|title-substr> [N]   # that conversation, last N events (0=all)

Shows, in order: user/agent messages, agent THOUGHTS, TOOL CALLS (name + args,
with elision markers + errors preserved), OBSERVATIONS (tool_result incl errors),
and PLAN revisions. This is what Dylan sees in the trace pane.
"""

import json
import os
import sqlite3
import sys
from pathlib import Path
from typing import Any

_REPO_ROOT = Path(__file__).resolve().parents[2]
DB = os.environ.get("DISCO_DB", str(_REPO_ROOT / "disco.db"))


def short(s, n=600):
    s = s if isinstance(s, str) else json.dumps(s, ensure_ascii=False)
    return s if len(s) <= n else s[:n] + f" …(+{len(s) - n} chars)"


def _resolve_conversation_id(c: sqlite3.Connection, arg: str | None) -> str:
    """Resolve the conversation id from a CLI argument (cid, title substring, or None=latest)."""
    if arg and arg.startswith("conv_"):
        return arg
    if arg:
        row = c.execute(
            "SELECT conversation_id,title FROM conversations "
            "WHERE lower(title) LIKE ? ORDER BY created_at DESC LIMIT 1",
            (f"%{arg.lower()}%",),
        ).fetchone()
        return row["conversation_id"]
    return c.execute(
        "SELECT conversation_id FROM conversations ORDER BY created_at DESC LIMIT 1"
    ).fetchone()[0]


def _payload(row: sqlite3.Row) -> dict[str, Any]:
    """Parse the JSON payload from an event row, returning {} on failure."""
    try:
        return json.loads(row["payload"])
    except Exception:
        return {}


def _render_message(seq: int, src: str, p: dict[str, Any]) -> None:
    print(f"[{seq}] {src.upper()}: {short(p.get('message', ''))}\n")


def _render_plan(seq: int, p: dict[str, Any]) -> None:
    steps = p.get("steps") or []
    ss = "; ".join(s.get("text", str(s)) if isinstance(s, dict) else str(s) for s in steps)
    print(
        f"[{seq}] PLAN rev{p.get('revision', '?')}: "
        f"{short(p.get('summary', ''), 120)} | steps: {short(ss, 200)}\n"
    )


def _render_action(seq: int, p: dict[str, Any]) -> None:
    th = p.get("thought") or ""
    tc = p.get("tool_call") or {}
    tn = tc.get("tool_name", "?")
    args = tc.get("arguments", {})
    if th.strip():
        print(f"[{seq}] THOUGHT: {short(th, 400)}")
    print(f"[{seq}] TOOL {tn}({short(args, 400)})\n")


def _render_observation(seq: int, p: dict[str, Any]) -> None:
    tr = p.get("tool_result")
    # tool_result may be dict/str; surface errors prominently
    txt = tr if isinstance(tr, str) else json.dumps(tr, ensure_ascii=False)
    tag = "OBS"
    low = txt.lower()
    if any(w in low for w in ("error", "failed", "read_before_write", "elision", "validation")):
        tag = "OBS ⚠ERROR"
    print(f"[{seq}] {tag}: {short(txt, 500)}\n")


def _render_status(seq: int, p: dict[str, Any]) -> None:
    detail = f" / {p['detail']}" if p.get("detail") else ""
    print(f"[{seq}] -- status {p.get('status')}{detail}")


def _render_event_row(row: sqlite3.Row) -> None:
    """Render a single event row in the trace pane format."""
    p = _payload(row)
    k, src, seq = row["kind"], row["source"], row["seq"]
    if k == "message":
        _render_message(seq, src, p)
    elif k == "plan":
        _render_plan(seq, p)
    elif k == "action":
        _render_action(seq, p)
    elif k == "observation":
        _render_observation(seq, p)
    elif k == "status":
        _render_status(seq, p)


def main():
    arg = sys.argv[1] if len(sys.argv) > 1 else None
    n = int(sys.argv[2]) if len(sys.argv) > 2 else 40
    c = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    c.row_factory = sqlite3.Row
    cid = _resolve_conversation_id(c, arg)
    conv = c.execute("SELECT * FROM conversations WHERE conversation_id=?", (cid,)).fetchone()
    print(
        f"### {cid}\n### title: {conv['title']}  | "
        f"surface={conv['surface']} | status={conv['status']}\n"
    )
    q = "SELECT seq,kind,source,payload FROM events WHERE conversation_id=? ORDER BY seq"
    rows = c.execute(q, (cid,)).fetchall()
    if n:
        rows = rows[-n:]
    for r in rows:
        _render_event_row(r)
    total = c.execute("SELECT COUNT(*) FROM events WHERE conversation_id=?", (cid,)).fetchone()[0]
    print(f"\n### {len(rows)} events shown (total in conv: {total})")


if __name__ == "__main__":
    main()
