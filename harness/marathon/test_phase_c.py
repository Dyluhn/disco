"""BP-16 Phase C — kernel + masking endurance probes.

Pure analysis over Phase A+B's artifacts (no UI, no new runs):
  * per-step prompt-token series (agent.step end-spans, PMX_LOG_JSON=1) shows
    no context hard-reset — BP-06's masking keeps the prompt BOUNDED, it never
    nukes it;
  * if code_exec was used, per-cell latency is flat (no monotonic growth) —
    BP-08's persistent kernel isn't degrading.
"""

from __future__ import annotations

import datetime as dt
import json
import pathlib

from common import RECORD_DIR, load_state

LOG = pathlib.Path("/tmp/pmx-marathon.log")


def _token_series(cid: str) -> list[int]:
    series: list[int] = []
    for ln in LOG.read_text(errors="replace").splitlines():
        if '"agent.step"' not in ln or '"end"' not in ln:
            continue
        try:
            rec = json.loads(ln[ln.index("{") :])
        except ValueError:
            continue
        if rec.get("cid") == cid and rec.get("span") == "agent.step" and rec.get("event") == "end":
            series.append(int(rec.get("in_tokens") or 0))
    return series


def _code_exec_latencies(events: list[dict]) -> list[float]:
    """action→observation wall-clock per code_exec cell, in event order."""
    pending: dict[str, dt.datetime] = {}
    out: list[float] = []
    for e in events:
        if e.get("kind") == "action" and (e.get("tool_call") or {}).get("tool_name") == "code_exec":
            pending[e["id"]] = dt.datetime.fromisoformat(e["timestamp"])
        elif e.get("kind") == "observation" and e.get("action_id") in pending:
            t0 = pending.pop(e["action_id"])
            out.append((dt.datetime.fromisoformat(e["timestamp"]) - t0).total_seconds())
    return out


def test_phase_c():
    state = load_state()
    witness: dict = {}
    assert LOG.exists(), "marathon JSON log missing — was PMX_LOG_JSON=1 set?"

    for phase in ("phase_a", "phase_b"):
        cid = (state.get(phase) or {}).get("cid")
        assert cid, f"{phase} cid missing from state.json — run that phase first"

        # 7a. no context hard-reset: after warmup, the prompt never collapses
        # below a quarter of the running maximum (masking BOUNDS, restart-resume
        # legitimately re-priming is phase-b's first post-restart step — the
        # series is per-server-life via the appended log, so a refill counts
        # as growth, not a reset).
        series = [t for t in _token_series(cid) if t > 0]
        assert len(series) >= 5, f"{phase}: too few agent.step spans ({len(series)})"
        run_max = series[0]
        resets = []
        for i, tok in enumerate(series):
            if i >= 3 and tok < 0.25 * run_max:
                resets.append({"step": i, "in_tokens": tok, "run_max": run_max})
            run_max = max(run_max, tok)
        assert not resets, f"{phase}: context hard-reset detected: {resets[:3]}"

        events = json.loads((RECORD_DIR / f"events-{cid}.json").read_text())
        lat = _code_exec_latencies(events)
        cell_check = "skipped (fewer than 4 code_exec cells)"
        if len(lat) >= 4:
            third = max(1, len(lat) // 3)
            first, last = lat[:third], lat[-third:]
            grew = sum(last) / len(last) > 2 * (sum(first) / len(first)) + 2.0
            assert not grew, f"{phase}: code_exec latency grew: first={first} last={last}"
            cell_check = f"flat across {len(lat)} cells"

        witness[phase] = {
            "cid": cid,
            "steps": len(series),
            "in_tokens_first": series[:3],
            "in_tokens_last": series[-3:],
            "in_tokens_max": max(series),
            "code_exec": cell_check,
            "code_exec_latencies": [round(x, 1) for x in lat],
        }

    (RECORD_DIR / "phase-c-witness.json").write_text(json.dumps(witness, indent=2))
    print(f"\n[phase-c] PASS — {json.dumps(witness, indent=2)[:400]}")
