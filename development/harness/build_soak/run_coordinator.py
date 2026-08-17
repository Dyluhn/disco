"""RunCoordinator — the single owner of one-run orchestration.

Its ``run_once`` signature is the frozen ``run_once`` signature plus ``self``.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from ._runner.bindings import CoordinatorBindings
from ._runner.coordinator import run_once
from .ports import ProductClient


class RunCoordinator:
    """Orchestrate one run end-to-end: drive, retain, classify, summarize."""

    def __init__(self, bindings: CoordinatorBindings | None = None) -> None:
        self._bindings = bindings

    async def run_once(
        self,
        client: ProductClient,
        scenario: dict[str, Any],
        *,
        run_id: str,
        out_root: str | Path,
        model: str | None = None,
        autonomous: bool = False,
        commit: str = "",
        repo_revision: str = "",
        repo_dirty: bool = False,
        kernel: str = "disco",
        timeout_s: float = 180.0,
        hard_cap_s: float = 1200.0,
        require_inspect_trace: bool = False,
        parallel_workers: int = 1,
        seed: int | None = None,
    ) -> dict[str, Any]:
        """Run one scenario: drive, collect evidence, classify, and return the record."""
        return await run_once(
            client,
            scenario,
            run_id=run_id,
            out_root=out_root,
            model=model,
            autonomous=autonomous,
            commit=commit,
            repo_revision=repo_revision,
            repo_dirty=repo_dirty,
            kernel=kernel,
            timeout_s=timeout_s,
            hard_cap_s=hard_cap_s,
            require_inspect_trace=require_inspect_trace,
            parallel_workers=parallel_workers,
            seed=seed,
            bindings=self._bindings,
        )
