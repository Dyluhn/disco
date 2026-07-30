"""One typed, versioned repository for durable Agent runtime settings."""

from __future__ import annotations

import json
import os
import tempfile
import threading
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError


class RuntimeSettingsCorruptionError(RuntimeError):
    """The authoritative runtime settings document is invalid."""


class RuntimeSettingsVersionError(RuntimeError):
    """The runtime settings document uses an unsupported future version."""


class RuntimeSettingsDocument(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: Literal[1] = 1
    model_overrides: dict[str, str] = Field(default_factory=dict)
    autonomous: dict[str, bool] = Field(default_factory=dict)
    assist: dict[str, bool] = Field(default_factory=dict)
    quiet: dict[str, bool] = Field(default_factory=dict)
    surfaces: dict[str, str] = Field(default_factory=dict)
    last_selected_model: str | None = None


class RuntimeSettingsRepository:
    """Own the v1 document and rollback-readable legacy projections."""

    def __init__(self, db_path: str) -> None:
        self._path = Path(f"{db_path}.runtime-settings.json") if db_path else None
        self._legacy_paths = {
            "model_overrides": Path(f"{db_path}.overrides.json") if db_path else None,
            "autonomous": Path(f"{db_path}.autonomous.json") if db_path else None,
            "assist": Path(f"{db_path}.assist.json") if db_path else None,
            "quiet": Path(f"{db_path}.quiet.json") if db_path else None,
            "surfaces": Path(f"{db_path}.surfaces.json") if db_path else None,
            "last_selected_model": Path(f"{db_path}.last_model.json") if db_path else None,
        }
        self._lock = threading.RLock()
        self._document = self._load()

    @property
    def document(self) -> RuntimeSettingsDocument:
        return self._document

    def legacy_path(self, field: str) -> str:
        path = self._legacy_paths[field]
        return "" if path is None else str(path)

    def _load(self) -> RuntimeSettingsDocument:
        if self._path is not None and self._path.exists():
            try:
                raw = json.loads(self._path.read_text(encoding="utf-8"))
            except (ValueError, OSError) as exc:
                raise RuntimeSettingsCorruptionError(f"cannot decode {self._path}") from exc
            if isinstance(raw, dict) and raw.get("schema_version") != 1:
                raise RuntimeSettingsVersionError(
                    f"{self._path} has unsupported schema_version {raw.get('schema_version')!r}"
                )
            try:
                return RuntimeSettingsDocument.model_validate(raw)
            except ValidationError as exc:
                raise RuntimeSettingsCorruptionError(
                    f"invalid runtime settings in {self._path}"
                ) from exc
        return self._load_legacy()

    def _load_legacy(self) -> RuntimeSettingsDocument:
        values: dict[str, object] = {}
        for field in ("model_overrides", "autonomous", "assist", "quiet", "surfaces"):
            raw = self._read_legacy(field)
            if not isinstance(raw, dict):
                continue
            if field in {"autonomous", "assist", "quiet"}:
                values[field] = {str(key): bool(value) for key, value in raw.items()}
            else:
                values[field] = {
                    str(key): str(value)
                    for key, value in raw.items()
                    if value and isinstance(value, str)
                }
        last = self._read_legacy("last_selected_model")
        if isinstance(last, dict) and isinstance(last.get("model"), str):
            values["last_selected_model"] = last["model"] or None
        return RuntimeSettingsDocument.model_validate(values)

    def _read_legacy(self, field: str) -> object | None:
        path = self._legacy_paths[field]
        if path is None:
            return None
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (FileNotFoundError, ValueError, OSError):
            return None

    def _replace(self, **updates: object) -> None:
        with self._lock:
            payload = self._document.model_dump(mode="json")
            payload.update(updates)
            document = RuntimeSettingsDocument.model_validate(payload)
            self._save(document)
            self._document = document

    def _map(self, field: str) -> dict[str, str] | dict[str, bool]:
        value = getattr(self._document, field)
        if not isinstance(value, dict):  # pragma: no cover - model invariant
            raise RuntimeSettingsCorruptionError(f"{field} is not a mapping")
        return value

    def set_value(self, field: str, key: str, value: str | bool | None) -> None:
        mapping = dict(self._map(field))
        if value is None:
            mapping.pop(key, None)
        else:
            mapping[key] = value
        self._replace(**{field: mapping})

    def set_last_selected_model(self, model_id: str | None) -> None:
        self._replace(last_selected_model=model_id)

    def replace_mapping(
        self,
        field: str,
        mapping: dict[str, str] | dict[str, bool],
    ) -> None:
        self._replace(**{field: dict(mapping)})

    def forget(self, conversation_id: str) -> None:
        updates: dict[str, object] = {}
        for field in (
            "model_overrides",
            "autonomous",
            "assist",
            "quiet",
            "surfaces",
        ):
            mapping = dict(self._map(field))
            mapping.pop(conversation_id, None)
            updates[field] = mapping
        self._replace(**updates)

    def save_current(self) -> None:
        with self._lock:
            self._save(self._document)

    def _save(self, document: RuntimeSettingsDocument) -> None:
        if self._path is None:
            return
        self._atomic_write(self._path, document.model_dump(mode="json"))
        legacy_payloads: dict[str, object] = {
            "model_overrides": document.model_overrides,
            "autonomous": document.autonomous,
            "assist": document.assist,
            "quiet": document.quiet,
            "surfaces": document.surfaces,
            "last_selected_model": {"model": document.last_selected_model},
        }
        for field, payload in legacy_payloads.items():
            path = self._legacy_paths[field]
            if path is not None:
                self._atomic_write(path, payload)

    def _atomic_write(self, path: Path, payload: object) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(
            "w",
            encoding="utf-8",
            dir=path.parent,
            prefix=f".{path.name}.",
            delete=False,
        ) as handle:
            json.dump(payload, handle)
            handle.flush()
            os.fsync(handle.fileno())
            temporary = Path(handle.name)
        temporary.replace(path)


class RuntimePersistentSettings:
    """Compatibility API backed by the single versioned settings repository."""

    _settings_repository: RuntimeSettingsRepository
    _model_overrides: dict[str, str]
    _autonomous: dict[str, bool]
    _assist: dict[str, bool]
    _quiet: dict[str, bool]

    def _effective_autonomous(self, conversation_id: str) -> bool:
        raise NotImplementedError

    def _effective_quiet(self, conversation_id: str) -> bool:
        raise NotImplementedError

    def _effective_assist(self, conversation_id: str) -> bool:
        raise NotImplementedError

    def _save_mapping(
        self,
        field: str,
        mapping: dict[str, str] | dict[str, bool],
    ) -> None:
        try:
            self._settings_repository.replace_mapping(field, mapping)
        except Exception:  # noqa: BLE001
            # Preserve the historical best-effort sidecar contract. Atomic
            # replacement ensures any prior persisted bytes remain intact.
            return

    def _load_overrides(self) -> dict[str, str]:
        return dict(self._settings_repository.document.model_overrides)

    def _save_overrides(self) -> None:
        self._save_mapping("model_overrides", self._model_overrides)

    def _set_model_override_unlocked(
        self,
        conversation_id: str,
        model_id: str | None,
    ) -> None:
        if model_id:
            self._model_overrides[conversation_id] = model_id
            self._save_overrides()
            self.set_last_selected_model(model_id)

    def _clear_model_override_unlocked(self, conversation_id: str) -> None:
        if self._model_overrides.pop(conversation_id, None) is not None:
            self._save_overrides()

    def set_model_override(self, conversation_id: str, model_id: str | None) -> None:
        self._set_model_override_unlocked(conversation_id, model_id)

    def get_last_selected_model(self) -> str | None:
        if not self._settings_repository.legacy_path("last_selected_model"):
            return None
        return self._settings_repository.document.last_selected_model

    def set_last_selected_model(self, model_id: str | None) -> None:
        if not self._settings_repository.legacy_path("last_selected_model"):
            return
        try:
            self._settings_repository.set_last_selected_model(model_id)
        except Exception:  # noqa: BLE001
            return

    def _load_autonomous(self) -> dict[str, bool]:
        return dict(self._settings_repository.document.autonomous)

    def _save_autonomous(self) -> None:
        self._save_mapping("autonomous", self._autonomous)

    def set_autonomous(self, conversation_id: str, value: bool = True) -> None:
        self._autonomous[conversation_id] = bool(value)
        self._save_autonomous()

    def is_autonomous(self, conversation_id: str) -> bool:
        return self._effective_autonomous(conversation_id)

    def _load_quiet(self) -> dict[str, bool]:
        return dict(self._settings_repository.document.quiet)

    def _save_quiet(self) -> None:
        self._save_mapping("quiet", self._quiet)

    def set_quiet(self, conversation_id: str, value: bool = True) -> None:
        self._quiet[conversation_id] = bool(value)
        self._save_quiet()

    def is_quiet(self, conversation_id: str) -> bool:
        return self._effective_quiet(conversation_id)

    def _load_assist(self) -> dict[str, bool]:
        return dict(self._settings_repository.document.assist)

    def _save_assist(self) -> None:
        self._save_mapping("assist", self._assist)

    def _set_assist_unlocked(self, conversation_id: str, value: bool) -> None:
        self._assist[conversation_id] = bool(value)
        self._save_assist()

    def set_assist(self, conversation_id: str, value: bool = True) -> None:
        self._set_assist_unlocked(conversation_id, value)

    def is_assist(self, conversation_id: str) -> bool:
        return self._effective_assist(conversation_id)
