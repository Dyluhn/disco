"""Server-side uploaded-file ownership for sandbox recreation."""

from __future__ import annotations

from pathlib import Path


class UploadStore:
    """Persist and inspect per-conversation uploads outside ephemeral sandboxes."""

    def __init__(self, base_path: str) -> None:
        self._base = Path(base_path) if base_path else None

    def store(self, conversation_id: str, filename: str, data: bytes) -> None:
        if self._base is None:
            return
        path = self._base / conversation_id / filename
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)

    def names(self, conversation_id: str) -> set[str]:
        directory = self.directory(conversation_id)
        if directory is None:
            return set()
        return {path.name for path in directory.iterdir() if path.is_file()}

    def size(self, conversation_id: str) -> int:
        directory = self.directory(conversation_id)
        if directory is None:
            return 0
        return sum(path.stat().st_size for path in directory.iterdir() if path.is_file())

    def directory(self, conversation_id: str) -> Path | None:
        if self._base is None:
            return None
        directory = self._base / conversation_id
        return directory if directory.is_dir() else None
