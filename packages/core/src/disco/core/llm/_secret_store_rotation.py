"""Atomic rotation workflow for :class:`SecretStore`."""

from __future__ import annotations

from typing import TYPE_CHECKING

from ._secret_store_format import (
    SecretDecryptionError,
    SecretStoreFormatError,
    UnknownSecretKeyError,
)

if TYPE_CHECKING:
    from .secrets import SecretStore


class SecretRotation:
    """Operate the bounded rollback window owned by one secret store."""

    def __init__(self, owner: SecretStore) -> None:
        self._owner = owner

    @property
    def pending(self) -> bool:
        """True until a migrated rollback window is verified and finalized."""
        return self._owner._document()["rotation"] is not None

    def reencrypt_to_active(self) -> int:
        """Atomically migrate every record to the active key.

        The pre-migration records remain encrypted in the same atomic document as
        a rollback window. An interruption before replace leaves the old document;
        an interruption after replace leaves a complete pending migration. Calling
        this method again with the same active key verifies and resumes either state.
        Returns the number of primary records protected by the active key.
        """
        owner = self._owner
        if not owner._box.available:
            raise RuntimeError("DISCO_SECRET_KEY is not set — cannot rotate encrypted keys")
        active_key_id = owner._active_key_id()
        document = owner._document()
        rotation = document["rotation"]
        if rotation is not None:
            if rotation["to_key_id"] != active_key_id:
                raise SecretStoreFormatError(
                    "a secret rotation is already pending for a different active key"
                )
            self.verify()
            return len(document["records"])

        source_records = owner._versioned_records(document["records"])
        if not document["legacy"] and all(
            record["key_id"] == active_key_id for record in source_records.values()
        ):
            owner._verify_primary_active(document, active_key_id)
            return len(source_records)
        if document["legacy"] and not source_records:
            owner._write(owner._v2_document({}, rotation=None))
            return 0

        migrated, source_key_ids = self._migrated_records(source_records, active_key_id)
        rollback_active_key_id = self._rollback_writer(
            document=document,
            source_key_ids=source_key_ids,
            active_key_id=active_key_id,
        )
        rotation = {
            "from_key_ids": sorted(source_key_ids - {active_key_id}),
            "from_active_key_id": rollback_active_key_id,
            "to_key_id": active_key_id,
            "rollback_records": source_records,
        }
        owner._write(owner._v2_document(migrated, rotation=rotation))
        written = owner._document()
        owner._require_rollback_window(written)
        owner._verify_primary_active(written, active_key_id)
        return len(migrated)

    def verify(self) -> bool:
        """Verify both the active records and retained rollback read window."""
        owner = self._owner
        document = owner._document()
        rotation = document["rotation"]
        if rotation is None:
            return False
        active_key_id = owner._active_key_id()
        if rotation["to_key_id"] != active_key_id:
            raise UnknownSecretKeyError(
                f"pending rotation requires active key {rotation['to_key_id']!r}"
            )
        owner._require_rollback_window(document)
        owner._verify_primary_active(document, active_key_id)
        return True

    def finalize(self) -> bool:
        """Drop the rollback records only after the migration verifies."""
        owner = self._owner
        document = owner._document()
        if document["rotation"] is None:
            return False
        self.verify()
        owner._write(owner._v2_document(document["records"], rotation=None))
        finalized = owner._document()
        if finalized["rotation"] is not None:
            raise SecretStoreFormatError("secret rotation finalization did not commit")
        owner._verify_primary_active(finalized, owner._active_key_id())
        return True

    def rollback(self) -> bool:
        """Atomically restore retained records before a migration is finalized."""
        owner = self._owner
        document = owner._document()
        rotation = document["rotation"]
        if rotation is None:
            return False
        rollback_records = rotation["rollback_records"]
        rollback_active_key_id = rotation["from_active_key_id"]
        if rollback_active_key_id not in owner._read_boxes:
            raise UnknownSecretKeyError(f"rollback requires active key {rollback_active_key_id!r}")
        self._verify_rollback_records(rollback_records)
        owner._write(
            owner._v2_document(
                rollback_records,
                rotation=None,
                active_key_id=rollback_active_key_id,
            )
        )
        restored = owner._document()
        for record in restored["records"].values():
            owner._decrypt_record(record, legacy_failure_is_none=False)
        return True

    def _migrated_records(
        self, source_records: dict[str, dict[str, str]], active_key_id: str
    ) -> tuple[dict[str, dict[str, str]], set[str]]:
        migrated: dict[str, dict[str, str]] = {}
        source_key_ids: set[str] = set()
        for name, record in source_records.items():
            key_id, plaintext = self._owner._decrypt_record(
                record, legacy_failure_is_none=False
            )
            if plaintext is None:  # strict mode above cannot produce None
                raise SecretDecryptionError(f"secret record {name!r} is not decryptable")
            source_key_ids.add(key_id)
            migrated[name] = {
                "key_id": active_key_id,
                "ciphertext": self._owner._box.encrypt(plaintext),
            }
        return migrated, source_key_ids

    @staticmethod
    def _rollback_writer(
        *, document: dict[str, object], source_key_ids: set[str], active_key_id: str
    ) -> object:
        # Legacy files have no writer metadata. A single resolved key is the only
        # evidence-backed rollback writer; mixed/empty legacy input uses active.
        if document["legacy"] and len(source_key_ids) == 1:
            return next(iter(source_key_ids))
        if document["legacy"]:
            return active_key_id
        return document["active_key_id"]

    def _verify_rollback_records(self, records: dict[str, dict[str, str]]) -> None:
        for name, record in records.items():
            try:
                self._owner._decrypt_record(record, legacy_failure_is_none=False)
            except (UnknownSecretKeyError, SecretDecryptionError) as exc:
                raise type(exc)(f"cannot roll back secret record {name!r}: {exc}") from exc
