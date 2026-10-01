"""Bounded regression: stored credential-reference persistence (core).

Saving ``image_gen.api_key_env`` must keep a reference that already has a
record in ``SecretStore``, including ENV-shaped uppercase names, while still
quarantining unbacked host-env names (e.g. ``GITHUB_TOKEN``) and control refs
(e.g. ``DISCO_CONFIG``). Allowlisted legacy names still canonicalize to their
slot; lowercase and non-ENV names pass through. Resolution stays
store-authoritative and origin approval stays exact. Synthetic private fixtures
only; no network or external auth.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from disco.core.llm import ConfigStore, SecretBox, SecretStore
from disco.core.llm.config import ImageGenSettings
from disco.core.llm.secret_refs import (
    migrate_legacy_secret_ref,
    resolve_provider_secret,
    secret_ref_allowed_for_origin,
)

APP_SECRET = "synthetic-app-secret-0123456789abcdefXYZ!@#"
WRONG_KEY = "entirely-different-synthetic-key-9876543210!"
IMAGE_URL = "https://acceptance-provider.invalid"
STORED_UPPER = ["IMAGE_ACCEPTANCE_KEY_NAME", "DISCO_ACCEPTANCE_XYZ"]


def _isolate(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("DISCO_SECRET_KEY", APP_SECRET)
    monkeypatch.setenv("DISCO_SECRETS", str(tmp_path / "secrets.json"))
    monkeypatch.setenv("DISCO_APPROVALS", str(tmp_path / "approvals.json"))
    monkeypatch.setenv("DISCO_CONFIG", str(tmp_path / "config.json"))
    for var in (
        "PMX_SECRET_KEY",
        "PMX_SECRETS",
        "PMX_APPROVALS",
        "PMX_CONFIG",
        "PMX_DATA_DIR",
        "DISCO_DATA_DIR",
    ):
        monkeypatch.delenv(var, raising=False)
    for var in (
        "OPENROUTER_API_KEY",
        "DISCO_OPENROUTER_API_KEY",
        "PMX_OPENROUTER_API_KEY",
        "DISCO_GEMMA_API_KEY",
        "PMX_GEMMA_API_KEY",
        "TAVILY_API_KEY",
        "DISCO_TAVILY_API_KEY",
        "PMX_TAVILY_API_KEY",
        "BRAVE_SEARCH_API_KEY",
        "BRAVE_API_KEY",
        "DISCO_BRAVE_SEARCH_API_KEY",
        "PMX_BRAVE_SEARCH_API_KEY",
        "SEMANTIC_SCHOLAR_API_KEY",
        "S2_API_KEY",
        "DISCO_SEMANTIC_SCHOLAR_API_KEY",
        "PMX_SEMANTIC_SCHOLAR_API_KEY",
        "EXA_API_KEY",
        "DISCO_EXA_API_KEY",
        "PARALLEL_API_KEY",
        "DISCO_PARALLEL_API_KEY",
        "FIRECRAWL_API_KEY",
        "DISCO_FIRECRAWL_API_KEY",
        "PMX_FIRECRAWL_API_KEY",
        "OPENAI_API_KEY",
        "DISCO_OPENAI_API_KEY",
        "PMX_OPENAI_API_KEY",
        "GITHUB_TOKEN",
    ):
        monkeypatch.delenv(var, raising=False)


def _stores(tmp_path: Path) -> tuple[ConfigStore, SecretStore]:
    store = ConfigStore(tmp_path / "config.json")
    secrets = SecretStore(tmp_path / "secrets.json", box=SecretBox(APP_SECRET))
    return store, secrets


def _save_image_ref(
    store: ConfigStore, ref: str, *, url: str = IMAGE_URL
) -> None:
    store.sections.save_image_gen(
        ImageGenSettings(provider="openai", base_url=url, api_key_env=ref,
                         model="synthetic-model")
    )


def _disk_image_ref(config_path: Path) -> str | None:
    raw = json.loads(config_path.read_text(encoding="utf-8"))
    return raw.get("image_gen", {}).get("api_key_env")


@pytest.mark.parametrize("name", STORED_UPPER)
def test_stored_uppercase_ref_survives_save_reload_and_disk(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, name: str
) -> None:
    """Stored ENV-shaped ref with a SecretStore record persists verbatim."""
    _isolate(monkeypatch, tmp_path)
    monkeypatch.setenv("GITHUB_TOKEN", "synthetic-poison-value")
    store, secrets = _stores(tmp_path)
    secrets.set_secret(name, f"synthetic-value-for-{name}")
    before_default = store.load().default_model
    before_search = store.load().search

    _save_image_ref(store, name)

    reloaded = store.load()
    assert reloaded.image_gen.api_key_env == name
    assert reloaded.image_gen.provider == "openai"
    assert reloaded.image_gen.base_url == IMAGE_URL
    assert reloaded.image_gen.model == "synthetic-model"
    assert reloaded.image_gen.workflow_json == ""
    assert _disk_image_ref(tmp_path / "config.json") == name
    assert secrets.get_secret(name) == f"synthetic-value-for-{name}"
    assert secrets.get_secret("GITHUB_TOKEN") is None
    assert reloaded.default_model == before_default
    assert reloaded.search == before_search
    assert "synthetic-value-for-" not in (tmp_path / "config.json").read_text(
        encoding="utf-8"
    )


def test_lowercase_stored_ref_retained_as_control(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _isolate(monkeypatch, tmp_path)
    store, secrets = _stores(tmp_path)
    secrets.set_secret("acceptance_key", "synthetic-lower-value")
    _save_image_ref(store, "acceptance_key")
    assert store.load().image_gen.api_key_env == "acceptance_key"
    assert _disk_image_ref(tmp_path / "config.json") == "acceptance_key"
    assert secrets.get_secret("acceptance_key") == "synthetic-lower-value"


def test_unbacked_host_env_name_quarantined_as_control(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _isolate(monkeypatch, tmp_path)
    monkeypatch.setenv("GITHUB_TOKEN", "synthetic-poison-value")
    store, secrets = _stores(tmp_path)
    _save_image_ref(store, "GITHUB_TOKEN")
    assert store.load().image_gen.api_key_env in (None, "")
    assert _disk_image_ref(tmp_path / "config.json") in (None, "")
    assert secrets.get_secret("GITHUB_TOKEN") is None
    assert "synthetic-poison-value" not in (tmp_path / "config.json").read_text(
        encoding="utf-8"
    )


def test_stored_control_ref_quarantined_as_control(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _isolate(monkeypatch, tmp_path)
    store, secrets = _stores(tmp_path)
    secrets.set_secret("DISCO_CONFIG", "synthetic-control-value")
    _save_image_ref(store, "DISCO_CONFIG")
    assert store.load().image_gen.api_key_env in (None, "")
    # Ciphertext itself is untouched; only the reference is refused authority.
    assert secrets.get_secret("DISCO_CONFIG") == "synthetic-control-value"


def test_allowlisted_legacy_name_canonicalizes_as_control(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _isolate(monkeypatch, tmp_path)
    store, secrets = _stores(tmp_path)
    secrets.set_secret("OPENAI_API_KEY", "synthetic-openai-value")
    _save_image_ref(store, "OPENAI_API_KEY")
    assert store.load().image_gen.api_key_env == "openai"


def test_resolver_honors_stored_ref_and_rejects_control(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _isolate(monkeypatch, tmp_path)
    _, secrets = _stores(tmp_path)
    secrets.set_secret("IMAGE_ACCEPTANCE_KEY_NAME", "synthetic-resolver-value")
    secrets.set_secret("DISCO_CONFIG", "synthetic-control-value")
    assert (
        resolve_provider_secret("IMAGE_ACCEPTANCE_KEY_NAME", secrets)
        == "synthetic-resolver-value"
    )
    assert resolve_provider_secret("DISCO_CONFIG", secrets) is None
    assert resolve_provider_secret("GITHUB_TOKEN", secrets) is None


def test_pinned_origin_negative_control(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _isolate(monkeypatch, tmp_path)
    assert secret_ref_allowed_for_origin("openrouter", "https://openrouter.ai")
    assert not secret_ref_allowed_for_origin(
        "openrouter", "https://acceptance-provider.invalid"
    )
    # Arbitrary stored refs carry no pin: allowed anywhere (no implicit grant).
    assert secret_ref_allowed_for_origin(
        "IMAGE_ACCEPTANCE_KEY_NAME", "https://acceptance-provider.invalid"
    )


def test_exact_origin_purpose_approval_and_cross_origin_negative(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _isolate(monkeypatch, tmp_path)
    store, secrets = _stores(tmp_path)
    store.approvals.approve_origin(
        IMAGE_URL, "image:openai", "IMAGE_ACCEPTANCE_KEY_NAME",
        secret_store=secrets,
    )
    assert store.approvals.origin_approved(
        IMAGE_URL, "image:openai", "IMAGE_ACCEPTANCE_KEY_NAME",
        secret_store=secrets,
    )
    assert not store.approvals.origin_approved(
        IMAGE_URL, "image:openai", "DISCO_ACCEPTANCE_XYZ",
        secret_store=secrets,
    )
    assert not store.approvals.origin_approved(
        "https://other-provider.invalid", "image:openai",
        "IMAGE_ACCEPTANCE_KEY_NAME", secret_store=secrets,
    )


def test_legacy_import_gated_on_origin_approval(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Canonical allowlist import happens only with exact origin approval;
    the ref canonicalizes either way but the env value is never smuggled."""
    _isolate(monkeypatch, tmp_path)
    monkeypatch.setenv("DISCO_GEMMA_API_KEY", "synthetic-gemma-value")
    _, secrets = _stores(tmp_path)
    denied: list[str] = []
    ref = migrate_legacy_secret_ref(
        "DISCO_GEMMA_API_KEY", slot="gemma",
        url="http://192.0.2.10:8087/v1", purpose="model:gemma",
        store=secrets, diagnostics=denied,
        origin_approved=lambda url, purpose, r: False,
    )
    assert ref == "gemma"
    assert secrets.get_secret("gemma") is None
    assert any("awaiting operator approval" in d for d in denied)

    allowed: list[str] = []
    ref2 = migrate_legacy_secret_ref(
        "DISCO_GEMMA_API_KEY", slot="gemma",
        url="http://192.0.2.10:8087/v1", purpose="model:gemma",
        store=secrets, diagnostics=allowed,
        origin_approved=lambda url, purpose, r: True,
    )
    assert ref2 == "gemma"
    assert secrets.get_secret("gemma") == "synthetic-gemma-value"


