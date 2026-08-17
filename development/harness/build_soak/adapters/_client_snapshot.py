"""Bounded client snapshot collection extracted behind the disco_api compatibility facade."""

from __future__ import annotations

import asyncio
import os
import sqlite3
import time
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from disco.tools.projects.store import tree_digest as project_tree_digest

from ..events import (
    NormalizationError,
    normalize_events,
)
from ._api_browser import (
    _file_entry,
    _manifest_ident,
    _manifest_lookup,
    _manifest_present,
    _manifest_sha,
    _proof_level,
)
from ._api_types import (
    _LOG,
    _NO_SYSTEM_FINISHED_TERMINAL,
    _SNAPSHOT_POLL_S,
    _WS_MANIFEST_MAX_FILES,
    PAUSED_STATE,
    TERMINAL_STATES,
    FinishUnsealableContentError,
    SnapshotNotReadyError,
    _exact_seq,
    _strict_final_workspace_seal,
    _typed_content_seal_refusal,
    _valid_workspace_version,
)
from ._api_workspace import (
    _agent_declared_expected,
)
from ._client_base import _ClientBase


def _snapshot_sort_seq(event: dict[str, Any]) -> int:
    seq = _exact_seq(event)
    return seq if seq is not None else -1


def _valid_snapshot_status(event: dict[str, Any]) -> bool:
    return (
        event.get("source") == "system"
        and _exact_seq(event) is not None
        and isinstance(event.get("status"), str)
    )


@dataclass(frozen=True)
class _SnapshotSeal:
    event_evidence_valid: bool
    terminal_seq: int | None
    latest_effect_seq: int | None
    commit_seq: int | None
    version_seq: int | None
    tree_digest: str | None
    file_count: int | None
    total_bytes: int | None
    error: str | None


def _strict_snapshot_seal(
    events: list[dict[str, Any]],
    conversation_id: str,
) -> _SnapshotSeal:
    evidence = _strict_final_workspace_seal(events, conversation_id)
    return _SnapshotSeal(
        event_evidence_valid=evidence.valid,
        terminal_seq=evidence.terminal_seq,
        latest_effect_seq=evidence.latest_effect_seq,
        commit_seq=evidence.event_seq,
        version_seq=evidence.version_seq,
        tree_digest=evidence.tree_digest,
        file_count=evidence.file_count,
        total_bytes=evidence.total_bytes,
        error=evidence.error,
    )


def _legacy_events_valid(
    events: list[dict[str, Any]],
    event_evidence_valid: bool,
) -> bool:
    for event in events:
        kind = event.get("kind")
        if kind == "status" and not _valid_snapshot_status(event):
            event_evidence_valid = False
        elif kind == "workspace_version" and not _valid_workspace_version(event):
            event_evidence_valid = False
        elif kind in {"action", "observation", "agent_error"} and _exact_seq(event) is None:
            event_evidence_valid = False
    return event_evidence_valid


def _legacy_terminal_seq(
    events: list[dict[str, Any]],
    event_evidence_valid: bool,
) -> int | None:
    statuses = [
        event for event in events if event.get("kind") == "status" and _valid_snapshot_status(event)
    ]
    latest_status = max(statuses, key=_snapshot_sort_seq, default=None)
    return (
        _exact_seq(latest_status)
        if event_evidence_valid
        and latest_status is not None
        and (
            str(latest_status.get("status")) in TERMINAL_STATES
            or str(latest_status.get("status")) == PAUSED_STATE
        )
        else None
    )


def _legacy_effect_seq(events: list[dict[str, Any]]) -> int:
    return max(
        (
            seq
            for event in events
            if event.get("kind") in {"action", "observation", "agent_error"}
            and (seq := _exact_seq(event)) is not None
        ),
        default=-1,
    )


