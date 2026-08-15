from __future__ import annotations

import hashlib
import http.cookiejar
import io
import json
import sqlite3
import subprocess
import tarfile
import zipfile
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from harness.reliability import fresh_device as fresh_device_module
from harness.reliability._runner.api_session import ApiSession
from harness.reliability._runner.fresh_device_host import CommandRunner, Engine
from harness.reliability._runner.fresh_device_journey import (
    FreshDeviceJourneyBindings,
    run_device_journey,
)
from harness.reliability.fresh_device import (
    ProductError,
    _assert_verify_output,
    _backup_manifest,
    _database_logical_digests,
    _export_project_digests,
    _manifest_digest,
    _phase_compose_env,
    _phase_uninstall,
    _project_ids,
    _safe_device_label,
    _sha256_bytes,
    _validate_zip,
)


def _zip_bytes(files: dict[str, bytes]) -> bytes:
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w") as archive:
        for name, data in files.items():
            archive.writestr(name, data)
    return output.getvalue()


def _add_session_cookie(api: ApiSession) -> None:
    api.jar.set_cookie(
        http.cookiejar.Cookie(
            version=0,
            name="disco_session",
            value="session",
            port=None,
            port_specified=False,
            domain="front",
            domain_specified=False,
            domain_initial_dot=False,
            path="/",
            path_specified=True,
            secure=False,
            expires=None,
            discard=True,
            comment=None,
            comment_url=None,
            rest={},
            rfc2109=False,
        )
    )


def test_device_label_is_only_safe_human_metadata() -> None:
    assert _safe_device_label(" Dylan's Workstation #4 ") == "dylan-s-workstation-4"
    assert _safe_device_label("!!!") == "device"
    assert len(_safe_device_label("x" * 100)) == 32


def test_verify_requires_real_grounding_pass() -> None:
    base = "PASS config\nPASS completion\nPASS tool-calling\n"
    _assert_verify_output(base, grounding=False)
    with pytest.raises(ProductError, match="SKIP never counts"):
        _assert_verify_output(base + "SKIP grounding\n", grounding=True)
    _assert_verify_output(base + "PASS grounding\n", grounding=True)


def test_project_ids_reject_bad_shape_and_missing_workspaces() -> None:
    assert _project_ids(
        {
            "projects": [
                {"id": "conv_z", "files_missing": False},
                {"id": "conv_ok", "files_missing": False},
                {"id": "conv_missing", "files_missing": True},
            ]
        }
    ) == ["conv_ok", "conv_z"]
    with pytest.raises(ProductError, match="wrong shape"):
        _project_ids([])
    with pytest.raises(ProductError, match="invalid project identity"):
        _project_ids({"projects": [{"title": "no identity"}]})
    with pytest.raises(ProductError, match="duplicate project identity"):
        _project_ids({"projects": [{"id": "same"}, {"id": "same"}]})


def test_project_zip_must_be_nonempty_and_uncorrupt(tmp_path: Path) -> None:
    data = _zip_bytes({"index.html": b"<h1>fresh</h1>", "src/app.js": b"ok"})
    target = tmp_path / "project.zip"
    assert _validate_zip(data, target) == ["index.html", "src/app.js"]
    assert target.read_bytes() == data
    with pytest.raises(ProductError, match="not a ZIP"):
        _validate_zip(b"not-a-zip", tmp_path / "bad.zip")
    with pytest.raises(ProductError, match="contains no files"):
        _validate_zip(_zip_bytes({}), tmp_path / "empty.zip")


def test_every_project_export_is_bound_to_a_deterministic_digest(tmp_path: Path) -> None:
    exports = {
        "project-a": _zip_bytes({"index.html": b"a"}),
        "project-b": _zip_bytes({"index.html": b"b"}),
    }

    class ProjectApi:
        def bytes(self, _base: str, path: str) -> bytes:
            return exports[path.split("/")[-2]]

    digests = _export_project_digests(
        ProjectApi(),  # type: ignore[arg-type]
        "http://agent",
        ["project-b", "project-a"],
        tmp_path,
        "baseline",
    )

    assert list(digests) == ["project-a", "project-b"]
    assert digests == {
        project_id: hashlib.sha256(data).hexdigest() for project_id, data in sorted(exports.items())
    }
    assert sorted(path.name for path in tmp_path.glob("*.zip")) == [
        "project-001-baseline.zip",
        "project-002-baseline.zip",
    ]


