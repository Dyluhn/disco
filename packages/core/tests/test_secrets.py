"""SecretBox / SecretStore — encrypted-at-rest secrets (the OpenRouter key).

The plaintext must never appear on disk; the stored key survives restarts but is
only usable with the right app secret (else the store is `locked` and yields None).
"""

from __future__ import annotations

from pathlib import Path

from perpleximanus.core.llm import SecretBox, SecretStore
from perpleximanus.core.llm.secrets import _default_secrets_path


def test_box_round_trips_and_ciphertext_is_not_plaintext():
    box = SecretBox("app-secret-xyz")
    token = box.encrypt("sk-or-v1-supersecret")
    assert token != "sk-or-v1-supersecret"
    assert "supersecret" not in token
    assert box.decrypt(token) == "sk-or-v1-supersecret"


def test_wrong_app_secret_cannot_decrypt():
    token = SecretBox("correct-secret").encrypt("sk-or-v1-key")
    assert SecretBox("WRONG-secret").decrypt(token) is None


def test_unavailable_box_cannot_encrypt_or_decrypt():
    box = SecretBox(None)
    assert not box.available
    assert box.decrypt("anything") is None


def test_store_persists_encrypted_and_plaintext_not_on_disk(tmp_path):
    path = tmp_path / "secrets.json"
    store = SecretStore(path, box=SecretBox("app-secret"))
    store.set_openrouter_key("sk-or-v1-PLAINTEXT")
    # the file holds only ciphertext
    raw = path.read_text()
    assert "PLAINTEXT" not in raw
    # a fresh store with the same app secret reads it back (survives "restart")
    assert (
        SecretStore(path, box=SecretBox("app-secret")).get_openrouter_key() == "sk-or-v1-PLAINTEXT"
    )


def test_store_locked_without_app_secret(tmp_path):
    path = tmp_path / "secrets.json"
    SecretStore(path, box=SecretBox("app-secret")).set_openrouter_key("sk-or-v1-key")
    # restart WITHOUT the app secret: key is present but locked, yields None
    locked = SecretStore(path, box=SecretBox(None))
    assert locked.has_openrouter_key()
    assert locked.locked
    assert locked.get_openrouter_key() is None


def test_cannot_store_without_app_secret(tmp_path):
    store = SecretStore(tmp_path / "secrets.json", box=SecretBox(None))
    assert not store.can_store
    try:
        store.set_openrouter_key("sk-or-v1-key")
        raise AssertionError("expected RuntimeError")
    except RuntimeError as exc:
        assert "PMX_SECRET_KEY" in str(exc)


def test_clear_removes_the_key(tmp_path):
    path = tmp_path / "secrets.json"
    store = SecretStore(path, box=SecretBox("app-secret"))
    store.set_openrouter_key("sk-or-v1-key")
    store.clear_openrouter_key()
    assert not store.has_openrouter_key()
    assert store.get_openrouter_key() is None


# ---- default path: out of the project tree (security defense-in-depth) -------


def test_default_path_is_under_xdg_config(monkeypatch, tmp_path):
    """With no explicit path / PMX_SECRETS and no legacy file, the default lands in
    the user config dir — NOT the cwd/project tree where credential ciphertext could
    be read by anything granted the working directory."""
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    monkeypatch.delenv("PMX_SECRETS", raising=False)
    monkeypatch.chdir(tmp_path)  # ensure cwd has no legacy file
    assert _default_secrets_path() == tmp_path / "perpleximanus" / "secrets.json"


def test_default_path_honors_legacy_in_tree_file(monkeypatch, tmp_path):
    """Back-compat: an existing legacy ./perpleximanus-secrets.json is still used, so a
    running deployment that relies on it keeps working after the relocation."""
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))  # empty → no new file
    monkeypatch.delenv("PMX_SECRETS", raising=False)
    monkeypatch.chdir(tmp_path)
    Path("perpleximanus-secrets.json").write_text("{}")
    assert _default_secrets_path() == Path("perpleximanus-secrets.json")


def test_default_path_xdg_wins_over_legacy(monkeypatch, tmp_path):
    """Once the XDG file exists (post-migration), it takes precedence over any
    lingering legacy in-tree file."""
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    monkeypatch.delenv("PMX_SECRETS", raising=False)
    monkeypatch.chdir(tmp_path)
    Path("perpleximanus-secrets.json").write_text("{}")
    new = tmp_path / "perpleximanus" / "secrets.json"
    new.parent.mkdir(parents=True)
    new.write_text("{}")
    assert _default_secrets_path() == new


def test_explicit_path_and_env_override_default(monkeypatch, tmp_path):
    """An explicit path beats PMX_SECRETS, which beats the computed default."""
    monkeypatch.setenv("PMX_SECRETS", str(tmp_path / "from_env.json"))
    explicit = tmp_path / "explicit.json"
    assert SecretStore(explicit)._path == explicit  # explicit wins
    assert SecretStore()._path == tmp_path / "from_env.json"  # else PMX_SECRETS