def _legacy_commit(
    events: list[dict[str, Any]],
    *,
    event_evidence_valid: bool,
    terminal_seq: int | None,
    effect_seq: int,
) -> dict[str, Any] | None:
    candidates = [
        event
        for event in events
        if event.get("kind") == "workspace_version"
        and _valid_workspace_version(event)
        and event.get("trigger") == "finish"
        and event_evidence_valid
        and terminal_seq is not None
        and _snapshot_sort_seq(event) > terminal_seq
        and _snapshot_sort_seq(event) > effect_seq
    ]
    return max(candidates, key=_snapshot_sort_seq, default=None)


def _legacy_snapshot_seal(
    events: list[dict[str, Any]],
    event_evidence_valid: bool,
) -> _SnapshotSeal:
    event_evidence_valid = _legacy_events_valid(events, event_evidence_valid)
    terminal_seq = _legacy_terminal_seq(events, event_evidence_valid)
    effect_seq = _legacy_effect_seq(events)
    commit = _legacy_commit(
        events,
        event_evidence_valid=event_evidence_valid,
        terminal_seq=terminal_seq,
        effect_seq=effect_seq,
    )
    return _SnapshotSeal(
        event_evidence_valid=event_evidence_valid,
        terminal_seq=terminal_seq,
        latest_effect_seq=effect_seq if effect_seq > 0 else None,
        commit_seq=_exact_seq(commit) if commit is not None else None,
        version_seq=(
            int(commit["version_seq"])
            if commit is not None and type(commit.get("version_seq")) is int
            else None
        ),
        tree_digest=(str(commit.get("tree_digest") or "") if commit is not None else None),
        file_count=None,
        total_bytes=None,
        error=None,
    )


@dataclass(frozen=True)
class _SnapshotRead:
    manifest: dict[str, Any]
    directory: Path | None
    commit_ready: bool
    observed_digest: str | None = None
    observed_file_count: int | None = None
    observed_total_bytes: int | None = None
    error: str | None = None


def _strict_snapshot_read(
    owner: Any,
    conversation_id: str,
    declared: list[str],
    seal: _SnapshotSeal,
) -> _SnapshotRead:
    if not seal.event_evidence_valid or seal.version_seq is None:
        return _SnapshotRead({}, None, False, error=seal.error)
    verified = owner._verified_workspace_version(conversation_id, seal.version_seq)
    if verified is None:
        return _SnapshotRead(
            {},
            None,
            False,
            error="immutable workspace version could not be freshly verified",
        )
    matches = (
        verified.tree_digest == seal.tree_digest
        and verified.file_count == seal.file_count
        and verified.total_bytes == seal.total_bytes
    )
    if not matches:
        return _SnapshotRead(
            {},
            verified.workspace,
            False,
            observed_digest=verified.tree_digest,
            observed_file_count=verified.file_count,
            observed_total_bytes=verified.total_bytes,
            error="final seal tree facts do not match the immutable version",
        )
    manifest = owner._read_snapshot_manifest(
        conversation_id,
        declared,
        verified.workspace,
    )
    return _SnapshotRead(
        manifest,
        verified.workspace,
        True,
        observed_digest=verified.tree_digest,
        observed_file_count=verified.file_count,
        observed_total_bytes=verified.total_bytes,
        error=seal.error,
    )


def _post_verify_strict_snapshot(
    owner: Any,
    conversation_id: str,
    seal: _SnapshotSeal,
    read: _SnapshotRead,
) -> _SnapshotRead:
    if not read.commit_ready or read.directory is None:
        return read
    verified = owner._verified_workspace_version(conversation_id, seal.version_seq)
    if verified is None:
        return replace(
            read,
            manifest={},
            commit_ready=False,
            error="immutable workspace version failed post-read verification",
        )
    unchanged = (
        verified.workspace == read.directory
        and verified.tree_digest == seal.tree_digest
        and verified.file_count == seal.file_count
        and verified.total_bytes == seal.total_bytes
    )
    if unchanged:
        return read
    return _SnapshotRead(
        {},
        read.directory,
        False,
        observed_digest=verified.tree_digest,
        observed_file_count=verified.file_count,
        observed_total_bytes=verified.total_bytes,
        error="immutable workspace version changed during collection",
    )


