"""Secrets encrypted at rest — currently the OpenRouter API key.

The plaintext key NEVER touches disk. A `SecretBox` encrypts/decrypts with a Fernet
key derived from an app secret (`DISCO_SECRET_KEY`, legacy `PMX_SECRET_KEY` fallback);
the ciphertext is persisted by `SecretStore` to a separate file (`DISCO_SECRETS`,
legacy `PMX_SECRETS` fallback). Server entrypoints auto-generate a strong app
secret on first startup when no env value is supplied, then reuse it on later
startups. If existing ciphertext was encrypted under a different app secret, the
store reports `locked` and callers get None rather than a broken key.

Kept separate from the config store: secrets are isolated from the catalogue, so
the config file stays free of credentials and can be shared/inspected safely.
"""

from __future__ import annotations

import base64
import contextlib
import hashlib
import json
import logging
import os
import secrets as _py_secrets
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from cryptography.fernet import Fernet, InvalidToken

from ._secret_store_format import (
    MAX_OLD_READ_KEYS as _MAX_OLD_READ_KEYS,
)
from ._secret_store_format import (
    SecretDecryptionError,
    SecretStoreFormatError,
    UnknownSecretKeyError,
)
from ._secret_store_format import (
    build_v2_document as _build_v2_document,
)
from ._secret_store_format import (
    derived_key_id as _derived_key_id,
)
from ._secret_store_format import (
    is_derived_key_id as _is_derived_key_id,
)
from ._secret_store_format import (
    read_secret_document as _read_secret_document,
)
from ._secret_store_format import (
    validate_key_id as _validate_key_id,
)
from ._secret_store_rotation import SecretRotation as _SecretRotation

_LOG = logging.getLogger("disco.secrets")

_ENV_SECRET = "DISCO_SECRET_KEY"
# honored so ciphertext encrypted under the old key still decrypts
_ENV_SECRET_LEGACY = "PMX_SECRET_KEY"
_ENV_SECRET_KEY_ID = "DISCO_SECRET_KEY_ID"
_ENV_SECRET_READ_KEYS = "DISCO_SECRET_READ_KEYS"
_ENV_PATH = "DISCO_SECRETS"
_ENV_PATH_LEGACY = "PMX_SECRETS"
_APP_SECRET_FILENAME = "secret-key"
# Legacy default: the encrypted secrets lived in the CWD, i.e. the repo root when a
# server is launched from the checkout. That put credential ciphertext inside the
# project tree — undesirable defense-in-depth-wise (anything granted read of the
# working dir could copy it). New default is the user config dir, OUTSIDE the tree.
_LEGACY_FILENAME = "disco-secrets.json"


def _default_app_data_dir() -> Path:
    data_dir = os.environ.get("DISCO_DATA_DIR") or os.environ.get("PMX_DATA_DIR")
    if data_dir:
        return Path(data_dir)
    xdg = os.environ.get("XDG_DATA_HOME")
    if xdg:
        return Path(xdg) / "disco"
    return Path.home() / ".local" / "share" / "disco"


def _default_app_secret_path() -> Path:
    return _default_app_data_dir() / _APP_SECRET_FILENAME


def ensure_process_secret_key(path: str | os.PathLike[str] | None = None) -> str:
    """Ensure this server process has an app secret for encrypting settings keys.

    Operator-supplied env wins. When neither ``DISCO_SECRET_KEY`` nor the legacy
    ``PMX_SECRET_KEY`` is present, a 32-byte random secret is generated once,
    stored in the app data dir with mode 0600, then loaded into ``os.environ`` as
    ``DISCO_SECRET_KEY`` for the rest of the process. Subsequent startups reuse
    the same file so encrypted settings keys remain decryptable.
    """
    existing = os.environ.get(_ENV_SECRET) or os.environ.get(_ENV_SECRET_LEGACY)
    if existing:
        return existing

    secret_path = Path(path) if path is not None else _default_app_secret_path()
    try:
        secret = secret_path.read_text(encoding="utf-8").strip()
    except FileNotFoundError:
        secret = ""
    else:
        with contextlib.suppress(OSError):
            os.chmod(secret_path, 0o600)
    if not secret:
        secret = base64.b64encode(_py_secrets.token_bytes(32)).decode("ascii")
        secret_path.parent.mkdir(parents=True, exist_ok=True)
        with contextlib.suppress(OSError):
            os.chmod(secret_path.parent, 0o700)
        tmp = secret_path.with_suffix(secret_path.suffix + ".tmp")
        fd = os.open(tmp, os.O_CREAT | os.O_WRONLY | os.O_TRUNC, 0o600)
        try:
            os.write(fd, (secret + "\n").encode("ascii"))
        finally:
            os.close(fd)
        os.chmod(tmp, 0o600)
        tmp.replace(secret_path)
    os.environ[_ENV_SECRET] = secret
    return secret