def test_docker_fresh_device_explicitly_selects_docker_backend() -> None:
    engine = Engine(
        binary="docker",
        compose=("docker", "compose"),
        sandbox_socket="/var/run/docker.sock",
    )
    env = _phase_compose_env(engine, "campaign", "fingerprint", "device", 1, 2, 3)
    assert env["DISCO_LOCAL_ENGINE"] == "docker"
    assert env["DISCO_SANDBOX_SOCKET"] == "/var/run/docker.sock"


def test_backup_manifest_digest_is_timestamp_neutral_and_database_selective() -> None:
    manifest = {
        "format": 1,
        "created_at": "first",
        "database": {
            "path": "disco.db",
            "method": "sqlite3_backup",
            "committed_sha256": "committed-one",
            "schema_sha256": "schema-one",
        },
        "scope": "complete Compose /data volume",
        "entries": [
            {"path": "disco.db", "type": "file", "sha256": "db-one"},
            {"path": "secrets.json", "type": "file", "sha256": "secrets"},
        ],
    }
    changed = json.loads(json.dumps(manifest))
    changed["created_at"] = "second"
    assert _manifest_digest(manifest, include_database=True) == _manifest_digest(
        changed, include_database=True
    )
    changed["entries"][0]["sha256"] = "db-two"
    assert _manifest_digest(manifest, include_database=True) == _manifest_digest(
        changed, include_database=True
    )
    changed["database"]["schema_sha256"] = "schema-two"
    assert _manifest_digest(manifest, include_database=True) != _manifest_digest(
        changed, include_database=True
    )
    assert _manifest_digest(manifest, include_database=False) == _manifest_digest(
        changed, include_database=False
    )
    changed["database"]["committed_sha256"] = "committed-two"
    assert _manifest_digest(manifest, include_database=False) != _manifest_digest(
        changed, include_database=False
    )


def _database_bytes(
    path: Path,
    rows: list[tuple[int, str]],
    *,
    extra_column: bool,
) -> bytes:
    connection = sqlite3.connect(path)
    suffix = ", note TEXT" if extra_column else ""
    connection.execute(f"CREATE TABLE records (id INTEGER PRIMARY KEY, value TEXT{suffix})")
    for row in rows:
        if extra_column:
            connection.execute("INSERT INTO records VALUES (?, ?, NULL)", row)
        else:
            connection.execute("INSERT INTO records VALUES (?, ?)", row)
    connection.commit()
    connection.close()
    return path.read_bytes()


def _add_tar_member(
    archive: tarfile.TarFile,
    name: str,
    *,
    data: bytes = b"",
    mode: int = 0o600,
    member_type: bytes = tarfile.REGTYPE,
    linkname: str = "",
) -> None:
    info = tarfile.TarInfo(name)
    info.mode = mode
    info.type = member_type
    info.linkname = linkname
    if member_type == tarfile.REGTYPE:
        info.size = len(data)
        archive.addfile(info, io.BytesIO(data))
    else:
        archive.addfile(info)


def test_logical_sqlite_digest_ignores_page_and_row_order_but_binds_schema_and_data(
    tmp_path: Path,
) -> None:
    first = _database_logical_digests(
        _database_bytes(tmp_path / "first.db", [(1, "a"), (2, "b")], extra_column=False)
    )
    second = _database_logical_digests(
        _database_bytes(tmp_path / "second.db", [(2, "b"), (1, "a")], extra_column=False)
    )
    changed_data = _database_logical_digests(
        _database_bytes(tmp_path / "changed-data.db", [(1, "a"), (2, "c")], extra_column=False)
    )
    changed_schema = _database_logical_digests(
        _database_bytes(tmp_path / "changed-schema.db", [(1, "a"), (2, "b")], extra_column=True)
    )

    assert first == second
    assert first[0] != changed_data[0]
    assert first[1] == changed_data[1]
    assert first[0] != changed_schema[0]
    assert first[1] != changed_schema[1]


