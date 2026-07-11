"""Provider secret-ref resolution and legacy env-name migration."""

from __future__ import annotations

import os
import re
from collections.abc import Callable, Iterable

from disco.core.host_egress import origin_for_url

from .secrets import (
    OPENROUTER_API_KEY_ENV,
    OPENROUTER_API_KEY_ENV_LEGACY,
    SecretStore,
)

OPENROUTER_REF = "openrouter"
GEMMA_REF = "gemma"

_CONTROL_REFS = frozenset(
    {
        "DISCO_SECRET_KEY",
        "PMX_SECRET_KEY",
        "DISCO_SECRETS",
        "PMX_SECRETS",
        "DISCO_CONFIG",
        "PMX_CONFIG",
        "DISCO_DATA_DIR",
        "PMX_DATA_DIR",
        "XDG_CONFIG_HOME",
        "XDG_DATA_HOME",
    }
)
_ENV_NAME_RE = re.compile(r"^[A-Z][A-Z0-9_]{2,}$")

_LEGACY_IMPORTS: dict[str, tuple[str, ...]] = {
    OPENROUTER_REF: (OPENROUTER_API_KEY_ENV, OPENROUTER_API_KEY_ENV_LEGACY),
    GEMMA_REF: ("DISCO_GEMMA_API_KEY", "PMX_GEMMA_API_KEY"),
    "tavily": ("TAVILY_API_KEY", "DISCO_TAVILY_API_KEY", "PMX_TAVILY_API_KEY"),
    "brave": (
        "BRAVE_SEARCH_API_KEY",
        "BRAVE_API_KEY",
        "DISCO_BRAVE_SEARCH_API_KEY",
        "PMX_BRAVE_SEARCH_API_KEY",
    ),
    "semantic_scholar": (
        "SEMANTIC_SCHOLAR_API_KEY",
        "S2_API_KEY",
        "DISCO_SEMANTIC_SCHOLAR_API_KEY",
        "PMX_SEMANTIC_SCHOLAR_API_KEY",
    ),
    "firecrawl": ("FIRECRAWL_API_KEY", "DISCO_FIRECRAWL_API_KEY", "PMX_FIRECRAWL_API_KEY"),
    "openai": ("OPENAI_API_KEY", "DISCO_OPENAI_API_KEY", "PMX_OPENAI_API_KEY"),
}
_PINNED_REF_ORIGINS: dict[str, frozenset[str]] = {
    OPENROUTER_REF: frozenset({"https://openrouter.ai"}),
    "stripe": frozenset({"https://api.stripe.com"}),
}


def is_control_secret_ref(ref: str | None) -> bool:
    return bool(ref and ref.strip() in _CONTROL_REFS)


def is_legacy_env_name(ref: str | None) -> bool:
    return bool(ref and _ENV_NAME_RE.fullmatch(ref.strip()))


def resolve_provider_secret(
    ref: str | None,
    store: SecretStore | None = None,
    *,
    strong_required: bool = False,
) -> str | None:
    """Resolve a provider secret by secret-ref id from SecretStore only."""
    if not ref:
        return None
    name = ref.strip()
    if not name or is_control_secret_ref(name):
        return None
    secret_store = store or SecretStore()
    if name in {OPENROUTER_REF, OPENROUTER_API_KEY_ENV, OPENROUTER_API_KEY_ENV_LEGACY}:
        if strong_required:
            return secret_store.get_secret(OPENROUTER_REF, strong_required=True)
        return secret_store.get_openrouter_key()
    return secret_store.get_secret(name, strong_required=strong_required)


def secret_ref_allowed_for_origin(ref: str | None, url: str | None) -> bool:
    if not ref:
        return True
    name = ref.strip()
    if name in {OPENROUTER_API_KEY_ENV, OPENROUTER_API_KEY_ENV_LEGACY}:
        name = OPENROUTER_REF
    allowed = _PINNED_REF_ORIGINS.get(name)
    if not allowed:
        return True
    origin = origin_for_url(url or "")
    return bool(origin and origin in allowed)


def allowed_legacy_refs(slot: str) -> tuple[str, ...]:
    return _LEGACY_IMPORTS.get(slot, ())


def canonical_ref_for(slot: str, ref: str) -> str | None:
    if ref in allowed_legacy_refs(slot):
        return slot
    return None


def migrate_legacy_secret_ref(
    ref: str | None,
    *,
    slot: str,
    url: str | None = None,
    purpose: str = "",
    store: SecretStore,
    diagnostics: list[str],
    origin_approved: Callable[[str, str, str | None], bool] | None = None,
) -> str:
    """Map one legacy env-name ref to a secret-ref id or quarantine it.

    Only the explicit per-slot allowlist may import from process env. Every other
    env-shaped name is rejected so a poisoned config cannot legitimize host env.
    """
    name = (ref or "").strip()
    if not name:
        return ""
    if is_control_secret_ref(name):
        diagnostics.append(f"Quarantined control secret ref {name!r} for {slot}")
        return ""
    canonical = canonical_ref_for(slot, name)
    if canonical is not None:
        _import_env_secret_if_approved(
            canonical,
            slot,
            url=url,
            purpose=purpose,
            store=store,
            diagnostics=diagnostics,
            origin_approved=origin_approved,
        )
        return canonical
    if name == slot:
        _import_env_secret_if_approved(
            name,
            slot,
            url=url,
            purpose=purpose,
            store=store,
            diagnostics=diagnostics,
            origin_approved=origin_approved,
        )
        return name
    if is_legacy_env_name(name):
        diagnostics.append(
            f"Quarantined legacy secret ref {name!r} for {slot}: not on the allowlist"
        )
        return ""
    return name


def _import_env_secret_if_approved(
    target_ref: str,
    slot: str,
    *,
    url: str | None,
    purpose: str,
    store: SecretStore,
    diagnostics: list[str],
    origin_approved: Callable[[str, str, str | None], bool] | None,
) -> None:
    if not allowed_legacy_refs(slot):
        return
    if not url or origin_approved is None or not origin_approved(url, purpose, target_ref):
        origin = origin_for_url(url or "") or "(no origin)"
        diagnostics.append(
            f"Not importing env secret for {target_ref!r}: origin {origin} "
            f"for {purpose or slot} awaiting operator approval"
        )
        return
    _import_env_secret(target_ref, allowed_legacy_refs(slot), store, diagnostics)


def _import_env_secret(
    target_ref: str,
    env_names: Iterable[str],
    store: SecretStore,
    diagnostics: list[str],
) -> None:
    if resolve_provider_secret(target_ref, store):
        return
    value = next((os.environ[n] for n in env_names if os.environ.get(n)), "")
    if not value:
        return
    if not store.can_store or store.locked:
        diagnostics.append(f"cannot migrate {target_ref!r}: SecretStore is unavailable or locked")
        return
    if target_ref == OPENROUTER_REF:
        store.set_openrouter_key(value)
    else:
        store.set_secret(target_ref, value)
