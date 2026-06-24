#!/usr/bin/env python3
"""Dump the FULL trace of a disco conversation the way the UI renders it.

Usage:
  trace.py                      # latest conversation, last 40 events
  trace.py <conv_id|title-substr> [N]   # that conversation, last N events (0=all)

Shows, in order: user/agent messages, agent THOUGHTS, TOOL CALLS (name + args,
with elision markers + errors preserved), OBSERVATIONS (tool_result incl errors),
and PLAN revisions. This is what Dylan sees in the trace pane.
"""
import sqlite3, json, sys, textwrap

DB = "/home/dylan/projects/disco/disco.db"

def short(s, n=600):
    s = s if isinstance(s, str) else json.dumps(s, ensure_ascii=False)
    return s if len(s) <= n else s[:n] + f" …(+{len(s)-n} chars)"

def main():
    arg = sys.argv[1] if len(sys.argv) > 1 else None
    n = int(sys.argv[2]) if len(sys.argv) > 2 else 40
    c = sqlite3.connect(f"file:{DB}?mode=ro", uri=True); c.row_factory = sqlite3.Row
    if arg and arg.startswith("conv_"):
        cid = arg
    elif arg:
        row = c.execute("SELECT conversation_id,title FROM conversations WHERE lower(title) LIKE ? ORDER BY created_at DESC LIMIT 1", (f"%{arg.lower()}%",)).fetchone()
        cid = row["conversation_id"]
    else:
        cid = c.execute("SELECT conversation_id FROM conversations ORDER BY created_at DESC LIMIT 1").fetchone()[0]
    conv = c.execute("SELECT * FROM conversations WHERE conversation_id=?", (cid,)).fetchone()
    print(f"### {cid}\n### title: {conv['title']}  | surface={conv['surface']} | status={conv['status']}\n")
    q = "SELECT seq,kind,source,payload FROM events WHERE conversation_id=? ORDER BY seq"
    rows = c.execute(q, (cid,)).fetchall()
    if n: rows = rows[-n:]
    for r in rows:
        try: p = json.loads(r["payload"])
        except Exception: p = {}
        k, src, seq = r["kind"], r["source"], r["seq"]
        if k == "message":
            print(f"[{seq}] {src.upper()}: {short(p.get('message',''))}\n")
        elif k == "plan":
            steps = p.get("steps") or []
            ss = "; ".join(s.get("text", str(s)) if isinstance(s, dict) else str(s) for s in steps)
            print(f"[{seq}] PLAN rev{p.get('revision','?')}: {short(p.get('summary',''),120)} | steps: {short(ss,200)}\n")
        elif k == "action":
            th = p.get("thought") or ""
            tc = p.get("tool_call") or {}
            tn = tc.get("tool_name", "?"); args = tc.get("arguments", {})
            if th.strip(): print(f"[{seq}] THOUGHT: {short(th,400)}")
            print(f"[{seq}] TOOL {tn}({short(args,400)})\n")
        elif k == "observation":
            tr = p.get("tool_result")
            # tool_result may be dict/str; surface errors prominently
            txt = tr if isinstance(tr, str) else json.dumps(tr, ensure_ascii=False)
            tag = "OBS"
            low = txt.lower()
            if any(w in low for w in ("error", "failed", "read_before_write", "elision", "validation")):
                tag = "OBS ⚠ERROR"
            print(f"[{seq}] {tag}: {short(txt,500)}\n")
        elif k == "status":
            print(f"[{seq}] -- status {p.get('status')}{' / '+p.get('detail') if p.get('detail') else ''}")
    print(f"\n### {len(rows)} events shown (total in conv: {c.execute('SELECT COUNT(*) FROM events WHERE conversation_id=?',(cid,)).fetchone()[0]})")

if __name__ == "__main__":
    main()