def test_backup_manifest_adds_logical_database_evidence(tmp_path: Path) -> None:
    database = _database_bytes(tmp_path / "archive.db", [(1, "a"), (2, "b")], extra_column=False)
    committed_digest, schema_digest = _database_logical_digests(database)
    manifest = {
        "format": 1,
        "created_at": "now",
        "database": {"path": "disco.db", "method": "sqlite3_backup"},
        "scope": "complete Compose /data volume",
        "entries": [
            {
                "path": "disco.db",
                "type": "file",
                "mode": 0o600,
                "size": len(database),
                "sha256": _sha256_bytes(database),
            }
        ],
    }

    archive_path = tmp_path / "backup.tar.gz"
    encoded = json.dumps(manifest).encode()
    with tarfile.open(archive_path, "w:gz") as archive:
        info = tarfile.TarInfo("disco-backup-v1/manifest.json")
        info.size = len(encoded)
        info.mode = 0o600
        archive.addfile(info, io.BytesIO(encoded))
        database_info = tarfile.TarInfo("disco-backup-v1/data/disco.db")
        database_info.size = len(database)
        database_info.mode = 0o600
        archive.addfile(database_info, io.BytesIO(database))
    enriched = _backup_manifest(archive_path)
    assert enriched["database"] == {
        "path": "disco.db",
        "method": "sqlite3_backup",
        "committed_sha256": committed_digest,
        "schema_sha256": schema_digest,
    }


def test_backup_manifest_accepts_bound_directories_and_safe_symlinks(tmp_path: Path) -> None:
    database = _database_bytes(tmp_path / "archive.db", [(1, "a")], extra_column=False)
    manifest = {
        "format": 1,
        "created_at": "now",
        "database": {"path": "disco.db", "method": "sqlite3_backup"},
        "scope": "complete Compose /data volume",
        "entries": [
            {
                "path": "disco.db",
                "type": "file",
                "mode": 0o600,
                "size": len(database),
                "sha256": _sha256_bytes(database),
            },
            {"path": "latest", "type": "symlink", "mode": 0o777, "target": "projects"},
            {"path": "projects", "type": "directory", "mode": 0o700},
        ],
    }
    archive_path = tmp_path / "structured.tar.gz"
    with tarfile.open(archive_path, "w:gz") as archive:
        _add_tar_member(
            archive,
            "disco-backup-v1/manifest.json",
            data=json.dumps(manifest).encode(),
        )
        _add_tar_member(archive, "disco-backup-v1/data/disco.db", data=database)
        _add_tar_member(
            archive,
            "disco-backup-v1/data/latest",
            mode=0o777,
            member_type=tarfile.SYMTYPE,
            linkname="projects",
        )
        _add_tar_member(
            archive,
            "disco-backup-v1/data/projects",
            mode=0o700,
            member_type=tarfile.DIRTYPE,
        )

    assert _backup_manifest(archive_path)["entries"] == manifest["entries"]


