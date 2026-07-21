"""Hermetic command-failure regressions for the closeout live-Docker harness.

These are supplemental harness tests, not substitutes for the real Docker lifecycle.
They deliberately seam only ``subprocess.run`` so otherwise-hard-to-produce Docker CLI
failures can be proven to fail closed.  The governed live lane still executes the real
binary and public release/download boundary.
"""

from __future__ import annotations

import hashlib
import io
import json
import subprocess
import zipfile
from collections.abc import Callable
from pathlib import Path
from typing import Any, cast

import pytest
from fastapi.testclient import TestClient

from . import _closeout_live_support as support
from ._closeout_live_support import ComposeBundle


def _bundle(tmp_path: Path, *, family: str | None = None) -> ComposeBundle:
    return ComposeBundle(project="proof-fail-closed", bundle_dir=tmp_path, family=family)


def _text_result(
    argv: list[str], *, returncode: int = 0, stdout: str = "", stderr: str = ""
) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(argv, returncode, stdout, stderr)


def _binary_result(
    argv: list[str], *, returncode: int = 0, stdout: bytes = b"", stderr: bytes = b""
) -> subprocess.CompletedProcess[bytes]:
    return subprocess.CompletedProcess(argv, returncode, stdout, stderr)


def _replace_runner(
    monkeypatch: pytest.MonkeyPatch,
    runner: Callable[..., subprocess.CompletedProcess[Any]],
) -> None:
    monkeypatch.setattr(support.subprocess, "run", runner)


@pytest.mark.parametrize(
    "args",
    [
        ("ps", "-aq", "--filter", "label=x"),
        ("images", "-q", "--filter", "reference=x-*"),
        ("volume", "ls", "-q", "--filter", "label=x"),
        ("network", "ls", "-q", "--filter", "label=x"),
    ],
)
def test_failed_resource_query_with_empty_stdout_is_not_zero_resources(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, args: tuple[str, ...]
) -> None:
    def run(argv: list[str], **_: object) -> subprocess.CompletedProcess[str]:
        return _text_result(argv, returncode=71)

    _replace_runner(monkeypatch, run)
    with pytest.raises(AssertionError, match=r"exit 71"):
        _bundle(tmp_path)._ids(*args)


def test_failed_image_history_with_empty_stdout_is_not_secret_free_evidence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def run(argv: list[str], **_: object) -> subprocess.CompletedProcess[str]:
        return _text_result(argv, returncode=72)

    _replace_runner(monkeypatch, run)
    with pytest.raises(AssertionError, match=r"docker image history.*exit 72"):
        _bundle(tmp_path).image_history_text("web")


def test_failed_compose_logs_with_empty_stdout_is_not_secret_free_evidence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def run(argv: list[str], **_: object) -> subprocess.CompletedProcess[str]:
        return _text_result(argv, returncode=73)

    _replace_runner(monkeypatch, run)
    with pytest.raises(AssertionError, match=r"docker compose logs.*exit 73"):
        _bundle(tmp_path).logs()


def test_failed_binary_export_with_empty_stdout_is_not_filesystem_evidence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def run(argv: list[str], **_: object) -> subprocess.CompletedProcess[bytes]:
        return _binary_result(argv, returncode=74)

    _replace_runner(monkeypatch, run)
    with pytest.raises(AssertionError, match=r"docker export.*exit 74"):
        _bundle(tmp_path).docker_binary("export", "container-id")


def test_image_filesystem_rejects_failed_temporary_container_creation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def run(argv: list[str], **_: object) -> subprocess.CompletedProcess[str]:
        return _text_result(argv, returncode=83)

    _replace_runner(monkeypatch, run)
    with pytest.raises(AssertionError, match=r"docker create.*exit 83"):
        _bundle(tmp_path).image_filesystem_bytes("web")


@pytest.mark.parametrize("export_returncode,export_stdout", [(75, b""), (0, b"")])
def test_image_filesystem_rejects_failed_or_empty_export_and_removes_temp_container(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    export_returncode: int,
    export_stdout: bytes,
) -> None:
    calls: list[list[str]] = []

    def run(
        argv: list[str], **kwargs: object
    ) -> subprocess.CompletedProcess[str] | subprocess.CompletedProcess[bytes]:
        calls.append(argv)
        if argv[1] == "create":
            return _text_result(argv, stdout="temporary-id\n")
        if argv[1] == "export":
            return _binary_result(argv, returncode=export_returncode, stdout=export_stdout)
        assert argv[1:3] == ["rm", "-f"]
        return _text_result(argv)

    _replace_runner(monkeypatch, run)
    expected = "failed" if export_returncode else "empty binary artifact"
    with pytest.raises(AssertionError, match=expected):
        _bundle(tmp_path).image_filesystem_bytes("web")
    assert ["docker", "rm", "-f", "temporary-id"] in calls


