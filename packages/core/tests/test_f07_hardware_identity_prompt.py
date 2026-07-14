"""F07 — Build/Agent drivers must not infer hardware marketing names."""

from __future__ import annotations

import pytest
from disco.core.llm import DriverPrompts, ModelRole, OperatingMode


@pytest.mark.parametrize("flavor", ["build", "agent"])
@pytest.mark.parametrize(
    ("mode", "assist"),
    [
        (OperatingMode.PLANNING, False),
        (OperatingMode.LONG_HORIZON, False),
        (OperatingMode.LONG_HORIZON, True),
    ],
)
def test_driver_prompts_require_structured_hardware_identity_evidence(
    flavor: str,
    mode: OperatingMode,
    assist: bool,
) -> None:
    prompt = DriverPrompts(flavor=flavor).system_prompt(
        model_family="unknown",
        mode=mode,
        role=ModelRole.AGENT_DRIVER,
        assist=assist,
    )

    assert "hardware_identity" in prompt
    assert "numeric PCI" in prompt
    assert "status=recognized" in prompt
    assert "status=unknown" in prompt
    assert "marketing name" in prompt
    assert "Never infer or guess" in prompt
