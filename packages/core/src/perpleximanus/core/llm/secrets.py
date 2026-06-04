"""Secrets encrypted at rest — currently the OpenRouter API key.

The plaintext key NEVER touches disk. A `SecretBox` encrypts/decrypts with a Fernet
key derived from an app secret (`PMX_SECRET_KEY` env var); the ciphertext is
persisted by `SecretStore` to a separate file (`PMX_SECRETS`). On restart the
stored key survives, but it is only *usable* once `PMX_SECRET_KEY` is present again
to decrypt it — if it's missing/wrong, the store reports `locked` and callers get
None rather than a broken key.

Kept separate from the config store: secrets are isolated from the catalogue, so
the config file stays free of credentials and can be shared/inspected safely.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
from collections.abc import Mapping
from pathlib import Path

from cryptography.fernet import Fernet, InvalidToken

_ENV_SECRET = "PMX_SECRET_KEY"
_ENV_PATH = "PMX_SECRETS"
_DEFAULT_PATH = "perpleximanus-secrets.json"

# OpenRouter models use this as their `api_key_env`. The agent-server overlays the
# DECRYPTED OpenRouter key into the provider env under this name at build time, so
# the existing api_key_env mechanism carries the secret without it touching disk.
OPENROUTER_API_KEY_ENV = "PMX_OPENROUTER_API_KEY"


def _fernet_from(secret: str) -> Fernet:
    # Derive a stable 32-byte Fernet key from the app secret. Use a high-entropy
    # PMX_SECRET_KEY (e.g. `openssl rand -base64 32`) — this is not a slow KDF.
    key = base64.urlsafe_b64encode(hashlib.sha256(secret.encode()).digest())
    return Fernet(key)


class SecretBox:
    """Symmetric encryption from an app secret. Fernet = AES-128-CBC + HMAC. With no
    app secret the box is unavailable: it can neither encrypt nor decrypt."""

    def __init__(self, app_secret: str | None) -> None:
        self._fernet = _fernet_from(app_secret) if app_secret else None

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> SecretBox:
        e = os.environ if env is None else env
        return cls(e.get(_ENV_SECRET))

    @property
    def available(self) -> bool:
        return self._fernet is not None

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


class SecretStore:
    """Persists named secrets encrypted at rest. Today: the OpenRouter API key."""

    def __init__(
        self,
        path: str | os.PathLike[str] | None = None,
        *,
        box: SecretBox | None = None,
    ) -> None:
        self._path = Path(path or os.environ.get(_ENV_PATH, _DEFAULT_PATH))
        self._box = box or SecretBox.from_env()

    @property
    def can_store(self) -> bool:
        """True when an app secret is present, so new secrets can be encrypted."""
        return self._box.available

    @property
    def locked(self) -> bool:
        """An encrypted secret exists but can't be decrypted (no/wrong app secret)."""
        return bool(self._raw().get("openrouter")) and not self._box.available

    def has_openrouter_key(self) -> bool:
        return bool(self._raw().get("openrouter"))

    def get_openrouter_key(self) -> str | None:
        token = self._raw().get("openrouter")
        return self._box.decrypt(token) if isinstance(token, str) else None

    def set_openrouter_key(self, plaintext: str) -> None:
        if not self._box.available:
            raise RuntimeError(f"{_ENV_SECRET} is not set — cannot store an encrypted key")
        data = self._raw()
        data["openrouter"] = self._box.encrypt(plaintext)
        self._write(data)

    def clear_openrouter_key(self) -> None:
        data = self._raw()
        data.pop("openrouter", None)
        self._write(data)

    # -- internals ------------------------------------------------------------

    def _raw(self) -> dict:
        try:
            return json.loads(self._path.read_text())
        except (FileNotFoundError, ValueError, OSError):
            return {}

    def _write(self, data: dict) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._path.with_suffix(self._path.suffix + ".tmp")
        tmp.write_text(json.dumps(data, indent=2))
        tmp.replace(self._path)  # atomic on POSIX