@pytest.mark.parametrize(
    "mutation",
    [
        "malformed-json",
        "wrong-shape",
        "unsafe-manifest-path",
        "unsafe-symlink-target",
        "duplicate-member",
        "mode-mismatch",
        "type-mismatch",
        "unsafe-member-path",
        "unsafe-member-type",
    ],
)
def test_backup_manifest_rejects_untrusted_archive_shapes(mutation: str, tmp_path: Path) -> None:
    database = _database_bytes(tmp_path / f"{mutation}.db", [(1, "a")], extra_column=False)
    manifest = {
        "format": 1,
        "created_at": "now",
        "database": {"path": "disco.db", "method": "sqlite3_backup"},
        "scope": "complete Compose /data volume",
        "entries": [
            {
                "path": "../disco.db" if mutation == "unsafe-manifest-path" else "disco.db",
                "type": "file",
                "mode": 0o600,
                "size": len(database),
                "sha256": _sha256_bytes(database),
            }
        ],
    }
    if mutation == "unsafe-symlink-target":
        manifest["entries"].append(
            {"path": "link", "type": "symlink", "mode": 0o777, "target": "../../escape"}
        )
    manifest_data = json.dumps(manifest).encode()
    if mutation == "malformed-json":
        manifest_data = b"{"
    elif mutation == "wrong-shape":
        manifest_data = b"[]"

    archive_path = tmp_path / f"{mutation}.tar.gz"
    with tarfile.open(archive_path, "w:gz") as archive:
        _add_tar_member(archive, "disco-backup-v1/manifest.json", data=manifest_data)
        _add_tar_member(
            archive,
            "disco-backup-v1/data/disco.db",
            data=database,
            mode=0o644 if mutation == "mode-mismatch" else 0o600,
            member_type=tarfile.DIRTYPE if mutation == "type-mismatch" else tarfile.REGTYPE,
        )
        if mutation == "unsafe-symlink-target":
            _add_tar_member(
                archive,
                "disco-backup-v1/data/link",
                mode=0o777,
                member_type=tarfile.SYMTYPE,
                linkname="../../escape",
            )
        elif mutation == "duplicate-member":
            _add_tar_member(archive, "disco-backup-v1/data/disco.db", data=database)
        elif mutation == "unsafe-member-path":
            _add_tar_member(archive, "../escape", data=b"x")
        elif mutation == "unsafe-member-type":
            _add_tar_member(
                archive,
                "disco-backup-v1/data/pipe",
                member_type=tarfile.FIFOTYPE,
            )

    with pytest.raises(ProductError):
        _backup_manifest(archive_path)


@pytest.mark.parametrize("mutation", ["checksum", "missing", "unexpected"])
def test_backup_manifest_recomputes_exact_archive_members(mutation: str, tmp_path: Path) -> None:
    database = _database_bytes(tmp_path / "source.db", [(1, "a")], extra_column=False)
    manifest = {
        "format": 1,
        "created_at": "now",
        "database": {"path": "disco.db", "method": "sqlite3_backup"},
        "scope": "complete Compose /data volume",
        "entries": [
            {
                "path": "disco.db",
                "type": "file",
                "mode": 0o600,
                "size": len(database),
                "sha256": "0" * 64 if mutation == "checksum" else _sha256_bytes(database),
            }
        ],
    }
    encoded = json.dumps(manifest).encode()
    archive_path = tmp_path / f"{mutation}.tar.gz"
    with tarfile.open(archive_path, "w:gz") as archive:
        manifest_info = tarfile.TarInfo("disco-backup-v1/manifest.json")
        manifest_info.size = len(encoded)
        manifest_info.mode = 0o600
        archive.addfile(manifest_info, io.BytesIO(encoded))
        if mutation != "missing":
            database_info = tarfile.TarInfo("disco-backup-v1/data/disco.db")
            database_info.size = len(database)
            database_info.mode = 0o600
            archive.addfile(database_info, io.BytesIO(database))
        if mutation == "unexpected":
            extra = tarfile.TarInfo("disco-backup-v1/data/unlisted.txt")
            extra.size = 1
            extra.mode = 0o600
            archive.addfile(extra, io.BytesIO(b"x"))

    with pytest.raises(ProductError, match="manifest"):
        _backup_manifest(archive_path)


