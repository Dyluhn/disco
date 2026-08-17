"""Durable finalization-journal encoding and workspace seal events."""

from __future__ import annotations

import contextlib
import json
import os
import tempfile
from pathlib import Path
from typing import Any

from disco.core import FinalWorkspaceSeal, ResourceKey, WorkspaceVersionEvent
from disco.tools.projects import ProjectStore, VersionRecord, WorkspaceTreeFacts

FINALIZATION_JOURNAL = "finalization-v1.json"


def write_json_durable(path: Path, payload: dict[str, Any]) -> None:
    """Atomically replace a small recovery journal and fsync its directory."""

    path.parent.mkdir(parents=True, exist_ok=True)
    if path.parent.is_symlink() or not path.parent.is_dir():
        raise RuntimeError("finalization journal parent is missing or symlinked")
    fd, raw_tmp = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    tmp = Path(raw_tmp)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, sort_keys=True, separators=(",", ":"))
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
        directory_fd = os.open(path.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        with contextlib.suppress(FileNotFoundError):
            tmp.unlink()


def finalization_journal_path(store: ProjectStore, conversation_id: str) -> Path:
    return store.manifest_for(conversation_id).parent / FINALIZATION_JOURNAL


def write_finalization_journal(
    store: ProjectStore,
    conversation_id: str,
    payload: dict[str, Any],
) -> None:
    write_json_durable(finalization_journal_path(store, conversation_id), payload)


def clear_finalization_journal(store: ProjectStore, conversation_id: str) -> None:
    path = finalization_journal_path(store, conversation_id)
    with contextlib.suppress(FileNotFoundError):
        path.unlink()
        directory_fd = os.open(path.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)


def seal_event(
    conversation_id: str,
    fence: tuple[int, int | None],
    version: VersionRecord,
) -> WorkspaceVersionEvent:
    terminal_seq, latest_effect_seq = fence
    return WorkspaceVersionEvent(
        version_seq=version.seq,
        tree_digest=version.tree_digest,
        trigger="finish",
        final_seal=FinalWorkspaceSeal(
            scope=ResourceKey(namespace="workspace.tree", identifier=conversation_id),
            terminal_seq=terminal_seq,
            latest_effect_seq=latest_effect_seq,
            version_seq=version.seq,
            tree_digest=version.tree_digest,
            file_count=version.file_count,
            total_bytes=version.total_bytes,
        ),
    )


def checkpoint_event(
    fence: tuple[int, int | None],
    version: VersionRecord,
) -> WorkspaceVersionEvent:
    """SQLite-side provenance for crash recovery of one filesystem version."""

    terminal_seq, _latest_effect_seq = fence
    return WorkspaceVersionEvent(
        version_seq=version.seq,
        tree_digest=version.tree_digest,
        trigger=f"finalizing:{terminal_seq}",
    )


def journal_facts(journal: dict[str, Any]) -> WorkspaceTreeFacts:
    file_count = journal.get("file_count")
    total_bytes = journal.get("total_bytes")
    tree_digest = journal.get("tree_digest")
    if (
        type(file_count) is not int
        or file_count < 0
        or type(total_bytes) is not int
        or total_bytes < 0
        or not isinstance(tree_digest, str)
        or len(tree_digest) != 64
        or any(char not in "0123456789abcdef" for char in tree_digest)
    ):
        raise ValueError("finalization journal carries invalid workspace facts")
    return WorkspaceTreeFacts(
        file_count=file_count,
        total_bytes=total_bytes,
        tree_digest=tree_digest,
    )
