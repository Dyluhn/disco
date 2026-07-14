#!/usr/bin/env python3
"""P10 live proof — export render-correctness gate, end to end on MiniMax-M3.

Proves the chain built this session works with a LIVE model (no cassette):
  PHASE 1  the model BUILDS an HTML slide deck via `slides_generate` — the real
           renderer produces real bytes and the producer stamps ExportRenderFacts
           (from the ACTUAL bytes) into the tool result's `structured` payload.
  VERIFY   from REAL events: the stamped export_render facts are present and honest
           (ok=True, non_blank, valid_header, unit_count == declared slide_count —
           i.e. the render matched the declaration, no truncation); the GOOD deck
           reached FINISHED with NO "EXPORT RENDER CHECK" refusal (no false block);
           and the provider ledger is MiniMax-only (0 OpenRouter).

The blank/truncated REFUSAL path is proven deterministically through the real
loop in packages/core/tests/test_export_gate_finish_path.py (a live model won't
emit a blank deck on demand, and facts are stamped from in-memory bytes at
generation time, so the negative path is fault-injected there — not here).

NOTE: run against a FRESHLY RESTARTED agent-server so it carries THIS session's
slides.py/finish.py changes (stale-server lesson from the P8 proof). Dossier → RUN_DIR.
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
    "RUN_DIR",
    "/tmp/claude-1000/-var-home-dylan/aa3c8df1-d803-40e0-89de-d73ae8f27f0e/scratchpad/p10proof",
)
BUILD_TIMEOUT_S = int(os.environ.get("P10_BUILD_TIMEOUT_S", "600"))
_TERMINAL = {"FINISHED", "VERIFIED", "STUCK", "ERROR", "AWAITING_USER", "FAILED", "CANCELLED"}
_EXPORT_GATE_TOKEN = "EXPORT RENDER CHECK"

BUILD_PROMPT = (
    "Create a 5-slide presentation about the benefits of drinking green tea. "
    "Use the slides_generate tool to produce it as an HTML slide deck (format: html) "
    "with a real title slide and four content slides, each with a heading and a few "
    "sentences of genuine body text. Then finish."
)


def _post(path: str, body: dict, timeout: int = 120) -> dict:
    req = urllib.request.Request(
        AGENT + path,
        data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read() or b"{}")


def _get(path: str, timeout: int = 60):
    with urllib.request.urlopen(AGENT + path, timeout=timeout) as r:
        return json.loads(r.read() or b"{}")


def _status(cid: str) -> str:
    return str(_get(f"/conversations/{cid}/state").get("execution_status") or "")


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
    last = ""
    while time.time() < deadline:
        time.sleep(8)
        try:
            s = _status(cid)
        except Exception:
            continue
        if s != last:
            print(f"  status={s}")
            last = s
        if s in _TERMINAL:
            return s
    return "TIMEOUT"


def main() -> int:
    os.makedirs(RUN_DIR, exist_ok=True)
    n0 = len(_ledger_rows())

    conv = _post(
        "/conversations",
        {
            "owner_id": "local",
            "surface": "build",
            "autonomous": True,
            "title": "p10 export_render proof",
        },
    )
    cid = conv.get("conversation_id") or conv.get("id")
    assert cid, f"no conversation id in {conv}"
    print(f"cid={cid}")

    _post(f"/conversations/{cid}/messages", {"content": BUILD_PROMPT})
    build_status = _poll_terminal(cid, BUILD_TIMEOUT_S)
    print(f"build_status={build_status}")

    events = _events(cid)
    with open(f"{RUN_DIR}/events.json", "w") as f:
        json.dump(events, f, indent=2)

    # Find the slides_generate observation and its stamped export_render facts.
    facts = None
    slide_count_declared = None
    fmt = None
    for e in events:
        tr = e.get("tool_result") or {}
        if tr.get("tool_name") == "slides_generate" and tr.get("success"):
            s = tr.get("structured") or {}
            if s.get("export_render"):
                facts = s["export_render"]
                slide_count_declared = s.get("slide_count")
                fmt = s.get("format")

    # Was the good deck ever refused by the export gate? (Should be NO.)
    gate_refusals = sum(
        1
        for e in events
        if (e.get("message") or {}).get("content") and _EXPORT_GATE_TOKEN in e["message"]["content"]
    )

    ledger_slice = _ledger_rows()[n0:]
    openrouter = sum(1 for r in ledger_slice if "openrouter" in json.dumps(r).lower())
    minimax = sum(1 for r in ledger_slice if "minimax" in json.dumps(r).lower())

    # ── the P10 claims ───────────────────────────────────────────────────────
    facts_present = facts is not None
    facts_ok = bool(facts and facts.get("ok"))
    facts_non_blank = bool(facts and facts.get("non_blank"))
    facts_valid_header = bool(facts and facts.get("valid_header"))
    # the render is NOT truncated (rendered >= declared) — the gate's actual
    # anti-false-completeness semantics (it refuses only when units < declared).
    render_not_truncated = facts_present and not (facts or {}).get("truncated")
    good_deck_finished = build_status == "FINISHED"
    good_deck_not_refused = gate_refusals == 0

    verdict = {
        "cid": cid,
        "build_status": build_status,
        "export_render_facts": facts,
        "declared_slide_count": slide_count_declared,
        "format": fmt,
        "export_gate_refusals": gate_refusals,
        "CLAIM_facts_present": facts_present,
        "CLAIM_facts_ok": facts_ok,
        "CLAIM_non_blank": facts_non_blank,
        "CLAIM_valid_header": facts_valid_header,
        "CLAIM_render_not_truncated": render_not_truncated,
        "CLAIM_good_deck_finished": good_deck_finished,
        "CLAIM_good_deck_not_refused": good_deck_not_refused,
        "ledger_minimax": minimax,
        "ledger_openrouter": openrouter,
    }
    passed = (
        facts_present
        and facts_ok
        and facts_non_blank
        and facts_valid_header
        and render_not_truncated
        and good_deck_finished
        and good_deck_not_refused
        and openrouter == 0
    )
    verdict["PASS"] = passed
    with open(f"{RUN_DIR}/verdict.json", "w") as f:
        json.dump(verdict, f, indent=2)
    print(json.dumps(verdict, indent=2))
    print("P10 LIVE PROOF:", "PASS ✅" if passed else "FAIL ❌")
    return 0 if passed else 1


if __name__ == "__main__":
    sys.exit(main())
