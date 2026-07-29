"""C4 experiment runner — constrained-vs-free slide schema evaluation.

Usage:
    cd /tmp/disco-wt-c4exp && source .disco-env && source ~/.config/disco/agent.env
    python3 -m harness.slides_experiment.run [--out RESULTS_JSON] [--prompts P01,P02]

Environment:
    DISCO_OPENROUTER_API_KEY  — required for real model calls
    C4_DRY_RUN=1              — skip LLM calls, use fixture stubs (for CI)

Outputs:
    RESULTS_JSON  — full cell scores (default: /tmp/c4exp-results.json)
    Prints a per-cell score table to stdout.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import time
from dataclasses import asdict
from pathlib import Path

import httpx

from .scorers import (
    CellScore,
    aggregate_score,
    score_content,
    score_overflow,
    score_parse,
)
from .strategies import ALL_STRATEGIES, STRATEGY_IDS

_ROOT = Path(__file__).resolve().parents[3]
_PROMPTS_DIR = Path(__file__).parent / "prompts"

# ---------------------------------------------------------------------------
# OpenRouter client
# ---------------------------------------------------------------------------

_OR_BASE = "https://openrouter.ai/api/v1"
_STRONG_MODEL = "openai/gpt-oss-120b:free"
_WEAK_MODEL = "openai/gpt-oss-20b:free"


def _get_api_key() -> str:
    key = os.environ.get("DISCO_OPENROUTER_API_KEY") or os.environ.get("PMX_OPENROUTER_API_KEY")
    if not key:
        raise RuntimeError(
            "DISCO_OPENROUTER_API_KEY not set — run: source ~/.config/disco/agent.env"
        )
    return key


async def _call_model(
    client: httpx.AsyncClient,
    model: str,
    system: str,
    user: str,
    *,
    max_tokens: int = 3000,
    retries: int = 2,
) -> str:
    """Call a model via OpenRouter, return the text content."""
    payload = {
        "model": model,
        "max_tokens": max_tokens,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
    }
    for attempt in range(retries + 1):
        try:
            r = await client.post(
                f"{_OR_BASE}/chat/completions",
                json=payload,
                timeout=90.0,
            )
            if r.status_code == 200:
                data = r.json()
                return data["choices"][0]["message"]["content"]
            elif r.status_code == 429:
                wait = 5 * (attempt + 1)
                print(f"  [rate-limit] waiting {wait}s...", flush=True)
                await asyncio.sleep(wait)
            else:
                print(f"  [ERROR {r.status_code}] {r.text[:200]}", flush=True)
                if attempt == retries:
                    return f"<API_ERROR:{r.status_code}>"
                await asyncio.sleep(2)
        except httpx.TimeoutException:
            print(f"  [TIMEOUT attempt {attempt + 1}]", flush=True)
            if attempt == retries:
                return "<TIMEOUT>"
            await asyncio.sleep(3)
    return "<MAX_RETRIES>"


# ---------------------------------------------------------------------------
# LLM judge
# ---------------------------------------------------------------------------

_JUDGE_SYSTEM = """\
You are a strict slide deck quality evaluator.
You will be given:
1. A prompt describing what slides to create
2. A model output (the generated slide deck)

Score the output from 0.0 to 1.0 on these criteria:
- COMPLETENESS: Does it address all the slide topics/content mentioned in the prompt?
- STRUCTURE: Are slides appropriately structured (not just one big blob of text)?
- ACCURACY: Does the content match the specific details, numbers, and facts in the prompt?
- USABILITY: Could someone actually use this as a slide deck without major rework?

Respond with ONLY a JSON object:
{"score": float, "reasoning": "one sentence explanation"}

Be strict. A score of 0.8+ means genuinely good output, not just functional."""

_JUDGE_USER_TMPL = """\
PROMPT (what was requested):
{goal}

MODEL OUTPUT:
{output}

