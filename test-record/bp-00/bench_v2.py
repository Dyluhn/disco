#!/usr/bin/env python3
"""BP-00 V2 bench — criterion (b): MTP effective t/s + draft-accept on standard
text prompts, measured against the LIVE llama-server (same methodology as the
2026-05-30 Q4-vs-Q5 verdict bench: server completions, 256-tok gens, temp 0).

Run once per profile, SAME DAY:
    .venv/bin/python test-record/bp-00/bench_v2.py baseline
    .venv/bin/python test-record/bp-00/bench_v2.py vision

All numbers come from the server's own `timings` object (predicted_per_second,
draft_n, draft_n_accepted) — no log scraping. VRAM/GTT sampled from host sysfs
(card1 = R9700) before and after the run. Output: a markdown table on stdout +
test-record/bp-00/bench-<label>.json for the record.
"""

from __future__ import annotations

import json
import statistics
import sys
import time
import urllib.request
from pathlib import Path

API = "http://127.0.0.1:18080"
GPU = Path("/sys/class/drm/card1/device")
OUT_DIR = Path(__file__).parent
N_PREDICT = 256
ROUNDS = 2  # each prompt twice: round 1 cold-ish, round 2 cache-warm

# The standard text set: shapes the driver actually produces (agentic JSON tool
# calls, code, prose, planning) — MTP accept-rate is workload-dependent, so the
# mix matters more than the count.
PROMPTS = [
    ("code-py", "Write a Python function that parses an ISO-8601 timestamp and returns the Unix epoch seconds, with full error handling and docstring.\n"),
    ("code-js", "Write a JavaScript fetch wrapper with retries, exponential backoff, and a timeout, as a single well-commented module.\n"),
    ("prose", "Explain how a CPU branch predictor works to a curious high-school student, in about three paragraphs.\n"),
    ("json-tool", 'You are an agent. Respond ONLY with a JSON tool call to write a file. Schema: {"tool":"file_write","arguments":{"path":...,"content":...}}. Task: create index.html with a centered "Hello, World!" heading and a dark background.\n'),
    ("plan", "Plan the steps to build and deploy a small Flask URL-shortener: list concrete numbered steps with the shell commands you would run.\n"),
    ("long-ctx", "Summarize the following design discussion, then list every decision made.\n" + ("The team debated storage engines. " * 400) + "\nDecisions: 1) SQLite for v1. 2) WAL mode on. 3) Daily backups.\n"),
]


def vram() -> dict[str, int]:
    rd = lambda n: int((GPU / n).read_text()) // 1048576
    return {"vram_used_mib": rd("mem_info_vram_used"), "gtt_used_mib": rd("mem_info_gtt_used")}


def completion(prompt: str) -> dict:
    body = json.dumps({"prompt": prompt, "n_predict": N_PREDICT, "temperature": 0}).encode()
    req = urllib.request.Request(f"{API}/completion", data=body, headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=600) as r:
        return json.load(r)


def main() -> int:
    label = sys.argv[1] if len(sys.argv) > 1 else "baseline"
    props = json.load(urllib.request.urlopen(f"{API}/props", timeout=10))
    record = {
        "label": label,
        "date": time.strftime("%Y-%m-%d %H:%M:%S"),
        "model_path": props.get("model_path"),
        "modalities": props.get("modalities"),
        "n_predict": N_PREDICT,
        "vram_before": vram(),
        "runs": [],
    }

    for rnd in range(1, ROUNDS + 1):
        for name, prompt in PROMPTS:
            t0 = time.time()
            res = completion(prompt)
            tm = res["timings"]
            accept = tm["draft_n_accepted"] / tm["draft_n"] if tm.get("draft_n") else None
            row = {
                "round": rnd, "prompt": name,
                "predicted_n": tm["predicted_n"],
                "tps": round(tm["predicted_per_second"], 1),
                "pp_tps": round(tm["prompt_per_second"], 1),
                "draft_n": tm.get("draft_n"), "draft_accepted": tm.get("draft_n_accepted"),
                "accept": round(accept, 3) if accept is not None else None,
                "wall_s": round(time.time() - t0, 1),
                **vram(),
            }
            record["runs"].append(row)
            print(f"  r{rnd} {name:9s} tg={row['tps']:6.1f} t/s  accept={row['accept']}  "
                  f"pp={row['pp_tps']:7.1f}  vram={row['vram_used_mib']}MiB", flush=True)

    record["vram_after"] = vram()
    # Warm-round (round 2) aggregates are THE comparison numbers — round 1 pays
    # one-time prompt-cache misses that the baseline/vision pair wouldn't share.
    warm = [r for r in record["runs"] if r["round"] == ROUNDS]
    record["summary"] = {
        "warm_tg_tps_mean": round(statistics.mean(r["tps"] for r in warm), 1),
        "warm_accept_mean": round(statistics.mean(r["accept"] for r in warm if r["accept"] is not None), 3),
        "warm_pp_tps_mean": round(statistics.mean(r["pp_tps"] for r in warm), 1),
        "peak_vram_mib": max(r["vram_used_mib"] for r in record["runs"]),
        "peak_gtt_mib": max(r["gtt_used_mib"] for r in record["runs"]),
    }

    out = OUT_DIR / f"bench-{label}.json"
    out.write_text(json.dumps(record, indent=2))
    s = record["summary"]
    print(f"\n| {label} | {s['warm_tg_tps_mean']} t/s | {s['warm_accept_mean']:.1%} | "
          f"{s['warm_pp_tps_mean']} | {s['peak_vram_mib']} MiB | {s['peak_gtt_mib']} MiB |")
    print(f"saved {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