def _default_secrets_path() -> Path:
    """Resolve the secrets file when neither an explicit path nor PMX_SECRETS is given.

    Prefers `$XDG_CONFIG_HOME/disco/secrets.json` (default `~/.config/...`) so
    credentials never land in the project tree. Back-compat: if that file doesn't
    exist yet but a legacy in-tree `disco-secrets.json` does, honor the legacy
    one so an existing deployment keeps working — but fresh installs write out-of-tree.
    Pure read (no side effects); migrate the legacy file with a one-time `mv`."""
    cfg = os.environ.get("XDG_CONFIG_HOME") or os.path.join(os.path.expanduser("~"), ".config")
    new = Path(cfg) / "disco" / "secrets.json"
    if new.exists():
        return new
    legacy = Path(_LEGACY_FILENAME)
    if legacy.exists():
        return legacy
    return new


# OpenRouter models use this as their `api_key_env`. The agent-server overlays the
# DECRYPTED OpenRouter key into the provider env under this name at build time, so
# the existing api_key_env mechanism carries the secret without it touching disk.
OPENROUTER_API_KEY_ENV = "DISCO_OPENROUTER_API_KEY"
OPENROUTER_API_KEY_ENV_LEGACY = "PMX_OPENROUTER_API_KEY"


def _looks_weak(secret: str) -> bool:
    """Heuristic for a low-entropy app secret. The KDF below is a single
    unsalted SHA-256 (no work factor), which is secure ONLY if the secret is
    itself high-entropy; a short or low-diversity secret (a human password) is
    brute-forceable offline. A 32-byte random key (`openssl rand -base64 32` →
    44 chars, many distinct symbols) passes."""
    return len(secret) < 16 or len(set(secret)) < 8


_weak_secret_warned = False


class WeakSecretError(RuntimeError):
    """Raised when a high-value deploy credential would be stored or used under a
    weak or absent app secret.

    Ordinary provider secrets keep the existing warn-only behavior for weak
    ``DISCO_SECRET_KEY`` values. Deploy credentials pass ``strong_required=True``
    so they fail closed instead of being protected by a brute-forceable app secret.
    """


def _warn_if_weak_secret(secret: str) -> None:
    """Log ONCE per process if the app secret looks weak. A warning, not an
    error: an existing deployment with a short key must keep working (raising
    would lock it out of its own encrypted secrets). The durable fix is a
    work-factor KDF (Argon2id) behind a versioned ciphertext format."""
    global _weak_secret_warned
    if _weak_secret_warned or not _looks_weak(secret):
        return
    _weak_secret_warned = True
    _LOG.warning(
        "%s looks low-entropy (length %d). The secrets key-derivation is an "
        "unsalted SHA-256 with no work factor, so a weak app secret is "
        "brute-forceable offline against the stored ciphertext. Set a "
        "high-entropy value — e.g. `openssl rand -base64 32`.",
        _ENV_SECRET,
        len(secret),
    )


def _fernet_from(secret: str) -> Fernet:
    # Derive a stable 32-byte Fernet key from the app secret. Use a high-entropy
    # DISCO_SECRET_KEY (e.g. `openssl rand -base64 32`) — this is not a slow KDF,
    # so a weak secret is brute-forceable (see _warn_if_weak_secret).
    key = base64.urlsafe_b64encode(hashlib.sha256(secret.encode()).digest())
    return Fernet(key)


