"""BP-10 behavioral rung — LIVE multi-port build against the real agent-server.

Drives the real stack (loopback :8000 API, gVisor backend, local 27B driver) with a
two-service prompt: a static page on sandbox :8000 ("BP10 multi-port" heading) plus a
JSON status service on sandbox :3000, BOTH kept running in persistent sessions.

CRITICAL TIMING (why assertions run MID-FLIGHT, not after FINISHED): the runtime's
G safe-leak fix tears the sandbox down on a clean FINISH (runtime.ensure_preview
docstring) — post-FINISH the preview payload honestly reports no sandbox and the
port proxy 503s. So this driver polls /preview while the run is live and fires the
proxy probes the moment ports[] shows BOTH 8000 and 3000 with owners.

Acceptance asserted (BP-10 order, behavioral rung):
  * preview ports[] contains BOTH 8000 and 3000, each with a live owner (pid);
  * GET /conversations/{cid}/port/8000/  -> 200 containing "BP10 multi-port";
  * GET /conversations/{cid}/port/3000/status.json -> 200 with parseable JSON;
  * GET /conversations/{cid}/port/8899/  -> 404 (INTERNAL port: not a surface) and
    GET /conversations/{cid}/port/9999/  -> 404 (unknown port) — the SECURITY gate;
  * conversation reaches FINISHED.

Plan approval is WS-only: when this prints NEEDS ATTENTION: AWAITING_PLAN_APPROVAL,
approve out-of-band with  .venv/bin/python test-record/bp-06/send_frame.py <cid> approve_plan

Evidence saved next to this file: events-<cid>.json, preview-<cid>.json,
proxy-probes-<cid>.json.
Exit codes: 0 PASS, 1 pipeline error, 2 assertion FAIL.
"""

from __future__ import annotations

import json
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

BASE = "http://127.0.0.1:8000"
HERE = Path(__file__).parent
RUN_TIMEOUT_S = 50 * 60
POLL_S = 10  # tighter than rung 4: we must catch the both-ports-live window

# Blocked-on-human states — surfaced loudly (the monitor picks the line up) but NOT
# terminal: an out-of-band approve/steer resumes the run and this driver keeps waiting.
ATTENTION = {
    "WAITING_FOR_CONFIRMATION",
    "AWAITING_PLAN_APPROVAL",
    "AWAITING_USER_DECISION",
    "AWAITING_USER_QUESTION",
    "STUCK",
    "PAUSED",
}

PROMPT = """Build a tiny two-service demo in this workspace:

1. Write `status_server.py`: a Python 3 stdlib HTTP server listening on port 3000 that
   answers GET /status.json with body {"service": "status", "ok": true}, header
   Content-Type: application/json, and header Access-Control-Allow-Origin: *.
2. Write `index.html`: a static page whose <h1> is exactly "BP10 multi-port", with a
   small script that fetches http://localhost:3000/status.json and writes the result
   into a <pre id="status"> (on fetch failure write "status: unavailable" instead —
   do not let a failed fetch break the page).
3. Start the JSON service in a persistent shell session: python3 status_server.py
4. Serve the page on port 8000 in a SECOND persistent shell session:
   python3 -m http.server 8000
5. Verify with curl that http://localhost:8000/ returns the page (contains
   "BP10 multi-port") and http://localhost:3000/status.json returns the JSON.
6. LEAVE BOTH SERVERS RUNNING (do not kill the sessions) and finish.
"""