def test_device_journey_runs_each_bound_phase_once_in_governed_order(tmp_path: Path) -> None:
    runner = object()
    engine = object()
    api = object()
    compose_env = {"DISCO_LOCAL_ENGINE": "docker"}
    project_digests = {"project-a": "a" * 64}
    calls: list[tuple[str, tuple[Any, ...]]] = []

    def step(name: str, result: Any = None) -> Callable[..., Any]:
        def invoke(*args: Any) -> Any:
            calls.append((name, args))
            return result

        return invoke

    checks: list[dict[str, Any]] = []
    run_device_journey(
        bindings=FreshDeviceJourneyBindings(
            install_and_boot=step("install", ("front", "app", "agent", ["initial-image"])),
            pair_and_config=step("pair", api),
            verify_and_scenarios=step("verify"),
            build_search_export=step("build", (["project-a"], project_digests)),
            cold_restart=step(
                "restart",
                {"data_digest": "restart-digest", "project_digests": project_digests},
            ),
            upgrade=step(
                "upgrade",
                {
                    "stable_data_digest": "upgrade-digest",
                    "project_digests": project_digests,
                    "image_ids": ["initial-image", "upgraded-image"],
                },
            ),
            backup_restore=step(
                "restore",
                {"backup_digest": "backup-digest", "project_digests": project_digests},
            ),
            uninstall=step("uninstall"),
        ),
        runner=runner,
        engine=engine,
        project="compose-project",
        checkout=tmp_path / "checkout",
        compose_env=compose_env,
        out=tmp_path / "evidence",
        ui_port=8088,
        app_port=8800,
        agent_port=8000,
        base_commit="base-commit",
        upgrade_commit="upgrade-commit",
        checks=checks,
    )

    assert [name for name, _args in calls] == [
        "install",
        "pair",
        "verify",
        "build",
        "restart",
        "upgrade",
        "restore",
        "uninstall",
    ]
    assert calls[1][1] == ("app", "front", "agent")
    assert calls[4][1][-3:] == (["project-a"], project_digests, tmp_path / "evidence")
    assert calls[5][1][-6:] == (
        ["project-a"],
        project_digests,
        ["initial-image"],
        "upgrade-commit",
        "base-commit",
        tmp_path / "evidence",
    )
    assert calls[6][1][-3:] == (["project-a"], project_digests, tmp_path / "evidence")
    assert [check["id"] for check in checks] == [
        "install-and-boot",
        "first-pair-and-config",
        "model-and-internet",
        "build-search-export",
        "cold-restart",
        "upgrade-in-place",
        "backup-restore",
        "uninstall",
    ]


def test_device_journey_stops_before_restore_and_uninstall_on_upgrade_failure(
    tmp_path: Path,
) -> None:
    calls: list[str] = []
    project_digests = {"project-a": "a" * 64}

    def step(name: str, result: Any = None, *, fail: bool = False) -> Callable[..., Any]:
        def invoke(*_args: Any) -> Any:
            calls.append(name)
            if fail:
                raise RuntimeError("upgrade failed")
            return result

        return invoke

    with pytest.raises(RuntimeError, match="upgrade failed"):
        run_device_journey(
            bindings=FreshDeviceJourneyBindings(
                install_and_boot=step("install", ("front", "app", "agent", ["initial-image"])),
                pair_and_config=step("pair", object()),
                verify_and_scenarios=step("verify"),
                build_search_export=step("build", (["project-a"], project_digests)),
                cold_restart=step("restart", {"project_digests": project_digests}),
                upgrade=step("upgrade", fail=True),
                backup_restore=step("restore", {}),
                uninstall=step("uninstall"),
            ),
            runner=object(),
            engine=object(),
            project="project",
            checkout=tmp_path / "checkout",
            compose_env={},
            out=tmp_path / "out",
            ui_port=1,
            app_port=2,
            agent_port=3,
            base_commit="base",
            upgrade_commit="upgrade",
            checks=[],
        )
    assert calls == ["install", "pair", "verify", "build", "restart", "upgrade"]


@pytest.mark.parametrize(
    ("install_images", "upgrade_images", "message"),
    [
        ([], ["upgraded-image"], "install"),
        (["initial-image"], [], "upgrade"),
    ],
)
def test_device_journey_rejects_empty_phase_image_inventory(
    tmp_path: Path,
    install_images: list[str],
    upgrade_images: list[str],
    message: str,
) -> None:
    project_digests = {"project-a": "a" * 64}

    def step(result: Any = None) -> Callable[..., Any]:
        return lambda *_args: result

    with pytest.raises(RuntimeError, match=message):
        run_device_journey(
            bindings=FreshDeviceJourneyBindings(
                install_and_boot=step(("front", "app", "agent", install_images)),
                pair_and_config=step(object()),
                verify_and_scenarios=step(),
                build_search_export=step((["project-a"], project_digests)),
                cold_restart=step({"project_digests": project_digests}),
                upgrade=step({"image_ids": upgrade_images, "project_digests": project_digests}),
                backup_restore=step({"project_digests": project_digests}),
                uninstall=step(),
            ),
            runner=object(),
            engine=object(),
            project="project",
            checkout=tmp_path / "checkout",
            compose_env={},
            out=tmp_path / "out",
            ui_port=1,
            app_port=2,
            agent_port=3,
            base_commit="base",
            upgrade_commit="upgrade",
            checks=[],
        )


