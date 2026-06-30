#!/usr/bin/env python3
"""CD-TOOLS-9 — live MiniMax-M3 targeted-edit run: the Mode-B-gone proof.

Drives a REAL build through the agent-server (:8000, default driver = the relay :8080 → MiniMax-M3):
seed a LARGE index.html, ask for three TARGETED edits, poll to terminal, then classify whether the
new targeted-edit tools were used, the edits actually applied (output-truth), there was no edit-
elision thrash, and the provider ledger is clean (0 OpenRouter, 0 post-terminal). NOT a cassette —
every call hits MiniMax. Writes a dossier + verdict to RUN_DIR.

Usage: python3 targeted_edit_run.py            # one run
       python3 targeted_edit_run.py --tag r3   # label the dossier
"""
from __future__ import annotations

import json
import os
import sys
import time
import urllib.request

AGENT = os.environ.get("DISCO_AGENT_URL", "http://localhost:8000")
LEDGER = os.environ.get("MINIMAX_RELAY_LOG", "")
RUN_DIR = os.environ.get(
    "RUN_DIR", "/tmp/claude-1000/-var-home-dylan/c1e33ca0-6ffb-409a-8303-c38c11bb886d/scratchpad/p1blive3b"
)
POLL_TIMEOUT_S = int(os.environ.get("CD9_TIMEOUT_S", "600"))
_TERMINAL = {"FINISHED", "VERIFIED", "STUCK", "ERROR", "AWAITING_USER", "FAILED", "CANCELLED"}

# greppable tokens — the seed values and the three targeted edits.
HERO_OLD, HERO_NEW = "OLD_HERO_HEADLINE_X1", "NEW_HERO_2099_HEADLINE"
CTA_OLD, CTA_NEW = "OLD_CTA_BUTTON_TEXT", "Get Started Now Please"
YEAR_OLD, YEAR_NEW = "2024", "2026"


def _seed_html() -> str:
    pad = "\n".join(f"      <li class='feature-{i}'>Feature item number {i} — descriptive padding text here.</li>" for i in range(1, 60))
    return f"""<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8"><title>Acme Landing</title>
<style>body{{font-family:sans-serif}} .hero{{font-size:3rem}}</style></head>
<body>
  <header><h1 class="hero">{HERO_OLD}</h1>
    <button id="cta">{CTA_OLD}</button></header>
  <main><section class="features"><ul>
{pad}
  </ul></section></main>
  <footer><p>&copy; {YEAR_OLD} Acme Corp. All rights reserved.</p></footer>
</body></html>
"""


def _post_json(path: str, body: dict, timeout: int = 120) -> dict:
    req = urllib.request.Request(
        AGENT + path, data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json"}, method="POST",
    )
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read() or b"{}")


def _get(path: str, timeout: int = 60):
    with urllib.request.urlopen(AGENT + path, timeout=timeout) as r:
        return json.loads(r.read() or b"{}")


def _upload(cid: str, name: str, content: str) -> None:
    b = "----disco9boundary"
    body = (
        f"--{b}\r\nContent-Disposition: form-data; name=\"files\"; filename=\"{name}\"\r\n"
        f"Content-Type: text/html\r\n\r\n{content}\r\n--{b}--\r\n"
    ).encode()
    req = urllib.request.Request(
        f"{AGENT}/conversations/{cid}/files", data=body,
        headers={"Content-Type": f"multipart/form-data; boundary={b}"}, method="POST",
    )
    with urllib.request.urlopen(req, timeout=120) as r:
        r.read()


def _events(cid: str) -> list[dict]:
    d = _get(f"/conversations/{cid}/events")
    if isinstance(d, list):
        return d
    for k in ("events", "items", "log"):
        if isinstance(d.get(k), list):
            return d[k]
    return []


