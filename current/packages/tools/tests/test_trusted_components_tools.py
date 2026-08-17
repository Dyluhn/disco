"""WO-TC2 — add_trusted_component / eject_trusted_component behavior (spec §3)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from disco.core.trusted_components.lockfile import LOCKFILE_RELPATH
from disco.core.trusted_components.registry import TrustedComponentRegistry
from disco.core.trusted_components.verify import pin
from disco.tools.anatomy import ToolContext
from disco.tools.builtin import trusted_components as tc_mod
from disco.tools.builtin.trusted_components import (
    AddTrustedComponentArgs,
    AddTrustedComponentTool,
    EjectTrustedComponentArgs,
    EjectTrustedComponentTool,
)
from disco.tools.secrets import CapabilityBroker
from tool_fakes import FakeSandboxInstance


def _ctx(sbx) -> ToolContext:
    return ToolContext(
        sandbox=sbx,
        workspace_path=".",
        timeout_s=30,
        capabilities=CapabilityBroker().grant(frozenset()),
        owner_id="t",
        conversation_id="c",
    )


def _write_component(
    root: Path,
    name: str,
    version: str = "1.0.0",
    *,
    requires: list[str] | None = None,
    core: dict[str, bytes] | None = None,
) -> None:
    comp = root / name / version
    (comp / "core").mkdir(parents=True)
    (comp / "config").mkdir()
    (comp / "probe").mkdir()
    core_files = core or {"core/main.js": b"export const x = 1;\n"}
    for rel, data in core_files.items():
        target = comp / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
    (comp / "config" / f"{name}.config.js").write_bytes(b"export default {};\n")
    (comp / "probe" / "probe.py").write_bytes(b"print('{}')\n")
    (comp / "GUIDE.md").write_bytes(f"# {name} guide\nwire it up.\n".encode())
    manifest = {
        "name": name,
        "version": version,
        "kind": "trusted_component",
        "summary": name,
        "when_to_use": f"use {name}",
        "files": {rel: pin(data) for rel, data in core_files.items()},
        "config_surface": [f"config/{name}.config.js"],
        "requires": requires or [],
        "provides": [name.removesuffix("-kit")],
        "mounts": {"routes_prefix": f"/{name}", "middleware": None},
        "probe": "probe/probe.py",
        "guide": "GUIDE.md",
    }
    (comp / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")


@pytest.fixture
def registry_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point the tool module's registry at a synthetic tree."""
    root = tmp_path / "registry"
    root.mkdir()
    _write_component(root, "database-kit")
    _write_component(root, "auth-kit", requires=["database-kit>=1.0"])

    class _Patched(TrustedComponentRegistry):
        @classmethod
        def default(cls) -> TrustedComponentRegistry:
            return TrustedComponentRegistry(root)

    monkeypatch.setattr(tc_mod, "TrustedComponentRegistry", _Patched)
    return root


def _lock(sbx: FakeSandboxInstance) -> dict:
    return json.loads(sbx._fs[LOCKFILE_RELPATH])


@pytest.mark.asyncio
async def test_unknown_component_refusal_teaches_catalog(registry_root: Path) -> None:
    out = await AddTrustedComponentTool().run(
        AddTrustedComponentArgs(name="nope-kit"), _ctx(FakeSandboxInstance())
    )
    assert not out.success and out.error == "unknown_component"
    assert "database-kit" in (out.content or "")  # the catalog is in the refusal


@pytest.mark.asyncio
async def test_missing_dependency_refusal_names_edge_and_fix(registry_root: Path) -> None:
    out = await AddTrustedComponentTool().run(
        AddTrustedComponentArgs(name="auth-kit"), _ctx(FakeSandboxInstance())
    )
    assert not out.success and out.error == "missing_dependency"
    assert "database-kit>=1.0" in out.content
    assert "add_trusted_component(name='database-kit')" in out.content


@pytest.mark.asyncio
async def test_install_writes_tree_lockfile_and_returns_guide(registry_root: Path) -> None:
    sbx = FakeSandboxInstance()
    out = await AddTrustedComponentTool().run(
        AddTrustedComponentArgs(name="database-kit"), _ctx(sbx)
    )
    assert out.success, out.content
    assert "src/trusted/database-kit/core/main.js" in sbx._fs
    assert "src/trusted/database-kit/GUIDE.md" in sbx._fs
    # probe + manifest never installed (D1/D3)
    assert not any("probe" in p or "manifest.json" in p for p in sbx._fs)
    assert _lock(sbx)["components"]["database-kit"]["version"] == "1.0.0"
    assert "# database-kit guide" in out.content  # GUIDE verbatim
    assert "routes live under /database-kit" in out.content  # mounts line


