"""Secrets-admin repository split out of ConfigState (PY-0365): the generic
provider-secret surface (list/status/set/clear/test), keyed by `api_key_env`
name, with the reserved slots (the dedicated OpenRouter key + the Stripe
restricted key) carved out for their own routes. `ConfigState` exposes an
instance of this class as the plain `secrets_admin` attribute.
"""

from __future__ import annotations

from disco.core.llm import ConfigStore, SecretStore
from disco.core.stripe_host_service import STRIPE_SECRET_REF

from . import origin_approval_wiring as _origin_wiring
from .config.dtos import ProbeResult, SecretsListDTO, SecretStatus


class ConfigSecretsAdmin:
    """Encrypted-at-rest provider secrets, generic over `api_key_env` name."""

    # "openrouter" is reserved for the dedicated route/UI above; the generic
    # surface neither lists nor accepts it (avoids two UIs fighting over one slot).
    _RESERVED_SECRETS = frozenset({"openrouter", STRIPE_SECRET_REF})

    def __init__(self, store: ConfigStore, secrets: SecretStore) -> None:
        self._store = store
        self._secrets = secrets

    @classmethod
    def _reserved_secret(cls, name: str) -> bool:
        return name in cls._RESERVED_SECRETS or name.startswith(
            ("stripe.binding.", "stripe.webhook.")
        )

    def list_secrets(self) -> SecretsListDTO:
        names = [n for n in self._secrets.secret_names() if not self._reserved_secret(n)]
        # The SPECIFIC stored keys that can't be decrypted (named, so the UI can
        # say which to fix) — generic, not the OpenRouter-only `locked` property.
        locked_names = [
            n for n in self._secrets.undecryptable_names() if not self._reserved_secret(n)
        ]
        return SecretsListDTO(
            names=sorted(names),
            locked_names=sorted(locked_names),
            locked=bool(locked_names),
            can_store=self._secrets.can_store,
        )

    def secret_status(self, name: str) -> SecretStatus:
        return SecretStatus(
            name=name,
            configured=self._secrets.has_secret(name),
            locked=self._secrets.locked,
            can_store=self._secrets.can_store,
        )

    def set_secret(self, name: str, value: str) -> SecretStatus:
        """Encrypt + persist a provider key under its api_key_env NAME. Raises
        ValueError (→ 400) on a reserved/invalid name, a blank value, or no app
        secret to encrypt with."""
        name = name.strip()
        from disco.core.llm.secret_refs import is_control_secret_ref

        if self._reserved_secret(name):
            route = "/api/openrouter/key" if name == "openrouter" else "/api/stripe/config/{app}"
            raise ValueError(f"use the dedicated {route} route for the {name} credential")
        if is_control_secret_ref(name):
            raise ValueError(f"{name} is an internal control secret and cannot be a provider ref")
        if not value.strip():
            raise ValueError("value is empty")
        try:
            self._secrets.set_secret(name, value.strip())
        except RuntimeError as exc:  # no DISCO_SECRET_KEY → can't encrypt
            raise ValueError(str(exc)) from exc
        return self.secret_status(name)

    def clear_secret(self, name: str) -> SecretStatus:
        name = name.strip()
        from disco.core.llm.secret_refs import is_control_secret_ref

        if self._reserved_secret(name):
            route = "/api/openrouter/key" if name == "openrouter" else "/api/stripe/config/{app}"
            raise ValueError(f"use the dedicated {route} route for the {name} credential")
        if is_control_secret_ref(name):
            raise ValueError(f"{name} is an internal control secret and cannot be cleared")
        self._secrets.clear_secret(name)
        return self.secret_status(name)

    def _resolve_secret_value(self, name: str) -> str | None:
        from disco.core.llm.secret_refs import resolve_provider_secret

        return resolve_provider_secret(name, self._secrets)

    async def test_secret(self, name: str) -> ProbeResult:
        """Probe T4.1: a cheap authenticated call against the OpenAI-compatible
        backend that USES this key, so a "green" means the provider really
        answered. The endpoint is resolved from the model catalogue (the
        ModelEntry whose ``api_key_env`` matches this name); the value is
        decrypted from the secret store. Honest failures (no endpoint, no value,
        bad key, host down) come back ok=False at HTTP 200."""
        cfg = self._store.load()
        # 1) Find an OpenAI-compatible model endpoint that consumes this key —
        # capture its model_id too, so the probe can do a real authenticated
        # completion (a key that only passes an UNauthenticated /models GET would
        # be a false green — OpenRouter serves /models without a key).
        base_url: str | None = None
        model_id: str = ""
        provider: str = ""
        for entry in cfg.models.values():
            if entry.api_key_env == name and entry.base_url:
                base_url = entry.base_url
                model_id = entry.model_id
                provider = entry.provider
                break
        if base_url is None:
            # Maybe a search/extraction key (no /models endpoint) — point the
            # user at the right probe rather than failing opaquely.
            ds_envs = {
                cfg.search.api_key_env,
                cfg.extraction.api_key_env,
            }
            if name in ds_envs and name:
                return ProbeResult(
                    ok=False,
                    status="misconfigured",
                    detail=(
                        f"{name} is used by a data source, which has no models "
                        "list to test. Use 'Test connection' under Data sources."
                    ),
                )
            return ProbeResult(
                ok=False,
                status="misconfigured",
                detail=(
                    f"No configured model references {name}. Assign it as a "
                    "model's api_key_env (Models), then test it there."
                ),
            )
        if gate := _origin_wiring.probe_approval_gate(
            self._store,
            "model",
            base_url,
            provider=provider,
            secret_ref=name,
            secrets=self._secrets,
            require_secret_ref_allowed=True,
        ):
            return gate
        # 2) Decrypt the stored value (or fall back to env).
        value = self._resolve_secret_value(name)
        if not value:
            locked = self._secrets.locked
            return ProbeResult(
                ok=False,
                status="misconfigured",
                detail=(
                    f"No decryptable value for {name} — "
                    + (
                        "the app secret can't decrypt it; re-enter the key."
                        if locked
                        else "store the key first."
                    )
                ),
            )
        # 3) The real, authenticated network call (a 1-token completion).
        from .probe_clients import probe_openai_auth

        ok, status, detail = await probe_openai_auth(base_url, value, model_id)
        return ProbeResult(ok=ok, status=status, detail=detail)
