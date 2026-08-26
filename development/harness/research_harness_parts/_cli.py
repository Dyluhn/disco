"""Argument parsing and the ``python -m harness.research_harness`` entrypoint."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any

from ._observe import ResearchRequest
from ._run import run_batch, run_harness
from ._transports import (
    FakeTransport,
    LiveWebSocketTransport,
    ReplayTransport,
    ResearchTransport,
)

DEFAULT_ACCEPTANCE_CORPUS = (
    "Can mechanistic interpretability predict dangerous AI behavior before deployment?",
    (
        "AI coding agents in production: productivity, reliability, security, and evidence vs "
        "vendor claims"
    ),
    "Solid-state batteries for EVs: commercialization pathways and mass-market readiness (2026)",
    "Causes of the Late Bronze Age Collapse: major theories and evidence",
    "What are the latest innovations in AI?",
)


def _parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Injectable Deep Research observation harness")
    parser.add_argument("--query", help="one research query (omit with --batch)")
    parser.add_argument(
        "--batch",
        action="store_true",
        help="run the acceptance corpus and preserve each isolated trace",
    )
    parser.add_argument(
        "--queries-file",
        help="newline-delimited query file for --batch (defaults to the acceptance corpus)",
    )
    parser.add_argument("--transport", choices=("live", "replay", "fake"), default="replay")
    parser.add_argument("--surface", choices=("deep_research", "research"), default="deep_research")
    parser.add_argument("--cassette")
    parser.add_argument(
        "--frames",
        help="JSON file containing ordered wire frames (required by --transport fake)",
    )
    parser.add_argument("--base-url", default="http://localhost:8000")
    parser.add_argument(
        "--depth", choices=("quick", "standard_deep", "exhaustive"), default="standard_deep"
    )
    parser.add_argument("--recency", choices=("week", "month"))
    parser.add_argument("--model")
    parser.add_argument("--provider")
    parser.add_argument(
        "--auth-token",
        default=os.environ.get("DISCO_PAIRING_TOKEN"),
        help=(
            "Disco pairing token for a non-loopback server; used only to mint "
            "the browser-compatible session and redacted from every artifact "
            "(or set DISCO_PAIRING_TOKEN)"
        ),
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=None,
        help=(
            "wall-clock timeout in seconds; defaults to the requested depth's "
            "budget (quick=600, standard_deep=1500, exhaustive=3000)"
        ),
    )
    parser.add_argument(
        "--watch",
        action="store_true",
        help="stream concise, redacted public event progress to stderr",
    )
    parser.add_argument(
        "--inspect-model-io",
        action="store_true",
        help="fetch the opt-in DISCO_INSPECT trace after each live conversation",
    )
    parser.add_argument(
        "--deck",
        action="store_true",
        help=(
            "after a successful live deep-research report, start and observe the "
            "server-owned Agent report→deck handoff"
        ),
    )
    parser.add_argument(
        "--deck-timeout",
        type=float,
        default=None,
        help="deck observation timeout in seconds (default 1200)",
    )
    parser.add_argument("--output-dir", default="/tmp/disco-deep-research-harness")
    parser.add_argument(
        "--concurrency",
        type=int,
        default=2,
        help="maximum number of simultaneous research requests in a batch",
    )
    parser.add_argument(
        "--repeat",
        type=int,
        default=1,
        help="repeat each batch query this many times for reliability sampling",
    )
    return parser.parse_args(argv)


def _cli_error(message: str) -> int:
    print(message, file=sys.stderr)
    return 2


def _load_fake_frames(frames_path: str | None) -> list[Mapping[str, Any]] | None:
    """Load --frames for the fake transport; None means a reported usage error."""
    if not frames_path:
        print("--frames is required with --transport fake", file=sys.stderr)
        return None
    loaded_frames = json.loads(Path(frames_path).read_text(encoding="utf-8"))
    if not isinstance(loaded_frames, list) or not all(
        isinstance(frame, Mapping) for frame in loaded_frames
    ):
        print("--frames must contain a JSON array of frame objects", file=sys.stderr)
        return None
    return loaded_frames


def _batch_queries(args: argparse.Namespace) -> tuple[str, ...]:
    if args.queries_file:
        return tuple(
            line.strip()
            for line in Path(args.queries_file).read_text(encoding="utf-8").splitlines()
            if line.strip() and not line.lstrip().startswith("#")
        )
    return DEFAULT_ACCEPTANCE_CORPUS


async def _run_batch_cli(
    args: argparse.Namespace,
    queries: Sequence[str],
    make_request: Callable[[str], ResearchRequest],
    make_transport: Callable[[ResearchRequest], ResearchTransport],
    *,
    concurrency: int,
) -> int:
    repeated = [query for query in queries for _ in range(args.repeat)]
    results = await run_batch(
        [make_request(query) for query in repeated],
        transport_factory=make_transport,
        output_dir=args.output_dir,
        max_concurrency=concurrency,
    )
    ok = all(result.ok for result in results)
    print(
        json.dumps(
            {
                "ok": ok,
                "runs": len(results),
                "output_dir": args.output_dir,
                "errors": [error for result in results for error in result.errors],
            },
            indent=2,
        )
    )
    return 0 if ok else 1


async def _main_async(args: argparse.Namespace) -> int:
    if not args.batch and not args.query:
        return _cli_error("--query is required unless --batch is supplied")
    if args.batch and args.query:
        return _cli_error("--query cannot be combined with --batch")
    if args.concurrency < 1:
        return _cli_error("--concurrency must be at least 1")
    if args.repeat < 1:
        return _cli_error("--repeat must be at least 1")
    cassette = args.cassette
    if args.transport == "replay" and not cassette:
        # The demo cassette lives beside the harness modules, one level above
        # this parts package.
        cassette = str(
            Path(__file__).resolve().parent.parent / "cassettes" / "research_demo.jsonl"
        )
    raw_frames: list[Mapping[str, Any]] | None = None
    if args.transport == "fake":
        raw_frames = _load_fake_frames(args.frames)
        if raw_frames is None:
            return 2

    def make_request(query: str) -> ResearchRequest:
        return ResearchRequest(
            query=query,
            depth=args.depth,
            recency=args.recency,
            model=args.model,
            provider=args.provider,
            surface=args.surface,
            base_url=args.base_url,
            timeout_s=args.timeout,
            cassette=cassette,
            auth_token=args.auth_token,
            capture_inspect=args.inspect_model_io,
            deck=args.deck,
            deck_timeout_s=args.deck_timeout,
        )

    def make_transport(request: ResearchRequest) -> ResearchTransport:
        if args.transport == "fake":
            return FakeTransport(raw_frames or [])
        if args.transport == "replay":
            return ReplayTransport(cassette or "")
        return LiveWebSocketTransport(request.base_url, watch=args.watch)

    if args.batch:
        queries = _batch_queries(args)
        if not queries:
            return _cli_error("--queries-file did not contain any queries")
        return await _run_batch_cli(
            args, queries, make_request, make_transport, concurrency=args.concurrency
        )

    request = make_request(args.query or "")
    transport = make_transport(request)
    result = await run_harness(request, transport=transport, output_dir=args.output_dir)
    print(
        json.dumps(
            {"ok": result.ok, "artifacts": result.artifacts, "errors": result.errors}, indent=2
        )
    )
    return 0 if result.ok else 1


def main(argv: Sequence[str] | None = None) -> int:
    try:
        return asyncio.run(_main_async(_parse_args(argv)))
    except KeyboardInterrupt:
        return 130
