"""Bounded regression: stored image-gen credential reference (app-server).

The real ``ConfigFeatures.update_image_gen_config`` + read + saved-file path
must preserve a stored uppercase reference while still quarantining unbacked
host-env names and control refs and canonicalizing allowlisted legacy names.
Synthetic private fixtures only; no network or external auth.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from disco.app_server.config.dtos import ImageGenConfigDTO
from disco.app_server.config_features import ConfigFeatures
from disco.core.llm import ConfigStore, SecretBox, SecretStore

APP_SECRET = "synthetic-app-secret-0123456789abcdefXYZ!@#"
IMAGE_URL = "https://acceptance-provider.invalid"
STORED_UPPER = ["IMAGE_ACCEPTANCE_KEY_NAME", "DISCO_ACCEPTANCE_XYZ"]


def _isolate(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("DISCO_SECRET_KEY", APP_SECRET)
    monkeypatch.setenv("DISCO_SECRETS", str(tmp_path / "secrets.json"))
    monkeypatch.setenv("DISCO_APPROVALS", str(tmp_path / "approvals.json"))
    monkeypatch.setenv("DISCO_CONFIG", str(tmp_path / "config.json"))
    for var in ("PMX_SECRET_KEY", "PMX_SECRETS", "PMX_APPROVALS", "PMX_CONFIG"):
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


def _features(tmp_path: Path) -> tuple[ConfigStore, SecretStore, ConfigFeatures]:
    store = ConfigStore(tmp_path / "config.json")
    secrets = SecretStore(tmp_path / "secrets.json", box=SecretBox(APP_SECRET))
    return store, secrets, ConfigFeatures(store, secrets)


def _disk_ref(config_path: Path) -> str | None:
    raw = json.loads(config_path.read_text(encoding="utf-8"))
    return raw.get("image_gen", {}).get("api_key_env")


@pytest.mark.parametrize("name", STORED_UPPER)
def test_update_preserves_stored_uppercase_ref(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, name: str
) -> None:
    """Stored ENV-shaped ref with a SecretStore record survives update/reload/disk."""
    _isolate(monkeypatch, tmp_path)
    monkeypatch.setenv("GITHUB_TOKEN", "synthetic-poison-value")
    store, secrets, features = _features(tmp_path)
    secrets.set_secret(name, f"synthetic-value-for-{name}")
    before = store.load()
    before_default = before.default_model

    dto = ImageGenConfigDTO(
        provider="openai", base_url=IMAGE_URL, api_key_env=name,
        model="synthetic-model", workflow_json="",
    )
    returned = features.update_image_gen_config(dto)

    assert returned.api_key_env == name
    assert returned.provider == "openai"
    assert returned.base_url == IMAGE_URL
    assert returned.model == "synthetic-model"
    assert returned.workflow_json == ""
    assert store.load().image_gen.api_key_env == name
    assert store.load().image_gen.provider == "openai"
    assert store.load().image_gen.base_url == IMAGE_URL
    assert store.load().image_gen.model == "synthetic-model"
    assert store.load().image_gen.workflow_json == ""
    assert _disk_ref(tmp_path / "config.json") == name
    assert json.loads((tmp_path / "config.json").read_text(encoding="utf-8"))[
        "image_gen"
    ]["workflow_json"] == ""
    assert secrets.get_secret(name) == f"synthetic-value-for-{name}"
    assert secrets.get_secret("GITHUB_TOKEN") is None
    after = store.load()
    assert after.default_model == before_default
    assert after.search == before.search
    disk_text = (tmp_path / "config.json").read_text(encoding="utf-8")
    assert f"synthetic-value-for-{name}" not in disk_text
    assert "synthetic-value-for-" not in returned.model_dump_json()


def test_update_controls_lowercase_poison_control_allowlist(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Controls: lowercase retained, unbacked poison and stored control cleared,
    allowlisted legacy name canonicalized."""
    _isolate(monkeypatch, tmp_path)
    monkeypatch.setenv("GITHUB_TOKEN", "synthetic-poison-value")
    cases: list[tuple[str, bool, str]] = [
        ("acceptance_key", True, "acceptance_key"),
        ("GITHUB_TOKEN", False, ""),
        ("DISCO_CONFIG", True, ""),
        ("OPENAI_API_KEY", True, "openai"),
    ]
    for name, stored, expected in cases:
        store, secrets, features = _features(tmp_path)
        # Fresh isolated files per sub-case.
        for p in ("config.json", "secrets.json", "approvals.json"):
            (tmp_path / p).unlink(missing_ok=True)
        store, secrets, features = _features(tmp_path)
        if stored:
            secrets.set_secret(name, f"synthetic-value-for-{name}")
        returned = features.update_image_gen_config(
            ImageGenConfigDTO(
                provider="openai", base_url=IMAGE_URL, api_key_env=name,
                model="synthetic-model", workflow_json="",
            )
        )
        assert returned.api_key_env == expected, name
        assert store.load().image_gen.api_key_env in (
            (expected,) if expected else (None, "")
        ), name
        assert _disk_ref(tmp_path / "config.json") in (
            (expected,) if expected else (None, "")
        ), name