Score this output 0.0-1.0 as described. Respond with ONLY the JSON."""


async def _judge_output(
    client: httpx.AsyncClient,
    goal: str,
    raw_output: str,
) -> tuple[float, str]:
    """Call the LLM judge and return (score, reasoning)."""
    # Truncate long outputs for the judge
    truncated = raw_output[:4000] if len(raw_output) > 4000 else raw_output

    response = await _call_model(
        client,
        _STRONG_MODEL,
        _JUDGE_SYSTEM,
        _JUDGE_USER_TMPL.format(goal=goal, output=truncated),
        max_tokens=200,
    )

    if response.startswith("<"):  # error sentinel
        return 0.5, f"judge failed: {response}"

    try:
        # Extract JSON from response
        import re

        m = re.search(r"\{[^{}]+\}", response, re.DOTALL)
        if m:
            data = json.loads(m.group())
            score = float(data.get("score", 0.5))
            score = max(0.0, min(1.0, score))
            return score, data.get("reasoning", "")
        return 0.5, f"judge unparseable: {response[:100]}"
    except (json.JSONDecodeError, ValueError, KeyError) as e:
        return 0.5, f"judge parse error: {e} | {response[:100]}"


# ---------------------------------------------------------------------------
# Stub (dry run)
# ---------------------------------------------------------------------------

_DRY_RUN_OUTPUTS: dict[str, str] = {
    "free_form": json.dumps(
        {
            "title": "DRY RUN DECK (free_form)",
            "slides": [
                {"type": "title", "title": "Title Slide", "body": ["Subtitle"]},
                {
                    "type": "bullets",
                    "title": "Key Points",
                    "body": ["Point one", "Point two", "Point three"],
                },
                {"type": "closing", "title": "Thank You", "body": []},
            ],
        }
    ),
    "rigid_json": json.dumps(
        {
            "title": "DRY RUN DECK (rigid)",
            "theme": "light",
            "slides": [
                {"type": "title", "title": "Title Slide", "bullets": []},
                {
                    "type": "bullets",
                    "title": "Key Points",
                    "bullets": ["Point one", "Point two", "Point three"],
                },
                {"type": "closing", "title": "The End", "bullets": []},
            ],
        }
    ),
    "loose_hybrid": json.dumps(
        {
            "title": "DRY RUN DECK (loose_hybrid)",
            "theme": "disco-light",
            "slides": [
                {
                    "type": "title",
                    "title": "Title Slide",
                    "body": ["Subtitle"],
                    "layout_hint": None,
                },
                {
                    "type": "bullets",
                    "title": "Key Points",
                    "body": ["Point one", "Point two"],
                    "layout_hint": None,
                },
                {
                    "type": "two_column",
                    "title": "Comparison",
                    "body": ["Left content", "Right content"],
                    "layout_hint": "two_column",
                },
                {"type": "closing", "title": "Thank You", "body": [], "layout_hint": None},
            ],
        }
    ),
}


# ---------------------------------------------------------------------------
# Main experiment runner
# ---------------------------------------------------------------------------


def _dry_run_cells(prompts: list[dict], strategies: list) -> list[CellScore]:
    """Build stub cells for a dry-run experiment."""
    cells: list[CellScore] = []
    for p in prompts:
        for s in strategies:
            raw = _DRY_RUN_OUTPUTS[s.id]
            parse = score_parse(s.id, p["id"], raw)
            overflow = score_overflow(parse)
            content = score_content(parse, p["goal"])
            cell = CellScore(
                prompt_id=p["id"],
                strategy=s.id,
                parse=parse,
                overflow=overflow,
                content=content,
                llm_judge=0.75,
                llm_judge_detail="dry run stub",
            )
            cells.append(cell)
    return cells


async def _run_real_cell(
    client: httpx.AsyncClient,
    model: str,
    p: dict,
    s,
    skip_judge: bool,
) -> CellScore:
    """Run one real prompt×strategy cell against the live model."""
    user_msg = s.user_template.format(goal=p["goal"])
    raw = await _call_model(client, model, s.system, user_msg)
    parse = score_parse(s.id, p["id"], raw)
    overflow = score_overflow(parse)
    content = score_content(parse, p["goal"])
    judge_score = None
    judge_detail = ""
    if not skip_judge and parse.ok:
        judge_score, judge_detail = await _judge_output(client, p["goal"], raw)
    return CellScore(
        prompt_id=p["id"],
        strategy=s.id,
        parse=parse,
        overflow=overflow,
        content=content,
        llm_judge=judge_score,
        llm_judge_detail=judge_detail,
    )


async def run_experiment(
    prompt_ids: list[str] | None = None,
    strategy_ids: list[str] | None = None,
    model: str = _STRONG_MODEL,
    dry_run: bool = False,
    out_path: Path | None = None,
    skip_judge: bool = False,
) -> list[CellScore]:
    """Run the full experiment matrix and return scored cells."""
    # Load prompts
    all_prompt_files = sorted(_PROMPTS_DIR.glob("*.json"))
    prompts = []
    for pf in all_prompt_files:
        data = json.loads(pf.read_text())
        if prompt_ids is None or data["id"] in prompt_ids:
            prompts.append(data)

    if not prompts:
        print("ERROR: no prompts found", file=sys.stderr)
        return []

    strategies = [
        ALL_STRATEGIES[sid] for sid in (strategy_ids or STRATEGY_IDS) if sid in ALL_STRATEGIES
    ]

    print(f"\n=== C4 Experiment: {len(prompts)} prompts × {len(strategies)} strategies ===")
    print(f"    Model: {model}  DryRun: {dry_run}  SkipJudge: {skip_judge}\n")

    if dry_run:
        cells = _dry_run_cells(prompts, strategies)
        _print_table(cells, prompts, strategies)
        if out_path:
            _save_results(cells, prompts, out_path)
        return cells

    # Real run
    api_key = _get_api_key()
    cells: list[CellScore] = []
    async with httpx.AsyncClient(
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
            "HTTP-Referer": "https://agenticdisco.app",
        }
    ) as client:
        total = len(prompts) * len(strategies)
        idx = 0
        for p in prompts:
            for s in strategies:
                idx += 1
                print(f"[{idx:3d}/{total}] {p['id']} × {s.id:12s} ...", end=" ", flush=True)
                t0 = time.monotonic()
                cell = await _run_real_cell(client, model, p, s, skip_judge)
                elapsed = time.monotonic() - t0
                print(f"{elapsed:.1f}s → {len(cell.parse.raw)} chars", flush=True)
                cells.append(cell)
                # Small pause between calls to avoid rate limits
                await asyncio.sleep(1.0)

    _print_table(cells, prompts, strategies)

    if out_path:
        _save_results(cells, prompts, out_path)

    return cells


# ---------------------------------------------------------------------------
# Output helpers
# ---------------------------------------------------------------------------


def _mean(values: list[float]) -> float:
    """Mean of a non-empty list."""
    return sum(values) / len(values)


def _mean_or_zero(values: list[float]) -> float:
    """Mean of a list, or 0.0 when empty."""
    return _mean(values) if values else 0.0


def _strategy_averages(cells: list[CellScore], sid: str) -> dict:
    """Compute per-strategy aggregate averages for printing and saving."""
    strat_cells = [c for c in cells if c.strategy == sid]
    if not strat_cells:
        return {}
    return _compute_strategy_averages(strat_cells)


def _compute_strategy_averages(strat_cells: list[CellScore]) -> dict:
    """Compute aggregate averages from a non-empty strategy cell list."""
    overflow_scores = [
        overflow_result.score
        for cell in strat_cells
        if (overflow_result := cell.overflow) is not None
    ]
    content_scores = [
        content_result.fidelity_score
        for cell in strat_cells
        if (content_result := cell.content) is not None
    ]
    judged = [c.llm_judge for c in strat_cells if c.llm_judge is not None]
    result: dict = {
        "avg": _mean([aggregate_score(c) for c in strat_cells]),
        "parse_rate": sum(1 for c in strat_cells if c.parse.ok) / len(strat_cells),
        "avg_overflow": _mean_or_zero(overflow_scores),
        "avg_content": _mean_or_zero(content_scores),
    }
    if judged:
        result["avg_llm_judge"] = _mean(judged)
    return result


def _print_table(cells: list[CellScore], prompts: list[dict], strategies: list) -> None:
    """Print a summary table to stdout."""
    print("\n" + "=" * 80)
    print("RESULTS TABLE (aggregate scores 0.0–1.0)")
    print("=" * 80)

    strategy_ids = [s.id for s in strategies]
    header = f"{'Prompt':<20}" + "".join(f"{sid:<16}" for sid in strategy_ids) + "PARSE OK"
    print(header)
    print("-" * len(header))

    for p in prompts:
        row = f"{p['id']:<20}"
        for sid in strategy_ids:
            cell = next((c for c in cells if c.prompt_id == p["id"] and c.strategy == sid), None)
            if cell:
                score = aggregate_score(cell)
                parse_ok = "✓" if cell.parse.ok else "✗"
                row += f"{score:.3f} ({parse_ok})     "
            else:
                row += f"{'N/A':<16}"
        print(row)

    print("\nPER-STRATEGY AVERAGES:")
    for sid in strategy_ids:
        avgs = _strategy_averages(cells, sid)
        if not avgs:
            continue
        print(
            f"  {sid:<14}: avg={avgs['avg']:.3f}  parse={avgs['parse_rate']:.0%}"
            f"  overflow_risk={avgs['avg_overflow']:.2f}  content={avgs['avg_content']:.2f}"
        )
        if "avg_llm_judge" in avgs:
            print(f"               llm_judge={avgs['avg_llm_judge']:.2f}")

    print("=" * 80)


def _save_results(cells: list[CellScore], prompts: list[dict], out_path: Path) -> None:
    """Save full results to JSON."""

    # Convert dataclasses to dicts
    def _asdict_safe(obj):
        if hasattr(obj, "__dataclass_fields__"):
            return {k: _asdict_safe(v) for k, v in asdict(obj).items()}
        return obj

    data = {
        "prompts": prompts,
        "cells": [_asdict_safe(c) for c in cells],
        "summary": {},
    }

    for sid in STRATEGY_IDS:
        avgs = _strategy_averages(cells, sid)
        if not avgs:
            continue
        data["summary"][sid] = {
            "avg_aggregate": avgs["avg"],
            "parse_rate": avgs["parse_rate"],
            "avg_overflow_risk": avgs["avg_overflow"],
            "avg_content_fidelity": avgs["avg_content"],
            "avg_llm_judge": avgs.get("avg_llm_judge"),
        }

    out_path.write_text(json.dumps(data, indent=2))
    print(f"\nResults saved to {out_path}")


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------


def main() -> None:
    parser = argparse.ArgumentParser(description="C4 slides schema experiment")
    parser.add_argument(
        "--out", default="/tmp/c4exp-results.json", help="Output JSON path for full results"
    )
    parser.add_argument(
        "--prompts",
        default=None,
        help="Comma-separated prompt IDs to run (e.g. p01,p02); default=all",
    )
    parser.add_argument(
        "--strategies",
        default=None,
        help="Comma-separated strategy IDs (free_form,rigid_json,loose_hybrid); default=all",
    )
    parser.add_argument(
        "--model", default=_STRONG_MODEL, help=f"OpenRouter model ID (default: {_STRONG_MODEL})"
    )
    parser.add_argument(
        "--skip-judge",
        action="store_true",
        help="Skip the LLM judge step (faster, less complete scoring)",
    )
    parser.add_argument(
        "--dry-run", action="store_true", help="Use stub outputs (no LLM calls; for CI/testing)"
    )
    args = parser.parse_args()

    dry_run = args.dry_run or os.environ.get("C4_DRY_RUN") == "1"

    prompt_ids = [p.strip() for p in args.prompts.split(",")] if args.prompts else None
    strategy_ids = [s.strip() for s in args.strategies.split(",")] if args.strategies else None

    cells = asyncio.run(
        run_experiment(
            prompt_ids=prompt_ids,
            strategy_ids=strategy_ids,
            model=args.model,
            dry_run=dry_run,
            out_path=Path(args.out),
            skip_judge=args.skip_judge,
        )
    )

    if not cells:
        sys.exit(1)


if __name__ == "__main__":
    main()
