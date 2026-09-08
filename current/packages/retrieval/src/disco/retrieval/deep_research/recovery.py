"""Content-addressed research checkpoints, committed by references in the event log.

The files have no mutable 'current' pointer. Only a reference durably appended
to the conversation is a committed boundary. Large passage bodies are shared
between checkpoints; a partial or uncommitted file cannot become recovery state.
"""

from __future__ import annotations

import hashlib
import json
import re
import shutil
from pathlib import Path
from typing import Any

from ._durable_files import atomic_write_bytes, research_data_dir
from ._progress_events import RECOVERY_ACTION as RECOVERY_ACTION
from ._recovery_state import RecoveryCheckpoint

_IDENTIFIER = re.compile(r"[A-Za-z0-9_-]{1,128}")
_HASH = re.compile(r"[a-f0-9]{64}")


class ResearchRecoveryError(RuntimeError):
    """A committed checkpoint cannot be safely saved or reconstructed."""


def _directory(conversation_id: str) -> Path:
    if not _IDENTIFIER.fullmatch(conversation_id):
        raise ResearchRecoveryError("invalid checkpoint conversation identity")
    return research_data_dir() / "research-recovery" / conversation_id


def remove_research_artifacts(conversation_id: str) -> None:
    """Remove only this conversation's evidence after owner-authorized deletion.

    The caller must stop and drain the live run before deleting its files.
    Never sweep other conversations or follow a directory symlink.
    """
    directory = _directory(conversation_id)
    if directory.is_symlink():
        directory.unlink()
    elif directory.exists():
        shutil.rmtree(directory)
    (research_data_dir() / "pools" / f"{conversation_id}.json").unlink(missing_ok=True)


def _put(directory: Path, value: Any) -> str:
    payload = json.dumps(value, ensure_ascii=False, sort_keys=True).encode()
    digest = hashlib.sha256(payload).hexdigest()
    path = directory / f"{digest}.json"
    if not path.exists():
        atomic_write_bytes(path, payload)
    return digest


def _get(directory: Path, digest: str) -> Any:
    if not _HASH.fullmatch(digest):
        raise ResearchRecoveryError("invalid checkpoint content identifier")
    payload = (directory / f"{digest}.json").read_bytes()
    if hashlib.sha256(payload).hexdigest() != digest:
        raise ResearchRecoveryError("checkpoint content hash does not match")
    return json.loads(payload)


def write_checkpoint(checkpoint: RecoveryCheckpoint) -> dict[str, Any]:
    directory = _directory(checkpoint.conversation_id)
    document = checkpoint.model_dump(mode="json")
    try:
        passages = document["state"].pop("passages")
        document["state"]["passage_refs"] = [_put(directory / "passages", p) for p in passages]
        digest = _put(directory / "checkpoints", document)
    except (OSError, ValueError, TypeError) as exc:
        raise ResearchRecoveryError("could not persist the research boundary") from exc
    return {
        "schema_version": 1,
        "conversation_id": checkpoint.conversation_id,
        "run_id": checkpoint.run_id,
        "checkpoint_id": digest,
        "stage": checkpoint.stage,
        "turns_spent": checkpoint.state.turns_charged,
        "sources_spent": len(checkpoint.state.budget_ids),
    }


def read_checkpoint(conversation_id: str, reference: dict[str, Any]) -> RecoveryCheckpoint:
    if reference.get("conversation_id") != conversation_id or reference.get("schema_version") != 1:
        raise ResearchRecoveryError("checkpoint identity or version is not supported")
    directory = _directory(conversation_id)
    try:
        document = _get(directory / "checkpoints", str(reference.get("checkpoint_id", "")))
        refs = document["state"].pop("passage_refs")
        document["state"]["passages"] = [_get(directory / "passages", ref) for ref in refs]
        checkpoint = RecoveryCheckpoint.model_validate(document)
    except (OSError, ValueError, TypeError, KeyError) as exc:
        raise ResearchRecoveryError("committed research checkpoint is unreadable") from exc
    if checkpoint.conversation_id != conversation_id or checkpoint.run_id != reference.get(
        "run_id"
    ):
        raise ResearchRecoveryError("checkpoint identity does not match its event reference")
    return checkpoint
