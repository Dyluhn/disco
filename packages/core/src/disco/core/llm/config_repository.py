"""Versioned general/router persistence and source-compatible adapters."""

from __future__ import annotations

import json
import os
import tempfile
import threading
from collections.abc import Mapping
from pathlib import Path
from types import TracebackType

from .config import RouterConfig

try:
    import fcntl
except ImportError:  # pragma: no cover - Windows compatibility
    fcntl = None  # type: ignore[assignment]

SCHEMA_VERSION = 1

GENERAL_SETTINGS_FIELDS = (
    "sandbox",
    "projects",
    "encoders",
    "tts",
    "search",
    "extraction",
    "image_gen",
    "mcp",
    "trusted_origins",
    "security_diagnostics",
    "live_browser",
    "role_fallback",
    "build_kernel",
)


class ConfigRepositoryError(RuntimeError):
    """Base class for authoritative configuration repository failures."""


class ConfigRepositoryCorruptionError(ConfigRepositoryError):
    """An authoritative versioned repository cannot be decoded."""


class UnsupportedConfigVersionError(ConfigRepositoryError):
    """A repository was written by an unsupported future schema."""


def _atomic_write_json(path: Path, payload: Mapping[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        "w",
        encoding="utf-8",
        dir=path.parent,
        prefix=f".{path.name}.",
        delete=False,
    ) as handle:
        json.dump(payload, handle, indent=2)
        handle.flush()
        os.fsync(handle.fileno())
        temporary = Path(handle.name)
    temporary.replace(path)


def _sidecar_path(path: Path, domain: str) -> Path:
    suffix = path.suffix or ".json"
    stem = path.name[: -len(suffix)] if path.suffix else path.name
    return path.with_name(f"{stem}.{domain}{suffix}")


class RouterDocumentRepository:
    """Rollback-compatible flattened router document."""

    def __init__(self, path: Path) -> None:
        self.path = path

    def load(self) -> dict[str, object] | None:
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (FileNotFoundError, ValueError, OSError):
            return None
        if not isinstance(raw, dict):
            return None
        version = raw.get("schema_version")
        if version is not None and (type(version) is not int or version != SCHEMA_VERSION):
            raise UnsupportedConfigVersionError(
                f"{self.path} has unsupported schema_version {version!r}"
            )
        return raw

    def save(self, config: RouterConfig) -> None:
        payload = config.model_dump(mode="json")
        payload["schema_version"] = SCHEMA_VERSION
        _atomic_write_json(self.path, payload)


class _VersionedSidecar:
    def __init__(self, path: Path, section: str) -> None:
        self.path = path
        self._section = section

    def _load(self) -> dict[str, object] | None:
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return None
        except (ValueError, OSError) as exc:
            raise ConfigRepositoryCorruptionError(f"cannot decode {self.path}") from exc
        if not isinstance(raw, dict):
            raise ConfigRepositoryCorruptionError(f"{self.path} must contain an object")
        version = raw.get("schema_version")
        if type(version) is not int or version != SCHEMA_VERSION:
            raise UnsupportedConfigVersionError(
                f"{self.path} has unsupported schema_version {version!r}"
            )
        section = raw.get(self._section)
        if not isinstance(section, dict):
            raise ConfigRepositoryCorruptionError(
                f"{self.path} has no object section {self._section!r}"
            )
        return section

    def _save(self, values: Mapping[str, object]) -> None:
        _atomic_write_json(
            self.path,
            {"schema_version": SCHEMA_VERSION, self._section: dict(values)},
        )


class GeneralSettingsRepository(_VersionedSidecar):
    """Independent owner of non-provider application settings."""

    def __init__(self, router_path: Path) -> None:
        super().__init__(_sidecar_path(router_path, "settings"), "settings")

    def load(self) -> dict[str, object] | None:
        return self._load()

    def save(self, config: RouterConfig) -> None:
        raw = config.model_dump(mode="json")
        self._save({field: raw[field] for field in GENERAL_SETTINGS_FIELDS})


_LOCKS_GUARD = threading.Lock()
_LOCKS: dict[Path, threading.RLock] = {}


def _process_lock(path: Path) -> threading.RLock:
    resolved = path.resolve()
    with _LOCKS_GUARD:
        return _LOCKS.setdefault(resolved, threading.RLock())


class ConfigTransaction:
    """Serialize read-modify-write cycles across threads and POSIX processes."""

    def __init__(self, router_path: Path) -> None:
        self._lock_path = _sidecar_path(router_path, "lock")
        self._process_lock = _process_lock(self._lock_path)
        self._handle: object | None = None

    def __enter__(self) -> ConfigTransaction:
        self._process_lock.acquire()
        self._lock_path.parent.mkdir(parents=True, exist_ok=True)
        self._handle = self._lock_path.open("a+", encoding="utf-8")
        if fcntl is not None:
            fcntl.flock(self._handle.fileno(), fcntl.LOCK_EX)  # type: ignore[union-attr]
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        try:
            if self._handle is not None and fcntl is not None:
                fcntl.flock(self._handle.fileno(), fcntl.LOCK_UN)  # type: ignore[union-attr]
            if self._handle is not None:
                self._handle.close()  # type: ignore[union-attr]
        finally:
            self._handle = None
            self._process_lock.release()