def _post(path: str, body: dict) -> dict:
    req = urllib.request.Request(
        f"{BASE}{path}",
        data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.load(r)


def _get(path: str) -> dict:
    with urllib.request.urlopen(f"{BASE}{path}", timeout=60) as r:
        return json.load(r)


def _get_raw(path: str) -> tuple[int, str]:
    """GET returning (status, body) — 4xx/5xx are DATA here (the 8899 probe WANTS 404)."""
    try:
        with urllib.request.urlopen(f"{BASE}{path}", timeout=30) as r:
            return r.status, r.read().decode(errors="replace")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode(errors="replace")


def fetch_events(cid: str) -> list[dict]:
    events: list[dict] = []
    after: int | None = None
    while True:
        url = f"/conversations/{cid}/events?limit=100"
        if after is not None:
            url += f"&after_seq={after}"
        batch = _get(url).get("events") or []
        if not batch:
            return events
        events.extend(batch)
        after = batch[-1].get("seq")


def probe_proxies(cid: str) -> dict:
    """The mid-flight probe battery. Returns raw results; judged in main()."""
    results: dict[str, dict] = {}
    for name, path in (
        ("page_8000", f"/conversations/{cid}/port/8000/"),
        ("status_3000", f"/conversations/{cid}/port/3000/status.json"),
        ("internal_8899", f"/conversations/{cid}/port/8899/"),
        ("unknown_9999", f"/conversations/{cid}/port/9999/"),
    ):
        status, body = _get_raw(path)
        results[name] = {"status": status, "body": body[:500]}
        print(f"  probe {name}: HTTP {status} ({len(body)}B)", flush=True)
    return results


def main() -> int:
    conv = _post(
        "/conversations",
        {"surface": "build", "title": "BP-10 behavioral: two-service multi-port build"},
    )
    cid = conv["conversation_id"]
    print(f"conversation: {cid}", flush=True)
    print(f"approve gate if needed:  .venv/bin/python test-record/bp-06/send_frame.py {cid} approve_plan", flush=True)  # noqa: E501

    sent = _post(f"/conversations/{cid}/messages", {"content": PROMPT})
    print(f"prompt sent (seq {sent['seq']})", flush=True)

    preview_snapshot: dict | None = None
    probes: dict | None = None
    last = None
    t0 = time.time()
    status = "?"
    while time.time() - t0 < RUN_TIMEOUT_S:
        try:
            status = _get(f"/conversations/{cid}/state").get("execution_status")
        except Exception as e:  # noqa: BLE001 — transient poll failure must not kill the run
            print(f"poll error (transient): {e}", flush=True)
            time.sleep(POLL_S)
            continue
        if status != last:
            print(f"status={status} t+{int(time.time() - t0)}s", flush=True)
            if status in ATTENTION:
                print(f"NEEDS ATTENTION: {status} — intervene via UI/API", flush=True)
            last = status
        if status in ("FINISHED", "ERROR"):
            break

        # Mid-flight: watch the preview payload for BOTH user ports going live, then
        # fire the proxy battery ONCE (before FINISH tears the sandbox down).
        if probes is None:
            try:
                pv = _get(f"/conversations/{cid}/preview")
            except Exception:  # noqa: BLE001
                pv = {}
            live = {p["port"] for p in pv.get("ports", []) if p.get("owner", {}).get("pid")}
            if {8000, 3000} <= live:
                print(f"both ports live t+{int(time.time() - t0)}s — probing proxies", flush=True)
                preview_snapshot = pv
                probes = probe_proxies(cid)
        time.sleep(POLL_S)
    else:
        print(f"TIMEOUT after {RUN_TIMEOUT_S}s (last status={last})", flush=True)

    # ---- evidence (saved even on failure, for the post-mortem) -----------------
    events = fetch_events(cid)
    (HERE / f"events-{cid}.json").write_text(json.dumps(events, indent=2))
    if preview_snapshot is not None:
        (HERE / f"preview-{cid}.json").write_text(json.dumps(preview_snapshot, indent=2))
    if probes is not None:
        (HERE / f"proxy-probes-{cid}.json").write_text(json.dumps(probes, indent=2))
    print(f"saved {len(events)} events", flush=True)

    # ---- assertions ------------------------------------------------------------
    failures: list[str] = []
    if status != "FINISHED":
        failures.append(f"run did not FINISH (final status={status})")
    if preview_snapshot is None or probes is None:
        failures.append("ports 8000+3000 never simultaneously live in preview ports[]")
    else:
        ports = {p["port"]: p.get("owner") for p in preview_snapshot.get("ports", [])}
        for want in (8000, 3000):
            if not (ports.get(want) or {}).get("pid"):
                failures.append(f"preview ports[] missing live owner for {want}: {ports.get(want)}")
        if probes["page_8000"]["status"] != 200 or "BP10 multi-port" not in probes["page_8000"]["body"]:
            failures.append(f"port/8000 proxy wrong: {probes['page_8000']['status']}")
        if probes["status_3000"]["status"] != 200:
            failures.append(f"port/3000 proxy wrong: {probes['status_3000']['status']}")
        else:
            try:
                json.loads(probes["status_3000"]["body"])
            except ValueError:
                failures.append("port/3000 body is not JSON")
        for sec in ("internal_8899", "unknown_9999"):
            if probes[sec]["status"] != 404:
                failures.append(f"SECURITY: {sec} returned {probes[sec]['status']}, want 404")

    if failures:
        for f in failures:
            print(f"FAIL: {f}", flush=True)
        return 2
    print("BEHAVIORAL PASS: both ports live+proxied, internal/unknown ports 404, run FINISHED", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