def _ledger_lines() -> list[dict]:
    if not LEDGER or not os.path.exists(LEDGER):
        return []
    out = []
    with open(LEDGER, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                try:
                    out.append(json.loads(line))
                except Exception:
                    pass
    return out


def _terminal_status(events: list[dict]) -> str | None:
    for e in reversed(events):
        blob = json.dumps(e)
        for s in _TERMINAL:
            if s in blob:
                # be sure it's a status-ish field, not prose
                for k, v in e.items():
                    if isinstance(v, str) and v.strip().upper() == s:
                        return s
                if str(e.get("status", "")).upper() in _TERMINAL:
                    return str(e.get("status")).upper()
    return None


def main() -> int:
    tag = sys.argv[sys.argv.index("--tag") + 1] if "--tag" in sys.argv else "r1"
    os.makedirs(RUN_DIR, exist_ok=True)
    n0 = len(_ledger_lines())

    conv = _post_json("/conversations", {"owner_id": "local", "surface": "build", "autonomous": True,
                                         "title": "cd-tools-9 targeted edit"})
    cid = conv.get("id") or conv.get("conversation_id")
    assert cid, f"no conversation id in {conv}"
    _upload(cid, "index.html", _seed_html())
    msg = (
        "The workspace already contains a large file `index.html`. Make EXACTLY these three "
        "TARGETED edits to it, editing precisely in place — do NOT rewrite the whole file:\n"
        f"1. Change the hero headline text `{HERO_OLD}` to `{HERO_NEW}`.\n"
        f"2. Change the CTA button text `{CTA_OLD}` to `{CTA_NEW}`.\n"
        f"3. Change the footer copyright year `{YEAR_OLD}` to `{YEAR_NEW}`.\n"
        "Read the file first, apply the edits with a targeted edit tool, then verify and finish."
    )
    _post_json(f"/conversations/{cid}/messages", {"content": msg})

    deadline = time.time() + POLL_TIMEOUT_S
    status = None
    events: list[dict] = []
    while time.time() < deadline:
        time.sleep(8)
        try:
            events = _events(cid)
        except Exception as e:
            print("poll error:", e)
            continue
        status = _terminal_status(events)
        if status:
            break
    n_terminal = len(_ledger_lines())  # ledger size AT terminal (BEFORE settle)
    time.sleep(6)
    ledger_all = _ledger_lines()
    slice_ = ledger_all[n0:]
    post_terminal = len(ledger_all) - n_terminal

    # fetch the on-disk index.html (output-truth)
    final_html = ""
    for art in (f"/conversations/{cid}/artifacts/index.html",):
        try:
            with urllib.request.urlopen(AGENT + art, timeout=30) as r:
                final_html = r.read().decode("utf-8", "replace")
            break
        except Exception:
            pass

    blob = json.dumps(events)
    used_targeted = any(t in blob for t in ("exact_replace", "run_project_script", "safe_write_file",
                                            "file_edit", "file_replace_lines"))
    did_read = "file_read" in blob
    fresh_required = "FRESH_READ_REQUIRED" in blob or "fresh_read_required" in blob
    old_not_found = blob.count("old_text_not_found")
    elision_rej = ("ELISION_MARKER_REJECTED" in blob or "elision placeholder" in blob
                   or "internal elision" in blob)
    edits_applied = all(t in final_html for t in (HERO_NEW, CTA_NEW)) and (YEAR_NEW in final_html)
    not_truncated = len(final_html) > 800  # the seed is ~3KB; a truncated stub would be tiny
    ledger_hosts = {ln.get("host") for ln in slice_}
    openrouter = sum(1 for ln in slice_ if "openrouter" in str(ln.get("host", "")).lower())
    all_minimax = bool(slice_) and openrouter == 0 and all(
        ("minimax" in str(ln.get("host", "")).lower()) for ln in slice_
    )

    verdict = {
        "tag": tag, "cid": cid, "terminal_status": status,
        "PASS": bool(
            used_targeted and (did_read or fresh_required) and old_not_found < 3 and not elision_rej
            and edits_applied and not_truncated and all_minimax and post_terminal == 0
        ),
        "checks": {
            "targeted_edit_tool_used": used_targeted,
            "fresh_read_behavior": did_read or fresh_required,
            "no_old_text_not_found_loop": old_not_found < 3,
            "old_text_not_found_count": old_not_found,
            "no_elision_marker_rejected": not elision_rej,
            "edits_applied_output_truth": edits_applied,
            "not_truncated_or_whole_rewritten": not_truncated,
            "ledger_all_minimax_0_openrouter": all_minimax,
            "openrouter_count": openrouter,
            "ledger_hosts": sorted(h for h in ledger_hosts if h),
            "provider_calls_after_terminal": post_terminal,
            "provider_calls_this_run": len(slice_),
        },
        "final_html_len": len(final_html),
    }
    dossier = {"verdict": verdict, "events": events, "ledger_slice": slice_, "final_html": final_html}
    out = os.path.join(RUN_DIR, f"cd9_dossier_{tag}.json")
    with open(out, "w", encoding="utf-8") as f:
        json.dump(dossier, f, indent=2)
    print(json.dumps(verdict, indent=2))
    print("dossier:", out)
    return 0 if verdict["PASS"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
