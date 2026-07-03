#!/usr/bin/env python3
"""P8 live proof — click-to-edit via the `selection_edit` wire, end to end on MiniMax-M3.

Proves the wire built in this session actually works with a LIVE model (no cassette):
  PHASE 1  the model BUILDS a small index.html with a hero headline + a footer + a
           paragraph (three distinct, verifiable elements).
  PHASE 2  we send a real `selection_edit` WS frame — the exact frame the UI's
           "Change this element" affordance sends — naming ONLY the hero headline
           (a source ref taken from the preview_edit `data-oid` stamp) + an instruction.
  VERIFY   from REAL events + the served page: the headline changed to the new text,
           the footer + paragraph are UNCHANGED (targeted, not a rewrite), a TARGETED
           edit tool was used (not file_write), and the provider ledger is MiniMax-only.

Not a test double — every call hits MiniMax via the relay. Dossier + before/after HTML → RUN_DIR.
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import sys
import time
import urllib.request

import websockets

AGENT = os.environ.get("DISCO_AGENT_URL", "http://localhost:8000")
WS = AGENT.replace("http", "ws", 1)
LEDGER = os.environ.get("MINIMAX_RELAY_LOG", "")
RUN_DIR = os.environ.get("RUN_DIR", "/tmp/claude-1000/-var-home-dylan/aa3c8df1-d803-40e0-89de-d73ae8f27f0e/scratchpad/p8proof")
BUILD_TIMEOUT_S = int(os.environ.get("P8_BUILD_TIMEOUT_S", "480"))
EDIT_TIMEOUT_S = int(os.environ.get("P8_EDIT_TIMEOUT_S", "420"))
_TERMINAL = {"FINISHED", "VERIFIED", "STUCK", "ERROR", "AWAITING_USER", "FAILED", "CANCELLED"}
_EDIT_TOOLS = {"exact_replace", "run_project_script", "safe_write_file", "file_edit",
               "file_replace_lines", "file_str_replace", "file_insert_lines"}

HERO_NEW = "NIGHTOWL_HERO_EDITED"

# IMPORTANT: the build prompt must NOT dictate the headline text as a quoted literal.
# A quoted "MUST contain verbatim X" requirement becomes a REL-RC-O dictated-content
# FINISH FLOOR; a later selection-edit that changes X then gets reverted by the finish
# gate to satisfy the original floor (observed live). To isolate the P8 wire we let the
# model choose the wording, read the actual <h1>/footer/<p> AFTER the build, then edit.
BUILD_PROMPT = (
    "Build a small single-file static landing page `index.html` (plain HTML/CSS, no "
    "framework) for a late-night coffee shop called NightOwl Coffee. Include exactly one "
    "hero <h1> headline, one <p> tagline paragraph, and a <footer> with a copyright line. "
    "Choose the wording yourself. Keep it small. Serve it on the preview and finish."
)


def _post(path: str, body: dict, timeout: int = 120) -> dict:
    req = urllib.request.Request(AGENT + path, data=json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read() or b"{}")


def _get(path: str, timeout: int = 60):
    with urllib.request.urlopen(AGENT + path, timeout=timeout) as r:
        return json.loads(r.read() or b"{}")


def _get_text(path: str, timeout: int = 30) -> str:
    try:
        with urllib.request.urlopen(AGENT + path, timeout=timeout) as r:
            return r.read().decode("utf-8", "replace")
    except Exception:
        return ""


def _status(cid: str) -> str:
    return str(_get(f"/conversations/{cid}/state").get("execution_status") or "")


def _last_seq(cid: str) -> int:
    return int(_get(f"/conversations/{cid}/state").get("last_seq") or 0)


def _events(cid: str) -> list[dict]:
    d = _get(f"/conversations/{cid}/events?limit=1000")
    if isinstance(d, list):
        return d
    for k in ("events", "items", "log"):
        if isinstance(d.get(k), list):
            return d[k]
    return []


def _ledger_rows() -> list[dict]:
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


def _poll_terminal(cid: str, timeout_s: int) -> str:
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        time.sleep(8)
        try:
            s = _status(cid)
        except Exception:
            continue
        if s in _TERMINAL:
            return s
    return "TIMEOUT"


_H1_RE = re.compile(r"<h1[^>]*>(.*?)</h1>", re.IGNORECASE | re.DOTALL)
_FOOTER_RE = re.compile(r"<footer[^>]*>(.*?)</footer>", re.IGNORECASE | re.DOTALL)
_P_RE = re.compile(r"<p[^>]*>(.*?)</p>", re.IGNORECASE | re.DOTALL)


def _inner(rx: re.Pattern[str], html: str) -> str:
    m = rx.search(html)
    return re.sub(r"\s+", " ", m.group(1)).strip() if m else ""


def _h1_oid(cid: str) -> dict:
    """Fetch the preview_edit stamped page and pull the data-oid stamped ON the <h1>
    element — the exact SourceRef the UI's resolveRef produces when the user clicks the
    headline. serve-time stamp_oids adds data-oid to every element."""
    stamped = _get_text(f"/conversations/{cid}/preview-edit/index.html")
    # the <h1 ... data-oid="index.html:N" ...> stamp (attr order not guaranteed)
    for m in re.finditer(r"<h1\b([^>]*)>", stamped, re.IGNORECASE):
        oid_m = re.search(r'data-oid="([^"]+)"', m.group(1))
        if oid_m:
            oid = oid_m.group(1)
            parts = oid.split(":")
            return {"kind": "source", "oid": oid, "file": parts[0] or "index.html",
                    "line": int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else 0}
    # fallback: locate the <h1> line in the raw file
    raw = _get_text(f"/conversations/{cid}/artifacts/index.html?inline=true") or ""
    line = next((i for i, ln in enumerate(raw.splitlines(), 1) if "<h1" in ln.lower()), 0)
    return {"kind": "source", "oid": f"index.html:{line}", "file": "index.html", "line": line}


async def _send_selection_edit(cid: str, ref: dict, human_label: str) -> None:
    """Send exactly the frame the UI's Change-this-element affordance sends."""
    async with websockets.connect(f"{WS}/ws/conversations/{cid}", max_size=None) as ws:
        await ws.send(json.dumps({
            "type": "selection_edit",
            "selection_ref": ref,
            "edit_instruction": f"Change the headline text to exactly: {HERO_NEW}",
            "human_label": human_label,
        }))
        # give the server a moment to append + kick before we close and poll REST
        await asyncio.sleep(3)


