"""Bounded client freeze and preview collection.

The public client remains defined by the ``disco_api`` compatibility facade.
"""

from __future__ import annotations

import asyncio
import hashlib
import sqlite3
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from ..events import (
    normalize_events,
)
from ._api_types import (
    PAUSED_STATE,
    _event_status_value,
    _freeze_horizon_violation,
    _latest_agent_view_id,
    _latest_run_intent_id,
    _VerifiedWorkspaceVersion,
)
from ._client_base import _ClientBase


def _freeze_disclosure(status: str) -> dict[str, Any]:
    return {
        "status": status,
        "manifest": {},
        "horizon_seq": None,
        "version_seq": None,
        "paused_seq": None,
        "reason": None,
    }


def _paused_status_seq_after(
    events: list[dict[str, Any]],
    after_seq: int,
) -> int | None:
    return next(
        (
            int(event.get("seq", -1))
            for event in events
            if int(event.get("seq", -1)) > after_seq and _event_status_value(event) == PAUSED_STATE
        ),
        None,
    )


def _paused_version_event_after(
    events: list[dict[str, Any]],
    paused_seq: int,
) -> dict[str, Any] | None:
    return next(
        (
            event
            for event in events
            if event.get("kind") == "workspace_version"
            and int(event.get("seq", -1)) > paused_seq
            and str(event.get("trigger") or "") == PAUSED_STATE
        ),
        None,
    )


async def _await_paused_version(
    collect_events: Callable[[], list[dict[str, Any]]],
    *,
    initial_events: list[dict[str, Any]],
    pre_seq: int,
    deadline_s: float,
) -> tuple[int | None, dict[str, Any] | None, list[dict[str, Any]]]:
    deadline = time.monotonic() + deadline_s
    paused_seq: int | None = None
    version_event: dict[str, Any] | None = None
    events = initial_events
    while time.monotonic() < deadline:
        await asyncio.sleep(0.5)
        try:
            events = normalize_events(collect_events())
        except Exception:  # noqa: BLE001 — retry to the deadline
            continue
        if paused_seq is None:
            paused_seq = _paused_status_seq_after(events, pre_seq)
        if paused_seq is not None:
            version_event = _paused_version_event_after(events, paused_seq)
            if version_event is not None:
                break
    return paused_seq, version_event, events


def _latest_paused_status_seq(events: list[dict[str, Any]]) -> int | None:
    candidates = [
        seq
        for event in events
        if type(seq := event.get("seq")) is int
        and seq > 0
        and _event_status_value(event) == PAUSED_STATE
    ]
    return max(candidates, default=None)


def _accepted_frozen_disclosure(
    *,
    verified: _VerifiedWorkspaceVersion,
    manifest: dict[str, Any],
    horizon_seq: int,
    version_seq: int | None,
    paused_seq: int,
) -> dict[str, Any]:
    return {
        "status": "frozen",
        "manifest": manifest,
        "horizon_seq": horizon_seq,
        "version_seq": version_seq,
        "paused_seq": paused_seq,
        "reason": None,
        "workspace_dir": str(verified.workspace),
        "tree_digest": verified.tree_digest,
        "file_count": verified.file_count,
        "total_bytes": verified.total_bytes,
    }


def _materialize_verified_files(verified: Any, workspace: Path) -> None:
    if workspace.exists():
        return
    workspace.mkdir(parents=False)
    for entry, data in verified.iter_bytes():
        target = workspace / entry.path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)


def _verified_cache_matches(verified: Any, workspace: Path) -> bool:
    cached_paths = {
        path.relative_to(workspace).as_posix()
        for path in workspace.rglob("*")
        if path.is_file() and not path.is_symlink()
    }
    expected_paths = {entry.path for entry in verified.files}
    if cached_paths != expected_paths:
        return False
    return all(
        len(data) == entry.size and hashlib.sha256(data).hexdigest() == entry.sha256
        for entry in verified.files
        if (data := (workspace / entry.path).read_bytes()) is not None
    )


