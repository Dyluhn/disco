"""Persistent named research Spaces.

Spaces are user-named, durable corpora stored under the configured projects
root. The registry is intentionally a JSON directory, mirroring workflow
instances, while vectors live in the retrieval package's disk-backed vector
store under the same root.
"""

from __future__ import annotations

import json
import os
import re
import uuid
from datetime import UTC, datetime
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field

_SAFE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,119}$")


def _now_iso() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


class SpaceDocument(BaseModel):
    model_config = ConfigDict(frozen=True)

    document_id: str
    name: str
    media_type: str
    byte_count: int
    passage_count: int
    created_at: str


class SpaceRecord(BaseModel):
    model_config = ConfigDict(frozen=True)

    space_id: str
    name: str
    description: str = ""
    created_at: str
    doc_count: int = 0
    byte_count: int = 0
    documents: list[SpaceDocument] = Field(default_factory=list)

    def summary(self) -> dict:
        return {
            "space_id": self.space_id,
            "name": self.name,
            "description": self.description,
            "created_at": self.created_at,
            "doc_count": self.doc_count,
            "byte_count": self.byte_count,
        }

    def detail(self) -> dict:
        return {
            **self.summary(),
            "documents": [doc.model_dump(mode="json") for doc in self.documents],
        }


class JsonSpaceStore:
    """Read/write Space records from ``<projects_root>/spaces``."""

    def __init__(self, projects_root: str | Path) -> None:
        self._dir = Path(projects_root).expanduser() / "spaces"

    @property
    def spaces_dir(self) -> Path:
        return self._dir

    @property
    def vectors_dir(self) -> Path:
        return self._dir / "vectors"

    def _path_for(self, space_id: str) -> Path:
        if not _SAFE_ID.fullmatch(space_id):
            raise ValueError(f"unsafe space_id: {space_id!r}")
        return self._dir / f"{space_id}.json"

    def list_spaces(self) -> list[SpaceRecord]:
        if not self._dir.is_dir():
            return []
        rows: list[SpaceRecord] = []
        for path in sorted(self._dir.glob("*.json")):
            if not _SAFE_ID.fullmatch(path.stem):
                continue
            try:
                rows.append(SpaceRecord.model_validate_json(path.read_text(encoding="utf-8")))
            except (OSError, ValueError):
                continue
        return sorted(rows, key=lambda row: row.created_at, reverse=True)

    def get(self, space_id: str) -> SpaceRecord | None:
        path = self._path_for(space_id)
        if not path.is_file():
            return None
        raw = path.read_text(encoding="utf-8")
        return SpaceRecord.model_validate_json(raw)

    def create(self, *, name: str, description: str = "") -> SpaceRecord:
        clean_name = name.strip()
        if not clean_name:
            raise ValueError("space name is required")
        record = SpaceRecord(
            space_id=f"space_{uuid.uuid4().hex}",
            name=clean_name[:120],
            description=description.strip()[:2000],
            created_at=_now_iso(),
        )
        self.save(record)
        return record

    def save(self, record: SpaceRecord) -> Path:
        path = self._path_for(record.space_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(f".{path.name}.{os.getpid()}.{id(record)}.tmp")
        tmp.write_text(
            json.dumps(record.model_dump(mode="json"), indent=2, sort_keys=True),
            encoding="utf-8",
        )
        tmp.replace(path)
        return path

    def add_document(self, space_id: str, document: SpaceDocument) -> SpaceRecord:
        record = self.get(space_id)
        if record is None:
            raise KeyError(space_id)
        documents = [*record.documents, document]
        updated = record.model_copy(
            update={
                "documents": documents,
                "doc_count": len(documents),
                "byte_count": sum(doc.byte_count for doc in documents),
            }
        )
        self.save(updated)
        return updated

    def delete(self, space_id: str) -> bool:
        path = self._path_for(space_id)
        try:
            path.unlink()
            return True
        except FileNotFoundError:
            return False


__all__ = ["JsonSpaceStore", "SpaceDocument", "SpaceRecord"]
