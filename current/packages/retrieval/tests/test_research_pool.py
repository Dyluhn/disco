"""The saved evidence pool — a finished run's whole writer input, on disk.

Why it exists: judging a writer change used to cost a fresh ~90-minute research
run, because nothing a finished run leaves behind can feed the writer again.
These tests pin the facts that make the file a replacement for that run: it
appears at the moment the engine hands the outcome to the writer, and it
round-trips to the SAME `ResearchOutcome` — same passages, same order, so the
`s1…sN` citation aliases the writer is taught reproduce exactly.

The run itself is the hermetic end-to-end one from `test_deep_research`; this
module reuses its scripted router and fake providers rather than building a
second fake stack that could drift from it.
"""

from __future__ import annotations

import json
from dataclasses import replace
from typing import Any

import pytest
from disco.retrieval.deep_research import RESEARCH_POOL_ACTION
from disco.retrieval.deep_research import engine as engine_module
from disco.retrieval.deep_research._citation_aliases import citation_aliases
from disco.retrieval.deep_research.agent import ResearchOutcome
from disco.retrieval.deep_research.pool import (
    POOL_SCHEMA_VERSION,
    ResearchPoolError,
    load_pool,
    pool_dir,
    pool_document,
    pool_path,
    read_pool,
)
from disco.retrieval.models import Passage
from test_deep_research import (
    _DONE,
    _READ_TURN,
    _TURN0,
    _collect_events,
    _make_run,
    _ScriptedRouter,
)

CID = "conv_pool"


async def _run_once(
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[list[tuple[str, dict[str, Any]]], ResearchOutcome]:
    """One full hermetic run, returning its emitted events and the exact
    outcome the writer was handed (the thing the pool file has to reproduce).

    The trail is COPIED at hand-off because report assembly appends the
    writer's own rows to the live list afterwards; the pool is a snapshot of
    what the writer read, not of what the run finished with.
    """
    handed: list[ResearchOutcome] = []
    real_write_report = engine_module.write_report

    async def capturing_write_report(query: str, outcome: ResearchOutcome, **kwargs: Any):
        handed.append(replace(outcome, trail=list(outcome.trail)))
        return await real_write_report(query, outcome, **kwargs)

    monkeypatch.setattr(engine_module, "write_report", capturing_write_report)
    router = _ScriptedRouter(turns=[_TURN0, _READ_TURN, _DONE])
    run = _make_run(router, conversation_id=CID)
    captured, emit = _collect_events()
    await run.run(emit=emit)
    assert handed, "the run never reached the writer"
    return captured, handed[0]


async def test_finished_run_saves_its_pool_and_names_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The run writes `<data dir>/pools/<conversation id>.json` and emits one
    event naming the pool id, so a reader can find the file without knowing
    where the server keeps its data."""
    captured, _outcome = await _run_once(monkeypatch)

    path = pool_path(CID)
    assert path.parent == pool_dir()
    assert path.is_file(), "a finished run must leave its writer input on disk"
    named = [payload for kind, payload in captured if kind == RESEARCH_POOL_ACTION]
    assert named == [{"pool_id": CID, "bytes": path.stat().st_size}]


async def test_pool_round_trips_to_the_outcome_the_writer_read(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The file rebuilds the writer's input exactly: the same passages in POOL
    ORDER with their full text, plus the brief, the coverage map as a dict, and
    the trail `untested_angles` reads."""
    _captured, outcome = await _run_once(monkeypatch)

    saved = load_pool(read_pool(CID))

    assert saved.query == "the state of X"
    assert saved.depth_tier == "standard_deep"
    assert saved.recency_window is None
    assert saved.outcome.passages == outcome.passages
    assert saved.outcome.brief == outcome.brief == json.loads(_DONE)["brief"]
    assert saved.outcome.coverage == outcome.coverage
    assert saved.outcome.trail == outcome.trail
    assert saved.outcome.brief and saved.outcome.coverage and saved.outcome.trail


async def test_saved_passage_text_is_not_truncated(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The report event cuts passage text to 1,536 characters; the pool does
    not cut it at all, which is why the writer can be run against it."""
    _captured, outcome = await _run_once(monkeypatch)

    document = read_pool(CID)
    saved_text = [row["text"] for row in document["passages"]]
    assert saved_text == [passage.text for passage in outcome.passages]


async def test_pool_order_reproduces_the_writer_citation_aliases(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Pool ORDER is what the report event loses — it splits the pool into
    cited and reviewed — and order is what `s1…sN` is numbered by. A replay
    that reordered the pool would renumber every citation in the report."""
    _captured, outcome = await _run_once(monkeypatch)

    saved = load_pool(read_pool(CID))
    assert citation_aliases(saved.outcome.passages) == citation_aliases(outcome.passages)


def test_pool_round_trip_preserves_late_accepted_user_steering() -> None:
    """The trail is the existing durable owner of accepted mid-run guidance."""
    original = ResearchOutcome(
        brief="Initial reading of the task.",
        passages=[
            Passage(
                id="p1",
                source_url="https://example.test/1",
                source_title="Source 1",
                text="Evidence for the late limitation.",
            )
        ],
        all_hits=[],
        trail=[
            {"kind": "search", "query": "initial query", "result": "evidence"},
            {"kind": "steer", "text": "Prioritize the late safety limitation."},
        ],
        bounded_by=None,
        coverage={},
    )
    document = pool_document(
        "late-steer",
        query="the original task",
        depth_tier="quick",
        recency_window=None,
        outcome=original,
    )
    saved = load_pool(document)

    assert saved.query == "the original task"
    assert saved.outcome.trail[-1] == {
        "kind": "steer",
        "text": "Prioritize the late safety limitation.",
    }


def test_pool_id_may_not_escape_the_pool_directory() -> None:
    """The id reaches `pool_path` from a request body, so anything that is not
    one plain path segment is refused rather than normalised."""
    for bad in ("../etc/passwd", "a/b", "", "..", "with space"):
        with pytest.raises(ResearchPoolError):
            pool_path(bad)


async def test_unknown_schema_version_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    """A pool from a future (or hand-edited) writer is named, never guessed at."""
    await _run_once(monkeypatch)

    document = read_pool(CID)
    document["schema_version"] = POOL_SCHEMA_VERSION + 1
    with pytest.raises(ResearchPoolError, match="schema"):
        load_pool(document)


async def test_pool_is_plain_json_with_the_declared_fields(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The file is the whole contract: a stranger reads it with `json.loads`."""
    await _run_once(monkeypatch)

    document = json.loads(pool_path(CID).read_text(encoding="utf-8"))
    assert set(document) == {
        "schema_version",
        "pool_id",
        "query",
        "depth_tier",
        "recency_window",
        "brief",
        "coverage",
        "passages",
        "trail",
    }
