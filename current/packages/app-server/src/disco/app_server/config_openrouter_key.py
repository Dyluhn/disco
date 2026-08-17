"""OpenRouter-key repository split out of ConfigState (PY-0365): status/set/
clear for the reserved "openrouter" secret slot, generalized by
`core/llm/secret_refs.py`. `ConfigState` exposes an instance of this class as
the plain `openrouter` attribute.
"""

from __future__ import annotations

from disco.core.llm import SecretStore
from disco.core.llm.secret_refs import (
    clear_openrouter_key,
    has_openrouter_key,
    set_openrouter_key,
)

from .config.dtos import OpenRouterKeyStatus


class ConfigOpenRouterKey:
    """Encrypted-at-rest status/set/clear for the reserved OpenRouter key slot."""

    def __init__(self, secrets: SecretStore) -> None:
        self._secrets = secrets

    def openrouter_key_status(self) -> OpenRouterKeyStatus:
        # `locked` must reflect ACTUAL decryptability, not just "app secret missing".
        # A key encrypted under a DIFFERENT app secret (rotated/wrong key) is present
        # but undecryptable — SecretStore.locked misses that (it only checks box
        # availability, and it gates the env-secret import so it must stay lenient).
        # Compute the DISPLAY status here from a real decrypt attempt so the UI shows
        # "re-enter the key" instead of a false green over an unusable key.
        present = has_openrouter_key(self._secrets)
        usable = present and "openrouter" not in self._secrets.undecryptable_names()
        return OpenRouterKeyStatus(
            configured=present,
            locked=present and not usable,
            can_store=self._secrets.can_store,
        )

    def set_openrouter_key(self, key: str) -> OpenRouterKeyStatus:
        """Encrypt + persist. Raises ValueError if PMX_SECRET_KEY isn't set (no app
        secret to encrypt with) or the key is blank — mapped to 400 by the wire."""
        if not key.strip():
            raise ValueError("key is empty")
        try:
            set_openrouter_key(self._secrets, key.strip())
        except RuntimeError as exc:
            raise ValueError(str(exc)) from exc
        return self.openrouter_key_status()

    def clear_openrouter_key(self) -> OpenRouterKeyStatus:
        clear_openrouter_key(self._secrets)
        return self.openrouter_key_status()
