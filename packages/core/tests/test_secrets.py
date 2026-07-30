"""SecretBox / SecretStore — encrypted-at-rest secrets (the OpenRouter key).

The plaintext must never appear on disk; the stored key survives restarts but is
only usable with the right app secret (else the store is `locked` and yields None).
"""

from __future__ import annotations

import stat
import sys
from pathlib import Path

import pytest
from disco.core.llm import SecretBox, SecretStore, ensure_process_secret_key
from disco.core.llm.secrets import SecretRotationError, _default_secrets_path


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


def test_generic_named_secrets_round_trip(tmp_path):
    path = tmp_path / "secrets.json"
    store = SecretStore(path, box=SecretBox("app-secret"))
    store.set_secret("OPENAI_API_KEY", "sk-openai-PLAIN")
    store.set_secret("DISCO_SEARCH_API_KEY", "tvly-PLAIN")

    # ciphertext only on disk
    raw = path.read_text()
    assert "PLAIN" not in raw

    fresh = SecretStore(path, box=SecretBox("app-secret"))
    assert fresh.get_secret("OPENAI_API_KEY") == "sk-openai-PLAIN"
    assert fresh.get_secret("DISCO_SEARCH_API_KEY") == "tvly-PLAIN"
    assert fresh.has_secret("OPENAI_API_KEY")
    assert not fresh.has_secret("NOPE")
    assert set(fresh.secret_names()) == {"OPENAI_API_KEY", "DISCO_SEARCH_API_KEY"}

    fresh.clear_secret("OPENAI_API_KEY")
    assert not SecretStore(path, box=SecretBox("app-secret")).has_secret("OPENAI_API_KEY")


def test_openrouter_wrappers_use_the_reserved_slot(tmp_path):
    """The legacy openrouter helpers operate on the generic store under the
    'openrouter' name — back-compat with existing secrets.json files."""
    path = tmp_path / "secrets.json"
    store = SecretStore(path, box=SecretBox("app-secret"))
    store.set_openrouter_key("sk-or-v1-KEY")
    assert store.get_secret("openrouter") == "sk-or-v1-KEY"
    assert store.get_openrouter_key() == "sk-or-v1-KEY"
    assert "openrouter" in store.secret_names()


def test_undecryptable_names_with_wrong_app_secret(tmp_path):
    """A store opened under a DIFFERENT app secret can't decrypt prior ciphertext
    → undecryptable_names lists exactly the stored keys (so the UI can name them)."""
    path = tmp_path / "secrets.json"
    SecretStore(path, box=SecretBox("right-secret")).set_secret("OPENAI_API_KEY", "sk-x")
    SecretStore(path, box=SecretBox("right-secret")).set_secret("TAVILY_API_KEY", "tvly-y")

    wrong = SecretStore(path, box=SecretBox("WRONG-secret"))
    assert sorted(wrong.undecryptable_names()) == ["OPENAI_API_KEY", "TAVILY_API_KEY"]

    # the right secret decrypts everything → none undecryptable
    right = SecretStore(path, box=SecretBox("right-secret"))
    assert right.undecryptable_names() == []


def test_weak_secret_predicate():
    from disco.core.llm.secrets import _looks_weak

    assert _looks_weak("short")  # too short
    assert _looks_weak("aaaaaaaaaaaaaaaaaaaa")  # long but low diversity
    assert not _looks_weak("Xk7$pQ2!mZ9vRt4wLn8c")  # 20 chars, diverse
    # a real `openssl rand -base64 32`-style value passes
    assert not _looks_weak("uF3kP1xV9bQwRtY2mN8sJ6hL0cZ4dA7gK5eB3rT9oM=")


def test_weak_secret_logs_a_warning(caplog):
    import disco.core.llm.secrets as secmod

    secmod._weak_secret_warned = False  # reset the once-per-process latch
    with caplog.at_level("WARNING", logger="disco.secrets"):
        SecretBox("weak")  # short → should warn
    assert any("low-entropy" in r.message for r in caplog.records)


def test_strong_secret_does_not_warn(caplog):
    import disco.core.llm.secrets as secmod

    secmod._weak_secret_warned = False
    with caplog.at_level("WARNING", logger="disco.secrets"):
        SecretBox("uF3kP1xV9bQwRtY2mN8sJ6hL0cZ4dA7gK5eB3rT9oM=")
    assert not any("low-entropy" in r.message for r in caplog.records)


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX file modes")
def test_secrets_file_and_dir_are_owner_only(tmp_path):
    """The credential file is written 0600 and its dir 0700 — another local user
    can't even copy the ciphertext (defense-in-depth atop the app-secret)."""
    secret_dir = tmp_path / "cfg"
    path = secret_dir / "secrets.json"
    store = SecretStore(path, box=SecretBox("app-secret"))
    store.set_openrouter_key("sk-or-v1-PLAINTEXT")

    file_mode = stat.S_IMODE(path.stat().st_mode)
    dir_mode = stat.S_IMODE(secret_dir.stat().st_mode)
    assert file_mode == 0o600, f"secrets file is {oct(file_mode)}, expected 0o600"
    assert dir_mode == 0o700, f"secrets dir is {oct(dir_mode)}, expected 0o700"
    # no group/other read bit on the file, ever
    assert not (file_mode & (stat.S_IRGRP | stat.S_IROTH | stat.S_IWGRP | stat.S_IWOTH))


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
        assert "DISCO_SECRET_KEY" in str(exc)


def test_clear_removes_the_key(tmp_path):
    path = tmp_path / "secrets.json"
    store = SecretStore(path, box=SecretBox("app-secret"))
    store.set_openrouter_key("sk-or-v1-key")
    store.clear_openrouter_key()
    assert not store.has_openrouter_key()
    assert store.get_openrouter_key() is None


