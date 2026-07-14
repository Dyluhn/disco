"""R2 (G03) — the expanded typed intent contract at the REAL release_declare tool.

Closeout remediation R2, at the public TOOL boundary: the `release_declare` tool must
ACCEPT and PERSIST the new typed fields (runtime, install_cmd, package_manager,
lockfile, output_dir, scoped env) into the host-owned `release-intent.json` sidecar,
and still REJECT an inline credential smuggled through the new install_cmd argv. These
live OUTSIDE the frozen closeout dirs (no `export_track1_closeout` marker) so they
never perturb the acceptance manifest. Harness mirrors the frozen C1/C4/G06 tool tests:
a REAL `ConversationRuntime` executing the REAL `ReleaseDeclareTool`, with the persisted
sidecar inspected as raw bytes; only `DISCO_DATA_DIR` + `ConfigStore.load` are seamed.
"""

from __future__ import annotations

import itertools
import json
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest
from disco.agent_server import ConversationRuntime
from disco.core import SqliteEventStore, ToolCall
from disco.core.llm import (
    ConfigStore,
    DefaultLLMRouter,
    ModelEntry,
    ProjectStorageSettings,
    RouterConfig,
)
from disco.tools import ProcessSandboxService
from disco.tools.projects import ProjectStore

_ID = itertools.count()


class _NeverCalledProvider:
    name = "fake"

    async def complete(self, req: Any, *, model: Any) -> Any:  # pragma: no cover
        raise AssertionError("release_declare must not call the model")

    async def stream_complete(
        self, req: Any, *, model: Any
    ) -> AsyncIterator[Any]:  # pragma: no cover
        raise AssertionError("release_declare must not call the model")
        yield

    def supports(self, requirement: Any, *, model: Any) -> bool:
        return True


def _cfg() -> RouterConfig:
    return RouterConfig(
        models={"m": ModelEntry(model_id="m", provider="fake", context_window=8192)},
        default_model="m",
    )


def _runtime(
    monkeypatch: pytest.MonkeyPatch, data_dir: Path
) -> tuple[ConversationRuntime, SqliteEventStore]:
    store = SqliteEventStore(":memory:")
    router = DefaultLLMRouter(_cfg(), {"fake": _NeverCalledProvider()})
    cfg_store = ConfigStore(path=Path("/dev/null"))
    monkeypatch.setenv("DISCO_DATA_DIR", str(data_dir))
    configured = _cfg().model_copy(update={"projects": ProjectStorageSettings(projects_root="")})
    monkeypatch.setattr(cfg_store, "load", lambda: configured)
    runtime = ConversationRuntime(
        store, router=router, config_store=cfg_store, sandbox_service=ProcessSandboxService()
    )
    return runtime, store


async def _declare(runtime: ConversationRuntime, cid: str, arguments: dict[str, object]) -> Any:
    runtime.set_surface(cid, "build")
    return await runtime.execute_pi_tool(
        cid, ToolCall(tool_name="release_declare", arguments=arguments, call_id=f"r2-{cid}")
    )


def _sidecar(cid: str) -> dict[str, Any]:
    path = ProjectStore("").release_intent_for(cid)
    assert path.is_file(), "no release-intent sidecar persisted"
    parsed = json.loads(path.read_text(encoding="utf-8"))
    assert isinstance(parsed, dict)
    return parsed


@pytest.mark.asyncio
async def test_expanded_intent_fields_round_trip_into_sidecar(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cid = f"conv_r2decl_{next(_ID)}"
    runtime, store = _runtime(monkeypatch, tmp_path / "data")
    store.create_conversation(cid, owner_id="local")
    result = await _declare(
        runtime,
        cid,
        {
            "runtime": "static",
            "build_cmd": ["npm", "run", "build"],
            "install_cmd": ["npm", "ci"],
            "package_manager": "npm",
            "lockfile": "package-lock.json",
            "output_dir": "dist",
            "health_path": "/",
            "env": [{"name": "API_BASE_URL", "scope": "runtime", "required": True}],
        },
    )
    assert result.success, (result.error, result.content)

    sidecar = _sidecar(cid)
    assert sidecar["schema_version"] == 3
    assert sidecar["runtime"] == "static"
    assert sidecar["install_cmd"] == ["npm", "ci"]
    assert sidecar["package_manager"] == "npm"
    assert sidecar["lockfile"] == "package-lock.json"
    assert sidecar["output_dir"] == "dist"
    assert sidecar["env"] == [
        {"name": "API_BASE_URL", "scope": "runtime", "required": True, "secret": "public"}
    ]


@pytest.mark.asyncio
async def test_install_cmd_positional_credential_is_rejected_and_persists_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A credential smuggled as a bare positional operand in the NEW install_cmd argv is
    rejected value-free, and NO sidecar is written (the same G02 rail R1 applied to
    start/build/migrate now covers install_cmd)."""
    cid = f"conv_r2badinst_{next(_ID)}"
    runtime, store = _runtime(monkeypatch, tmp_path / "data")
    store.create_conversation(cid, owner_id="local")
    secret = "npm_R2CREDMARK0aK7bQ2xR9mL4wZ8vT1nH6pJ3cF5dS"
    result = await _declare(
        runtime,
        cid,
        {
            "start_cmd": ["node", "server.js"],
            "install_cmd": ["npm", "config", "set", "//registry.example/:_authToken", secret],
        },
    )
    assert result.success is False
    assert "R2CREDMARK" not in (result.content or ""), "the tool must not echo the secret"
    assert not ProjectStore("").release_intent_for(cid).is_file(), "a rejection persists nothing"


@pytest.mark.asyncio
async def test_benign_minimal_declaration_still_accepted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cid = f"conv_r2benign_{next(_ID)}"
    runtime, store = _runtime(monkeypatch, tmp_path / "data")
    store.create_conversation(cid, owner_id="local")
    result = await _declare(runtime, cid, {"start_cmd": ["node", "server.js"]})
    assert result.success, (result.error, result.content)
    sidecar = _sidecar(cid)
    assert sidecar["schema_version"] == 3
    assert sidecar["start_cmd"] == ["node", "server.js"]
    # Absent optional fields serialize out (exclude_if=None) — a clean minimal shape.
    for absent in ("runtime", "output_dir", "lockfile", "package_manager"):
        assert absent not in sidecar, absent
