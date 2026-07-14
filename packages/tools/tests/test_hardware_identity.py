"""F07 — PCI identities must be resolved from structured, sourced evidence."""

from __future__ import annotations

import json

import pytest
from disco.tools.anatomy import ToolContext
from disco.tools.builtin.hardware_identity import HardwareIdentityArgs, HardwareIdentityTool
from disco.tools.sandbox.base import ExecResult
from pydantic import ValidationError


class _Sandbox:
    def __init__(self, result: ExecResult) -> None:
        self.result = result
        self.commands: list[str] = []

    async def exec_shell(self, command: str, *, timeout_s: int) -> ExecResult:
        self.commands.append(command)
        assert timeout_s == 10
        return self.result


def _ctx(sandbox: _Sandbox) -> ToolContext:
    return ToolContext(
        sandbox=sandbox,
        workspace_path="/workspace",
        timeout_s=30,
        capabilities=object(),
        owner_id="owner",
        conversation_id="conversation",
    )


def test_pci_ids_are_normalized_and_shell_metacharacters_are_rejected() -> None:
    args = HardwareIdentityArgs(bus="pci", vendor_id="1A2b", device_id="0x00Cd")
    assert args.vendor_id == "0x1a2b"
    assert args.device_id == "0x00cd"

    with pytest.raises(ValidationError):
        HardwareIdentityArgs(bus="pci", vendor_id="1234; id", device_id="5678")


@pytest.mark.asyncio
async def test_recognized_pci_id_returns_authoritative_mapping_and_provenance() -> None:
    sandbox = _Sandbox(
        ExecResult(
            exit_code=0,
            stdout=(
                "SOURCE\t/usr/share/hwdata/pci.ids\n"
                "VENDOR\tExample Devices Incorporated\n"
                "DEVICE\tModel One Graphics Adapter\n"
            ),
            stderr="",
        )
    )

    outcome = await HardwareIdentityTool().run(
        HardwareIdentityArgs(bus="pci", vendor_id="0x1a2b", device_id="0x00cd"),
        _ctx(sandbox),
    )

    assert outcome.success is True
    assert outcome.structured == {
        "bus": "pci",
        "vendor_id": "0x1a2b",
        "device_id": "0x00cd",
        "status": "recognized",
        "vendor_name": "Example Devices Incorporated",
        "device_name": "Model One Graphics Adapter",
        "mapping_source": {
            "kind": "pci.ids",
            "path": "/usr/share/hwdata/pci.ids",
        },
    }
    assert json.loads(outcome.content) == outcome.structured
    assert len(sandbox.commands) == 1
    assert "1a2b" in sandbox.commands[0]
    assert "00cd" in sandbox.commands[0]


@pytest.mark.asyncio
async def test_unknown_pci_id_preserves_numeric_identity_without_guessing() -> None:
    sandbox = _Sandbox(
        ExecResult(
            exit_code=0,
            stdout=(
                "SOURCE\t/usr/share/misc/pci.ids\n"
                "VENDOR\tExample Devices Incorporated\n"
            ),
            stderr="",
        )
    )

    outcome = await HardwareIdentityTool().run(
        HardwareIdentityArgs(bus="pci", vendor_id="0x1a2b", device_id="0xffff"),
        _ctx(sandbox),
    )

    assert outcome.success is True
    assert outcome.structured == {
        "bus": "pci",
        "vendor_id": "0x1a2b",
        "device_id": "0xffff",
        "status": "unknown",
        "vendor_name": "Example Devices Incorporated",
        "device_name": None,
        "mapping_source": {
            "kind": "pci.ids",
            "path": "/usr/share/misc/pci.ids",
        },
    }
    assert "Model" not in outcome.content
    assert "0xffff" in outcome.content


@pytest.mark.asyncio
async def test_missing_pci_database_fails_closed_to_unknown_with_provenance() -> None:
    sandbox = _Sandbox(ExecResult(exit_code=0, stdout="", stderr=""))

    outcome = await HardwareIdentityTool().run(
        HardwareIdentityArgs(bus="pci", vendor_id="0xbeef", device_id="0xcafe"),
        _ctx(sandbox),
    )

    assert outcome.success is True
    assert outcome.structured == {
        "bus": "pci",
        "vendor_id": "0xbeef",
        "device_id": "0xcafe",
        "status": "unknown",
        "vendor_name": None,
        "device_name": None,
        "mapping_source": None,
    }
    assert "unknown" in outcome.content


@pytest.mark.asyncio
async def test_lookup_execution_failure_is_not_reclassified_as_unknown() -> None:
    sandbox = _Sandbox(ExecResult(exit_code=2, stdout="", stderr="awk failed"))

    outcome = await HardwareIdentityTool().run(
        HardwareIdentityArgs(bus="pci", vendor_id="0x1234", device_id="0x5678"),
        _ctx(sandbox),
    )

    assert outcome.success is False
    assert outcome.structured == {
        "bus": "pci",
        "vendor_id": "0x1234",
        "device_id": "0x5678",
        "status": "lookup_error",
    }
    assert outcome.error == "PCI identity lookup exited 2: awk failed"
