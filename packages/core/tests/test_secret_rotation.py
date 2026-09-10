"""Versioned SecretStore rotation and crash-safety contract."""

from __future__ import annotations

import json
import os
import stat
import subprocess
import sys
from pathlib import Path

import pytest
from disco.core.llm.secrets import (
    SecretBox,
    SecretDecryptionError,
    SecretStore,
    SecretStoreFormatError,
    UnknownSecretKeyError,
)

_OLD_SECRET = "old-secret-material-0123456789-ABCDE"
_NEW_SECRET = "new-secret-material-0123456789-VWXYZ"
_REPO = Path(__file__).resolve().parents[3]
_ROTATE_SCRIPT = _REPO / "development" / "scripts" / "rotate_secret_store.py"


def _old_box() -> SecretBox:
    return SecretBox(_OLD_SECRET, key_id="old-2026-01")


def _new_box() -> SecretBox:
    return SecretBox(_NEW_SECRET, key_id="new-2026-08")


def _json(path: Path) -> dict:
    value = json.loads(path.read_text(encoding="utf-8"))
    assert isinstance(value, dict)
    return value


def _run_rotation_cli(path: Path, action: str) -> subprocess.CompletedProcess[str]:
    environment = os.environ.copy()
    environment.update(
        {
            "DISCO_SECRET_KEY": _NEW_SECRET,
            "DISCO_SECRET_KEY_ID": "new-2026-08",
            "DISCO_SECRET_READ_KEYS": json.dumps({"old-2026-01": _OLD_SECRET}),
        }
    )
    return subprocess.run(
        [sys.executable, str(_ROTATE_SCRIPT), "--path", str(path), action],
        cwd=_REPO,
        env=environment,
        check=False,
        capture_output=True,
        text=True,
    )


def test_new_writes_carry_active_key_id_without_plaintext(tmp_path: Path) -> None:
    path = tmp_path / "secrets.json"
    store = SecretStore(path, box=_old_box())

    store.set_secret("OPENAI_API_KEY", "plaintext-value")

    raw = path.read_text(encoding="utf-8")
    document = _json(path)
    assert "plaintext-value" not in raw
    assert document["$format"] == "disco.secret-store"
    assert document["version"] == 2
    assert document["active_key_id"] == "old-2026-01"
    assert document["records"]["OPENAI_API_KEY"]["key_id"] == "old-2026-01"
    assert document["rotation"] is None


def test_legacy_read_and_next_write_upgrade_without_reencrypting_old_record(
    tmp_path: Path,
) -> None:
    path = tmp_path / "secrets.json"
    old_token = _old_box().encrypt("old-plaintext")
    path.write_text(json.dumps({"OLD": old_token}), encoding="utf-8")
    rotating = SecretStore(path, box=_new_box(), read_boxes=[_old_box()])

    assert rotating.get_secret("OLD") == "old-plaintext"
    rotating.set_secret("NEW", "new-plaintext")

    document = _json(path)
    assert document["records"]["OLD"] == {
        "key_id": "old-2026-01",
        "ciphertext": old_token,
    }
    assert document["records"]["NEW"]["key_id"] == "new-2026-08"
    assert "old-plaintext" not in path.read_text(encoding="utf-8")
    assert "new-plaintext" not in path.read_text(encoding="utf-8")


def test_old_read_window_is_bounded_and_key_ids_are_unique(tmp_path: Path) -> None:
    old_boxes = [
        SecretBox(f"old-material-{index}-0123456789", key_id=f"old-{index}") for index in range(5)
    ]
    with pytest.raises(ValueError, match="at most 4"):
        SecretStore(tmp_path / "too-many.json", box=_new_box(), read_boxes=old_boxes)

    with pytest.raises(ValueError, match="duplicate secret key ID"):
        SecretStore(tmp_path / "duplicate.json", box=_new_box(), read_boxes=[_new_box()])

    store = SecretStore(tmp_path / "read-only-rotation.json", box=_new_box())
    with pytest.raises(AttributeError):
        store.rotation = None  # type: ignore[misc]