def test_locked_store_retains_ref_without_credential_use(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Wrong-key lock keeps the named reference while yielding no credential."""
    _isolate(monkeypatch, tmp_path)
    store, secrets = _stores(tmp_path)
    secrets.set_secret("IMAGE_ACCEPTANCE_KEY_NAME", "synthetic-locked-value")
    _save_image_ref(store, "IMAGE_ACCEPTANCE_KEY_NAME")
    # Same files, wrong app secret: ciphertext present but undecryptable.
    locked = SecretStore(tmp_path / "secrets.json", box=SecretBox(WRONG_KEY))
    assert locked.locked
    assert locked.get_secret("IMAGE_ACCEPTANCE_KEY_NAME") is None
    assert resolve_provider_secret("IMAGE_ACCEPTANCE_KEY_NAME", locked) is None
    # Migration still retains the stored name (record presence only): no
    # decrypt, no env import, and still no credential use.
    retained_diagnostics: list[str] = []
    retained = migrate_legacy_secret_ref(
        "IMAGE_ACCEPTANCE_KEY_NAME", slot="image_gen",
        url=IMAGE_URL, purpose="image:openai",
        store=locked, diagnostics=retained_diagnostics,
        origin_approved=lambda url, purpose, r: False,
    )
    assert retained == "IMAGE_ACCEPTANCE_KEY_NAME"
    assert resolve_provider_secret("IMAGE_ACCEPTANCE_KEY_NAME", locked) is None
    assert locked.get_secret("IMAGE_ACCEPTANCE_KEY_NAME") is None
    # Persisted reference string survives even under the wrong-key env.
    monkeypatch.setenv("DISCO_SECRET_KEY", WRONG_KEY)
    assert (
        ConfigStore(tmp_path / "config.json").load().image_gen.api_key_env
        == "IMAGE_ACCEPTANCE_KEY_NAME"
    )
    assert _disk_image_ref(tmp_path / "config.json") == "IMAGE_ACCEPTANCE_KEY_NAME"
    # Negatives: unbacked host-env name still quarantined, stored control ref
    # still rejected.
    monkeypatch.setenv("GITHUB_TOKEN", "synthetic-poison-value")
    poison_diagnostics: list[str] = []
    assert (
        migrate_legacy_secret_ref(
            "GITHUB_TOKEN", slot="image_gen",
            url=IMAGE_URL, purpose="image:openai",
            store=locked, diagnostics=poison_diagnostics,
            origin_approved=lambda url, purpose, r: False,
        )
        == ""
    )
    assert locked.get_secret("GITHUB_TOKEN") is None
    control_diagnostics: list[str] = []
    assert (
        migrate_legacy_secret_ref(
            "DISCO_CONFIG", slot="image_gen",
            url=IMAGE_URL, purpose="image:openai",
            store=secrets, diagnostics=control_diagnostics,
            origin_approved=lambda url, purpose, r: False,
        )
        == ""
    )