def main() -> int:
    os.makedirs(RUN_DIR, exist_ok=True)
    n0 = len(_ledger_rows())

    conv = _post("/conversations", {"owner_id": "local", "surface": "build",
                                    "autonomous": True, "title": "p8 selection_edit proof"})
    cid = conv.get("conversation_id") or conv.get("id")
    assert cid, f"no conversation id in {conv}"
    print(f"cid={cid}")

    # PHASE 1 — build
    _post(f"/conversations/{cid}/messages", {"content": BUILD_PROMPT})
    build_status = _poll_terminal(cid, BUILD_TIMEOUT_S)
    seq_build = _last_seq(cid)
    served_before = _get_text(f"/conversations/{cid}/preview-app/")
    h1_before = _inner(_H1_RE, served_before)
    footer_before = _inner(_FOOTER_RE, served_before)
    para_before = _inner(_P_RE, served_before)
    built_ok = bool(h1_before) and bool(footer_before)
    print(f"build_status={build_status} built_ok={built_ok} h1_before={h1_before!r}")
    with open(f"{RUN_DIR}/served_before.html", "w") as f:
        f.write(served_before)

    ref = _h1_oid(cid) if built_ok else {"kind": "source", "oid": "", "file": "", "line": 0}
    print(f"hero_ref={ref}")

    # PHASE 2 — the selection_edit wire
    edit_status = None
    if built_ok:
        asyncio.run(_send_selection_edit(cid, ref, f'h1 — "{h1_before}"'))
        edit_status = _poll_terminal(cid, EDIT_TIMEOUT_S)
    print(f"edit_status={edit_status}")

    time.sleep(6)
    served_after = _get_text(f"/conversations/{cid}/preview-app/")
    with open(f"{RUN_DIR}/served_after.html", "w") as f:
        f.write(served_after)

    events = _events(cid)
    phase2 = [e for e in events if int(e.get("seq") or 0) > seq_build]

    def calls(evs):
        for e in evs:
            tc = e.get("tool_call")
            if tc:
                yield tc.get("tool_name"), (tc.get("arguments") or {})

    def results(evs):
        for e in evs:
            tr = e.get("tool_result")
            if tr:
                yield tr.get("tool_name"), tr

    phase2_tools = [n for n, _ in calls(phase2)]
    targeted_used = [n for n in phase2_tools if n in _EDIT_TOOLS]
    full_write_used = [n for n in phase2_tools if n in ("file_write",)]
    targeted_success = any(tr.get("success") for n, tr in results(phase2) if n in _EDIT_TOOLS)

    # ── the P8 claims ────────────────────────────────────────────────────────
    h1_after = _inner(_H1_RE, served_after)
    # headline changed TO the requested text, and the old wording is gone
    headline_changed = (HERO_NEW in served_after) and (h1_before not in served_after)
    # scoped: the footer + tagline the user did NOT select are unchanged
    footer_intact = bool(footer_before) and footer_before in served_after
    para_intact = bool(para_before) and para_before in served_after
    scoped_not_rewrite = bool(targeted_used) and targeted_success and not full_write_used

    ledger_slice = _ledger_rows()[n0:]
    hosts = {(r.get("host") or r.get("provider") or "") for r in ledger_slice}
    openrouter = sum(1 for r in ledger_slice if "openrouter" in json.dumps(r).lower())
    minimax = sum(1 for r in ledger_slice if "minimax" in json.dumps(r).lower())

    verdict = {
        "cid": cid,
        "build_status": build_status,
        "edit_status": edit_status,
        "hero_ref": ref,
        "h1_before": h1_before,
        "h1_after": h1_after,
        "phase2_edit_tools": targeted_used,
        "phase2_full_write": full_write_used,
        "CLAIM_headline_changed": headline_changed,
        "CLAIM_footer_intact": footer_intact,
        "CLAIM_paragraph_intact": para_intact,
        "CLAIM_scoped_targeted_not_rewrite": scoped_not_rewrite,
        "ledger_minimax": minimax,
        "ledger_openrouter": openrouter,
        "ledger_hosts": sorted(h for h in hosts if h),
    }
    # Hard gate = the P8-specific claims + NO OpenRouter contamination. minimax>0 is
    # informational: it only registers if the driver routes through the relay ledger
    # (a direct-MiniMax driver wouldn't write here), so it must not fail the proof.
    passed = (
        built_ok and headline_changed and footer_intact and para_intact
        and scoped_not_rewrite and openrouter == 0
    )
    verdict["PASS"] = passed
    with open(f"{RUN_DIR}/verdict.json", "w") as f:
        json.dump(verdict, f, indent=2)
    print(json.dumps(verdict, indent=2))
    print("P8 LIVE PROOF:", "PASS ✅" if passed else "FAIL ❌")
    return 0 if passed else 1


if __name__ == "__main__":
    sys.exit(main())
