"""The write-from-pool surface — replay a run's writer, not its research.

A writer change used to be judged by a fresh ~90-minute research run, and each
run gathers a different evidence pool, so two writers were never compared on the
same evidence. The product now saves the writer's whole input as a pool file and
serves ``POST /research/write-from-pool``; this surface points the harness at
that route and leaves everything downstream — the observer, the invariants,
`write_artifacts` — exactly as it is.

Pinned here: the request body the surface builds (a pool FILE travels inline, an
id does not), and that a terminal frame still produces the full artifact set.
"""

from __future__ import annotations

import asyncio
import json

import pytest
from harness.research_harness import (
    WRITE_FROM_POOL_SURFACE,
    FakeTransport,
    ResearchRequest,
    run_harness,
)
from harness.research_harness_parts._cli import _parse_args, _validate_main_args

POOL_DOCUMENT = {
    "schema_version": 1,
    "pool_id": "conv_saved_run_01",
    "query": "the state of X",
    "depth_tier": "standard_deep",
    "recency_window": None,
    "brief": "The question asks about X.",
    "coverage": {"covered": [], "open": []},
    "passages": [
        {
            "id": "p0",
            "source_url": "https://example.test/source",
            "source_title": "Example source",
            "text": "The result.",
        }
    ],
    "trail": [{"kind": "search", "query": "what is X", "result": "ok"}],
}


def _terminal_frames() -> list[dict]:
    body = " ".join(
        f"Finding {index} remains supported by the cited source [[p0]], with distinct "
        "context for this evaluation."
        for index in range(40)
    )
    return [
        {"type": "state", "status": "running"},
        {
            "type": "final",
            "answer": {
                "query": "the state of X",
                "summary": "A substantive answer explains the result and its caveats [[p0]].",
                "sections": [
                    {
                        "id": "s0",
                        "title": "Findings",
                        "markdown": body,
                        "cited_passage_ids": ["p0"],
                    }
                ],
                "passages": POOL_DOCUMENT["passages"],
                "depth_tier": "standard_deep",
            },
        },
        {"type": "state", "status": "finished"},
    ]


def test_a_pool_file_travels_inline(tmp_path) -> None:
    """The file is the whole contract, so the harness sends its CONTENT — a pool
    captured on one machine replays against a server that never saw the run."""
    pool_file = tmp_path / "conv_saved_run_01.json"
    pool_file.write_text(json.dumps(POOL_DOCUMENT), encoding="utf-8")

    request = ResearchRequest(
        "the state of X", surface=WRITE_FROM_POOL_SURFACE, pool=str(pool_file)
    )

    assert request.wire_body() == {"pool": POOL_DOCUMENT}


def test_a_pool_id_names_a_pool_the_server_already_holds() -> None:
    """Anything that is not a readable file is an id for the server to resolve."""
    request = ResearchRequest(
        "the state of X", surface=WRITE_FROM_POOL_SURFACE, pool="conv_saved_run_01"
    )

    assert request.wire_body() == {"pool_id": "conv_saved_run_01"}


def test_the_body_carries_nothing_but_the_pool() -> None:
    """Depth, recency, model and provider come from the SAVED run, so sending
    them here would let a replay quietly disagree with the pool it replays."""
    request = ResearchRequest(
        "the state of X",
        surface=WRITE_FROM_POOL_SURFACE,
        pool="conv_saved_run_01",
        depth="exhaustive",
        recency="week",
        model="model-x",
        provider="brave",
    )

    assert set(request.wire_body()) == {"pool_id"}


def test_a_missing_pool_is_refused_before_any_request() -> None:
    request = ResearchRequest("the state of X", surface=WRITE_FROM_POOL_SURFACE)

    with pytest.raises(ValueError, match="pool"):
        request.wire_body()


def test_cli_requires_a_pool_for_this_surface() -> None:
    args = _parse_args(["--surface", WRITE_FROM_POOL_SURFACE, "--pool", "conv_saved_run_01"])

    assert args.surface == WRITE_FROM_POOL_SURFACE
    assert args.pool == "conv_saved_run_01"
    assert _validate_main_args(args) is None
    missing = _parse_args(["--surface", WRITE_FROM_POOL_SURFACE])
    assert _validate_main_args(missing) == 2


def test_the_surface_writes_the_same_artifacts(tmp_path) -> None:
    """Everything downstream of the transport is unchanged: one terminal frame
    still produces the full artifact set through `write_artifacts`."""
    request = ResearchRequest(
        "the state of X", surface=WRITE_FROM_POOL_SURFACE, pool="conv_saved_run_01"
    )

    result = asyncio.run(
        run_harness(
            request,
            transport=FakeTransport(_terminal_frames()),
            output_dir=tmp_path,
        )
    )

    assert result.request["surface"] == WRITE_FROM_POOL_SURFACE
    for name in ("report.md", "events.jsonl", "model_io.jsonl", "inspect.json", "summary.json"):
        assert (tmp_path / name).is_file(), name
    assert "Findings" in (tmp_path / "report.md").read_text(encoding="utf-8")