def test_pair_phase_resolves_parent_api_and_driver_seams(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[str, Any]] = []

    class FakeApi:
        def __init__(self, *, app_base: str, agent_base: str, origin: str) -> None:
            self.app_base = app_base
            self.agent_base = agent_base
            calls.append(("init", (app_base, agent_base, origin)))

        def pair(self) -> None:
            calls.append(("pair", None))

        def json(self, method: str, base: str, path: str) -> dict[str, str]:
            calls.append(("json", (method, base, path)))
            return {"default_model": "fresh-device-driver"}

    def configure(api: Any, **settings: str) -> None:
        calls.append(("configure", (api, settings)))

    monkeypatch.setattr(fresh_device_module, "ApiSession", FakeApi)
    monkeypatch.setattr(fresh_device_module, "_configure_driver", configure)
    monkeypatch.setenv("DISCO_FRESH_DRIVER_BASE_URL", "https://driver.invalid/v1")
    monkeypatch.setenv("DISCO_FRESH_DRIVER_MODEL", "driver-model")
    monkeypatch.setenv("DISCO_FRESH_DRIVER_API_KEY", "driver-secret")

    api = fresh_device_module._phase_pair_and_config("app", "front", "agent")

    assert isinstance(api, FakeApi)
    assert calls[0:2] == [
        ("init", ("front/svc/app", "front/svc/agent", "front")),
        ("pair", None),
    ]
    assert calls[2][0] == "configure"
    assert calls[2][1][0] is api
    assert calls[2][1][1] == {
        "base_url": "https://driver.invalid/v1",
        "model": "driver-model",
        "api_key": "driver-secret",
    }
    assert calls[3] == (
        "json",
        ("GET", "front/svc/app", "/api/models/assignments"),
    )