def _legacy_snapshot_read(
    owner: Any,
    conversation_id: str,
    declared: list[str],
    seal: _SnapshotSeal,
) -> _SnapshotRead:
    directory = owner._snapshot_workspace_dir(conversation_id)
    observed_digest = None
    if seal.commit_seq is not None and directory is not None:
        try:
            observed_digest = project_tree_digest(directory)
        except OSError:
            pass
    manifest = (
        owner._read_snapshot_manifest(conversation_id, declared, directory)
        if directory is not None
        else {}
    )
    return _SnapshotRead(
        manifest,
        directory,
        True,
        observed_digest=observed_digest,
    )


def _update_snapshot_stability(
    declared: list[str],
    manifest: dict[str, Any],
    previous: dict[str, Any],
    stable_counts: dict[str, int],
) -> None:
    for path in declared:
        identity = _manifest_ident(manifest, path)
        stable_counts[path] = (
            stable_counts.get(path, 1) + 1 if path in previous and previous[path] == identity else 1
        )
        previous[path] = identity


def _warn_unproven_snapshot(
    conversation_id: str,
    paths: list[str],
    expected: dict[str, tuple[Any, ...]],
) -> None:
    for path in paths:
        _LOG.warning(
            "snapshot %s: declared file %r accepted on EXTENDED "
            "content-stability (%s) — no sha/readback proof of the agent's "
            "final bytes (expected=%s)",
            conversation_id,
            path,
            expected.get(path, ("unknown",))[0],
            list(expected.get(path, ("unknown",))),
        )


def _snapshot_not_ready_details(
    *,
    conversation_id: str,
    wait_s: float,
    seal: _SnapshotSeal,
    read: _SnapshotRead,
    manifest: dict[str, Any],
    expected: dict[str, tuple[Any, ...]],
    blocking: list[str],
) -> dict[str, Any]:
    return {
        "conversation_id": conversation_id,
        "snapshot_wait_s": wait_s,
        "snapshot_dir": str(read.directory) if read.directory else None,
        "terminal_seq": seal.terminal_seq,
        "event_evidence_valid": seal.event_evidence_valid,
        "workspace_version_seq": seal.commit_seq,
        "workspace_version_version_seq": seal.version_seq,
        "workspace_version_digest": seal.tree_digest,
        "final_seal_file_count": seal.file_count,
        "final_seal_total_bytes": seal.total_bytes,
        "latest_effect_seq": seal.latest_effect_seq,
        "final_seal_error": read.error,
        "observed_file_count": read.observed_file_count,
        "observed_total_bytes": read.observed_total_bytes,
        "observed_tree_digest": read.observed_digest,
        "unsatisfied": [
            {
                "path": path,
                "expected": list(expected.get(path, ("unknown",))),
                "on_disk_sha256": _manifest_sha(manifest, path),
                "present": _manifest_present(manifest, path),
            }
            for path in blocking
        ],
    }


