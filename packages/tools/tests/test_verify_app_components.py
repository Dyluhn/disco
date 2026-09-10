"""WO-TC3 — the verify_web_app fold: component checks ride the W-45 verdict."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from disco.core.trusted_components.lockfile import LOCKFILE_RELPATH
from disco.core.trusted_components.registry import TrustedComponentRegistry
from disco.core.trusted_components.verify import pin
from disco.tools.anatomy import ToolContext
from disco.tools.builtin import verify_app as va_mod
from disco.tools.builtin.verify_app import VerifyWebAppTool
from disco.tools.sandbox.base import ExecResult
from disco.tools.secrets import CapabilityBroker
from tool_fakes import FakeSandboxInstance


class ProbeFakeSandbox(FakeSandboxInstance):
    """exec_shell returns a canned probe verdict and records the command."""

    def __init__(self, probe_stdout: str = '{"passed": true, "summary": "seam ok"}') -> None:
        super().__init__()
        self.probe_stdout = probe_stdout
        self.execs: list[str] = []

    async def exec_shell(self, cmd: str, *, timeout_s: int) -> ExecResult:
        self.execs.append(cmd)
        return ExecResult(exit_code=0, stdout=self.probe_stdout, stderr="")


def _ctx(sbx) -> ToolContext:
    return ToolContext(
        sandbox=sbx,
        workspace_path=".",
        timeout_s=30,
        capabilities=CapabilityBroker().grant(frozenset()),
        owner_id="t",
        conversation_id="c",
    )


def _verdict(passed: bool = True) -> dict:
    return {
        "verdict": "pass" if passed else "fail",
        "passed": passed,
        "url": "http://127.0.0.1:4173/",
        "http_status": 200,
        "summary": "render ok",
        "next_action": "",
        "failure_fingerprint": "",
        "console_errors": [],
        "network_failures": [],
    }


def _seed_registry(root: Path, *, requires: list[str] | None = None) -> None:
    comp = root / "auth-kit" / "1.0.0"
    (comp / "core").mkdir(parents=True)
    (comp / "config").mkdir()
    (comp / "probe").mkdir()
    (comp / "core" / "auth.js").write_bytes(b"// auth core\n")
    (comp / "config" / "auth.config.js").write_bytes(b"{}\n")
    (comp / "probe" / "probe.py").write_bytes(b"print('{}')\n")
    (comp / "GUIDE.md").write_bytes(b"# auth\n")
    manifest = {
        "name": "auth-kit",
        "version": "1.0.0",
        "kind": "trusted_component",
        "summary": "auth",
        "when_to_use": "t",
        "files": {"core/auth.js": pin(b"// auth core\n")},
        "config_surface": ["config/auth.config.js"],
        "requires": requires or [],
        "probe": "probe/probe.py",
        "guide": "GUIDE.md",
    }
    (comp / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")


@pytest.fixture
def registry_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = tmp_path / "reg"
    root.mkdir()

    class _Patched(TrustedComponentRegistry):
        @classmethod
        def default(cls) -> TrustedComponentRegistry:
            return TrustedComponentRegistry(root)

    monkeypatch.setattr(va_mod, "TrustedComponentRegistry", _Patched)
    return root


def _install(sbx, *, edited: bool = False) -> None:
    sbx._fs["src/trusted/auth-kit/core/auth.js"] = b"model edit" if edited else b"// auth core\n"
    sbx._fs["src/trusted/auth-kit/GUIDE.md"] = b"# auth\n"
    sbx._fs[LOCKFILE_RELPATH] = json.dumps(
        {
            "lockfile_version": 1,
            "components": {
                "auth-kit": {"version": "1.0.0", "installed_at": "2026-07-11T00:00:00+00:00"}
            },
            "ejects": [],
        }
    ).encode()


@pytest.mark.asyncio
async def test_no_lockfile_leaves_verdict_untouched(registry_root: Path) -> None:
    _seed_registry(registry_root)
    verdict = _verdict()
    out = await VerifyWebAppTool()._fold_trusted_components(
        _ctx(ProbeFakeSandbox()), dict(verdict), probes_allowed=True
    )
    assert out == verdict


@pytest.mark.asyncio
async def test_green_components_pass_and_probe_ran_in_sandbox(registry_root: Path) -> None:
    _seed_registry(registry_root)
    sbx = ProbeFakeSandbox()
    _install(sbx)
    out = await VerifyWebAppTool()._fold_trusted_components(
        _ctx(sbx), _verdict(), probes_allowed=True
    )
    assert out["passed"] is True
    statuses = {c["name"]: c["status"] for c in out["component_checks"]}
    assert statuses["component_integrity:auth-kit"] == "pass"
    assert statuses["component_probe:auth-kit"] == "pass"
    # probe was re-materialized fresh and executed inside the sandbox
    assert ".disco/tc-probe/auth-kit/probe.py" in sbx._fs
    assert len(sbx.execs) == 1
    assert "--base-url http://127.0.0.1:4173/" in sbx.execs[0]
    # fingerprint extended (component state keyed into STUCK detection)
    assert out["failure_fingerprint"] != ""


@pytest.mark.asyncio
async def test_core_edit_ejects_persists_and_does_not_block(registry_root: Path) -> None:
    _seed_registry(registry_root)
    sbx = ProbeFakeSandbox()
    _install(sbx, edited=True)
    out = await VerifyWebAppTool()._fold_trusted_components(
        _ctx(sbx), _verdict(), probes_allowed=True
    )
    assert out["passed"] is True  # eject never blocks
    assert "components:" in out["summary"]
    lock = json.loads(sbx._fs[LOCKFILE_RELPATH])
    assert lock["components"]["auth-kit"]["ejected"] is True
    assert lock["ejects"][0]["reason"] == "core-edit-detected"
    guide = sbx._fs["src/trusted/auth-kit/GUIDE.md"].decode()
    assert "Ejected" in guide and "core/auth.js" in guide
    assert sbx.execs == []  # no probe for an ejected component


@pytest.mark.asyncio
async def test_missing_dep_flips_verdict_to_fail_with_teaching(registry_root: Path) -> None:
    _seed_registry(registry_root, requires=["database-kit>=1.0"])
    sbx = ProbeFakeSandbox()
    _install(sbx)
    out = await VerifyWebAppTool()._fold_trusted_components(
        _ctx(sbx), _verdict(), probes_allowed=True
    )
    assert out["passed"] is False and out["verdict"] == "fail"
    assert "database-kit>=1.0" in out["summary"]
    assert "add_trusted_component" in out["next_action"]


@pytest.mark.asyncio
async def test_probe_fail_flips_verdict(registry_root: Path) -> None:
    _seed_registry(registry_root)
    sbx = ProbeFakeSandbox(
        probe_stdout=json.dumps(
            {
                "passed": False,
                "summary": "unauth read allowed",
                "checks": [{"name": "unauth_401", "passed": False, "detail": "/x got 200"}],
            }
        )
    )
    _install(sbx)
    out = await VerifyWebAppTool()._fold_trusted_components(
        _ctx(sbx), _verdict(), probes_allowed=True
    )
    assert out["passed"] is False
    assert "/x got 200" in out["summary"] or "unauth read allowed" in out["summary"]


@pytest.mark.asyncio
async def test_probes_not_allowed_skip_is_not_blocking(registry_root: Path) -> None:
    _seed_registry(registry_root)
    sbx = ProbeFakeSandbox()
    _install(sbx)
    out = await VerifyWebAppTool()._fold_trusted_components(
        _ctx(sbx), _verdict(passed=False), probes_allowed=False
    )
    statuses = {c["name"]: c["status"] for c in out["component_checks"]}
    assert statuses["component_probe:auth-kit"] == "skipped"
    assert out["verdict"] == "fail"  # already failing render verdict, unchanged


@pytest.mark.asyncio
async def test_corrupt_lockfile_is_blocking_and_teaches_fix(registry_root: Path) -> None:
    _seed_registry(registry_root)
    sbx = ProbeFakeSandbox()
    sbx._fs[LOCKFILE_RELPATH] = b"{nope"
    out = await VerifyWebAppTool()._fold_trusted_components(
        _ctx(sbx), _verdict(), probes_allowed=True
    )
    assert out["passed"] is False
    assert LOCKFILE_RELPATH in out["next_action"]
    assert sbx._fs[LOCKFILE_RELPATH] == b"{nope"  # never overwritten


@pytest.mark.asyncio
async def test_probe_nonzero_exit_is_honest_fail(registry_root: Path) -> None:
    _seed_registry(registry_root)

    class BrokenExec(ProbeFakeSandbox):
        async def exec_shell(self, cmd: str, *, timeout_s: int) -> ExecResult:
            return ExecResult(exit_code=127, stdout="", stderr="python3: not found")

    sbx = BrokenExec()
    _install(sbx)
    out = await VerifyWebAppTool()._fold_trusted_components(
        _ctx(sbx), _verdict(), probes_allowed=True
    )
    assert out["passed"] is False
    assert "probe" in out["summary"] and "not found" in out["summary"]
