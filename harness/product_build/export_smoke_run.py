#!/usr/bin/env python3
"""P10b — LIVE export-smoke deliverable capture. Proves the export/download path end-to-end on a
REAL MiniMax-M3 build: the model builds a single self-contained `export.html` and serves it as a
downloadable file (serve(kind='files')) → a DeliverableEvent → and the harness fetches the REAL
bytes from GET /conversations/{cid}/artifacts/{path}. download_bytes is the length of the ACTUAL GET
(NOT a fixture claim); fail-closed if no deliverable or 0 bytes. Sibling of targeted_edit_run.py.
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
    "/tmp/claude-1000/-var-home-dylan/c1e33ca0-6ffb-409a-8303-c38c11bb886d/scratchpad/p1blive3b",
)
BUILD_TIMEOUT_S = int(os.environ.get("P10B_TIMEOUT_S", "480"))
_TERMINAL = {"FINISHED", "VERIFIED", "STUCK", "ERROR", "AWAITING_USER", "FAILED", "CANCELLED"}

PIN = "export.html"
TOKEN = "P10B_EXPORT_LANDING_OK"  # a token the build must put in the file → bytes-are-real check
PROMPT = (
    f"Build a small, self-contained landing page as a SINGLE file `{PIN}` — inline CSS only, no "
    f"external assets. Put the exact text `{TOKEN}` somewhere visible in the page. Then serve "
    f"`{PIN}` as a DOWNLOADABLE FILE so I can download it, and finish."
)


def _post_json(path: str, body: dict, timeout: int = 120) -> dict:
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


def _get_bytes(path: str, timeout: int = 30) -> tuple[int, bytes]:
    """Return (http_status, body_bytes). status 0 on connection error, 404 etc. surfaced."""
    try:
        with urllib.request.urlopen(AGENT + path, timeout=timeout) as r:
            return r.status, r.read()
    except urllib.error.HTTPError as e:  # type: ignore[attr-defined]
        return e.code, b""
    except Exception:
        return 0, b""


def _events(cid: str) -> list[dict]:
    d = _get(f"/conversations/{cid}/events?limit=1000")
    if isinstance(d, list):
        return d
    for k in ("events", "items", "log"):
        if isinstance(d.get(k), list):
            return d[k]
    return []


def _status(cid: str) -> str:
    return str(_get(f"/conversations/{cid}/state").get("execution_status") or "")


def _ledger() -> list[dict]:
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


def _find_deliverable(events: list[dict]) -> dict | None:
    """A DeliverableEvent carries artifact_kind in {app,files} + a path. Scan robustly (top-level or
    one level nested) and return {'artifact_kind','path'} for the LAST one (the final handoff)."""
    found = None
    for e in events:
        for obj in (e, *(v for v in e.values() if isinstance(v, dict))):
            ak = obj.get("artifact_kind")
            if ak in ("app", "files") and obj.get("path"):
                found = {"artifact_kind": ak, "path": str(obj.get("path"))}
    return found


def _fetch_deliverable_bytes(cid: str, deliverable: dict | None) -> tuple:
    """Fetch REAL bytes from the deliverable path, then the pinned file.

    Returns (fetched_from, status, body).
    """
    fetched_from, status, body = None, None, b""
    candidates = []
    if deliverable:
        candidates.append(deliverable["path"].lstrip("/"))
    candidates.append(PIN)
    for rel in candidates:
        st, b = _get_bytes(f"/conversations/{cid}/artifacts/{rel}")
        if st == 200 and b:
            fetched_from, status, body = rel, st, b
            break
        if status is None:
            status = st  # record the first attempt's status for the dossier
    return fetched_from, status, body


def _verdict_checks(
    deliverable: dict | None,
    download_present: bool,
    download_bytes: int,
    bytes_are_real: bool,
    fetched_from: str | None,
    all_minimax: bool,
    openrouter: int,
    slice_: list,
    post_terminal: int,
) -> dict:
    """Assemble the checks sub-dict for the verdict."""
    return {
        "deliverable_event_present": deliverable is not None,
        "deliverable_artifact_kind": (deliverable or {}).get("artifact_kind"),
        "download_present": download_present,
        "download_bytes": download_bytes,
        "bytes_are_real_html_with_token": bytes_are_real,
        "fetched_from_path": fetched_from,
        "ledger_all_minimax_0_openrouter": all_minimax,
        "openrouter_count": openrouter,
        "ledger_hosts": sorted({str(ln.get("host")) for ln in slice_ if ln.get("host")}),
        "provider_calls_after_terminal": post_terminal,
        "provider_calls_this_run": len(slice_),
    }


def _all_minimax(slice_: list, openrouter: int) -> bool:
    """True when every ledger host is minimax and none is openrouter."""
    return (
        bool(slice_)
        and openrouter == 0
        and all("minimax" in str(ln.get("host") or "").lower() for ln in slice_)
    )


def _build_verdict(
    tag: str,
    cid: str,
    build_status: str,
    deliverable: dict | None,
    fetched_from: str | None,
    status: int | None,
    body: bytes,
    slice_: list,
    post_terminal: int,
) -> dict:
    """Assemble the verdict dict from the collected evidence."""
    download_bytes = len(body)
    download_present = (status == 200) and download_bytes > 0
    text = body.decode("utf-8", "replace")
    bytes_are_real = ("<html" in text.lower() or "<!doctype" in text.lower()) and (TOKEN in text)
    openrouter = sum(1 for ln in slice_ if "openrouter" in str(ln.get("host") or "").lower())
    all_minimax = _all_minimax(slice_, openrouter)
    checks = _verdict_checks(
        deliverable,
        download_present,
        download_bytes,
        bytes_are_real,
        fetched_from,
        all_minimax,
        openrouter,
        slice_,
        post_terminal,
    )
    return {
        "tag": tag,
        "cid": cid,
        "build_status": build_status,
        "deliverable": deliverable,
        "fetched_from": fetched_from,
        "http_status": status,
        "PASS": bool(
            deliverable is not None
            and download_present
            and download_bytes > 0
            and bytes_are_real
            and all_minimax
            and post_terminal == 0
        ),
        "checks": checks,
        "export": {
            "requested": True,
            "download_present": download_present,
            "download_bytes": download_bytes,
        },
    }


def main() -> int:
    tag = sys.argv[sys.argv.index("--tag") + 1] if "--tag" in sys.argv else "e1"
    os.makedirs(RUN_DIR, exist_ok=True)
    n0 = len(_ledger())

    conv = _post_json(
        "/conversations",
        {"owner_id": "local", "surface": "build", "autonomous": True, "title": f"p10b {tag}"},
    )
    cid = conv.get("conversation_id") or conv.get("id")
    assert cid, f"no conversation id in {conv}"
    _post_json(f"/conversations/{cid}/messages", {"content": PROMPT})
    build_status = _poll_terminal(cid, BUILD_TIMEOUT_S)
    n_terminal = len(_ledger())
    time.sleep(6)
    ledger_all = _ledger()
    slice_ = ledger_all[n0:]
    post_terminal = len(ledger_all) - n_terminal

    events = _events(cid)
    deliverable = _find_deliverable(events)

    fetched_from, status, body = _fetch_deliverable_bytes(cid, deliverable)
    verdict = _build_verdict(
        tag, cid, build_status, deliverable, fetched_from, status, body, slice_, post_terminal
    )

    dossier = {
        "verdict": verdict,
        "deliverable": deliverable,
        "ledger_slice": slice_,
        "downloaded_head": body.decode("utf-8", "replace")[:2000],
    }
    with open(os.path.join(RUN_DIR, f"p10b_dossier_{tag}.json"), "w", encoding="utf-8") as f:
        json.dump(dossier, f, indent=2)
    print(json.dumps(verdict, indent=2))
    return 0 if verdict["PASS"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
