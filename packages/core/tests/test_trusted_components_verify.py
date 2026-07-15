"""WO-TC3 — verify_trusted_components: the spec §8 adversarial matrix on the
pure core function (integrity auto-eject, requires-graph, probe folding)."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest
from disco.core.trusted_components.lockfile import (
    ComponentsLock,
    InstalledComponent,
    parse_lock,
    record_eject,
)
from disco.core.trusted_components.registry import TrustedComponentRegistry
from disco.core.trusted_components.verify import (
    ProbeVerdict,
    install_path,
    parse_probe_stdout,
    pin,
    verify_trusted_components,
)

NOW = "2026-07-11T00:00:00+00:00"


def _write_component(
    root: Path,
    name: str,
    version: str = "1.0.0",
    *,
    requires: list[str] | None = None,
    probe: bool = True,
    core: dict[str, bytes] | None = None,
) -> dict[str, bytes]:
    comp = root / name / version
    (comp / "core").mkdir(parents=True)
    (comp / "config").mkdir()
    core_files = core or {f"core/{name}.js": f"// {name}\n".encode()}
    for rel, data in core_files.items():
        target = comp / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
    (comp / "config" / "c.js").write_bytes(b"{}\n")
    (comp / "GUIDE.md").write_bytes(b"# g\n")
    manifest = {
        "name": name,
        "version": version,
        "kind": "trusted_component",
        "summary": name,
        "when_to_use": "t",
        "files": {rel: pin(d) for rel, d in core_files.items()},
        "config_surface": ["config/c.js"],
        "requires": requires or [],
        "guide": "GUIDE.md",
    }
    if probe:
        (comp / "probe").mkdir()
        (comp / "probe" / "probe.py").write_bytes(b"print('{}')\n")
        manifest["probe"] = "probe/probe.py"
    (comp / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    # workspace bytes as-installed
    return {install_path(name, rel): d for rel, d in core_files.items()}


def _lock_for(*names_versions: tuple[str, str]) -> ComponentsLock:
    lock = ComponentsLock()
    for name, version in names_versions:
        lock.components[name] = InstalledComponent(version=version, installed_at=NOW)
    return lock


async def _probe_pass(name: str) -> ProbeVerdict:
    return ProbeVerdict(passed=True, summary="seam ok")


def _run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


@pytest.mark.asyncio
async def test_all_green_is_verified(tmp_path: Path) -> None:
    files = _write_component(tmp_path, "database-kit")
    reg = TrustedComponentRegistry(tmp_path)
    lock = _lock_for(("database-kit", "1.0.0"))
    res = await verify_trusted_components(lock, reg, files, run_probe=_probe_pass, now_iso=NOW)
    assert [c.status for c in res.checks] == ["pass", "pass"]  # integrity + probe
    assert not res.failing and not res.newly_ejected


@pytest.mark.asyncio
async def test_core_edit_auto_ejects_and_never_blocks(tmp_path: Path) -> None:
    files = _write_component(tmp_path, "database-kit")
    reg = TrustedComponentRegistry(tmp_path)
    lock = _lock_for(("database-kit", "1.0.0"))
    key = next(iter(files))
    files[key] = b"model edited this"
    res = await verify_trusted_components(lock, reg, files, run_probe=_probe_pass, now_iso=NOW)
    assert res.newly_ejected == ["database-kit"]
    assert lock.components["database-kit"].ejected is True
    assert lock.ejects[0].reason == "core-edit-detected"
    assert lock.ejects[0].diverged_files == ["core/database-kit.js"]
    integrity = res.checks[0]
    assert integrity.status == "ejected" and "custom code you own" in integrity.evidence
    assert not res.failing  # eject NEVER blocks
    # eject consumed the component: no probe check ran for it
    assert len(res.checks) == 1


@pytest.mark.asyncio
async def test_missing_core_file_is_core_edit(tmp_path: Path) -> None:
    files = _write_component(tmp_path, "database-kit")
    reg = TrustedComponentRegistry(tmp_path)
    lock = _lock_for(("database-kit", "1.0.0"))
    files[next(iter(files))] = None  # deleted
    res = await verify_trusted_components(lock, reg, files, run_probe=None, now_iso=NOW)
    assert res.newly_ejected == ["database-kit"]


@pytest.mark.asyncio
async def test_locked_version_missing_from_registry_ejects_d11(tmp_path: Path) -> None:
    _write_component(tmp_path, "database-kit", "1.0.0")
    reg = TrustedComponentRegistry(tmp_path)
    lock = _lock_for(("database-kit", "9.9.9"))  # user upgraded Disco; version gone
    res = await verify_trusted_components(lock, reg, {}, run_probe=None, now_iso=NOW)
    assert res.newly_ejected == ["database-kit"]
    assert lock.ejects[0].reason == "registry-version-missing"
    assert not res.failing


@pytest.mark.asyncio
async def test_missing_dep_edge_fails_and_blocks(tmp_path: Path) -> None:
    dbfiles = _write_component(tmp_path, "database-kit")
    authfiles = _write_component(tmp_path, "auth-kit", requires=["database-kit>=1.0"])
    reg = TrustedComponentRegistry(tmp_path)
    lock = _lock_for(("auth-kit", "1.0.0"))  # auth WITHOUT database
    res = await verify_trusted_components(
        lock, reg, {**dbfiles, **authfiles}, run_probe=_probe_pass, now_iso=NOW
    )
    deps = next(c for c in res.checks if c.name == "component_deps:auth-kit")
    assert deps.status == "fail"
    assert "database-kit>=1.0" in deps.evidence and "add_trusted_component" in deps.evidence
    assert res.failing


@pytest.mark.asyncio
async def test_ejected_dep_satisfies_edge_with_taint_d6(tmp_path: Path) -> None:
    dbfiles = _write_component(tmp_path, "database-kit")
    authfiles = _write_component(tmp_path, "auth-kit", requires=["database-kit>=1.0"])
    reg = TrustedComponentRegistry(tmp_path)
    lock = _lock_for(("database-kit", "1.0.0"), ("auth-kit", "1.0.0"))
    record_eject(lock, "database-kit", "explicit", NOW)
    res = await verify_trusted_components(
        lock, reg, {**dbfiles, **authfiles}, run_probe=_probe_pass, now_iso=NOW
    )
    deps = next(c for c in res.checks if c.name == "component_deps:auth-kit")
    assert deps.status == "pass" and "rest on custom code" in deps.evidence
    assert not res.failing


@pytest.mark.asyncio
async def test_probe_failure_blocks_with_detail(tmp_path: Path) -> None:
    files = _write_component(tmp_path, "auth-kit")
    reg = TrustedComponentRegistry(tmp_path)
    lock = _lock_for(("auth-kit", "1.0.0"))

    async def failing_probe(name: str) -> ProbeVerdict:
        return ProbeVerdict(
            passed=False,
            summary="unauth read allowed",
            checks=[{"name": "unauth_401", "passed": False, "detail": "/admin returned 200"}],
        )

    res = await verify_trusted_components(lock, reg, files, run_probe=failing_probe, now_iso=NOW)
    probe = next(c for c in res.checks if c.name == "component_probe:auth-kit")
    assert probe.status == "fail" and "/admin returned 200" in probe.evidence
    assert res.failing


@pytest.mark.asyncio
async def test_probe_infra_exception_is_fail_never_pass(tmp_path: Path) -> None:
    files = _write_component(tmp_path, "auth-kit")
    reg = TrustedComponentRegistry(tmp_path)
    lock = _lock_for(("auth-kit", "1.0.0"))

    async def broken(name: str) -> ProbeVerdict:
        raise RuntimeError("no python3 in sandbox")

    res = await verify_trusted_components(lock, reg, files, run_probe=broken, now_iso=NOW)
    probe = next(c for c in res.checks if c.name.startswith("component_probe"))
    assert probe.status == "fail" and "no python3" in probe.evidence


@pytest.mark.asyncio
async def test_no_runner_skips_probe_with_teaching_evidence(tmp_path: Path) -> None:
    files = _write_component(tmp_path, "auth-kit")
    reg = TrustedComponentRegistry(tmp_path)
    lock = _lock_for(("auth-kit", "1.0.0"))
    res = await verify_trusted_components(lock, reg, files, run_probe=None, now_iso=NOW)
    probe = next(c for c in res.checks if c.name.startswith("component_probe"))
    assert probe.status == "skipped" and "start the preview" in probe.evidence
    assert res.skipped_probes and not res.failing


@pytest.mark.asyncio
async def test_previously_ejected_reports_once_and_runs_nothing(tmp_path: Path) -> None:
    files = _write_component(tmp_path, "database-kit")
    reg = TrustedComponentRegistry(tmp_path)
    lock = _lock_for(("database-kit", "1.0.0"))
    record_eject(lock, "database-kit", "explicit", NOW, note="mine now")
    calls: list[str] = []

    async def counting(name: str) -> ProbeVerdict:
        calls.append(name)
        return ProbeVerdict(passed=True)

    res = await verify_trusted_components(lock, reg, files, run_probe=counting, now_iso=NOW)
    assert len(res.checks) == 1 and res.checks[0].status == "ejected"
    assert calls == []  # nothing executed for an ejected component
    assert not res.newly_ejected  # no duplicate eject


@pytest.mark.asyncio
async def test_fingerprint_material_is_deterministic(tmp_path: Path) -> None:
    files = _write_component(tmp_path, "database-kit")
    reg = TrustedComponentRegistry(tmp_path)
    a = await verify_trusted_components(
        _lock_for(("database-kit", "1.0.0")), reg, files, run_probe=None, now_iso=NOW
    )
    b = await verify_trusted_components(
        _lock_for(("database-kit", "1.0.0")), reg, files, run_probe=None, now_iso=NOW
    )
    assert a.fingerprint_material() == b.fingerprint_material()


@pytest.mark.asyncio
async def test_rbac_two_edge_requires_graph_blocks_then_passes() -> None:
    """WO-TC5: rbac-kit's 2-edge requires graph, proven on the REAL registry.
    Missing EITHER auth-kit or database-kit FAILS (blocks finish); both present
    passes. This is the broken-deps-then-fixed demonstration the spec calls for."""
    reg = TrustedComponentRegistry.default()
    if reg.get("rbac-kit") is None:
        pytest.skip("rbac-kit not shipped in the registry")

    def files_for(*names: str) -> dict[str, bytes]:
        out: dict[str, bytes] = {}
        for n in names:
            comp = reg.get(n)
            assert comp is not None
            for rel, data in comp.install_tree().items():
                out[install_path(n, rel)] = data
        return out

    def rbac_deps(res):
        return next(c for c in res.checks if c.name == "component_deps:rbac-kit")

    # rbac alone — BOTH edges missing → deps FAIL and block.
    res = await verify_trusted_components(
        _lock_for(("rbac-kit", "1.0.0")), reg, files_for("rbac-kit"), run_probe=None, now_iso=NOW
    )
    deps = rbac_deps(res)
    assert deps.status == "fail"
    assert "auth-kit>=1.0" in deps.evidence and "database-kit>=1.0" in deps.evidence
    assert res.failing

    # one edge satisfied (auth), the other (database) still missing → FAIL.
    res2 = await verify_trusted_components(
        _lock_for(("rbac-kit", "1.0.0"), ("auth-kit", "1.0.0")),
        reg,
        files_for("rbac-kit", "auth-kit"),
        run_probe=None,
        now_iso=NOW,
    )
    assert rbac_deps(res2).status == "fail"
    assert "database-kit>=1.0" in rbac_deps(res2).evidence

    # both edges installed → rbac's deps PASS (installing the dep fixed it).
    res3 = await verify_trusted_components(
        _lock_for(("rbac-kit", "1.0.0"), ("auth-kit", "1.0.0"), ("database-kit", "1.0.0")),
        reg,
        files_for("rbac-kit", "auth-kit", "database-kit"),
        run_probe=None,
        now_iso=NOW,
    )
    assert rbac_deps(res3).status == "pass"


def test_parse_probe_stdout_last_line_wins_and_garbage_raises() -> None:
    v = parse_probe_stdout('debug noise\nmore noise\n{"passed": true, "summary": "ok"}\n')
    assert v.passed and v.summary == "ok"
    with pytest.raises(ValueError):
        parse_probe_stdout("")
    with pytest.raises((ValueError, json.JSONDecodeError)):
        parse_probe_stdout("not json at all")


def test_forged_lockfile_cannot_carry_pins() -> None:
    """D1 regression: even a hand-crafted lockfile has NO channel for hashes —
    unknown keys are rejected wholesale by strict parsing? No: pydantic default
    IGNORES extras, so assert the model simply has no pin-shaped fields and a
    forged 'files' key does not round-trip."""
    forged = json.dumps(
        {
            "lockfile_version": 1,
            "components": {
                "auth-kit": {
                    "version": "1.0.0",
                    "installed_at": NOW,
                    "files": {"core/auth.js": "sha256:" + "0" * 64},
                }
            },
            "pins": {"anything": "sha256:" + "0" * 64},
        }
    ).encode()
    lock = parse_lock(forged)
    dumped = json.loads(lock.model_dump_json())
    assert "pins" not in dumped
    assert "files" not in dumped["components"]["auth-kit"]


def test_lockfile_rejects_malformed_component_name() -> None:
    """TC3-review LOW: the dict KEY (component name) validates `version` but had
    no shape guard — a forged name must fail closed as LockfileCorrupt at parse."""
    from disco.core.trusted_components.lockfile import LockfileCorrupt

    for bad_name in ("Auth-Kit", "auth_kit", "auth kit", "../etc", "auth.kit", ""):
        forged = json.dumps(
            {
                "lockfile_version": 1,
                "components": {bad_name: {"version": "1.0.0", "installed_at": NOW}},
            }
        ).encode()
        with pytest.raises(LockfileCorrupt):
            parse_lock(forged)

    # A malformed name hiding in eject history is rejected too.
    forged_eject = json.dumps(
        {
            "lockfile_version": 1,
            "components": {},
            "ejects": [{"name": "BAD NAME", "at": NOW, "reason": "explicit"}],
        }
    ).encode()
    with pytest.raises(LockfileCorrupt):
        parse_lock(forged_eject)
