"""W2 — file-state tracker for the workspace snapshot (per-path SHA-based staleness).

Extracted as a standalone module (no loop-state dependency) so it can be
tested independently of the engine. The tracker is a mutable object held
by ``ViewBuilder`` across turns; it is NOT reconstructed from the event log
(SHA comparison is the ground truth, not event-sourced replication).

Responsibilities:
  * Track the SHA of each file the snapshot has shown in full (so the next
    turn can compare disk-sha to known-sha and decide: full vs. pointer).
  * ``record_agent_io`` — call after a successful file_read or file_write /
    file_edit / file_append; pre-registers the new SHA so a self-write is
    never falsely flagged as an external change.
  * ``stale_paths`` — async scan: for each known path, read from disk and
    return paths whose disk-SHA diverges from the last-seen SHA.
  * ``file_state_notice`` — module-level pure function: empty stale list →
    None (silent, the common fast-path); else a named "re-read these" block.
"""

from __future__ import annotations

import asyncio
import hashlib
from dataclasses import dataclass

from ..events import Event, LLMMessage

# Same cap as _WS_READ_TIMEOUT_S in view_render.py — the stale check runs
# under the conversation lock, so a hung sandbox read must not freeze control
# ops (pause/steer/cancel). Mirrored here so file_state.py has no import
# dependency on view_render.py (which imports from here — cycle prevention).
_STALE_READ_TIMEOUT_S = 2.0

# Sentinel prefix for the stale notice; tests key on it for quick detection.
_STALE_NOTICE_SENTINEL = "# Files changed on disk"


@dataclass
class FileSnap:
    """Per-path snapshot record: the SHA and seq of the last full-body render."""

    last_seen_seq: int  # event seq at which the snapshot last showed this file
    sha: str  # hex sha256 of the content bytes (content-addressed, not mtime)
    mtime: float | None = None  # optional, for future diagnostics only


