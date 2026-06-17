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
import contextlib
import hashlib
import json
import os
from collections.abc import Mapping
from pathlib import Path

from cryptography.fernet import Fernet, InvalidToken

_ENV_SECRET = "DISCO_SECRET_KEY"
# honored so ciphertext encrypted under the old key still decrypts
_ENV_SECRET_LEGACY = "PMX_SECRET_KEY"
_ENV_PATH = "DISCO_SECRETS"
_ENV_PATH_LEGACY = "PMX_SECRETS"
# Legacy default: the encrypted secrets lived in the CWD, i.e. the repo root when a
# server is launched from the checkout. That put credential ciphertext inside the
# project tree — undesirable defense-in-depth-wise (anything granted read of the
# working dir could copy it). New default is the user config dir, OUTSIDE the tree.
_LEGACY_FILENAME = "disco-secrets.json"


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
        # DISCO_SECRET_KEY preferred; fall back to the legacy PMX_SECRET_KEY so
        # ciphertext encrypted under the old key still decrypts.
        secret = e.get(_ENV_SECRET)
        if secret is None:
            secret = e.get(_ENV_SECRET_LEGACY)
        return cls(secret)

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
        env_path = os.environ.get(_ENV_PATH)
        if env_path is None:
            env_path = os.environ.get(_ENV_PATH_LEGACY)
        if path:
            self._path = Path(path)
        else:
            self._path = Path(env_path) if env_path else _default_secrets_path()
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
        fd = os.open(tmp, os.O_CREAT | os.O_WRONLY | os.O_TRUNC, 0o600)
        try:
            os.write(fd, json.dumps(data, indent=2).encode())
        finally:
            os.close(fd)
        os.chmod(tmp, 0o600)  # explicit: umask can mask bits off the O_CREAT mode
        tmp.replace(self._path)  # atomic on POSIX; the 0600 mode rides along