def test_rotation_is_resumable_and_finalize_expires_old_key_window(tmp_path: Path) -> None:
    path = tmp_path / "secrets.json"
    old = SecretStore(path, box=_old_box())
    old.set_secret("A", "alpha-secret")
    old.set_secret("B", "beta-secret")

    rotating = SecretStore(path, box=_new_box(), read_boxes=[_old_box()])
    rotating.set_secret("C", "gamma-secret")
    assert rotating.rotation.reencrypt_to_active() == 3
    first_migrated_bytes = path.read_bytes()

    document = _json(path)
    assert rotating.rotation.pending
    assert {record["key_id"] for record in document["records"].values()} == {"new-2026-08"}
    assert document["rotation"]["from_key_ids"] == ["old-2026-01"]
    # C was already written with the new writer before the bulk migration, so
    # rollback returns to that exact mixed-document writer identity.
    assert document["rotation"]["from_active_key_id"] == "new-2026-08"
    assert document["rotation"]["rollback_records"]["A"]["key_id"] == "old-2026-01"
    assert rotating.get_secret("A") == "alpha-secret"
    assert rotating.get_secret("C") == "gamma-secret"

    # A resume after the atomic replace verifies rather than rewriting the file.
    assert rotating.rotation.reencrypt_to_active() == 3
    assert path.read_bytes() == first_migrated_bytes
    assert rotating.rotation.verify()
    assert rotating.rotation.finalize()
    assert not rotating.rotation.pending

    new_only = SecretStore(path, box=_new_box())
    assert new_only.get_secret("A") == "alpha-secret"
    with pytest.raises(UnknownSecretKeyError, match="new-2026-08"):
        SecretStore(path, box=_old_box()).get_secret("A")


def test_rotation_cli_resume_is_idempotent(tmp_path: Path) -> None:
    path = tmp_path / "secrets.json"
    SecretStore(path, box=_old_box()).set_secret("A", "alpha-secret")

    first = _run_rotation_cli(path, "resume")
    second = _run_rotation_cli(path, "resume")

    assert first.returncode == second.returncode == 0
    assert json.loads(first.stdout) == {"record_count": 1, "rotation_pending": True}
    assert json.loads(second.stdout) == {"record_count": 1, "rotation_pending": True}
    assert first.stderr == second.stderr == ""


@pytest.mark.parametrize(
    ("action", "result_field"),
    [("verify", "verified"), ("finalize", "finalized"), ("rollback", "rolled_back")],
)
def test_rotation_cli_noop_lifecycle_action_fails(
    action: str, result_field: str, tmp_path: Path
) -> None:
    result = _run_rotation_cli(tmp_path / "missing.json", action)

    assert result.returncode != 0
    assert json.loads(result.stdout)[result_field] is False
    assert result.stderr == ""


def test_rotation_cli_failure_never_prints_record_names_or_tracebacks(tmp_path: Path) -> None:
    path = tmp_path / "secrets.json"
    record_name = "SENSITIVE_PROVIDER_ACCOUNT_NAME"
    SecretStore(path, box=_old_box()).set_secret(record_name, "alpha-secret")
    SecretStore(path, box=_new_box(), read_boxes=[_old_box()]).rotation.reencrypt_to_active()
    document = _json(path)
    document["records"][record_name]["ciphertext"] = "invalid-token"
    path.write_text(json.dumps(document), encoding="utf-8")

    result = _run_rotation_cli(path, "verify")

    combined = result.stdout + result.stderr
    assert result.returncode != 0
    assert json.loads(result.stderr) == {"error": "SecretDecryptionError", "ok": False}
    assert result.stdout == ""
    assert record_name not in combined
    assert _OLD_SECRET not in combined
    assert _NEW_SECRET not in combined
    assert "Traceback" not in combined


def test_pending_rotation_requires_old_material_until_finalize(tmp_path: Path) -> None:
    path = tmp_path / "secrets.json"
    SecretStore(path, box=_old_box()).set_secret("A", "alpha-secret")
    rotating = SecretStore(path, box=_new_box(), read_boxes=[_old_box()])
    rotating.rotation.reencrypt_to_active()

    without_old = SecretStore(path, box=_new_box())
    with pytest.raises(UnknownSecretKeyError, match="old-2026-01"):
        without_old.get_secret("A")
    with pytest.raises(UnknownSecretKeyError, match="old-2026-01"):
        without_old.rotation.finalize()


def test_locked_v2_secret_can_be_cleared_without_key_material(tmp_path: Path) -> None:
    path = tmp_path / "secrets.json"
    original = SecretStore(path, box=_old_box())
    original.set_secret("A", "alpha-secret")
    original.set_secret("B", "beta-secret")

    locked = SecretStore(path, box=SecretBox(None))
    assert locked.undecryptable_names() == ["A", "B"]
    locked.clear_secret("A")

    document = _json(path)
    assert document["active_key_id"] == "old-2026-01"
    assert set(document["records"]) == {"B"}
    assert SecretStore(path, box=_old_box()).get_secret("B") == "beta-secret"