class FileStateTracker:
    """Mutable per-ViewBuilder tracker: records which files have been shown
    in full and with what content SHA. Used by ``workspace_snapshot_message``
    to decide full-body vs. one-line pointer rendering each turn.

    Content-SHA comparison (not mtime, not a filesystem watcher) is sandbox-
    robust: it works in gVisor / fake-FS sandboxes, and a no-op ``touch``
    (mtime changes, content does not) never triggers a false positive.

    Thread/async safety: the tracker is accessed only from the
    ``ViewBuilder.build`` critical section (which runs under the conversation
    lock in the loop). No internal locking is needed.
    """

    def __init__(self) -> None:
        self._snaps: dict[str, FileSnap] = {}
        self._applied_mutations: dict[str, int] = {}

    # ------------------------------------------------------------------
    # Mutation API
    # ------------------------------------------------------------------

    def record_agent_io(
        self,
        path: str,
        content: str | bytes,
        seq: int,
        mtime: float | None = None,
    ) -> None:
        """Record that the agent (or snapshot) last saw ``path`` with
        ``content``. Updates the per-path SHA so a subsequent
        ``stale_paths`` call can determine whether the disk content has
        changed since.

        Call after:
          * A successful file_read (agent pulled the file).
          * A successful file_write / file_edit / file_append (pre-register
            the new SHA so the self-write is NOT flagged as external
            staleness on the NEXT snapshot build).
          * ``workspace_snapshot_message`` showing a file in full (the
            snapshot function calls this itself; callers need not duplicate
            it after a snapshot build).
        """
        if not isinstance(path, str) or not path:
            return
        byt = content.encode("utf-8", "replace") if isinstance(content, str) else content
        sha = hashlib.sha256(byt).hexdigest()
        self._snaps[path] = FileSnap(last_seen_seq=seq, sha=sha, mtime=mtime)

    def record_mutation_revision(
        self,
        path: str,
        revision: str | None,
        seq: int,
    ) -> None:
        """Apply one authenticated host mutation to the staleness baseline.

        Mutation receipts carry the exact post-write digest even though tool
        output does not echo the written body.  Apply each receipt once so the
        agent's own write is not reported as an external edit.  A later
        snapshot may observe a genuinely external revision; replaying an old
        receipt must not overwrite that newer baseline.
        """
        if not isinstance(path, str) or not path or seq <= self._applied_mutations.get(path, 0):
            return
        if revision is not None and (
            len(revision) != 64
            or revision != revision.lower()
            or any(char not in "0123456789abcdef" for char in revision)
        ):
            return
        self._applied_mutations[path] = seq
        if revision is None:
            self._snaps.pop(path, None)
            return
        self._snaps[path] = FileSnap(last_seen_seq=seq, sha=revision)

    # ------------------------------------------------------------------
    # Query API
    # ------------------------------------------------------------------

    def is_known(self, path: str) -> bool:
        """True iff this path has been shown in full at least once in this
        session (i.e., an entry exists in the tracker)."""
        return path in self._snaps

    def get_sha(self, path: str) -> str | None:
        """The hex-sha256 of the last-shown content, or None if never shown."""
        snap = self._snaps.get(path)
        return snap.sha if snap is not None else None

    # ------------------------------------------------------------------
    # Async staleness scan
    # ------------------------------------------------------------------

    async def stale_paths(
        self,
        sbx: object,
        working_set: list[str],
    ) -> list[str]:
        """Return the subset of ``working_set`` paths whose current disk SHA
        diverges from the last-seen SHA recorded in this tracker.

        Paths that have never been shown (not in the tracker) are SKIPPED —
        they are "never-shown" and handled separately by the snapshot's
        full-body branch. Sandboxed reads are timeout-guarded and swallow
        common filesystem exceptions (the path may have been deleted,
        permissions changed, etc.) — a file we cannot read is conservatively
        treated as "not stale" (the snapshot will handle the missing case).

        This is a pure DETECTION scan; it does NOT update the tracker.
        """
        stale: list[str] = []
        for path in working_set:
            snap = self._snaps.get(path)
            if snap is None:
                continue  # never shown → "never-shown", not "stale"
            try:
                raw = await asyncio.wait_for(
                    sbx.read_file(path),  # type: ignore[attr-defined]
                    timeout=_STALE_READ_TIMEOUT_S,
                )
            except (FileNotFoundError, TimeoutError, PermissionError, OSError):
                continue  # cannot read → treat conservatively as not stale
            disk_sha = hashlib.sha256(raw).hexdigest()
            if disk_sha != snap.sha:
                stale.append(path)
        return stale


def reconcile_mutation_receipts(
    tracker: FileStateTracker,
    events: list[Event],
) -> None:
    """Advance the tracker from authenticated workspace mutation receipts."""
    from .progress_reducer import reduce_progress_events

    for mutation in reduce_progress_events(events).mutations:
        if mutation.resource.namespace != "workspace.file":
            continue
        revision = mutation.after.digest if mutation.after is not None else None
        tracker.record_mutation_revision(
            mutation.resource.identifier,
            revision,
            mutation.result_event_seq,
        )


# ---------------------------------------------------------------------------
# Pure helper: stale notice message
# ---------------------------------------------------------------------------


def file_state_notice(stale: list[str]) -> LLMMessage | None:
    """Pure function: return None when ``stale`` is empty (SILENT — the
    common no-change fast-path); otherwise return a concise user-role
    message listing ONLY the stale paths.

    The notice uses a NAMED heading (``_STALE_NOTICE_SENTINEL``) so tests
    and the snapshot function can detect it reliably. The body is minimal —
    it names the paths and says what to do (file_read before editing); it
    does NOT reproduce file content (that is the snapshot's job).

    Callers: ``ViewBuilder.build`` passes the list from
    ``FileStateTracker.stale_paths``, which already filters to known paths
    with changed disk SHA.
    """
    if not stale:
        return None
    lines = [
        f"{_STALE_NOTICE_SENTINEL} since the last grounded workspace state",
        "The following files no longer match the content last grounded for this run.",
        "Use `file_read` on each before editing to avoid overwriting the newer version:",
    ]
    lines.extend(f"  - {p}" for p in stale)
    return LLMMessage(role="user", content="\n".join(lines))