@pytest.mark.asyncio
async def test_dependency_chain_installs_and_reinstall_is_idempotent(registry_root: Path) -> None:
    sbx = FakeSandboxInstance()
    ctx = _ctx(sbx)
    assert (
        await AddTrustedComponentTool().run(AddTrustedComponentArgs(name="database-kit"), ctx)
    ).success
    assert (
        await AddTrustedComponentTool().run(AddTrustedComponentArgs(name="auth-kit"), ctx)
    ).success
    again = await AddTrustedComponentTool().run(AddTrustedComponentArgs(name="auth-kit"), ctx)
    assert again.success and again.structured["written"] == []  # byte-identical → no-op


@pytest.mark.asyncio
async def test_collision_refuses_and_lists_paths(registry_root: Path) -> None:
    sbx = FakeSandboxInstance()
    sbx._fs["src/trusted/database-kit/core/main.js"] = b"my own code"
    out = await AddTrustedComponentTool().run(
        AddTrustedComponentArgs(name="database-kit"), _ctx(sbx)
    )
    assert not out.success and out.error == "collision"
    assert "src/trusted/database-kit/core/main.js" in out.content
    assert LOCKFILE_RELPATH not in sbx._fs  # nothing recorded on refusal


@pytest.mark.asyncio
async def test_corrupt_lockfile_refuses_never_overwrites(registry_root: Path) -> None:
    sbx = FakeSandboxInstance()
    sbx._fs[LOCKFILE_RELPATH] = b"{not json"
    out = await AddTrustedComponentTool().run(
        AddTrustedComponentArgs(name="database-kit"), _ctx(sbx)
    )
    assert not out.success and out.error == "lockfile_corrupt"
    assert sbx._fs[LOCKFILE_RELPATH] == b"{not json"  # untouched


@pytest.mark.asyncio
async def test_eject_flow_and_idempotence(registry_root: Path) -> None:
    sbx = FakeSandboxInstance()
    ctx = _ctx(sbx)
    await AddTrustedComponentTool().run(AddTrustedComponentArgs(name="database-kit"), ctx)
    not_installed = await EjectTrustedComponentTool().run(
        EjectTrustedComponentArgs(name="auth-kit", reason="x"), ctx
    )
    assert not not_installed.success and not_installed.error == "not_installed"

    out = await EjectTrustedComponentTool().run(
        EjectTrustedComponentArgs(name="database-kit", reason="need custom migrations"), ctx
    )
    assert out.success
    lock = _lock(sbx)
    assert lock["components"]["database-kit"]["ejected"] is True
    assert lock["ejects"][0]["reason"] == "explicit"
    assert lock["ejects"][0]["note"] == "need custom migrations"
    guide = sbx._fs["src/trusted/database-kit/GUIDE.md"].decode()
    assert "Ejected" in guide and "custom code you own" in guide

    again = await EjectTrustedComponentTool().run(
        EjectTrustedComponentArgs(name="database-kit", reason="again"), ctx
    )
    assert again.success and again.structured["already_ejected"] is True
    assert len(_lock(sbx)["ejects"]) == 1  # no duplicate history


@pytest.mark.asyncio
async def test_reinstall_after_eject_requires_reverting_core(registry_root: Path) -> None:
    sbx = FakeSandboxInstance()
    ctx = _ctx(sbx)
    await AddTrustedComponentTool().run(AddTrustedComponentArgs(name="database-kit"), ctx)
    await EjectTrustedComponentTool().run(
        EjectTrustedComponentArgs(name="database-kit", reason="fork"), ctx
    )
    sbx._fs["src/trusted/database-kit/core/main.js"] = b"forked"
    blocked = await AddTrustedComponentTool().run(
        AddTrustedComponentArgs(name="database-kit"), _ctx(sbx)
    )
    assert not blocked.success and blocked.error == "collision"
    # Revert the fork → reinstall succeeds and starts a fresh trusted lifetime,
    # keeping the eject history (GUIDE.md still carries the banner → collision on
    # it is expected; drop the banner too, as a real revert would).
    reg = TrustedComponentRegistry(registry_root).get("database-kit")
    assert reg is not None
    for rel, data in reg.install_tree().items():
        sbx._fs[f"src/trusted/database-kit/{rel}"] = data
    fresh = await AddTrustedComponentTool().run(
        AddTrustedComponentArgs(name="database-kit"), _ctx(sbx)
    )
    assert fresh.success
    lock = _lock(sbx)
    assert lock["components"]["database-kit"]["ejected"] is False
    assert len(lock["ejects"]) == 1  # history preserved
