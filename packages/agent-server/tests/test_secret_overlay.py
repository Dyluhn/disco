"""Encrypt-all-keys: stored provider keys are overlaid into the build env and
resolved store-first, exactly like the OpenRouter key.

Proves the generalized path — ANY provider key (search/extract/TTS/paid model),
keyed by its api_key_env name, authenticates from the encrypted store without
the plaintext ever being in the process environment.
"""

from __future__ import annotations

from disco.agent_server import ConversationRuntime
from disco.core import SqliteEventStore
from disco.core.llm import ConfigStore, SecretBox, SecretStore
from disco.core.llm.secrets import OPENROUTER_API_KEY_ENV, OPENROUTER_API_KEY_ENV_LEGACY


def _runtime(tmp_path) -> ConversationRuntime:
    store = SecretStore(tmp_path / "secrets.json", box=SecretBox("app-secret"))
    store.set_secret("OPENAI_API_KEY", "sk-openai-STORED")
    store.set_secret("DISCO_SEARCH_API_KEY", "tvly-STORED")
    store.set_openrouter_key("sk-or-v1-STORED")
    return ConversationRuntime(
        SqliteEventStore(":memory:"),
        config_store=ConfigStore(tmp_path / "config.json"),
        secret_store=store,
    )


def test_overlay_puts_every_stored_key_into_the_env(tmp_path):
    rt = _runtime(tmp_path)
    env: dict[str, str] = {}
    rt._overlay_stored_secrets(env)
    # generic keys overlay onto their own env-var name
    assert env["OPENAI_API_KEY"] == "sk-openai-STORED"
    assert env["DISCO_SEARCH_API_KEY"] == "tvly-STORED"
    # the reserved openrouter slot maps to the OpenRouter env-var name — under BOTH
    # the canonical and legacy names, so config entries declaring either resolve the key
    assert env[OPENROUTER_API_KEY_ENV] == "sk-or-v1-STORED"
    assert env[OPENROUTER_API_KEY_ENV_LEGACY] == "sk-or-v1-STORED"  # PMX_OPENROUTER_API_KEY
    # the literal "openrouter" slot name is NOT leaked as an env var
    assert "openrouter" not in env


def test_overlay_wins_over_a_plaintext_env_var(tmp_path):
    rt = _runtime(tmp_path)
    env = {"OPENAI_API_KEY": "sk-openai-PLAINTEXT-ENV"}
    rt._overlay_stored_secrets(env)
    assert env["OPENAI_API_KEY"] == "sk-openai-STORED"  # encrypted source is authoritative


def test_resolve_prefers_store_then_falls_back_to_env(tmp_path, monkeypatch):
    rt = _runtime(tmp_path)
    # stored → store value
    assert rt._resolve_secret("OPENAI_API_KEY") == "sk-openai-STORED"
    # not stored but in env → env value (back-compat)
    monkeypatch.setenv("SOME_OTHER_KEY", "from-env")
    assert rt._resolve_secret("SOME_OTHER_KEY") == "from-env"
    # neither → None; empty name → None
    assert rt._resolve_secret("NOPE_KEY") is None
    assert rt._resolve_secret(None) is None
    assert rt._resolve_secret("") is None


def test_no_stored_secrets_is_a_noop(tmp_path):
    rt = ConversationRuntime(
        SqliteEventStore(":memory:"),
        config_store=ConfigStore(tmp_path / "config.json"),
        secret_store=SecretStore(tmp_path / "secrets.json", box=SecretBox(None)),
    )
    env = {"PRE": "existing"}
    rt._overlay_stored_secrets(env)
    assert env == {"PRE": "existing"}  # nothing added, nothing clobbered
