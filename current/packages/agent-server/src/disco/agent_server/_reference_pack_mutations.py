"""Mutation operations for the Reference Pack store.

The durable store keeps read/query mechanics separate from its atomic
create/update/delete transactions while sharing the same instance state.
"""

# pyright: reportAttributeAccessIssue=false

from __future__ import annotations

import hashlib
import os
import secrets
import shutil
from typing import Any
from uuid import uuid4

from .reference_pack_store import (
    ReferencePack,
    ReferencePackError,
    ReferencePackLimits,
    ReferencePackNotFound,
    ReferencePackVersion,
    _atomic_json,
    _canonical_inputs,
    _canonical_inputs_async,
    _canonical_path,
    _file_media_type,
    _synchronized_mutation,
)


class ReferencePackMutationMixin:
    @_synchronized_mutation
    def create(
        self,
        owner_id: str,
        name: str,
        description: str,
        files: list[Any] | tuple[Any, ...],
        *,
        reader: Any,
    ) -> ReferencePack:
        name, description = self._validate_text(name, description, self.limits)
        selected = _canonical_inputs(files, reader, self.limits)
        pack_id = uuid4().hex
        version = self._write_version(owner_id, pack_id, name, description, selected)
        created = version.created_at
        try:
            _atomic_json(
                self._pack_root(owner_id, pack_id) / "head.json",
                {
                    "id": pack_id,
                    "owner_id": owner_id,
                    "name": name,
                    "description": description,
                    "current_version_id": version.id,
                    "created_at": created,
                    "updated_at": created,
                },
            )
        except Exception:
            shutil.rmtree(self._pack_root(owner_id, pack_id), ignore_errors=True)
            raise
        return self.get(owner_id, pack_id)

    @_synchronized_mutation
    async def acreate(
        self,
        owner_id: str,
        name: str,
        description: str,
        files: list[Any] | tuple[Any, ...],
        *,
        reader: Any,
    ) -> ReferencePack:
        """Production ingress: host stats and async reads precede the atomic commit."""
        name, description = self._validate_text(name, description, self.limits)
        selected = await _canonical_inputs_async(files, reader, self.limits)
        pack_id = uuid4().hex
        version = self._write_version(owner_id, pack_id, name, description, selected)
        created = version.created_at
        try:
            _atomic_json(
                self._pack_root(owner_id, pack_id) / "head.json",
                {
                    "id": pack_id,
                    "owner_id": owner_id,
                    "name": name,
                    "description": description,
                    "current_version_id": version.id,
                    "created_at": created,
                    "updated_at": created,
                },
            )
        except Exception:
            shutil.rmtree(self._pack_root(owner_id, pack_id), ignore_errors=True)
            raise
        return self.get(owner_id, pack_id)

    def get(self, owner_id: str, pack_id: str) -> ReferencePack:
        head = self._load_head(owner_id, pack_id)
        version = self._load_version(owner_id, pack_id, str(head["current_version_id"]))
        return ReferencePack(
            id=str(head["id"]),
            owner_id=str(head["owner_id"]),
            name=str(head["name"]),
            description=str(head.get("description") or version.description),
            current_version_id=version.id,
            current=version,
            created_at=str(head.get("created_at") or version.created_at),
            updated_at=str(head.get("updated_at") or version.created_at),
        )

    def get_version(self, owner_id: str, pack_id: str, version_id: str) -> ReferencePackVersion:
        return self._load_version(owner_id, pack_id, version_id)

    def read_version_file(self, owner_id: str, pack_id: str, version_id: str, name: str) -> bytes:
        version = self.get_version(owner_id, pack_id, version_id)
        item = next((entry for entry in version.files if entry.name == name), None)
        if item is None:
            raise ReferencePackNotFound(name)
        # Manifests are durable data, so do not allow a corrupted/tampered
        # entry to turn a read into a path traversal or symlink follow.
        safe_name = _canonical_path(item.name)
        version_root = self._pack_root(owner_id, pack_id) / "versions" / version_id
        path = version_root / "files" / safe_name
        try:
            resolved = path.resolve(strict=True)
            files_root = (version_root / "files").resolve(strict=True)
            resolved.relative_to(files_root)
        except (FileNotFoundError, OSError, ValueError) as exc:
            raise ReferencePackError("immutable Reference Pack file path is invalid") from exc
        if path.is_symlink() or not path.is_file():
            raise ReferencePackError("immutable Reference Pack file is not a regular file")
        data = path.read_bytes()
        if hashlib.sha256(data).hexdigest() != item.sha256:
            raise ReferencePackError("immutable Reference Pack bytes failed hash verification")
        return data

    def list(self, owner_id: str) -> tuple[ReferencePack, ...]:
        owner_root = self._owner_root(owner_id)
        if not owner_root.is_dir():
            return ()
        result: list[ReferencePack] = []
        for child in sorted(owner_root.iterdir(), key=lambda p: p.name):
            if child.is_dir() and (child / "head.json").is_file():
                try:
                    result.append(self.get(owner_id, child.name))
                except ReferencePackNotFound:
                    continue
        return tuple(result)

    @_synchronized_mutation
    def update(
        self,
        owner_id: str,
        pack_id: str,
        *,
        name: str | None = None,
        description: str | None = None,
        files: list[Any] | tuple[Any, ...] | None = None,
        reader: Any | None = None,
    ) -> ReferencePack:
        current = self.get(owner_id, pack_id)
        next_name, next_description = self._validate_text(
            name if name is not None else current.name,
            description if description is not None else current.description,
            self.limits,
        )
        if files is None:
            selected = tuple(
                (
                    item.name,
                    self.read_version_file(
                        owner_id, pack_id, current.current_version_id, item.name
                    ),
                    item.media_type,
                )
                for item in current.current.files
            )
        else:
            selected = _canonical_inputs(files, reader, self.limits)
        previous_version_id = current.current_version_id
        version = self._write_version(owner_id, pack_id, next_name, next_description, selected)
        head = self._load_head(owner_id, pack_id)
        head.update(
            {
                "name": next_name,
                "description": next_description,
                "current_version_id": version.id,
                "updated_at": version.created_at,
            }
        )
        try:
            _atomic_json(self._pack_root(owner_id, pack_id) / "head.json", head)
        except Exception:
            self._remove_version(owner_id, pack_id, version.id)
            raise
        # The library deliberately has one mutable head, not a user-visible
        # version-history product. Builds that already selected this pack own
        # their copied snapshot, so the replaced library bytes are no longer an
        # authority and can be removed after the atomic head swap.
        if previous_version_id != version.id:
            self._remove_version(owner_id, pack_id, previous_version_id)
        return self.get(owner_id, pack_id)

    @_synchronized_mutation
    async def aupdate(
        self,
        owner_id: str,
        pack_id: str,
        *,
        name: str | None = None,
        description: str | None = None,
        files: list[Any] | tuple[Any, ...] | None = None,
        reader: Any | None = None,
    ) -> ReferencePack:
        current = self.get(owner_id, pack_id)
        next_name, next_description = self._validate_text(
            name if name is not None else current.name,
            description if description is not None else current.description,
            self.limits,
        )
        if files is None:
            selected = tuple(
                (
                    item.name,
                    self.read_version_file(
                        owner_id, pack_id, current.current_version_id, item.name
                    ),
                    item.media_type,
                )
                for item in current.current.files
            )
        else:
            selected = await _canonical_inputs_async(files, reader, self.limits)
        previous_version_id = current.current_version_id
        version = self._write_version(owner_id, pack_id, next_name, next_description, selected)
        head = self._load_head(owner_id, pack_id)
        head.update(
            {
                "name": next_name,
                "description": next_description,
                "current_version_id": version.id,
                "updated_at": version.created_at,
            }
        )
        try:
            _atomic_json(self._pack_root(owner_id, pack_id) / "head.json", head)
        except Exception:
            self._remove_version(owner_id, pack_id, version.id)
            raise
        if previous_version_id != version.id:
            self._remove_version(owner_id, pack_id, previous_version_id)
        return self.get(owner_id, pack_id)

    @staticmethod
    def _canonical_uploaded_files(
        files: list[tuple[str, bytes, str]],
        limits: ReferencePackLimits,
    ) -> tuple[tuple[str, bytes, str], ...]:
        """Validate already-read browser uploads before starting a new version.

        Uploads are deliberately kept separate from ``_canonical_inputs``:
        Settings is allowed to send bytes directly, while Agent-created packs
        must continue to read only through an active workspace reader.
        """
        if not isinstance(files, list):
            raise ReferencePackError("uploaded files must be a list")
        if len(files) > limits.max_files:
            raise ReferencePackError(f"too many files (maximum {limits.max_files})")
        seen: dict[str, str] = {}
        selected: list[tuple[str, bytes, str]] = []
        for raw_name, data, media_type in files:
            path = _canonical_path(raw_name)
            if path.casefold() in seen:
                raise ReferencePackError(f"duplicate or case-colliding file: {path}")
            seen[path.casefold()] = path
            if not isinstance(data, bytes):
                raise ReferencePackError(f"uploaded file is not bytes: {path}")
            if len(data) > limits.max_file_bytes:
                raise ReferencePackError(
                    f"file exceeds the {limits.max_file_bytes}-byte limit: {path}"
                )
            selected.append((path, data, _file_media_type(path, media_type)))
        selected.sort(key=lambda row: (row[0].casefold(), row[0]))
        if sum(len(row[1]) for row in selected) > limits.max_total_bytes:
            raise ReferencePackError(f"files exceed the {limits.max_total_bytes}-byte total limit")
        return tuple(selected)

    @_synchronized_mutation
    def update_files(
        self,
        owner_id: str,
        pack_id: str,
        *,
        uploads: list[tuple[str, bytes, str]],
        remove: list[str] | tuple[str, ...] = (),
    ) -> ReferencePack:
        """Atomically replace/add uploads and remove exact current paths.

        All bytes and path operations are validated before ``_write_version``.
        The existing head remains authoritative until the staged version is
        complete, so a rejected batch cannot partially change a pack.
        """
        current = self.get(owner_id, pack_id)
        uploaded = self._canonical_uploaded_files(uploads, self.limits)
        remove_paths: set[str] = set()
        for raw_name in remove:
            path = _canonical_path(raw_name)
            if path.casefold() in remove_paths:
                raise ReferencePackError(f"duplicate or case-colliding removal: {path}")
            remove_paths.add(path.casefold())
        upload_paths = {path.casefold() for path, _data, _media in uploaded}
        if remove_paths & upload_paths:
            raise ReferencePackError("a file cannot be uploaded and removed in the same batch")

        existing: dict[str, tuple[str, bytes, str]] = {}
        for item in current.current.files:
            key = item.name.casefold()
            if key not in remove_paths:
                existing[key] = (
                    item.name,
                    self.read_version_file(
                        owner_id, pack_id, current.current_version_id, item.name
                    ),
                    item.media_type,
                )
        for item in uploaded:
            existing[item[0].casefold()] = item
        if not existing:
            raise ReferencePackError("a Reference Pack requires at least one file")
        if len(existing) > self.limits.max_files:
            raise ReferencePackError(f"too many files (maximum {self.limits.max_files})")
        selected = tuple(sorted(existing.values(), key=lambda row: (row[0].casefold(), row[0])))
        if sum(len(row[1]) for row in selected) > self.limits.max_total_bytes:
            raise ReferencePackError(
                f"files exceed the {self.limits.max_total_bytes}-byte total limit"
            )

        previous_version_id = current.current_version_id
        version = self._write_version(
            owner_id, pack_id, current.name, current.description, selected
        )
        head = self._load_head(owner_id, pack_id)
        head.update({"current_version_id": version.id, "updated_at": version.created_at})
        try:
            _atomic_json(self._pack_root(owner_id, pack_id) / "head.json", head)
        except Exception:
            self._remove_version(owner_id, pack_id, version.id)
            raise
        if previous_version_id != version.id:
            self._remove_version(owner_id, pack_id, previous_version_id)
        return self.get(owner_id, pack_id)

    @_synchronized_mutation
    def delete(self, owner_id: str, pack_id: str) -> bool:
        self._load_head(owner_id, pack_id)
        pack_root = self._pack_root(owner_id, pack_id)
        tombstone = pack_root.with_name(f".deleted-{pack_root.name}-{secrets.token_hex(8)}")
        # Rename first so the pack disappears atomically from all future
        # listings/bindings, then remove the complete library copy. Existing
        # Build snapshots live in the binding store and remain untouched.
        os.replace(pack_root, tombstone)
        shutil.rmtree(tombstone)
        return True
