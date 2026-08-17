"""DefaultScenarioDriver — the single owner of scenario driving.

Its ``drive`` signature is the frozen ``drive_scenario`` signature plus ``self``.
"""

from __future__ import annotations

from typing import Any

from ._runner.bindings import ScenarioBindings
from ._runner.drive import drive_scenario
from .adapters.disco_api import CollectedRun
from .ports import ProductClient


class DefaultScenarioDriver:
    """Drive one scenario and return collected facts."""

    def __init__(self, bindings: ScenarioBindings | None = None) -> None:
        self._bindings = bindings

    async def drive(
        self,
        client: ProductClient,
        scenario: dict[str, Any],
        *,
        model: str | None,
        autonomous: bool,
        timeout_s: float,
        hard_cap_s: float,
        seed: int | None = None,
    ) -> CollectedRun:
        """Create + drive one scenario end-to-end, then collect all evidence."""
        return await drive_scenario(
            client,
            scenario,
            model=model,
            autonomous=autonomous,
            timeout_s=timeout_s,
            hard_cap_s=hard_cap_s,
            seed=seed,
            bindings=self._bindings,
        )