def test_key_rotation_reencrypts_every_secret_atomically(tmp_path):
    path = tmp_path / "secrets.json"
    store = SecretStore(path, box=SecretBox("old-app-secret"))
    store.set_secret("OPENAI_API_KEY", "sk-openai-plain")
    store.set_openrouter_key("sk-openrouter-plain")
    before = path.read_bytes()

    store.rotate_key("new-app-secret")

    after = path.read_bytes()
    assert after != before
    assert b"plain" not in after
    assert SecretStore(path, box=SecretBox("old-app-secret")).undecryptable_names()
    restarted = SecretStore(path, box=SecretBox("new-app-secret"))
    assert restarted.get_secret("OPENAI_API_KEY") == "sk-openai-plain"
    assert restarted.get_openrouter_key() == "sk-openrouter-plain"
    assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_key_rotation_with_wrong_current_key_preserves_original_bytes(tmp_path):
    path = tmp_path / "secrets.json"
    SecretStore(path, box=SecretBox("correct-old-secret")).set_secret("KEY", "value")
    before = path.read_bytes()
    wrong = SecretStore(path, box=SecretBox("wrong-old-secret"))

    with pytest.raises(SecretRotationError, match="undecryptable"):
        wrong.rotate_key("new-secret")

    assert path.read_bytes() == before
    assert SecretStore(path, box=SecretBox("correct-old-secret")).get_secret("KEY") == "value"


def test_key_rotation_refuses_corrupt_ciphertext_store(tmp_path):
    path = tmp_path / "secrets.json"
    path.write_text("{broken")
    before = path.read_bytes()

    with pytest.raises(SecretRotationError, match="corrupt"):
        SecretStore(path, box=SecretBox("old-secret")).rotate_key("new-secret")

    assert path.read_bytes() == before


# ---- default path: out of the project tree (security defense-in-depth) -------


def test_default_path_is_under_xdg_config(monkeypatch, tmp_path):
    """With no explicit path / PMX_SECRETS and no legacy file, the default lands in
    the user config dir — NOT the cwd/project tree where credential ciphertext could
    be read by anything granted the working directory."""
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    monkeypatch.delenv("PMX_SECRETS", raising=False)
    monkeypatch.chdir(tmp_path)  # ensure cwd has no legacy file
    assert _default_secrets_path() == tmp_path / "disco" / "secrets.json"


def test_default_path_honors_legacy_in_tree_file(monkeypatch, tmp_path):
    """Back-compat: an existing legacy ./disco-secrets.json is still used, so a
    running deployment that relies on it keeps working after the relocation."""
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))  # empty → no new file
    monkeypatch.delenv("PMX_SECRETS", raising=False)
    monkeypatch.chdir(tmp_path)
    Path("disco-secrets.json").write_text("{}")
    assert _default_secrets_path() == Path("disco-secrets.json")


def test_default_path_xdg_wins_over_legacy(monkeypatch, tmp_path):
    """Once the XDG file exists (post-migration), it takes precedence over any
    lingering legacy in-tree file."""
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    monkeypatch.delenv("PMX_SECRETS", raising=False)
    monkeypatch.chdir(tmp_path)
    Path("disco-secrets.json").write_text("{}")
    new = tmp_path / "disco" / "secrets.json"
    new.parent.mkdir(parents=True)
    new.write_text("{}")
    assert _default_secrets_path() == new


def test_explicit_path_and_env_override_default(monkeypatch, tmp_path):
    """An explicit path beats PMX_SECRETS, which beats the computed default."""
    monkeypatch.setenv("PMX_SECRETS", str(tmp_path / "from_env.json"))
    explicit = tmp_path / "explicit.json"
    assert SecretStore(explicit)._path == explicit  # explicit wins
    assert SecretStore()._path == tmp_path / "from_env.json"  # else PMX_SECRETS


# ---- process app secret autogeneration --------------------------------------


def test_process_secret_autogen_persists_and_reloads_stable(monkeypatch, tmp_path):
    monkeypatch.delenv("DISCO_SECRET_KEY", raising=False)
    monkeypatch.delenv("PMX_SECRET_KEY", raising=False)
    path = tmp_path / "data" / "secret-key"

    first = ensure_process_secret_key(path)
    assert first
    assert path.read_text().strip() == first
    assert SecretBox.from_env().available

    monkeypatch.delenv("DISCO_SECRET_KEY", raising=False)
    second = ensure_process_secret_key(path)
    assert second == first
    assert path.read_text().strip() == first


def test_process_secret_env_precedence(monkeypatch, tmp_path):
    monkeypatch.setenv("DISCO_SECRET_KEY", "env-secret-wins")
    monkeypatch.delenv("PMX_SECRET_KEY", raising=False)
    path = tmp_path / "secret-key"

    assert ensure_process_secret_key(path) == "env-secret-wins"
    assert not path.exists()

    monkeypatch.delenv("DISCO_SECRET_KEY", raising=False)
    monkeypatch.setenv("PMX_SECRET_KEY", "legacy-env-secret-wins")
    assert ensure_process_secret_key(path) == "legacy-env-secret-wins"
    assert not path.exists()


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX file modes")
def test_process_secret_file_is_owner_only(monkeypatch, tmp_path):
    monkeypatch.delenv("DISCO_SECRET_KEY", raising=False)
    monkeypatch.delenv("PMX_SECRET_KEY", raising=False)
    path = tmp_path / "data" / "secret-key"

    ensure_process_secret_key(path)

    file_mode = stat.S_IMODE(path.stat().st_mode)
    dir_mode = stat.S_IMODE(path.parent.stat().st_mode)
    assert file_mode == 0o600
    assert dir_mode == 0o700
