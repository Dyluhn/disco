from __future__ import annotations

from disco.agent_server import ConversationRuntime
from disco.core import SqliteEventStore
from disco.core.llm import ConfigStore, SecretBox, SecretStore
from disco.core.llm.secret_refs import OPENROUTER_REF, set_openrouter_key
from disco.core.llm.secrets import OPENROUTER_API_KEY_ENV, OPENROUTER_API_KEY_ENV_LEGACY


def _runtime(tmp_path) -> ConversationRuntime:
    store = SecretStore(tmp_path / "secrets.json", box=SecretBox("app-secret"))
    store.set_secret("openai", "sk-openai-STORED")
    store.set_secret("search-tavily", "tvly-STORED")
    set_openrouter_key(store, "sk-or-v1-STORED")
    return ConversationRuntime(
        SqliteEventStore(":memory:"),
        config_store=ConfigStore(tmp_path / "config.json"),
        secret_store=store,
    )


def test_resolve_secret_reads_secret_store_refs_only(tmp_path, monkeypatch):
    rt = _runtime(tmp_path)
    monkeypatch.setenv("openai", "sk-openai-ENV")
    monkeypatch.setenv("SOME_OTHER_KEY", "from-env")

    assert rt._resolve_secret("openai") == "sk-openai-STORED"
    assert rt._resolve_secret("search-tavily") == "tvly-STORED"
    assert rt._resolve_secret("SOME_OTHER_KEY") is None
    assert rt._resolve_secret(None) is None
    assert rt._resolve_secret("") is None


def test_openrouter_legacy_refs_map_to_reserved_store_slot(tmp_path, monkeypatch):
    rt = _runtime(tmp_path)
    monkeypatch.setenv(OPENROUTER_API_KEY_ENV, "sk-or-v1-ENV")

    assert rt._resolve_secret(OPENROUTER_REF) == "sk-or-v1-STORED"
    assert rt._resolve_secret(OPENROUTER_API_KEY_ENV) == "sk-or-v1-STORED"
    assert rt._resolve_secret(OPENROUTER_API_KEY_ENV_LEGACY) == "sk-or-v1-STORED"


def test_control_refs_are_denied_even_when_stored(tmp_path):
    store = SecretStore(tmp_path / "secrets.json", box=SecretBox("app-secret"))
    store.set_secret("DISCO_SECRET_KEY", "stored-master-secret")
    rt = ConversationRuntime(
        SqliteEventStore(":memory:"),
        config_store=ConfigStore(tmp_path / "config.json"),
        secret_store=store,
    )

    assert rt._resolve_secret("DISCO_SECRET_KEY") is None
    assert rt._resolve_secret("PMX_SECRET_KEY") is None


def test_unavailable_store_does_not_fall_back_to_env(tmp_path, monkeypatch):
    monkeypatch.setenv("openai", "sk-openai-ENV")
    rt = ConversationRuntime(
        SqliteEventStore(":memory:"),
        config_store=ConfigStore(tmp_path / "config.json"),
        secret_store=SecretStore(tmp_path / "secrets.json", box=SecretBox(None)),
    )

    assert rt._resolve_secret("openai") is None
