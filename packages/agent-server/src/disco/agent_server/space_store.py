"""Persistent named Spaces for organizing conversations.

Spaces are user-named folders stored under the configured projects root. The
registry is intentionally a JSON directory, mirroring workflow instances. Old
Space vector namespaces may still exist on disk and are cleaned up on delete,
but new Space records no longer describe a grounding corpus.
"""

from __future__ import annotations

import json
import os
import re
import uuid
from datetime import UTC, datetime
from pathlib import Path

from disco.core.store.sqlite import install_owner_id
from pydantic import BaseModel, ConfigDict, Field

_SAFE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,119}$")


def _now_iso() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


class SpaceRecord(BaseModel):
    model_config = ConfigDict(frozen=True, extra="ignore")

    space_id: str
    owner_id: str = Field(default_factory=install_owner_id)
    name: str
    description: str = ""
    created_at: str
    member_count: int = 0
    legacy_unclaimed_owner: bool = False

    def summary(self) -> dict:
        return {
            "space_id": self.space_id,
            "owner_id": self.owner_id,
            "name": self.name,
            "description": self.description,
            "created_at": self.created_at,
            "member_count": self.member_count,
        }

    def detail(self) -> dict:
        return self.summary()


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

    def _load_record(self, path: Path) -> SpaceRecord:
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            raise ValueError("space record must be a JSON object")
        raw_owner = data.get("owner_id")
        legacy_unclaimed = not isinstance(raw_owner, str) or not raw_owner.strip()
        if legacy_unclaimed:
            data["owner_id"] = install_owner_id()
        row = SpaceRecord.model_validate(data)
        return row.model_copy(update={"legacy_unclaimed_owner": legacy_unclaimed})

    def list_spaces(
        self,
        *,
        owner_id: str | None = None,
        include_unclaimed_legacy: bool = False,
    ) -> list[SpaceRecord]:
        if not self._dir.is_dir():
            return []
        rows: list[SpaceRecord] = []
        for path in sorted(self._dir.glob("*.json")):
            if not _SAFE_ID.fullmatch(path.stem):
                continue
            try:
                row = self._load_record(path)
            except (OSError, ValueError):
                continue
            if row.legacy_unclaimed_owner and not include_unclaimed_legacy:
                continue
            if owner_id is None or row.owner_id == owner_id:
                rows.append(row)
        return sorted(rows, key=lambda row: row.created_at, reverse=True)

    def get(
        self,
        space_id: str,
        *,
        owner_id: str | None = None,
        include_unclaimed_legacy: bool = False,
    ) -> SpaceRecord | None:
        path = self._path_for(space_id)
        if not path.is_file():
            return None
        row = self._load_record(path)
        if row.legacy_unclaimed_owner and not include_unclaimed_legacy:
            return None
        if owner_id is not None and row.owner_id != owner_id:
            return None
        return row

    def create(
        self, *, name: str, description: str = "", owner_id: str | None = None
    ) -> SpaceRecord:
        clean_name = name.strip()
        if not clean_name:
            raise ValueError("space name is required")
        record = SpaceRecord(
            space_id=f"space_{uuid.uuid4().hex}",
            owner_id=owner_id or install_owner_id(),
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
            json.dumps(
                record.model_dump(
                    mode="json",
                    exclude={"member_count", "legacy_unclaimed_owner"},
                ),
                indent=2,
                sort_keys=True,
            ),
            encoding="utf-8",
        )
        tmp.replace(path)
        return path

    def rename(
        self,
        space_id: str,
        *,
        owner_id: str | None = None,
        name: str | None = None,
        description: str | None = None,
        include_unclaimed_legacy: bool = False,
    ) -> SpaceRecord:
        record = self.get(
            space_id,
            owner_id=owner_id,
            include_unclaimed_legacy=include_unclaimed_legacy,
        )
        if record is None:
            raise KeyError(space_id)
        clean_name = record.name if name is None else name.strip()
        if not clean_name:
            raise ValueError("space name is required")
        updated = record.model_copy(
            update={
                "name": clean_name[:120],
                "description": (
                    record.description if description is None else description.strip()[:2000]
                ),
            }
        )
        self.save(updated)
        return updated

    def delete(
        self,
        space_id: str,
        *,
        owner_id: str | None = None,
        include_unclaimed_legacy: bool = False,
    ) -> bool:
        if (
            owner_id is not None
            and self.get(
                space_id,
                owner_id=owner_id,
                include_unclaimed_legacy=include_unclaimed_legacy,
            )
            is None
        ):
            return False
        path = self._path_for(space_id)
        try:
            path.unlink()
            return True
        except FileNotFoundError:
            return False


__all__ = ["JsonSpaceStore", "SpaceRecord"]
