"""`.disco/components.lock` — the workspace install record (spec §1.3).

The lockfile records WHICH (name, version) is installed plus eject history —
NEVER hash pins (D1: pins live only in the host registry, so a forged lockfile
degrades to an honest state change, not a forged "verified"). Written and
updated only by host-side code (the install/eject tools and the verify path).
"""

from __future__ import annotations

import json

from pydantic import BaseModel, ConfigDict, Field

LOCKFILE_RELPATH = ".disco/components.lock"


class LockfileCorrupt(ValueError):
    """The lockfile exists but does not parse. Install tools REFUSE on this
    (silently overwriting would lose eject history); verify treats it as
    absent (no components → no claims → no badge — fail-closed and honest)."""


class InstalledComponent(BaseModel):
    version: str = Field(pattern=r"^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)$")
    installed_at: str
    installed_by: str = "add_trusted_component"
    ejected: bool = False


class EjectRecord(BaseModel):
    """Append-only divergence history: 'this copy diverged at <date>'."""

    name: str
    at: str
    reason: str  # "core-edit-detected" | "explicit" | "registry-version-missing"
    diverged_files: list[str] = Field(default_factory=list)
    note: str = ""  # free text from an explicit eject


class ComponentsLock(BaseModel):
    model_config = ConfigDict(validate_assignment=True)

    lockfile_version: int = Field(default=1, ge=1, le=1)
    components: dict[str, InstalledComponent] = Field(default_factory=dict)
    ejects: list[EjectRecord] = Field(default_factory=list)


def parse_lock(data: bytes | None) -> ComponentsLock:
    """None (no file) → empty lock. Unparsable/invalid → LockfileCorrupt."""
    if data is None:
        return ComponentsLock()
    try:
        return ComponentsLock.model_validate(json.loads(data.decode("utf-8")))
    except Exception as exc:  # noqa: BLE001 — every parse failure maps to one typed error
        raise LockfileCorrupt(f"{LOCKFILE_RELPATH} is corrupt: {exc}") from exc


def dump_lock(lock: ComponentsLock) -> bytes:
    return (json.dumps(lock.model_dump(mode="json"), indent=2, ensure_ascii=False) + "\n").encode(
        "utf-8"
    )


def record_install(lock: ComponentsLock, name: str, version: str, now_iso: str) -> None:
    """Fresh install entry (ejected=False). Eject HISTORY is never rewritten —
    a reinstall starts a new trusted lifetime but the past divergence stays
    on record (spec §1.3)."""
    lock.components[name] = InstalledComponent(version=version, installed_at=now_iso)


def record_eject(
    lock: ComponentsLock,
    name: str,
    reason: str,
    now_iso: str,
    *,
    diverged_files: list[str] | None = None,
    note: str = "",
) -> bool:
    """Mark `name` ejected + append the history record. Idempotent: an
    already-ejected component is left as-is and False is returned."""
    entry = lock.components.get(name)
    if entry is None or entry.ejected:
        return False
    lock.components[name] = entry.model_copy(update={"ejected": True})
    lock.ejects.append(
        EjectRecord(
            name=name,
            at=now_iso,
            reason=reason,
            diverged_files=sorted(diverged_files or []),
            note=note,
        )
    )
    return True
