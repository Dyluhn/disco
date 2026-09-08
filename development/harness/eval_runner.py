"""Eval harness — scores the BEHAVIOR of research (and build) against a versioned
corpus, with a scorecard gated on a baseline. Reuses the existing eval primitives
(`grounding_metrics`, `FrontierJudge`) and the headless `research_answer` flow;
runs `--replay` (cassette, fast, deterministic) or `--real` (hits the services).

A research task (evals/research/*.yaml):
    query: "..."
    min_faithfulness: 0.6        # gate: supported/total claims
    must_cite_domains: [...]     # at least one cited source from each

Run:  uv run python -m harness.eval_runner --replay
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
from pathlib import Path
from typing import Any

from disco.retrieval.engine import DefaultRetrievalEngine
from disco.retrieval.evaluation import grounding_metrics
from disco.retrieval.grounding import GroundingPipeline
from disco.retrieval.wiring import research_answer

from .cassette import Cassette
from .providers import (
    RecordingExtractionProvider,
    RecordingSearchProvider,
    ReplayExtractionProvider,
    ReplaySearchProvider,
)
from .router import RecordingRouter, ReplayRouter

_ROOT = Path(__file__).resolve().parents[2]
_EVALS = _ROOT / "evals"


# ---- the headless research pipeline (reuses production composition) ----------


def _pipeline(search, extraction, reranker, embedder, nli, router):
    engine = DefaultRetrievalEngine(
        search=search,
        extraction=extraction,
        reranker=reranker,
        embedder=embedder,
    )
    grounding = GroundingPipeline(router, nli)
    return engine, grounding


async def run_research(query: str, search, extraction, encoders: dict, router, depth="standard"):
    engine, grounding = _pipeline(
        search, extraction, encoders["reranker"], encoders.get("embedder"), encoders["nli"], router
    )
    return await research_answer(query, engine, grounding, depth=depth)


# ---- scoring (reuses grounding_metrics) --------------------------------------


def score_research(answer, task: dict) -> dict:
    m = grounding_metrics(answer)
    cited = {p.source_url for p in answer.passages}
    domains_ok = all(any(dom in url for url in cited) for dom in task.get("must_cite_domains", []))
    min_f = float(task.get("min_faithfulness", 0.0))
    passed = m.faithfulness >= min_f and domains_ok and m.total_claims > 0
    return {
        "faithfulness": round(m.faithfulness, 3),
        "claims": m.total_claims,
        "cited_sources": len(cited),
        "domains_ok": domains_ok,
        "passed": passed,
    }


# ---- live encoders (deterministic; live in both modes) -----------------------


def _encoders() -> dict:
    from disco.retrieval.live import build_live_retrieval

    d = build_live_retrieval()
    return {"reranker": d["reranker"], "embedder": d["embedder"], "nli": d["nli"]}


# ---- capture (record a real research run into a cassette) --------------------


async def capture_research(query: str, cassette: Cassette, router_real, depth="standard") -> None:
    from disco.retrieval.bundled_providers import LocalExtractionProvider
    from disco.retrieval.live import build_keyless_search

    search = RecordingSearchProvider(build_keyless_search(), cassette)
    extraction = RecordingExtractionProvider(LocalExtractionProvider(), cassette)
    router = RecordingRouter(router_real, cassette)
    await run_research(query, search, extraction, _encoders(), router, depth=depth)


# ---- the runner --------------------------------------------------------------


def _load_corpus(kind: str) -> list[dict]:
    import yaml

    d = _EVALS / kind
    return [yaml.safe_load(p.read_text()) | {"_name": p.stem} for p in sorted(d.glob("*.yaml"))]


def _real_search_extract_router(model_pick: str):
    """Live providers + a live router (built from the persisted config/secrets in the
    env) — the `--real` path. Encoders come from `_encoders()` separately."""
    from disco.agent_server import ConversationRuntime
    from disco.core import SqliteEventStore
    from disco.core.llm import ConfigStore, SecretStore
    from disco.retrieval.bundled_providers import LocalExtractionProvider
    from disco.retrieval.live import build_keyless_search

    rt = ConversationRuntime(
        SqliteEventStore(":memory:"),
        config_store=ConfigStore(os.environ.get("PMX_CONFIG", "/tmp/pmx-live-config.json")),
        secret_store=SecretStore(os.environ.get("PMX_SECRETS", "/tmp/pmx-live-secrets.json")),
    )
    return build_keyless_search(), LocalExtractionProvider(), rt._router_now(pick=model_pick)


async def run_eval(*, replay: bool, cassette_path: str | None, model_pick="driver-local") -> dict:
    cas = Cassette.load(cassette_path) if (replay and cassette_path) else Cassette()
    encoders = _encoders()
    results: dict[str, Any] = {}

    if replay:
        search, extraction, router = (
            ReplaySearchProvider(cas),
            ReplayExtractionProvider(cas),
            ReplayRouter(cas),
        )
    else:
        search, extraction, router = _real_search_extract_router(model_pick)

    for task in _load_corpus("research"):
        answer = await run_research(task["query"], search, extraction, encoders, router)
        results[task["_name"]] = score_research(answer, task)
    return results


def diff_baseline(scorecard: dict) -> tuple[bool, list[str]]:
    base_path = _EVALS / "baselines" / "scorecard.json"
    base = json.loads(base_path.read_text()) if base_path.exists() else {}
    regressions = []
    for name, r in scorecard.items():
        if not r["passed"]:
            regressions.append(f"{name}: FAILED gate ({r})")
        b = base.get(name)
        if b and r["faithfulness"] < b["faithfulness"] - 0.05:
            regressions.append(
                f"{name}: faithfulness dropped {b['faithfulness']}→{r['faithfulness']}"
            )
    return (not regressions), regressions


async def _main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--replay", action="store_true")
    ap.add_argument("--cassette", default=str(_ROOT / "cassettes" / "research_demo.jsonl"))
    args = ap.parse_args()
    scorecard = await run_eval(replay=args.replay, cassette_path=args.cassette)
    ok, regressions = diff_baseline(scorecard)
    print(json.dumps(scorecard, indent=2))
    if not ok:
        print("REGRESSIONS:\n  " + "\n  ".join(regressions))
        raise SystemExit(1)
    print("✓ eval passed (all gates met, no regression vs baseline)")


if __name__ == "__main__":
    asyncio.run(_main())