class _FreezeMixin(_ClientBase):
    async def freeze_progressing_workspace(
        self,
        conversation_id: str,
        declared: list[str],
        *,
        deadline_s: float = 90.0,
    ) -> dict[str, Any]:
        """Freeze a still-progressing run's workspace BEFORE the destructive kill.

        The hard-cap stop used to kill first and read the durable store afterwards.
        Product kill correctly destroys the executor/sandbox WITHOUT snapshotting, so
        the store still held only the original import snapshot: every edit, REPORT.md
        and every `.pmx/screenshots/*.png` was absent. Counted context seeds
        460004/460005 surfaced that as MISSING_REQUIRED_EVIDENCE naming
        `0001-navigate.png`, which was merely the FIRST referenced missing path.

        No product change is needed. A Build run that ends PAUSED is snapshotted by
        `_run_with_persistence`'s end-gate under the product's own `workspace_lock`,
        emitting a durable WorkspaceVersionEvent and an immutable ProjectStore version.
        So: pause -> await a NEW durable PAUSED -> await the NEW version event that
        FOLLOWS it -> verify and read that exact immutable version. The mutable store
        head is never read and the live sandbox is never read.

        Returns a disclosure dict; `status="frozen"` only when every step held.
        Any bounded failure returns `FREEZE_TIMEOUT` with an EMPTY manifest — the
        caller must then kill unchanged and must never claim evidence was preserved.
        """

        result = _freeze_disclosure("FREEZE_TIMEOUT")
        # 1. pre-pause watermarks and run authority
        #
        # NORMALIZE. `collect_events` returns raw SQLite ROWS --
        # {seq, kind, source, id, created_at, payload} -- where `payload` is an
        # unparsed JSON STRING. Only seq/kind/source are real top-level columns, so
        # reading `status`, `trigger`, `version_seq`, `run_intent_id`, or
        # `agent_view_id` straight off a row yields None every time and this freeze
        # would report FREEZE_TIMEOUT on every real run. `normalize_events` is the
        # canonical flattener the rest of this adapter already uses.
        try:
            before = normalize_events(self.collect_events(conversation_id))
        except Exception as exc:  # noqa: BLE001 — disclosed, never fabricated
            result["reason"] = f"pre-pause event read failed: {type(exc).__name__}"
            return result
        pre_seq = max((int(e.get("seq", -1)) for e in before), default=-1)
        pre_intent = _latest_run_intent_id(before)
        pre_view = _latest_agent_view_id(before)

        # 2. the existing cooperative WS pause
        try:
            await self.pause(conversation_id)
        except Exception as exc:  # noqa: BLE001
            result["reason"] = f"pause control failed: {type(exc).__name__}"
            return result

        # 3/4. bounded wait for a NEW durable PAUSED, then the version event AFTER it
        paused_seq, version_event, events = await _await_paused_version(
            lambda: self.collect_events(conversation_id),
            initial_events=before,
            pre_seq=pre_seq,
            deadline_s=deadline_s,
        )
        if paused_seq is None:
            result["reason"] = "no durable PAUSED status within the freeze deadline"
            return result
        result["paused_seq"] = paused_seq
        if version_event is None:
            result["reason"] = "no PAUSED WorkspaceVersionEvent within the freeze deadline"
            return result

        horizon_seq = int(version_event.get("seq", -1))
        # 5. a numerically NEW version_seq is deliberately NOT required: an unchanged
        # tree may deduplicate onto an existing immutable version while still emitting
        # a new event. The EVENT is the freeze proof, not the version number.
        version_seq = version_event.get("version_seq")

        # 8. no user turn, resume, newer run intent, or superseded agent view may
        # cross the horizon. One choke point; see _freeze_horizon_violation.
        violation = _freeze_horizon_violation(
            events,
            pre_seq=pre_seq,
            paused_seq=paused_seq,
            horizon_seq=horizon_seq,
            pre_intent=pre_intent,
            pre_view=pre_view,
        )
        if violation is not None:
            result["reason"] = violation
            return result

        # 6. verify and read THAT immutable version — never the mutable head.
        verified = self._verified_workspace_version(conversation_id, version_seq)
        if verified is None:
            result["reason"] = "the PAUSED immutable version could not be freshly verified"
            return result
        # 7. Register THIS immutable version as the conversation's authoritative
        # workspace directory.
        #
        # `collect_browser_evidence` resolves its source as
        # `_collected_workspace_dirs.get(cid) or _snapshot_workspace_dir(cid)`.  The
        # freeze path deliberately bypasses `collect_workspace`, so without this
        # registration that dict is empty and the `or` silently falls back to the
        # MUTABLE ProjectStore head -- which, after the kill, holds only the pre-run
        # import snapshot.  Registering the verified immutable path makes the
        # mutable-head fallback unreachable here rather than merely unlikely.
        self._collected_workspace_dirs[conversation_id] = verified.workspace
        return _accepted_frozen_disclosure(
            verified=verified,
            manifest=self._read_snapshot_manifest(conversation_id, declared, verified.workspace),
            horizon_seq=horizon_seq,
            version_seq=version_seq if type(version_seq) is int else None,
            paused_seq=paused_seq,
        )

    def collect_paused_workspace(self, conversation_id: str, declared: list[str]) -> dict[str, Any]:
        """Read the immutable version a TERMINAL PAUSED run already sealed (F-26).

        cert10 `p4_ff_react_steer` (conv_4c025e93e7ad47bfb8ce665658d52ef6): the
        drive exhausted its resume budget and honestly returned PAUSED so the
        oracle could classify the non-finished run — but the evidence gate
        funneled the run into the FINISHED-seal demand. Strict collection
        honestly returned an empty manifest (no SYSTEM FINISHED exists), and the
        mandatory browser references then converted the designed
        BUILD_DID_NOT_FINISH adjudication into INVALID_RUN /
        MISSING_REQUIRED_EVIDENCE — while the truthful bytes sat in the
        PAUSED-triggered immutable version (event seq 321, version
        003-bc7ae1f7396f; a provider-free replay at that horizon captured 9/9
        referenced screenshots).

        The product's own pause end-gate (`_run_with_persistence`, under the
        product `workspace_lock`) emits a durable
        WorkspaceVersionEvent(trigger=PAUSED) plus an immutable ProjectStore
        version for every Build run that ends PAUSED — the same authority
        `freeze_progressing_workspace` relies on, minus the pause control (the
        pause already happened) and minus polling (the run is stopped and the
        log durable, so one read decides).

        Returns the same disclosure shape as `freeze_progressing_workspace`:
        `status="frozen"` only when every step held; any failure returns a
        fail-closed disclosure with an EMPTY manifest and a reason — never
        fabricated evidence, never the mutable head.
        """

        result = _freeze_disclosure("PAUSED_VERSION_UNAVAILABLE")
        try:
            events = normalize_events(self.collect_events(conversation_id))
        except Exception as exc:  # noqa: BLE001 — disclosed, never fabricated
            result["reason"] = f"durable event read failed: {type(exc).__name__}"
            return result

        paused_seq = _latest_paused_status_seq(events)
        if paused_seq is None:
            result["reason"] = "no durable PAUSED status in the event log"
            return result
        result["paused_seq"] = paused_seq

        version_event = _paused_version_event_after(events, paused_seq)
        if version_event is None:
            result["reason"] = "no PAUSED WorkspaceVersionEvent after the terminal pause"
            return result
        horizon_seq = int(version_event.get("seq", -1))
        raw_version_seq = version_event.get("version_seq")
        version_seq = raw_version_seq if type(raw_version_seq) is int else None

        pre = [event for event in events if int(event.get("seq", -1)) <= paused_seq]
        violation = _freeze_horizon_violation(
            events,
            pre_seq=paused_seq,
            paused_seq=paused_seq,
            horizon_seq=horizon_seq,
            pre_intent=_latest_run_intent_id(pre),
            pre_view=_latest_agent_view_id(pre),
        )
        if violation is not None:
            result["reason"] = violation
            return result

        verified = self._verified_workspace_version(conversation_id, version_seq)
        if verified is None:
            result["reason"] = "the PAUSED immutable version could not be freshly verified"
            return result
        self._collected_workspace_dirs[conversation_id] = verified.workspace
        return _accepted_frozen_disclosure(
            verified=verified,
            manifest=self._read_snapshot_manifest(conversation_id, declared, verified.workspace),
            horizon_seq=horizon_seq,
            version_seq=version_seq,
            paused_seq=paused_seq,
        )

    def _snapshot_workspace_dir(self, conversation_id: str) -> Path | None:
        """The host ProjectStore ``workspace/`` directory for this conversation, or None
        when no projects_root is configured/valid or the snapshot isn't on disk yet.
        Uses the product's OWN ProjectStore so the runner resolves the SAME root the
        agent-server does (DISCO_DATA_DIR / XDG_DATA_HOME / ~/.local/share/disco/projects)."""
        if self._projects_root is None:
            return None
        try:
            from disco.tools.projects.store import ProjectStore, StorageStatus

            store = ProjectStore(self._projects_root)
            if store.status() != StorageStatus.OK:
                return None
            raw_ws = store.path_for(conversation_id)
            root = store.root.resolve() if store.root is not None else None
            if raw_ws.is_symlink() or root is None:
                return None
            ws = raw_ws.resolve()
            if not ws.is_relative_to(root):
                return None
        except Exception:  # noqa: BLE001 — any resolution failure ⇒ no snapshot available
            return None
        return ws if ws.is_dir() else None

    def _verified_workspace_version(
        self, conversation_id: str, version_seq: int | None
    ) -> _VerifiedWorkspaceVersion | None:
        """Freshly verify and resolve one immutable ProjectStore version.

        ``verify_version`` rescans every regular file without following symlinks,
        then checks those facts against both the version index and sidecar.  The
        strict soak consumer compares the returned, scan-proven facts to the seal
        and reads only this immutable path — never the mutable live mirror.
        """

        if self._projects_root is None or version_seq is None:
            return None
        try:
            from disco.tools.projects.store import ProjectStore, StorageStatus

            store = ProjectStore(self._projects_root)
            if store.status() != StorageStatus.OK:
                return None
            cache_key = hashlib.sha256(f"{conversation_id}:{version_seq}".encode()).hexdigest()
            ws = Path(self._verified_workspace_cache.name) / cache_key
            with store.open_verified_version(conversation_id, version_seq) as verified:
                record = verified.record
                _materialize_verified_files(verified, ws)
                if not _verified_cache_matches(verified, ws):
                    return None
        except Exception:  # noqa: BLE001 — missing/corrupt version evidence is not ready
            return None
        if not ws.is_dir():
            return None
        return _VerifiedWorkspaceVersion(
            workspace=ws,
            file_count=record.file_count,
            total_bytes=record.total_bytes,
            tree_digest=record.tree_digest,
        )

    async def collect_preview(self, conversation_id: str) -> dict[str, Any]:
        """Capture post-finish preview truth through the public isolated boundary.

        The application session may mint a one-time path capability, but generated
        content is fetched only after body-only redemption in a clean cookie jar.
        ``fetch_isolated_preview`` validates the server-minted p3s origin and never
        sends the app session to it.  Its final HTTP status/body are authoritative:
        a mint, redemption, or product preview failure is retained as a failure and
        is never hidden by serving the host snapshot inside the harness.
        """
        avail_status, avail = await self._t.get_json(f"/conversations/{conversation_id}/preview")
        status, text, _hdrs = await self._t.fetch_isolated_preview(conversation_id)
        if status == 409 and text.strip() == "preview generation changed":
            # The canonical authority rotates BY DESIGN when the first post-finish
            # request replays the sealed runtime from immutable bytes; the real
            # client re-bootstraps on this exact 409 (counted seed 440041, where
            # the availability projection was simultaneously and truthfully 200).
            # Mirror that protocol ONCE with a fresh mint+redeem+fetch; a second
            # rotation in a row is retained as the truthful failure and any other
            # 409 is never retried.
            status, text, _hdrs = await self._t.fetch_isolated_preview(conversation_id)
        return {
            "health": {"status": status},
            "content": text,
            "available": status < 400,
            "runtime_available": (bool(avail.get("available")) if avail_status < 400 else False),
            "runtime_availability_status": avail_status,
            "source": "isolated_path_capability",
        }

    def _read_events(self, conversation_id: str) -> list[dict[str, Any]]:
        uri = f"file:{self._db_path}?mode=ro"
        conn = sqlite3.connect(uri, uri=True)
        try:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                "SELECT seq, kind, source, id, created_at, payload "
                "FROM events WHERE conversation_id = ? ORDER BY seq",
                (conversation_id,),
            ).fetchall()
        finally:
            conn.close()
        return [
            {
                "seq": r["seq"],
                "kind": r["kind"],
                "source": r["source"],
                "id": r["id"],
                "created_at": r["created_at"],
                "payload": r["payload"],  # JSON string — normalizer parses it
            }
            for r in rows
        ]