def _raise_snapshot_deadline(
    *,
    conversation_id: str,
    wait_s: float,
    events: list[dict[str, Any]],
    finished_live: bool,
    seal: _SnapshotSeal,
    read: _SnapshotRead,
    manifest: dict[str, Any],
    expected: dict[str, tuple[Any, ...]],
    blocking: list[str],
) -> None:
    if seal.error == _NO_SYSTEM_FINISHED_TERMINAL and not finished_live:
        return
    if not blocking and read.commit_ready:
        return
    refusal = _typed_content_seal_refusal(events)
    if refusal is not None:
        raise FinishUnsealableContentError(
            "the product refused the final workspace seal on deterministic "
            "content it could not attribute (typed seal_incomplete_content disclosure)",
            {
                "conversation_id": conversation_id,
                "seal_refused_blocking": refusal,
                "finished_live": finished_live,
                "terminal_seq": seal.terminal_seq,
                "event_evidence_valid": seal.event_evidence_valid,
                "workspace_version_seq": seal.commit_seq,
                "final_seal_error": read.error,
                "latest_effect_seq": seal.latest_effect_seq,
            },
        )
    raise SnapshotNotReadyError(
        f"workspace snapshot never reached the agent's final state within {wait_s:g}s",
        _snapshot_not_ready_details(
            conversation_id=conversation_id,
            wait_s=wait_s,
            seal=seal,
            read=read,
            manifest=manifest,
            expected=expected,
            blocking=blocking,
        ),
    )


def _stamp_snapshot_proof(
    *,
    declared: list[str],
    manifest: dict[str, Any],
    expected: dict[str, tuple[Any, ...]],
    stable_counts: dict[str, int],
    stable_polls: int,
    final_tree_proven: bool,
) -> None:
    for path in declared:
        entry = _manifest_lookup(manifest, path)
        if entry is None:
            continue
        entry["proof"] = (
            "final_tree_seal" if final_tree_proven else _proof_level(expected.get(path))
        )
        entry["content_stable"] = stable_counts.get(path, 0) >= stable_polls


async def _snapshot_finished_live(owner: Any, conversation_id: str) -> bool:
    """Unknown live status takes the strict path."""
    try:
        status = owner._status_of(await owner.get_state(conversation_id))
    except Exception:  # noqa: BLE001
        return True
    return status in {"FINISHED", "VERIFIED"}


def _current_snapshot_events(
    owner: Any,
    conversation_id: str,
    prior_validity: bool,
) -> tuple[list[dict[str, Any]], bool]:
    try:
        return normalize_events(owner.collect_events(conversation_id)), True
    except (sqlite3.Error, NormalizationError):
        return [], prior_validity


def _snapshot_should_accept(
    *,
    ready: bool,
    commit_ready: bool,
    commit_required: bool,
    unproven_ready: list[str],
    deadline: float,
) -> bool:
    return bool(
        ready
        and commit_ready
        and (commit_required or not unproven_ready or time.monotonic() >= deadline)
    )


def _finish_snapshot_capture(
    owner: Any,
    conversation_id: str,
    declared: list[str],
    expected: dict[str, tuple[Any, ...]],
    stable_counts: dict[str, int],
    read: _SnapshotRead,
) -> tuple[dict[str, Any], Path | None]:
    final_tree_proven = bool(
        owner._require_workspace_commit
        and read.commit_ready
        and read.directory is not None
        and read.error is None
    )
    _stamp_snapshot_proof(
        declared=declared,
        manifest=read.manifest,
        expected=expected,
        stable_counts=stable_counts,
        stable_polls=owner._snapshot_unproven_stable_polls(),
        final_tree_proven=final_tree_proven,
    )
    if read.directory is not None:
        owner._collected_workspace_dirs[conversation_id] = read.directory
    return read.manifest, read.directory