def test_clear_during_rotation_cannot_be_undone_by_rollback(tmp_path: Path) -> None:
    path = tmp_path / "secrets.json"
    old = SecretStore(path, box=_old_box())
    old.set_secret("A", "alpha-secret")
    old.set_secret("B", "beta-secret")

    rotating = SecretStore(path, box=_new_box(), read_boxes=[_old_box()])
    rotating.rotation.reencrypt_to_active()
    # Deletion is ciphertext bookkeeping, so it remains available even when
    # the process has lost both sides of the pending read window.
    SecretStore(path, box=SecretBox(None)).clear_secret("A")

    pending = _json(path)
    assert "A" not in pending["records"]
    assert "A" not in pending["rotation"]["rollback_records"]
    # The ordinary operator configuration still has the new writer active and
    # the old key in its read window. Rollback itself restores the recorded old
    # writer identity; it must not require the operator to swap roles first.
    rollback = SecretStore(path, box=_new_box(), read_boxes=[_old_box()])
    assert rollback.rotation.rollback()
    assert not rollback.has_secret("A")
    assert rollback.get_secret("B") == "beta-secret"


def test_writes_during_rotation_survive_rollback_under_rollback_writer(tmp_path: Path) -> None:
    path = tmp_path / "secrets.json"
    SecretStore(path, box=_old_box()).set_secret("A", "alpha-before")
    rotating = SecretStore(path, box=_new_box(), read_boxes=[_old_box()])
    rotating.rotation.reencrypt_to_active()

    rotating.set_secret("A", "alpha-after")
    rotating.set_secret("B", "beta-after")
    assert rotating.rotation.rollback()

    restored = _json(path)
    assert restored["active_key_id"] == "old-2026-01"
    assert {record["key_id"] for record in restored["records"].values()} == {"old-2026-01"}
    old_only = SecretStore(path, box=_old_box())
    assert old_only.get_secret("A") == "alpha-after"
    assert old_only.get_secret("B") == "beta-after"


def test_clearing_last_rotating_record_closes_empty_rollback_window(tmp_path: Path) -> None:
    path = tmp_path / "secrets.json"
    SecretStore(path, box=_old_box()).set_secret("A", "alpha-secret")
    SecretStore(path, box=_new_box(), read_boxes=[_old_box()]).rotation.reencrypt_to_active()

    SecretStore(path, box=SecretBox(None)).clear_secret("A")

    cleared = _json(path)
    assert cleared["records"] == {}
    assert cleared["rotation"] is None
    new_only = SecretStore(path, box=_new_box())
    new_only.set_secret("B", "beta-secret")
    assert new_only.get_secret("B") == "beta-secret"


def test_rotation_rollback_restores_old_ciphertext_atomically(tmp_path: Path) -> None:
    path = tmp_path / "secrets.json"
    old = SecretStore(path, box=_old_box())
    old.set_secret("A", "alpha-secret")
    old_record = _json(path)["records"]["A"]

    SecretStore(path, box=_new_box(), read_boxes=[_old_box()]).rotation.reencrypt_to_active()
    rollback = SecretStore(path, box=_old_box(), read_boxes=[_new_box()])
    assert rollback.rotation.rollback()

    document = _json(path)
    assert document["rotation"] is None
    assert document["active_key_id"] == "old-2026-01"
    assert document["records"]["A"] == old_record
    assert SecretStore(path, box=_old_box()).get_secret("A") == "alpha-secret"


def test_unknown_explicit_key_and_known_key_tamper_fail_loudly(tmp_path: Path) -> None:
    path = tmp_path / "secrets.json"
    document = {
        "$format": "disco.secret-store",
        "version": 2,
        "active_key_id": "new-2026-08",
        "records": {"A": {"key_id": "missing-2025-01", "ciphertext": _old_box().encrypt("alpha")}},
        "rotation": None,
    }
    path.write_text(json.dumps(document), encoding="utf-8")
    store = SecretStore(path, box=_new_box())
    with pytest.raises(UnknownSecretKeyError, match="missing-2025-01"):
        store.get_secret("A")
    with pytest.raises(UnknownSecretKeyError, match="missing-2025-01"):
        store.rotation.reencrypt_to_active()
    with pytest.raises(UnknownSecretKeyError, match="missing-2025-01"):
        store.set_secret("B", "beta")

    document["records"]["A"] = {
        "key_id": "new-2026-08",
        "ciphertext": "not-a-fernet-token",
    }
    path.write_text(json.dumps(document), encoding="utf-8")
    with pytest.raises(SecretDecryptionError, match="failed authentication"):
        store.get_secret("A")