def test_image_filesystem_rejects_failed_temporary_container_removal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def run(
        argv: list[str], **kwargs: object
    ) -> subprocess.CompletedProcess[str] | subprocess.CompletedProcess[bytes]:
        if argv[1] == "create":
            return _text_result(argv, stdout="temporary-id\n")
        if argv[1] == "export":
            return _binary_result(argv, stdout=b"tar-stream")
        return _text_result(argv, returncode=76)

    _replace_runner(monkeypatch, run)
    with pytest.raises(AssertionError, match=r"docker rm -f temporary-id.*exit 76"):
        _bundle(tmp_path).image_filesystem_bytes("web")


def test_compose_ps_capture_rejects_failed_command_before_writing_evidence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(support.EVIDENCE_DIR_ENV, str(tmp_path / "evidence"))

    def run(argv: list[str], **_: object) -> subprocess.CompletedProcess[str]:
        return _text_result(argv, returncode=77)

    _replace_runner(monkeypatch, run)
    with pytest.raises(AssertionError, match=r"compose ps.*exit 77"):
        _bundle(tmp_path, family="express")._record_compose_ps()
    assert not (tmp_path / "evidence" / "live-fixtures" / "express.compose-ps.json").exists()


def test_inspect_capture_rejects_a_failed_image_inspect(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(support.EVIDENCE_DIR_ENV, str(tmp_path / "evidence"))

    def run(argv: list[str], **_: object) -> subprocess.CompletedProcess[str]:
        if argv[1:3] == ["images", "-q"]:
            return _text_result(argv, stdout="sha256:abc123\n")
        assert argv[1:3] == ["image", "inspect"]
        return _text_result(argv, returncode=78)

    _replace_runner(monkeypatch, run)
    with pytest.raises(AssertionError, match=r"docker image inspect.*exit 78"):
        _bundle(tmp_path, family="express")._record_inspect()
    assert not (tmp_path / "evidence" / "live-fixtures" / "express.inspect.json").exists()


def test_disk_usage_capture_rejects_a_failed_command(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def run(argv: list[str], **_: object) -> subprocess.CompletedProcess[str]:
        return _text_result(argv, returncode=79)

    _replace_runner(monkeypatch, run)
    with pytest.raises(AssertionError, match=r"docker system df.*exit 79"):
        _bundle(tmp_path)._disk_usage()


_DF_ROWS = [
    {
        "Type": "Images",
        "TotalCount": "2",
        "Active": "1",
        "Size": "12MB",
        "Reclaimable": "4MB (33%)",
    },
    {
        "Type": "Containers",
        "TotalCount": "1",
        "Active": "1",
        "Size": "6.074MB",
        "Reclaimable": "0B (0%)",
    },
    {
        "Type": "Local Volumes",
        "TotalCount": "0",
        "Active": "0",
        "Size": "0B",
        "Reclaimable": "0B",
    },
    {
        "Type": "Build Cache",
        "TotalCount": "10",
        "Active": "0",
        "Size": "10.24GB",
        "Reclaimable": "10.24GB (100%)",
    },
]


@pytest.mark.parametrize(
    "stdout",
    [
        json.dumps(_DF_ROWS),
        "\n".join(json.dumps(row) for row in _DF_ROWS),
    ],
)
def test_disk_usage_normalizes_supported_docker_json_shapes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    stdout: str,
) -> None:
    _replace_runner(
        monkeypatch,
        lambda argv, **_: _text_result(argv, stdout=stdout),
    )
    result = _bundle(tmp_path)._disk_usage()
    assert set(result) == {"rows"}
    rows = result["rows"]
    assert rows == _DF_ROWS


@pytest.mark.parametrize(
    "stdout",
    [
        "not-json",
        "42",
        json.dumps({"Type": "Images", "Size": "12MB"}),
        json.dumps([*_DF_ROWS, "scalar"]),
        json.dumps([{**_DF_ROWS[0], "TotalCount": True}, *_DF_ROWS[1:]]),
        json.dumps([{**_DF_ROWS[0], "TotalCount": "02"}, *_DF_ROWS[1:]]),
        json.dumps([{**_DF_ROWS[0], "Active": "3"}, *_DF_ROWS[1:]]),
        json.dumps([{**_DF_ROWS[0], "Size": "NaNGB"}, *_DF_ROWS[1:]]),
        json.dumps([{**_DF_ROWS[0], "Reclaimable": "4MB (101%)"}, *_DF_ROWS[1:]]),
        json.dumps([{**_DF_ROWS[0], "Extra": "forbidden"}, *_DF_ROWS[1:]]),
        json.dumps([_DF_ROWS[0], _DF_ROWS[0], *_DF_ROWS[2:]]),
    ],
)
def test_disk_usage_rejects_malformed_scalar_or_incomplete_rows(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, stdout: str
) -> None:
    _replace_runner(
        monkeypatch,
        lambda argv, **_: _text_result(argv, stdout=stdout),
    )
    with pytest.raises(AssertionError):
        _bundle(tmp_path)._disk_usage()


def test_teardown_attempts_other_families_but_rejects_a_failed_purge_query(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[list[str]] = []

    def run(argv: list[str], **_: object) -> subprocess.CompletedProcess[str]:
        calls.append(argv)
        if argv[1:3] == ["ps", "-aq"]:
            return _text_result(argv, returncode=80)
        return _text_result(argv)

    _replace_runner(monkeypatch, run)
    monkeypatch.setattr(support.shutil, "which", lambda _: "/usr/bin/docker")
    with pytest.raises(
        AssertionError,
        match=r"docker ps.*exit 80",
    ):
        _bundle(tmp_path).teardown_and_assert_clean()
    assert any(argv[1:3] == ["images", "-q"] for argv in calls)
    assert any(argv[1:4] == ["volume", "ls", "-q"] for argv in calls)
    assert any(argv[1:4] == ["network", "ls", "-q"] for argv in calls)


def test_teardown_rejects_failed_resource_removal_even_when_final_queries_are_empty(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ps_queries = 0

    def run(argv: list[str], **_: object) -> subprocess.CompletedProcess[str]:
        nonlocal ps_queries
        if argv[1:3] == ["ps", "-aq"]:
            ps_queries += 1
            return _text_result(argv, stdout="container-id\n" if ps_queries == 1 else "")
        if argv[1:3] == ["rm", "-f"]:
            return _text_result(argv, returncode=81)
        return _text_result(argv)

    _replace_runner(monkeypatch, run)
    monkeypatch.setattr(support.shutil, "which", lambda _: "/usr/bin/docker")
    with pytest.raises(AssertionError, match=r"docker rm -f container-id.*exit 81"):
        _bundle(tmp_path).teardown_and_assert_clean()


def test_teardown_rejects_failed_compose_down_after_still_attempting_label_purge(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "compose.yaml").write_text("services: {}\n", encoding="utf-8")
    calls: list[list[str]] = []

    def run(argv: list[str], **_: object) -> subprocess.CompletedProcess[str]:
        calls.append(argv)
        if argv[1:3] == ["compose", "-p"]:
            return _text_result(argv, returncode=82)
        return _text_result(argv)

    _replace_runner(monkeypatch, run)
    monkeypatch.setattr(support.shutil, "which", lambda _: "/usr/bin/docker")
    with pytest.raises(AssertionError, match=r"compose down.*exit 82"):
        _bundle(tmp_path).teardown_and_assert_clean()
    assert any(argv[1:3] == ["ps", "-aq"] for argv in calls)


def test_cleanup_artifact_records_query_failure_and_never_claims_zero(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    evidence_root = tmp_path / "evidence"
    monkeypatch.setenv(support.EVIDENCE_DIR_ENV, str(evidence_root))
    monkeypatch.setenv(support.EVIDENCE_RUN_ID_ENV, "run-id")
    ps_queries = 0

    def run(argv: list[str], **_: object) -> subprocess.CompletedProcess[str]:
        nonlocal ps_queries
        if argv[1:4] == ["system", "df", "--format"]:
            return _text_result(argv, stdout='{"Type":"Images","TotalCount":"0"}\n')
        if argv[1:3] == ["ps", "-aq"]:
            ps_queries += 1
            return _text_result(argv, returncode=84)
        return _text_result(argv)

    bundle = _bundle(tmp_path, family="express")
    _replace_runner(monkeypatch, run)
    monkeypatch.setattr(support.shutil, "which", lambda _: "/usr/bin/docker")
    monkeypatch.setattr(bundle, "_record_inspect", lambda: None)
    with pytest.raises(AssertionError, match=r"docker ps.*exit 84"):
        bundle.teardown_and_assert_clean()

    artifact = evidence_root / "live-fixtures" / "express.cleanup.json"
    payload = support.json.loads(artifact.read_text(encoding="utf-8"))
    assert payload["_run_id"] == "run-id"
    assert payload["zero_remaining"] is False
    assert payload["errors"]
    assert payload["remaining"] == {}
    assert ps_queries == 2  # purge discovery + independent post-purge proof


def test_successful_evidence_and_cleanup_controls_remain_accepted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def run(
        argv: list[str], **kwargs: object
    ) -> subprocess.CompletedProcess[str] | subprocess.CompletedProcess[bytes]:
        if kwargs.get("text") is False or "text" not in kwargs:
            return _binary_result(argv, stdout=b"tar-stream")
        if argv[1:3] == ["image", "history"]:
            return _text_result(argv, stdout="layer history\n")
        if argv[1:3] == ["compose", "-p"]:
            return _text_result(argv, stdout="application log\n")
        return _text_result(argv)

    _replace_runner(monkeypatch, run)
    monkeypatch.setattr(support.shutil, "which", lambda _: "/usr/bin/docker")
    bundle = _bundle(tmp_path)
    assert bundle.logs() == "application log\n"
    assert bundle.image_history_text("web") == "layer history\n"
    assert bundle.docker_binary("export", "container-id") == b"tar-stream"
    assert bundle._ids("ps", "-aq", "--filter", "label=x") == []
    bundle.teardown_and_assert_clean()


class _Response:
    def __init__(self, content: bytes, status_code: int = 200) -> None:
        self.content = content
        self.status_code = status_code

    def json(self) -> object:
        return json.loads(self.content)


class _ReleaseClient:
    def __init__(
        self,
        zip_bytes: bytes,
        release_body: dict[str, object],
        *,
        release_status: int = 200,
    ) -> None:
        self.zip_bytes = zip_bytes
        self.release_body = release_body
        self.release_status = release_status

    def get(self, path: str) -> _Response:
        if "/download?" in path:
            return _Response(self.zip_bytes)
        return _Response(json.dumps(self.release_body).encode(), self.release_status)


def _bundle_zip(release_json: dict[str, object]) -> bytes:
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr(support.RELEASE_JSON_PATH, json.dumps(release_json))
    return out.getvalue()


@pytest.mark.parametrize("mismatch", [False, True])
def test_digest_helper_binds_response_to_parsed_release_manifest(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mismatch: bool
) -> None:
    release_json: dict[str, object] = {
        "kind": "static",
        "name": "fixture",
        "version_seq": 3,
        "tree_digest": "a" * 64,
        "services": [],
        "env": [],
        "resources": [],
        "provenance": {"assessment": "candidate"},
    }
    canonical = json.dumps(
        release_json, sort_keys=True, ensure_ascii=False, separators=(",", ":")
    ).encode("utf-8")
    body: dict[str, object] = {
        "assessment": "candidate",
        "self_host": True,
        "version_seq": 3,
        "tree_digest": ("b" if mismatch else "a") * 64,
        "spec_digest": f"sha256:{hashlib.sha256(canonical).hexdigest()}",
    }
    evidence_root = tmp_path / "evidence"
    monkeypatch.setenv(support.EVIDENCE_DIR_ENV, str(evidence_root))
    monkeypatch.setenv(support.EVIDENCE_RUN_ID_ENV, "digest-run")
    support.record_bundle_digest(
        "express",
        cast(TestClient, _ReleaseClient(_bundle_zip(release_json), body)),
        "conversation",
        body,
    )
    record = json.loads(
        (evidence_root / "live-fixtures" / "express.digest.json").read_text(encoding="utf-8")
    )
    assert record["release_response"]["tree_digest"] == body["tree_digest"]
    assert record["binding_matches_response"] is (not mismatch)


@pytest.mark.parametrize(
    ("release_status", "change_body"),
    [(503, False), (200, True)],
)
def test_digest_helper_rejects_failed_or_unrelated_second_release_response(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    release_status: int,
    change_body: bool,
) -> None:
    release_json: dict[str, object] = {
        "kind": "static",
        "name": "fixture",
        "version_seq": 3,
        "tree_digest": "a" * 64,
        "services": [],
        "env": [],
        "resources": [],
        "provenance": {"assessment": "candidate"},
    }
    canonical = json.dumps(
        release_json, sort_keys=True, ensure_ascii=False, separators=(",", ":")
    ).encode()
    body: dict[str, object] = {
        "assessment": "candidate",
        "self_host": True,
        "version_seq": 3,
        "tree_digest": "a" * 64,
        "spec_digest": f"sha256:{hashlib.sha256(canonical).hexdigest()}",
    }
    second = {**body, "tree_digest": "b" * 64} if change_body else body
    evidence_root = tmp_path / "evidence"
    monkeypatch.setenv(support.EVIDENCE_DIR_ENV, str(evidence_root))
    monkeypatch.setenv(support.EVIDENCE_RUN_ID_ENV, "digest-run")
    support.record_bundle_digest(
        "express",
        cast(
            TestClient,
            _ReleaseClient(_bundle_zip(release_json), second, release_status=release_status),
        ),
        "conversation",
        body,
    )
    record = json.loads((evidence_root / "live-fixtures" / "express.digest.json").read_text())
    assert record["binding_matches_response"] is False
    assert record["source_binding"]["release_status"] == release_status
    assert record["source_binding"]["response_matches_supplied"] is (
        release_status == 200 and not change_body
    )
