"""The research pool file — everything the writer reads, saved untruncated.

A writer change used to cost a whole research run to evaluate, because nothing
a finished run leaves behind can feed the writer again. The report event keeps
only the CITED passages with their text cut to 1,536 characters
(``_report_event``), the cited/reviewed split loses the pool's ORDER — which is
what the ``s1…sN`` citation aliases are numbered by, so ids would not reproduce
— the brief and the coverage map survive only as rendered prompt text inside a
bounded inspect ring, and the trail is not persisted at all.

So the run is saved here instead, at the one moment its whole input exists: when
the engine hands the outcome to the writer. The file is the writer's ENTIRE
input, which is what makes it enough on its own — a stranger with the file and a
running server can write the report again through the agent-server's
``/research/write-from-pool``.

Two fields of :class:`~.agent.ResearchOutcome` are deliberately absent:
``all_hits`` and ``bounded_by`` are never read by ``writer.write_report``, and a
file that carried them would invite a reader to believe the replay used them.
The trail is kept WHOLE rather than reduced to the ``search`` rows
``_untested_angles`` consumes, because the run's own report assembly also
derives ``completed_probes`` and the turn-accounting rollup from it; a trimmed
trail would replay the prose correctly and the report metadata wrongly. It is
small beside the passages either way.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from ..models import Passage
from ._durable_files import atomic_write_bytes, research_data_dir
from .agent import ResearchOutcome

#: Bumped when a field changes meaning. A reader that does not recognise the
#: version says so rather than guessing at the shape.
POOL_SCHEMA_VERSION = 1

#: A pool id is a file NAME, and it arrives from a request body. Anything that
#: is not one plain path segment is refused rather than normalised — including
#: a name made only of dots, which is a directory, not an id.
_POOL_ID = re.compile(r"(?!\.+$)[A-Za-z0-9._-]{1,128}")


class ResearchPoolError(RuntimeError):
    """A pool file could not be written, found, or read as a pool."""


@dataclass(frozen=True)
class SavedPool:
    """One saved run, in the shape the writer needs to be called again."""

    pool_id: str
    query: str
    depth_tier: str
    recency_window: Literal["month", "week"] | None
    outcome: ResearchOutcome


def pool_dir() -> Path:
    """``<data dir>/pools``, resolved the way the agent-server resolves every
    other data directory (``report_audio._default_cache_dir``):

    1. ``DISCO_DATA_DIR`` (or legacy ``PMX_DATA_DIR``) → ``<DATA>/pools``
    2. ``XDG_DATA_HOME`` → ``<XDG>/disco/pools``
    3. POSIX fallback → ``~/.local/share/disco/pools``
    """
    return research_data_dir() / "pools"


def pool_path(pool_id: str) -> Path:
    """Where the pool with this id lives. Refuses an id that is not one plain
    path segment, so a request body can never reach outside ``pool_dir()``."""
    if not _POOL_ID.fullmatch(pool_id):
        raise ResearchPoolError(f"unusable research pool id: {pool_id!r}")
    return pool_dir() / f"{pool_id}.json"


def pool_document(
    pool_id: str,
    *,
    query: str,
    depth_tier: str,
    recency_window: Literal["month", "week"] | None,
    outcome: ResearchOutcome,
) -> dict[str, Any]:
    """The saved shape. Passages keep POOL ORDER and every field they carry;
    nothing here is truncated."""
    return {
        "schema_version": POOL_SCHEMA_VERSION,
        "pool_id": pool_id,
        "query": query,
        "depth_tier": depth_tier,
        "recency_window": recency_window,
        "brief": outcome.brief,
        "coverage": dict(outcome.coverage),
        "passages": [passage.model_dump(mode="json") for passage in outcome.passages],
        "trail": list(outcome.trail),
    }


def write_pool(
    pool_id: str,
    *,
    query: str,
    depth_tier: str,
    recency_window: Literal["month", "week"] | None,
    outcome: ResearchOutcome,
) -> tuple[Path, int]:
    """Save one pool. Returns ``(path, bytes written)``.

    ``default=str`` covers a trail row that ever carries a date or an enum: a
    replayable file with one stringified field beats no file at all.
    """
    path = pool_path(pool_id)
    document = pool_document(
        pool_id,
        query=query,
        depth_tier=depth_tier,
        recency_window=recency_window,
        outcome=outcome,
    )
    try:
        payload = json.dumps(document, ensure_ascii=False, default=str).encode("utf-8")
        atomic_write_bytes(path, payload)
    except (OSError, TypeError, ValueError) as exc:
        raise ResearchPoolError(f"could not write the research pool at {path}: {exc}") from exc
    return path, len(payload)


def read_pool(pool_id: str) -> dict[str, Any]:
    """The saved document for ``pool_id``, straight off disk."""
    path = pool_path(pool_id)
    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise ResearchPoolError(f"no saved research pool at {path}") from exc
    try:
        document = json.loads(raw)
    except ValueError as exc:
        raise ResearchPoolError(f"research pool at {path} is not JSON: {exc}") from exc
    if not isinstance(document, dict):
        raise ResearchPoolError(f"research pool at {path} is not a pool document")
    return document


def _load_passages(raw_passages: Any) -> list[Passage]:
    if not isinstance(raw_passages, list) or not raw_passages:
        raise ResearchPoolError("research pool has no passages")
    try:
        return [Passage.model_validate(row) for row in raw_passages]
    except (TypeError, ValueError) as exc:
        raise ResearchPoolError(f"research pool has an unreadable passage: {exc}") from exc


def load_pool(document: Mapping[str, Any]) -> SavedPool:
    """Rebuild the writer's input from a saved (or posted) pool document.

    ``all_hits`` and ``bounded_by`` are set empty/None because the writer never
    reads them — see the module docstring.
    """
    version = document.get("schema_version")
    if version != POOL_SCHEMA_VERSION:
        raise ResearchPoolError(
            f"research pool schema {version!r} is not version {POOL_SCHEMA_VERSION}"
        )
    query = str(document.get("query") or "").strip()
    if not query:
        raise ResearchPoolError("research pool has no query")
    passages = _load_passages(document.get("passages"))
    trail = [dict(row) for row in document.get("trail") or [] if isinstance(row, Mapping)]
    coverage = document.get("coverage")
    recency = document.get("recency_window")
    return SavedPool(
        pool_id=str(document.get("pool_id") or ""),
        query=query,
        depth_tier=str(document.get("depth_tier") or ""),
        recency_window=recency if recency in ("month", "week") else None,
        outcome=ResearchOutcome(
            brief=str(document.get("brief") or ""),
            passages=passages,
            all_hits=[],
            trail=trail,
            bounded_by=None,
            coverage=dict(coverage) if isinstance(coverage, Mapping) else {},
        ),
    )


__all__ = [
    "POOL_SCHEMA_VERSION",
    "ResearchPoolError",
    "SavedPool",
    "load_pool",
    "pool_dir",
    "pool_document",
    "pool_path",
    "read_pool",
    "write_pool",
]