class SecretBox:
    """Symmetric encryption from an app secret. Fernet = AES-128-CBC + HMAC. With no
    app secret the box is unavailable: it can neither encrypt nor decrypt."""

    def __init__(self, app_secret: str | None, *, key_id: str | None = None) -> None:
        if app_secret:
            _warn_if_weak_secret(app_secret)
        if key_id is not None:
            _validate_key_id(key_id)
            if app_secret and _is_derived_key_id(key_id) and key_id != _derived_key_id(app_secret):
                raise ValueError("derived secret key IDs are reserved for their key material")
        self._app_secret = app_secret
        self._fernet = _fernet_from(app_secret) if app_secret else None
        self._key_id = key_id or (_derived_key_id(app_secret) if app_secret else None)

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> SecretBox:
        e = os.environ if env is None else env
        # DISCO_SECRET_KEY preferred; fall back to the legacy PMX_SECRET_KEY so
        # ciphertext encrypted under the old key still decrypts.
        secret = e.get(_ENV_SECRET)
        if secret is None:
            secret = e.get(_ENV_SECRET_LEGACY)
        # Compose represents an optional unset value as the empty string. Keep
        # that equivalent to an omitted ID so ordinary first boot uses the
        # derived compatibility ID; nonempty malformed IDs still fail loudly.
        return cls(secret, key_id=e.get(_ENV_SECRET_KEY_ID) or None)

    @property
    def available(self) -> bool:
        return self._fernet is not None

    @property
    def is_strong(self) -> bool:
        return bool(self._app_secret and not _looks_weak(self._app_secret))

    @property
    def key_id(self) -> str | None:
        """Non-secret identifier persisted beside ciphertext written by this box."""
        return self._key_id

    @property
    def signing_secret(self) -> str | None:
        """Raw app secret for HMAC-bound operator metadata, never persisted."""
        return self._app_secret

    def encrypt(self, plaintext: str) -> str:
        if self._fernet is None:
            raise RuntimeError(f"{_ENV_SECRET} is not set — cannot encrypt secrets")
        return self._fernet.encrypt(plaintext.encode()).decode()

    def decrypt(self, token: str) -> str | None:
        """Plaintext, or None if the box is unavailable or the token doesn't verify
        (wrong app secret / tampering)."""
        if self._fernet is None:
            return None
        try:
            return self._fernet.decrypt(token.encode()).decode()
        except (InvalidToken, ValueError):
            return None


def _old_read_boxes_from_env(env: Mapping[str, str]) -> tuple[SecretBox, ...]:
    raw = env.get(_ENV_SECRET_READ_KEYS)
    if raw is None or not raw.strip():
        return ()
    try:
        values = json.loads(raw)
    except ValueError as exc:
        raise SecretStoreFormatError(
            f"{_ENV_SECRET_READ_KEYS} must be a JSON object of key IDs to key material"
        ) from exc
    if not isinstance(values, dict):
        raise SecretStoreFormatError(
            f"{_ENV_SECRET_READ_KEYS} must be a JSON object of key IDs to key material"
        )
    if len(values) > _MAX_OLD_READ_KEYS:
        raise SecretStoreFormatError(
            f"{_ENV_SECRET_READ_KEYS} exceeds the {_MAX_OLD_READ_KEYS}-key rollback window"
        )
    boxes: list[SecretBox] = []
    for key_id, secret in values.items():
        if not isinstance(key_id, str) or not isinstance(secret, str) or not secret:
            raise SecretStoreFormatError(
                f"{_ENV_SECRET_READ_KEYS} entries must map string key IDs to non-empty strings"
            )
        boxes.append(SecretBox(secret, key_id=key_id))
    return tuple(boxes)


def _store_path(path: str | os.PathLike[str] | None) -> Path:
    env_path = os.environ.get(_ENV_PATH)
    if env_path is None:
        env_path = os.environ.get(_ENV_PATH_LEGACY)
    if path:
        return Path(path)
    return Path(env_path) if env_path else _default_secrets_path()


def _configured_boxes(
    box: SecretBox | None, read_boxes: Sequence[SecretBox] | None
) -> tuple[SecretBox, tuple[SecretBox, ...]]:
    if box is None:
        active = SecretBox.from_env()
        old = _old_read_boxes_from_env(os.environ) if read_boxes is None else tuple(read_boxes)
    else:
        active = box
        old = () if read_boxes is None else tuple(read_boxes)
    if len(old) > _MAX_OLD_READ_KEYS:
        raise ValueError(f"at most {_MAX_OLD_READ_KEYS} old secret read keys are allowed")
    return active, old


def _read_keyring(active: SecretBox, old: Sequence[SecretBox]) -> dict[str, SecretBox]:
    keyring: dict[str, SecretBox] = {}
    for candidate in (active, *old):
        if not candidate.available:
            if candidate is active:
                continue
            raise ValueError("old secret read keys must contain key material")
        key_id = candidate.key_id
        if key_id is None:
            raise ValueError("available secret boxes must have a key ID")
        if key_id in keyring:
            raise ValueError(f"duplicate secret key ID {key_id!r}")
        keyring[key_id] = candidate
    _add_compatibility_aliases(keyring, (active, *old))
    return keyring