def test_key_prefix_does_not_hide_an_unknown_explicit_key(tmp_path: Path) -> None:
    path = tmp_path / "secrets.json"
    path.write_text(
        json.dumps(
            {
                "$format": "disco.secret-store",
                "version": 2,
                "active_key_id": "new-2026-08",
                "records": {
                    "A": {
                        "key_id": "key-explicit",
                        "ciphertext": _old_box().encrypt("alpha"),
                    }
                },
                "rotation": None,
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(UnknownSecretKeyError, match="key-explicit"):
        SecretStore(path, box=_new_box()).get_secret("A")


def test_derived_key_namespace_cannot_be_claimed_by_different_material() -> None:
    reserved = SecretBox(_OLD_SECRET).key_id
    assert reserved is not None
    with pytest.raises(ValueError, match="reserved"):
        SecretBox(_NEW_SECRET, key_id=reserved)


@pytest.mark.parametrize(
    "document",
    [
        "not-json",
        json.dumps({"$format": "disco.secret-store", "version": 99, "records": {}}),
        json.dumps(
            {
                "$format": "disco.secret-store",
                "version": 2,
                "active_key_id": "new-2026-08",
                "records": {"A": {"key_id": "new-2026-08"}},
                "rotation": None,
            }
        ),
    ],
)
def test_malformed_or_future_store_never_looks_empty(tmp_path: Path, document: str) -> None:
    path = tmp_path / "secrets.json"
    path.write_text(document, encoding="utf-8")
    with pytest.raises(SecretStoreFormatError):
        SecretStore(path, box=_new_box()).secret_names()


def test_interruption_before_replace_preserves_old_file_and_retry_resumes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "secrets.json"
    SecretStore(path, box=_old_box()).set_secret("A", "alpha-secret")
    original = path.read_bytes()
    rotating = SecretStore(path, box=_new_box(), read_boxes=[_old_box()])

    def interrupted(_tmp: Path) -> None:
        raise OSError("injected interruption before atomic replace")

    monkeypatch.setattr(rotating, "_replace", interrupted)
    with pytest.raises(OSError, match="injected interruption"):
        rotating.rotation.reencrypt_to_active()

    assert path.read_bytes() == original
    temp = path.with_suffix(".json.tmp")
    assert temp.exists()
    assert "alpha-secret" not in temp.read_text(encoding="utf-8")
    assert stat.S_IMODE(temp.stat().st_mode) == 0o600

    resumed = SecretStore(path, box=_new_box(), read_boxes=[_old_box()])
    assert resumed.rotation.reencrypt_to_active() == 1
    assert resumed.rotation.verify()
    assert resumed.get_secret("A") == "alpha-secret"


def test_environment_keyring_reads_old_and_writes_active(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "secrets.json"
    path.write_text(json.dumps({"A": _old_box().encrypt("alpha-secret")}), encoding="utf-8")
    monkeypatch.setenv("DISCO_SECRET_KEY", _NEW_SECRET)
    monkeypatch.setenv("DISCO_SECRET_KEY_ID", "new-2026-08")
    monkeypatch.setenv("DISCO_SECRET_READ_KEYS", json.dumps({"old-2026-01": _OLD_SECRET}))

    store = SecretStore(path)
    assert store.get_secret("A") == "alpha-secret"
    store.set_secret("B", "beta-secret")
    assert _json(path)["records"]["B"]["key_id"] == "new-2026-08"
    assert _OLD_SECRET not in path.read_text(encoding="utf-8")
    assert _NEW_SECRET not in path.read_text(encoding="utf-8")


def test_blank_compose_key_id_uses_derived_compatibility_identity() -> None:
    box = SecretBox.from_env({"DISCO_SECRET_KEY": _OLD_SECRET, "DISCO_SECRET_KEY_ID": ""})

    assert box.available
    assert box.key_id == SecretBox(_OLD_SECRET).key_id


def test_naming_the_same_key_keeps_implicit_id_records_readable(tmp_path: Path) -> None:
    path = tmp_path / "secrets.json"
    unnamed = SecretStore(path, box=SecretBox(_OLD_SECRET))
    unnamed.set_secret("A", "alpha-secret")
    old_record_id = _json(path)["records"]["A"]["key_id"]

    named = SecretStore(path, box=SecretBox(_OLD_SECRET, key_id="named-old-key"))
    assert named.get_secret("A") == "alpha-secret"
    named.set_secret("B", "beta-secret")

    document = _json(path)
    assert document["records"]["A"]["key_id"] == old_record_id
    assert document["records"]["B"]["key_id"] == "named-old-key"
    assert document["active_key_id"] == "named-old-key"


def test_legacy_rotation_rollback_restores_the_inferred_old_writer(tmp_path: Path) -> None:
    path = tmp_path / "secrets.json"
    old_token = _old_box().encrypt("alpha-secret")
    path.write_text(json.dumps({"A": old_token}), encoding="utf-8")

    rotating = SecretStore(path, box=_new_box(), read_boxes=[_old_box()])
    rotating.rotation.reencrypt_to_active()
    assert _json(path)["rotation"]["from_active_key_id"] == "old-2026-01"
    assert rotating.rotation.rollback()

    restored = _json(path)
    assert restored["active_key_id"] == "old-2026-01"
    assert restored["records"]["A"] == {
        "key_id": "old-2026-01",
        "ciphertext": old_token,
    }
