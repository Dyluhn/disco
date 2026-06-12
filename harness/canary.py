"""Synthetic canary (plan Phase 8) — a standalone probe for a DEPLOYED instance.

Hits `/health` (cheap liveness) AND runs a REAL grounded research query through
`/ws/research`, asserting the answer actually cites sources. This is the probe
that would have caught the agent-server dying mid-run: `/health` alone says "the
process is up"; the research probe says "the brain still works end-to-end —
search → extract → ground → cite". Wire it to a systemd-timer / cron on the
homelab; a non-zero exit + the `CANARY FAIL` line is the alert.

Run live:   uv run python -m harness.canary --base http://localhost:8001 --query "..."
Health only: uv run python -m harness.canary --base http://localhost:8001 --no-research

The I/O (`probe_health`/`probe_research`) is thin; the JUDGEMENT lives in the pure
`evaluate_health`/`evaluate_research_frames` so it's unit-tested without a server
(`harness/tests/test_canary.py`) against the verbatim frame protocol.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from dataclasses import dataclass


@dataclass(frozen=True)
class CanaryResult:
    name: str
    ok: bool
    detail: str


# ---- pure judgement (unit-tested, no network) -------------------------------


def evaluate_health(status_code: int, payload: dict) -> CanaryResult:
    """200 + status 'ok' passes; anything else (503 degraded, a bad body) fails with
    the surfaced reason so the alert says WHAT broke, not just 'down'."""
    if status_code != 200:
        return CanaryResult("health", False, f"HTTP {status_code}: {payload}")
    status = payload.get("status")
    if status != "ok":
        return CanaryResult("health", False, f"status={status} checks={payload.get('checks')}")
    return CanaryResult("health", True, f"checks={payload.get('checks')}")


def evaluate_research_frames(frames: list[dict]) -> CanaryResult:
    """A healthy research run ends in a `final` frame whose answer cites real sources
    (non-empty `passages`). An `error` frame, no `final`, or a final with zero
    passages = the pipeline is up but NOT grounding — exactly the silent-degradation
    a plain liveness check misses."""
    errors = [f.get("message") for f in frames if f.get("type") == "error"]
    if errors:
        return CanaryResult("research", False, f"pipeline error: {errors[0]}")
    finals = [f for f in frames if f.get("type") == "final"]
    if not finals:
        return CanaryResult("research", False, "no final frame (stream never completed)")
    answer = finals[-1].get("answer") or {}
    passages = answer.get("passages") or []
    if not passages:
        return CanaryResult("research", False, "final answer cited ZERO sources (not grounded)")
    # Answer text lives in `blocks` (the live /ws/research stream) OR `answer_markdown`
    # (the offline GroundingPipeline shape) — accept either.
    blocks = answer.get("blocks") or []
    text = (
        (answer.get("answer_markdown") or "") + "".join(b.get("text", "") for b in blocks)
    ).strip()
    if not text:
        return CanaryResult("research", False, "final answer is empty prose")
    # Grounding means the prose actually CITES the sources, not just lists them.
    cited = any(b.get("cited_passage_ids") for b in blocks) or any(
        (c.get("claim") or {}).get("cited_passage_ids") for c in (answer.get("claims") or [])
    )
    if not cited:
        return CanaryResult("research", False, "answer has sources but no inline citations")
    return CanaryResult("research", True, f"grounded on {len(passages)} sources")


# ---- live I/O ---------------------------------------------------------------


async def probe_health(base: str, *, timeout: float = 10.0) -> CanaryResult:
    import httpx

    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            res = await client.get(f"{base.rstrip('/')}/health")
            return evaluate_health(res.status_code, res.json())
    except Exception as e:  # noqa: BLE001 — a connection refused IS the signal
        return CanaryResult("health", False, f"unreachable: {type(e).__name__}: {e}")


async def probe_research(base: str, query: str, *, timeout: float = 120.0) -> CanaryResult:
    import websockets

    ws_url = base.rstrip("/").replace("http://", "ws://").replace("https://", "wss://")
    url = f"{ws_url}/ws/research"
    frames: list[dict] = []
    try:
        async with websockets.connect(url, open_timeout=timeout) as ws:
            await ws.send(json.dumps({"query": query}))
            async with asyncio.timeout(timeout):
                async for raw in ws:
                    frame = json.loads(raw)
                    frames.append(frame)
                    if frame.get("type") == "state" and frame.get("status") == "finished":
                        break
                    if frame.get("type") == "error":
                        break
    except Exception as e:  # noqa: BLE001
        return CanaryResult("research", False, f"ws failure: {type(e).__name__}: {e}")
    return evaluate_research_frames(frames)


async def run(base: str, *, query: str, with_research: bool) -> list[CanaryResult]:
    results = [await probe_health(base)]
    if with_research:
        results.append(await probe_research(base, query))
    return results


def _main() -> int:
    ap = argparse.ArgumentParser(description="disco synthetic canary")
    ap.add_argument("--base", default="http://localhost:8001", help="agent-server base URL")
    ap.add_argument("--query", default="What is the capital of France and its population?")
    ap.add_argument("--no-research", dest="research", action="store_false")
    args = ap.parse_args()

    results = asyncio.run(run(args.base, query=args.query, with_research=args.research))
    all_ok = True
    for r in results:
        mark = "PASS" if r.ok else "FAIL"
        print(f"[{mark}] {r.name}: {r.detail}")
        all_ok = all_ok and r.ok
    if not all_ok:
        print("CANARY FAIL", file=sys.stderr)
        return 1
    print("CANARY OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
