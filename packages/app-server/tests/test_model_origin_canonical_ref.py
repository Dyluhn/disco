"""Regression (live bug, 2026-07-06): an OpenRouter model's origin approval must be
written under the CANONICAL secret-ref the store normalizes to (the reserved
``openrouter`` slot), NOT the raw submitted ``api_key_env`` (``DISCO_OPENROUTER_API_KEY``).

When the two diverge, ``build_providers`` checks approval under the canonical ref,
finds none, and SKIPS the model ("origin is not operator-approved") — which then (a)
crashes deep research with an uncaught ``KeyError`` on the missing provider and (b)
blocks the env-secret import so the OpenRouter key never becomes usable and image gen
reports "not configured". Approving the STORED (canonical) entry fixes all of it.

Also: the OpenRouter key-status ``locked`` flag must reflect ACTUAL decryptability,
so the UI prompts "re-enter the key" over a key encrypted under a rotated/old app
secret instead of showing a false green.
"""

from __future__ import annotations

from disco.app_server.config.dtos import ModelUpsert
from disco.app_server.config_state import ConfigState
from disco.core import SkillStore
from disco.core.llm import ConfigStore, SecretBox, SecretStore

OPENROUTER_BASE = "https://openrouter.ai/api/v1"


def _state(tmp_path, secret: str = "test-app-secret") -> ConfigState:
    return ConfigState(
        store=ConfigStore(tmp_path / "config.json"),
        secrets=SecretStore(tmp_path / "secrets.json", box=SecretBox(secret)),
        skills=SkillStore(tmp_path / "skills"),
    )


def test_add_openrouter_model_approves_canonical_slot_ref(tmp_path, monkeypatch):
    # Isolate the migration's internal SecretStore() to tmp + a known app secret, and
    # remove any ambient OpenRouter env key so the load can't import into real data.
    monkeypatch.setenv("DISCO_SECRET_KEY", "test-app-secret")
    monkeypatch.setenv("DISCO_SECRETS", str(tmp_path / "secrets.json"))
    monkeypatch.delenv("DISCO_OPENROUTER_API_KEY", raising=False)
    monkeypatch.delenv("PMX_OPENROUTER_API_KEY", raising=False)

    state = _state(tmp_path)
    # The Add flow submits the RAW env-var name; the store normalizes it to the
    # 'openrouter' slot on load (key starts with 'or-').
    state.models.add_model(
        ModelUpsert(
            id="or-deepseek-x",
            model_id="deepseek/x",
            base_url=OPENROUTER_BASE,
            api_key_env="DISCO_OPENROUTER_API_KEY",
        )
    )

    # The approval MUST be under the canonical ref wiring + the env-import check.
    assert state._store.approvals.origin_approved(
        OPENROUTER_BASE, "model:or-deepseek-x", "openrouter", secret_store=state._secrets
    )
    # ...and NOT under the raw submitted ref (the pre-fix behaviour that broke wiring).
    assert not state._store.approvals.origin_approved(
        OPENROUTER_BASE,
        "model:or-deepseek-x",
        "DISCO_OPENROUTER_API_KEY",
        secret_store=state._secrets,
    )


def test_openrouter_key_status_locked_when_ciphertext_undecryptable(tmp_path):
    # Store a key under app secret A ...
    a = _state(tmp_path, secret="app-secret-A")
    a.openrouter.set_openrouter_key("sk-or-unit-test-key")
    assert a.openrouter.openrouter_key_status().configured
    assert not a.openrouter.openrouter_key_status().locked

    # ... then read the SAME secrets file through a DIFFERENT app secret (rotated key):
    # ciphertext is present but no longer decryptable. Must report locked (re-enter),
    # never a false green.
    b = _state(tmp_path, secret="app-secret-B-rotated")
    status = b.openrouter.openrouter_key_status()
    assert status.configured  # ciphertext IS present
    assert status.locked  # ...but undecryptable -> UI prompts re-entry


def test_openrouter_key_status_is_locked_for_missing_named_rotation_key(tmp_path):
    path = tmp_path / "secrets.json"
    SecretStore(path, box=SecretBox("original-secret", key_id="original-key")).set_secret(
        "openrouter", "sk-or-unit-test-key"
    )
    state = ConfigState(
        store=ConfigStore(tmp_path / "config.json"),
        secrets=SecretStore(path, box=SecretBox("replacement-secret", key_id="replacement-key")),
        skills=SkillStore(tmp_path / "skills"),
    )

    status = state.openrouter.openrouter_key_status()

    assert status.configured
    assert status.locked