def test_api_session_pair_uses_tokenless_mint_without_fetching_token(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    api = ApiSession(
        app_base="http://front/svc/app", agent_base="http://front/svc/agent", origin="http://front"
    )
    calls: list[tuple[str, str, Any]] = []

    def request(
        method: str,
        url: str,
        *,
        data: Any | None = None,
        timeout: float = 60,
    ) -> tuple[int, bytes, dict[str, str]]:
        del timeout
        calls.append((method, url, data))
        return 200, b'{"csrf_token":"csrf"}', {}

    monkeypatch.setattr(api, "_request", request)
    _add_session_cookie(api)

    api.pair()

    assert calls == [("POST", "http://front/svc/app/api/auth/mint", {})]
    assert api.csrf == "csrf"


def test_api_session_pair_fetches_token_only_after_pairing_required(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    api = ApiSession(
        app_base="http://front/svc/app", agent_base="http://front/svc/agent", origin="http://front"
    )
    calls: list[tuple[str, str, Any]] = []
    responses = iter(
        [
            (401, b'{"detail":{"reason":"pairing_required"}}', {}),
            (200, b'{"pairing_token":"from-loopback"}', {}),
            (200, b'{"csrf_token":"csrf"}', {}),
        ]
    )

    def request(
        method: str,
        url: str,
        *,
        data: Any | None = None,
        timeout: float = 60,
    ) -> tuple[int, bytes, dict[str, str]]:
        del timeout
        calls.append((method, url, data))
        return next(responses)

    monkeypatch.setattr(api, "_request", request)
    _add_session_cookie(api)

    api.pair()

    assert calls == [
        ("POST", "http://front/svc/app/api/auth/mint", {}),
        ("GET", "http://front/svc/app/api/auth/pairing-token", None),
        (
            "POST",
            "http://front/svc/app/api/auth/mint",
            {"pairing_token": "from-loopback"},
        ),
    ]


def test_api_session_pair_raises_the_shared_product_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    api = ApiSession(
        app_base="http://front/svc/app", agent_base="http://front/svc/agent", origin="http://front"
    )
    responses = iter(
        [
            (401, b'{"detail":{"reason":"pairing_required"}}', {}),
            (403, b'{"detail":{"reason":"loopback_required"}}', {}),
        ]
    )
    monkeypatch.setattr(api, "_request", lambda *_args, **_kwargs: next(responses))

    with pytest.raises(ProductError, match="pairing is required.*HTTP 403"):
        api.pair()


class _UninstallRunner(CommandRunner):
    def __init__(self, all_images: str = "", *, removal_succeeds: bool = False) -> None:
        self.all_images = all_images
        self.removal_succeeds = removal_succeeds
        self.calls: list[tuple[str, tuple[str, ...]]] = []

    def run(
        self,
        name: str,
        command: list[str] | tuple[str, ...],
        *,
        cwd: Path | None = None,
        env: dict[str, str] | None = None,
        timeout: float = 600,
        check: bool = True,
    ) -> subprocess.CompletedProcess[str]:
        del cwd, env, timeout, check
        self.calls.append((name, tuple(command)))
        output = ""
        if name == "pre-uninstall-compose-images":
            output = "sha256:candidate-image\n"
        elif name in {"post-uninstall-all-image-ids", "post-uninstall-final-image-ids"}:
            output = self.all_images
        elif name == "post-uninstall-images":
            output = self.all_images
        elif name == "uninstall-retired-images" and self.removal_succeeds:
            self.all_images = ""
        return subprocess.CompletedProcess(command, 0, stdout=output)


def test_uninstall_binds_known_compose_images_and_removes_clone(tmp_path: Path) -> None:
    checkout = tmp_path / "clone"
    checkout.mkdir()
    runner = _UninstallRunner()
    engine = Engine(
        binary="docker",
        compose=("docker", "compose"),
        sandbox_socket="/var/run/docker.sock",
    )

    _phase_uninstall(runner, engine, "project", checkout, {}, tmp_path, ["sha256:candidate-image"])

    assert not checkout.exists()
    uninstall = dict(runner.calls)["uninstall"]
    assert uninstall[-4:] == ("--volumes", "--remove-orphans", "--rmi", "all")
    assert "post-uninstall-all-image-ids" in dict(runner.calls)


def test_uninstall_rejects_a_known_compose_image_that_survives(tmp_path: Path) -> None:
    checkout = tmp_path / "clone"
    checkout.mkdir()
    runner = _UninstallRunner(all_images="sha256:candidate-image\n")
    engine = Engine(
        binary="docker",
        compose=("docker", "compose"),
        sandbox_socket="/var/run/docker.sock",
    )

    with pytest.raises(ProductError, match="known_images=.*candidate-image"):
        _phase_uninstall(
            runner, engine, "project", checkout, {}, tmp_path, ["sha256:candidate-image"]
        )
    assert checkout.exists()


def test_uninstall_inventory_is_taken_after_exact_retired_image_removal(tmp_path: Path) -> None:
    checkout = tmp_path / "clone"
    checkout.mkdir()
    runner = _UninstallRunner(
        all_images="sha256:candidate-image\n",
        removal_succeeds=True,
    )
    engine = Engine(
        binary="docker",
        compose=("docker", "compose"),
        sandbox_socket="/var/run/docker.sock",
    )

    _phase_uninstall(runner, engine, "project", checkout, {}, tmp_path, ["sha256:candidate-image"])

    names = [name for name, _command in runner.calls]
    assert names.index("uninstall-retired-images") < names.index("post-uninstall-images")
    assert not checkout.exists()


def test_uninstall_matches_bare_compose_id_to_full_dangling_inventory(tmp_path: Path) -> None:
    digest = "a" * 64
    checkout = tmp_path / "clone"
    checkout.mkdir()
    runner = _UninstallRunner(
        all_images=f"sha256:{digest}\n",
        removal_succeeds=True,
    )
    engine = Engine(
        binary="docker",
        compose=("docker", "compose"),
        sandbox_socket="/var/run/docker.sock",
    )

    _phase_uninstall(runner, engine, "project", checkout, {}, tmp_path, [digest])

    removal = dict(runner.calls)["uninstall-retired-images"]
    assert removal[-1] == f"sha256:{digest}"
    all_inventory = dict(runner.calls)["post-uninstall-all-image-ids"]
    assert "--all" in all_inventory
    assert not checkout.exists()
