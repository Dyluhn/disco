#!/usr/bin/env python3
"""BP-00 V2 criterion (e): 30-minute mixed-traffic serving soak on the vision
profile. Exists because of the -150mV lesson: passing llama-bench (or a short
bench) is NOT evidence of MTP-serving stability — only sustained serving is.

Traffic mix, cycled back-to-back (single slot, like real driver traffic):
  tool-json -> image turn -> code-gen -> long-ctx text -> image+long-ctx

PASS = for the full 30 min: every request HTTP-200 with sane output,
/health OK, the systemd unit's InvocationID NEVER changes (no silent
restart), no VRAM/GTT runaway. Log: soak.log + soak.json summary.
"""

from __future__ import annotations

import base64
import json
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

API = "http://127.0.0.1:18080"
GPU = Path("/sys/class/drm/card1/device")
OUT = Path(__file__).parent
DURATION_S = 30 * 60
LOG = OUT / "soak.log"

B64 = base64.b64encode((OUT / "screenshot-image-turn.png").read_bytes()).decode()
LONG = " ".join(
    f"Trace {i}: request handled in {i % 89} ms with cache {'hit' if i % 3 else 'miss'}."
    for i in range(600)
)

WORKLOADS = [
    ("tool-json", {"messages": [{"role": "user", "content":
        'Respond ONLY with a JSON tool call: {"tool":"shell","arguments":{"command":...}}. Task: list files in /workspace.'}],
        "max_tokens": 400}),
    ("image", {"messages": [{"role": "user", "content": [
        {"type": "text", "text": "What is the largest number on this dashboard? Answer briefly."},
        {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{B64}"}}]}],
        "max_tokens": 700}),
    ("code", {"messages": [{"role": "user", "content":
        "Write a bash one-liner to find the 10 largest files under /var, with a one-sentence explanation."}],
        "max_tokens": 500}),
    ("long-ctx", {"messages": [{"role": "user", "content":
        LONG + "\nSummarize the trace log in two sentences."}], "max_tokens": 600}),
    ("image+ctx", {"messages": [{"role": "user", "content": [
        {"type": "text", "text": LONG[: len(LONG) // 2]},
        {"type": "text", "text": "Combine: page header text + typical request latency from the traces, briefly."},
        {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{B64}"}}]}],
        "max_tokens": 700}),
]


def vram() -> tuple[int, int]:
    rd = lambda n: int((GPU / n).read_text()) // 1048576
    return rd("mem_info_vram_used"), rd("mem_info_gtt_used")


def invocation_id() -> str:
    return subprocess.run(
        ["systemctl", "--user", "show", "llama-server.service", "-p", "InvocationID", "--value"],
        capture_output=True, text=True, timeout=10,
    ).stdout.strip()


def health_ok() -> bool:
    try:
        return json.load(urllib.request.urlopen(f"{API}/health", timeout=5)).get("status") == "ok"
    except Exception:
        return False


def chat(body: dict) -> dict:
    req = urllib.request.Request(
        f"{API}/v1/chat/completions",
        data=json.dumps({"model": "driver", "temperature": 0, **body}).encode(),
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=600) as r:
        return json.load(r)


def main() -> int:
    log = LOG.open("w")

    def out(line: str) -> None:
        print(line, flush=True)
        log.write(line + "\n")
        log.flush()

    inv0 = invocation_id()
    assert inv0, "could not read InvocationID"
    out(f"soak start {time.strftime('%F %T')} invocation={inv0} duration={DURATION_S}s")

    t0 = time.time()
    n = ok = 0
    failures: list[str] = []
    peak_vram = peak_gtt = 0
    tps_by: dict[str, list[float]] = {}

    while time.time() - t0 < DURATION_S:
        name, body = WORKLOADS[n % len(WORKLOADS)]
        n += 1
        try:
            res = chat(body)
            tm = res.get("timings", {})
            msg = res["choices"][0]["message"]
            produced = len(msg.get("content") or "") + len(msg.get("reasoning_content") or "")
            assert produced > 0, "empty completion (no content, no reasoning)"
            tps = tm.get("predicted_per_second", 0.0)
            tps_by.setdefault(name, []).append(tps)
            v, g = vram()
            peak_vram, peak_gtt = max(peak_vram, v), max(peak_gtt, g)
            inv, hp = invocation_id(), health_ok()
            status = "OK" if (inv == inv0 and hp) else "RESTART-OR-UNHEALTHY"
            if status != "OK":
                failures.append(f"req{n} {name}: invocation {inv0}->{inv} health={hp}")
            ok += 1
            out(f"[{int(time.time()-t0):4d}s] req{n:3d} {name:9s} {tps:5.1f} t/s "
                f"accept={tm.get('draft_n_accepted',0)}/{tm.get('draft_n',0)} "
                f"vram={v} gtt={g} {status}")
        except Exception as e:  # noqa: BLE001 — soak must log, not die
            failures.append(f"req{n} {name}: {e}")
            out(f"[{int(time.time()-t0):4d}s] req{n:3d} {name:9s} FAILED: {e}")

    inv_end = invocation_id()
    summary = {
        "date": time.strftime("%F %T"),
        "duration_s": int(time.time() - t0),
        "requests": n, "succeeded": ok,
        "failures": failures,
        "invocation_unchanged": inv_end == inv0,
        "peak_vram_mib": peak_vram, "peak_gtt_mib": peak_gtt,
        "mean_tps_by_workload": {k: round(sum(v) / len(v), 1) for k, v in tps_by.items()},
        "pass": not failures and inv_end == inv0 and ok == n and n > 0,
    }
    (OUT / "soak.json").write_text(json.dumps(summary, indent=2))
    out(json.dumps(summary, indent=2))
    out("SOAK " + ("PASS" if summary["pass"] else "FAIL"))
    log.close()
    return 0 if summary["pass"] else 1


if __name__ == "__main__":
    sys.exit(main())