class _SnapshotMixin(_ClientBase):
    async def _await_ready_snapshot(
        self, conversation_id: str, declared: list[str]
    ) -> tuple[dict[str, Any], Path | None]:
        """Return the settled snapshot bound to the strongest available durable proof."""
        try:
            initial_events = self.collect_events(conversation_id)
        except sqlite3.Error:
            initial_events = []
        expected = _agent_declared_expected(initial_events, declared)
        deadline = time.monotonic() + self._snapshot_wait_s
        previous_identity: dict[str, Any] = {}
        stable_counts: dict[str, int] = {}
        manifest: dict[str, Any] = {}
        event_evidence_valid = False
        finished_live = await _snapshot_finished_live(self, conversation_id)
        seal = _SnapshotSeal(False, None, None, None, None, None, None, None, None)
        read = _SnapshotRead({}, None, not self._require_workspace_commit)

        while True:
            current_events, event_evidence_valid = _current_snapshot_events(
                self,
                conversation_id,
                event_evidence_valid,
            )
            if self._require_workspace_commit:
                seal = _strict_snapshot_seal(current_events, conversation_id)
                read = _strict_snapshot_read(self, conversation_id, declared, seal)
                read = _post_verify_strict_snapshot(
                    self,
                    conversation_id,
                    seal,
                    read,
                )
            else:
                seal = _legacy_snapshot_seal(
                    current_events,
                    event_evidence_valid,
                )
                event_evidence_valid = seal.event_evidence_valid
                read = _legacy_snapshot_read(
                    self,
                    conversation_id,
                    declared,
                    seal,
                )
            manifest = read.manifest
            _update_snapshot_stability(
                declared,
                manifest,
                previous_identity,
                stable_counts,
            )
            ready, blocking, unproven_ready = self._evaluate_snapshot_readiness(
                declared,
                expected,
                manifest,
                stable_counts,
            )
            if _snapshot_should_accept(
                ready=ready,
                commit_ready=read.commit_ready,
                commit_required=self._require_workspace_commit,
                unproven_ready=unproven_ready,
                deadline=deadline,
            ):
                if not self._require_workspace_commit:
                    _warn_unproven_snapshot(
                        conversation_id,
                        unproven_ready,
                        expected,
                    )
                break
            if time.monotonic() >= deadline:
                _raise_snapshot_deadline(
                    conversation_id=conversation_id,
                    wait_s=self._snapshot_wait_s,
                    events=current_events,
                    finished_live=finished_live,
                    seal=seal,
                    read=read,
                    manifest=manifest,
                    expected=expected,
                    blocking=blocking,
                )
                break
            await asyncio.sleep(_SNAPSHOT_POLL_S)

        return _finish_snapshot_capture(
            self,
            conversation_id,
            declared,
            expected,
            stable_counts,
            read,
        )

    def _read_snapshot_manifest(
        self, conversation_id: str, declared: list[str], ws: Path | None = None
    ) -> dict[str, Any]:
        """Walk the host ProjectStore snapshot workspace for `conversation_id` and
        build the per-file manifest. Symlink-jailed (resolve + is_relative_to) so a
        planted ``leak.html -> /etc/passwd`` can never escape the workspace. Empty dict
        when no snapshot exists yet (caller retries / falls back to the preview proxy)."""
        if ws is None:
            ws = self._snapshot_workspace_dir(conversation_id)
        if ws is None:
            return {}
        try:
            # Compare canonical paths on both sides of the symlink jail.  Codex's
            # temp root is exposed through ``/home`` while files resolve through
            # ``/var/home``; mixing those spellings otherwise rejects every
            # legitimate file as outside the workspace.
            ws = ws.resolve(strict=True)
        except OSError:
            return {}
        manifest: dict[str, Any] = {}
        count = 0
        for root, _dirs, names in os.walk(ws):  # followlinks=False → no dir-symlink escape
            for name in sorted(names):
                fp = Path(root) / name
                try:
                    resolved = fp.resolve()
                    if not resolved.is_relative_to(ws) or not resolved.is_file():
                        continue
                    rel = resolved.relative_to(ws).as_posix()
                    data = resolved.read_bytes()
                except OSError:
                    continue
                manifest[rel] = _file_entry(data)
                count += 1
                if count >= _WS_MANIFEST_MAX_FILES:
                    break
            if count >= _WS_MANIFEST_MAX_FILES:
                break
        # Guarantee each DECLARED path is keyed by its EXACT scenario string (the oracle
        # checks `spec["path"] in files`); the walk keys by the leading-slash-free relpath.
        for path in declared:
            rel = path.lstrip("/")
            if path not in manifest and rel in manifest:
                manifest[path] = manifest[rel]
        return manifest
