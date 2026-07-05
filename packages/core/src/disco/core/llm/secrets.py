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
from collections.abc import Mapping
from pathlib import Path

from cryptography.fernet import Fernet, InvalidToken

_LOG = logging.getLogger("disco.secrets")

_ENV_SECRET = "DISCO_SECRET_KEY"
# honored so ciphertext encrypted under the old key still decrypts
_ENV_SECRET_LEGACY = "PMX_SECRET_KEY"
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

    def __init__(self, app_secret: str | None) -> None:
        if app_secret:
            _warn_if_weak_secret(app_secret)
        self._app_secret = app_secret
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

    @property
    def is_strong(self) -> bool:
        return bool(self._app_secret and not _looks_weak(self._app_secret))

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

    # -- generic named secrets (any provider key, encrypted at rest) ----------
    # The store holds {name: ciphertext}. For provider keys the `name` is the
    # `api_key_env` var name (e.g. "OPENAI_API_KEY", "DISCO_SEARCH_API_KEY"), so
    # the agent-server can overlay the decrypted value into that env var at
    # build time — exactly the OpenRouter mechanism, generalized. "openrouter"
    # is a reserved legacy slot (see the wrappers below).

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
        return bool(self._raw().get(name))

    def get_secret(self, name: str, *, strong_required: bool = False) -> str | None:
        if strong_required:
            self._require_strong(name)
        token = self._raw().get(name)
        return self._box.decrypt(token) if isinstance(token, str) else None

    def set_secret(self, name: str, plaintext: str, *, strong_required: bool = False) -> None:
        if strong_required:
            self._require_strong(name)
        if not self._box.available:
            raise RuntimeError(f"{_ENV_SECRET} is not set — cannot store an encrypted key")
        data = self._raw()
        data[name] = self._box.encrypt(plaintext)
        self._write(data)

    def clear_secret(self, name: str) -> None:
        data = self._raw()
        data.pop(name, None)
        self._write(data)

    def secret_names(self) -> list[str]:
        """The names of all stored secrets (the ones with ciphertext present)."""
        return [k for k, v in self._raw().items() if v]

    def undecryptable_names(self) -> list[str]:
        """Stored names whose ciphertext can't currently be decrypted — a wrong
        or missing app secret (then ALL stored names), or an individually
        corrupted token. These are the keys the operator must restore the app
        secret for, or clear and re-enter."""
        raw = self._raw()
        bad = []
        for name, token in raw.items():
            if isinstance(token, str) and token and self._box.decrypt(token) is None:
                bad.append(name)
        return bad

    # -- OpenRouter convenience wrappers (the reserved "openrouter" slot) ------

    def has_openrouter_key(self) -> bool:
        return self.has_secret("openrouter")

    def get_openrouter_key(self) -> str | None:
        return self.get_secret("openrouter")

    def set_openrouter_key(self, plaintext: str) -> None:
        self.set_secret("openrouter", plaintext)

    def clear_openrouter_key(self) -> None:
        self.clear_secret("openrouter")

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