def _add_compatibility_aliases(keyring: dict[str, SecretBox], boxes: Sequence[SecretBox]) -> None:
    # Naming an existing deployment's key must not strand records written
    # before key IDs were configurable. The deterministic ID remains a read-only
    # alias for the same material; all new writes use the explicit active ID.
    for candidate in boxes:
        signing_secret = candidate.signing_secret
        if not signing_secret:
            continue
        compatibility_id = _derived_key_id(signing_secret)
        existing = keyring.get(compatibility_id)
        if existing is not None and existing.signing_secret != signing_secret:
            raise ValueError(f"ambiguous secret key ID {compatibility_id!r}")
        keyring.setdefault(compatibility_id, candidate)


class SecretStore:
    """Persist named secrets with one write key and a bounded rotation keyring.

    Existing ``{name: ciphertext}`` files remain readable. New writes use the
    active box and the version-2 record envelope. During a key rotation callers
    supply at most four old read boxes, re-encrypt atomically, verify, and then
    explicitly finalize the rollback window.
    """

    def __init__(
        self,
        path: str | os.PathLike[str] | None = None,
        *,
        box: SecretBox | None = None,
        read_boxes: Sequence[SecretBox] | None = None,
    ) -> None:
        self._path = _store_path(path)
        self._box, old_boxes = _configured_boxes(box, read_boxes)
        self._read_boxes = _read_keyring(self._box, old_boxes)
        self._rotation = _SecretRotation(self)

    @property
    def rotation(self) -> _SecretRotation:
        """The bounded migration workflow for this store."""
        return self._rotation

    @property
    def can_store(self) -> bool:
        """True when an app secret is present, so new secrets can be encrypted."""
        return self._box.available

    @property
    def locked(self) -> bool:
        """At least one stored or rollback record is not decryptable now."""
        return bool(self.undecryptable_names())

    @property
    def signing_secret(self) -> str | None:
        return self._box.signing_secret

    # -- generic named secrets (any provider key, encrypted at rest) ----------
    # The legacy store held {name: ciphertext}. For provider keys the `name` is the
    # `api_key_env` var name (e.g. "OPENAI_API_KEY", "DISCO_SEARCH_API_KEY"), so
    # the agent-server can overlay the decrypted value into that env var at
    # build time — exactly the OpenRouter mechanism, generalized. "openrouter"
    # is a reserved legacy slot; the named-slot convenience functions for it
    # live in `secret_refs.py` (PY-0473) — this store only owns storage.

    def _require_strong(self, name: str) -> None:
        """Hard gate for high-value deploy credentials."""
        if self._box.is_strong:
            return
        why = (
            f"{_ENV_SECRET} is not set"
            if not self._box.available
            else f"{_ENV_SECRET} is low-entropy"
        )
        raise WeakSecretError(
            f"refusing to store or use the high-value deploy credential {name!r}: "
            f"{why}. Set a high-entropy {_ENV_SECRET} (for example, "
            "`openssl rand -base64 32`) and reconnect."
        )

    def has_secret(self, name: str) -> bool:
        return bool(self._document()["records"].get(name))

    def get_secret(self, name: str, *, strong_required: bool = False) -> str | None:
        if strong_required:
            self._require_strong(name)
        document = self._document()
        self._require_rollback_window(document)
        record = document["records"].get(name)
        if record is None:
            return None
        _, plaintext = self._decrypt_record(record, legacy_failure_is_none=True)
        return plaintext

    def set_secret(self, name: str, plaintext: str, *, strong_required: bool = False) -> None:
        if strong_required:
            self._require_strong(name)
        if not self._box.available:
            raise RuntimeError(f"{_ENV_SECRET} is not set — cannot store an encrypted key")
        key_id = self._active_key_id()
        document = self._document()
        self._require_rollback_window(document)
        records = self._versioned_records(document["records"])
        records[name] = {"key_id": key_id, "ciphertext": self._box.encrypt(plaintext)}
        rotation = document["rotation"]
        if rotation is not None:
            # Rollback changes the writer key, not application data history.
            # Shadow every write under the recorded rollback writer so a later
            # rollback cannot silently discard a secret added or replaced while
            # the verification window was open.
            if rotation["to_key_id"] != key_id:
                raise UnknownSecretKeyError(
                    f"pending rotation requires active key {rotation['to_key_id']!r}"
                )
            rotation = dict(rotation)
            rollback_records = dict(rotation["rollback_records"])
            rollback_key_id = rotation["from_active_key_id"]
            rollback_box = self._read_boxes[rollback_key_id]
            rollback_records[name] = {
                "key_id": rollback_key_id,
                "ciphertext": rollback_box.encrypt(plaintext),
            }
            rotation["rollback_records"] = rollback_records
            rotation["from_key_ids"] = sorted(
                {
                    record["key_id"]
                    for record in rollback_records.values()
                    if record["key_id"] != rotation["to_key_id"]
                }
            )
        self._write(self._v2_document(records, rotation=rotation))

    def clear_secret(self, name: str) -> None:
        document = self._document()
        records = dict(document["records"])
        records.pop(name, None)
        if document["legacy"]:
            self._write(records)
            return
        rotation = document["rotation"]
        if rotation is not None:
            # A clear during the rollback window must remove both copies.  Leaving
            # the retained ciphertext behind would let rollback resurrect a key
            # the operator deliberately deleted.
            rotation = dict(rotation)
            rollback_records = dict(rotation["rollback_records"])
            rollback_records.pop(name, None)
            rotation["rollback_records"] = rollback_records
            rotation["from_key_ids"] = sorted(
                {
                    record["key_id"]
                    for record in rollback_records.values()
                    if record["key_id"] != rotation["to_key_id"]
                }
            )
            if not rollback_records:
                # Nothing remains to restore. Close the rollback window rather
                # than retaining an empty state that still requires a retired key.
                rotation = None
        # Clearing ciphertext requires no key material.  Preserve the validated
        # on-disk writer identity so a locked deployment can delete and re-enter
        # a secret instead of being trapped by the unavailable active key.
        self._write(
            self._v2_document(
                records,
                rotation=rotation,
                active_key_id=document["active_key_id"],
            )
        )

    def secret_names(self) -> list[str]:
        """The names of all stored secrets (the ones with ciphertext present)."""
        return [name for name, record in self._document()["records"].items() if record]

    def undecryptable_names(self) -> list[str]:
        """Stored names whose ciphertext can't currently be decrypted — a wrong
        or missing app secret (then ALL stored names), or an individually
        corrupted token. These are the keys the operator must restore the app
        secret for, or clear and re-enter."""
        document = self._document()
        bad: set[str] = set()
        for name, record in document["records"].items():
            try:
                _, plaintext = self._decrypt_record(record, legacy_failure_is_none=True)
            except (SecretStoreFormatError, UnknownSecretKeyError, SecretDecryptionError):
                bad.add(name)
            else:
                if plaintext is None:
                    bad.add(name)
        rotation = document["rotation"]
        if rotation is not None:
            for name, record in rotation["rollback_records"].items():
                try:
                    self._decrypt_record(record, legacy_failure_is_none=False)
                except (SecretStoreFormatError, UnknownSecretKeyError, SecretDecryptionError):
                    bad.add(name)
            if rotation["from_active_key_id"] not in self._read_boxes:
                bad.update(rotation["rollback_records"])
        return sorted(bad)

    # -- internals ------------------------------------------------------------

    def _active_key_id(self) -> str:
        key_id = self._box.key_id
        if key_id is None:
            raise RuntimeError(f"{_ENV_SECRET} is not set — no active secret key ID")
        return key_id

    def _document(self) -> dict[str, Any]:
        return _read_secret_document(self._path)

    def _versioned_records(self, records: Mapping[str, Any]) -> dict[str, dict[str, str]]:
        versioned: dict[str, dict[str, str]] = {}
        for name, record in records.items():
            if record is None or record == "":
                continue
            if isinstance(record, dict):
                # Ordinary writes may not silently carry an unreadable explicit
                # key forward into a mixed-key document.  The operator can still
                # clear that ciphertext without material, then re-enter it.
                self._decrypt_record(record, legacy_failure_is_none=False)
                versioned[name] = dict(record)
                continue
            key_id, plaintext = self._decrypt_record(record, legacy_failure_is_none=False)
            if plaintext is None:
                raise SecretDecryptionError(f"legacy secret record {name!r} is not decryptable")
            versioned[name] = {"key_id": key_id, "ciphertext": record}
        return versioned

    def _decrypt_record(
        self, record: Any, *, legacy_failure_is_none: bool
    ) -> tuple[str, str | None]:
        if isinstance(record, str):
            for key_id, box in self._read_boxes.items():
                plaintext = box.decrypt(record)
                if plaintext is not None:
                    return key_id, plaintext
            if legacy_failure_is_none:
                return "legacy-unknown", None
            raise SecretDecryptionError("legacy secret ciphertext is not decryptable")
        if not isinstance(record, dict):
            raise SecretStoreFormatError("encrypted secret record has an invalid shape")
        key_id = record.get("key_id")
        ciphertext = record.get("ciphertext")
        if not isinstance(key_id, str) or not isinstance(ciphertext, str):
            raise SecretStoreFormatError("encrypted secret record has invalid key metadata")
        box = self._read_boxes.get(key_id)
        if box is None:
            # Preserve the pre-rotation status/read contract for deployments that
            # never named their single app key: callers see ``locked`` + ``None``.
            # Explicit versioned IDs, and every rotation operation (strict mode),
            # fail loudly instead of silently accepting a missing key.
            if legacy_failure_is_none and _is_derived_key_id(key_id):
                return key_id, None
            raise UnknownSecretKeyError(f"secret record requires unknown key ID {key_id!r}")
        plaintext = box.decrypt(ciphertext)
        if plaintext is None:
            raise SecretDecryptionError(
                f"secret ciphertext failed authentication for key ID {key_id!r}"
            )
        return key_id, plaintext

    def _require_rollback_window(self, document: Mapping[str, Any]) -> None:
        rotation = document["rotation"]
        if rotation is None:
            return
        from_active_key_id = rotation["from_active_key_id"]
        if from_active_key_id not in self._read_boxes:
            raise UnknownSecretKeyError(
                f"pending rotation requires rollback active key {from_active_key_id!r}"
            )
        for record in rotation["rollback_records"].values():
            self._decrypt_record(record, legacy_failure_is_none=False)

    def _verify_primary_active(self, document: Mapping[str, Any], active_key_id: str) -> None:
        active_box = self._read_boxes.get(active_key_id)
        if active_box is None:
            raise UnknownSecretKeyError(f"active secret key {active_key_id!r} is unavailable")
        for name, record in document["records"].items():
            if not isinstance(record, dict) or record.get("key_id") != active_key_id:
                raise SecretStoreFormatError(
                    f"secret record {name!r} was not migrated to the active key"
                )
            ciphertext = record.get("ciphertext")
            if not isinstance(ciphertext, str) or active_box.decrypt(ciphertext) is None:
                raise SecretDecryptionError(
                    f"secret record {name!r} failed post-migration verification"
                )

    def _v2_document(
        self,
        records: Mapping[str, Any],
        *,
        rotation: Mapping[str, Any] | None,
        active_key_id: str | None = None,
    ) -> dict[str, Any]:
        writer_key_id = self._active_key_id() if active_key_id is None else active_key_id
        return _build_v2_document(
            records,
            rotation=rotation,
            active_key_id=writer_key_id,
        )

    def _write(self, data: Mapping[str, Any]) -> None:
        # The secrets file holds credential ciphertext — lock it to the owner
        # (0600), and the parent dir to 0700, so another local user can't even
        # copy the ciphertext. Defense-in-depth: decrypting still needs the app
        # secret, but best practice is least-privilege on anything credential-bearing.
        self._path.parent.mkdir(parents=True, exist_ok=True)
        with contextlib.suppress(OSError):  # best-effort on non-POSIX / odd mounts
            os.chmod(self._path.parent, 0o700)
        tmp = self._path.with_suffix(self._path.suffix + ".tmp")
        # Create the temp file 0600 from the start (don't briefly expose 0644):
        # open with O_CREAT|O_WRONLY|O_TRUNC at mode 0o600, then write.
        payload = (json.dumps(data, indent=2, sort_keys=True) + "\n").encode()
        fd = os.open(tmp, os.O_CREAT | os.O_WRONLY | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(tmp, 0o600)  # explicit: umask can mask bits off the O_CREAT mode
        self._replace(tmp)  # atomic on POSIX; the 0600 mode rides along
        with contextlib.suppress(OSError):
            parent_fd = os.open(self._path.parent, os.O_RDONLY)
            try:
                os.fsync(parent_fd)
            finally:
                os.close(parent_fd)

    def _replace(self, tmp: Path) -> None:
        """Single injection seam for crash-before-commit rotation tests."""
        tmp.replace(self._path)
