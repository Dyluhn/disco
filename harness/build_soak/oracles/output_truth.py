"""OutputTruthOracle (guidelines §11.5, §16).

If the run says it FINISHED, the claimed output must actually exist (§7 truth
hierarchy: a model/agent saying "the build worked" never overrides workspace /
preview truth).

Checks (only when the run reached a finished/verified terminal state AND the
scenario asserts output):
  * required workspace files exist             -> FALSE_FINISH_NO_OUTPUT
  * required file contents present (must_contain) -> ARTIFACT_TRUTH_MISMATCH
  * preview health passes (when required)      -> FALSE_FINISH_PREVIEW_BROKEN
  * preview content present (must_contain)     -> PREVIEW_TRUTH_MISMATCH

Evidence shapes (captured by the runner; supplied as plain dicts):
  workspace_manifest: {"<relpath>": "<file content>", ...}
                  or  {"<relpath>": {"content": "..."}, ...}
                  or  {"files": {"<relpath>": "...", ...}}
  preview: {"health": {"status": 200|404|...}, "content": "<served html>"}
"""

from __future__ import annotations

from typing import Any

from .. import failure_codes as fc
from ..events import terminal_status
from .schema import OracleResult, failing, passing, skipping

_ORACLE = "OutputTruthOracle"

_FINISHED_STATES = frozenset({"FINISHED", "VERIFIED"})


def _normalize_workspace(manifest: dict[str, Any] | None) -> dict[str, str]:
    if not manifest:
        return {}
    nested = manifest.get("files")
    files: dict[str, Any] = nested if isinstance(nested, dict) else manifest
    out: dict[str, str] = {}
    for path, val in files.items():
        if isinstance(val, dict):
            out[str(path)] = str(val.get("content", ""))
        else:
            out[str(path)] = str(val)
    return out


class OutputTruthOracle:
    def check(
        self,
        events: list[dict[str, Any]],
        *,
        scenario: dict[str, Any] | None = None,
        workspace_manifest: dict[str, Any] | None = None,
        preview: dict[str, Any] | None = None,
    ) -> list[OracleResult]:
        assertions = (scenario or {}).get("assertions") or {}
        workspace_assert = assertions.get("workspace") or {}
        preview_assert = assertions.get("preview") or {}
        wants_output = bool(workspace_assert.get("files")) or bool(preview_assert.get("required"))

        if not wants_output:
            return [skipping(_ORACLE, reason="scenario asserts no output truth")]

        term = terminal_status(events)
        terminal_expected = (scenario or {}).get("assertions", {}).get("terminal_status_in")
        finished = term in _FINISHED_STATES or (
            bool(terminal_expected) and term in set(terminal_expected)
        )
        if not finished:
            # The run did not claim to finish, so there is no false-finish to judge.
            return [
                skipping(
                    _ORACLE,
                    reason=f"run not in a finished terminal state (terminal={term})",
                    facts={"terminal_status": term},
                )
            ]

        files = _normalize_workspace(workspace_manifest)

        # --- workspace file existence + content truth ---
        for spec in workspace_assert.get("files") or []:
            path = spec.get("path")
            if path is None:
                continue
            if path not in files:
                return [
                    failing(
                        _ORACLE,
                        fc.FALSE_FINISH_NO_OUTPUT,
                        first_broken_link="finish -> required_file_exists",
                        facts={"required_path": path, "present_paths": sorted(files)},
                    )
                ]
            content = files[path]
            for needle in spec.get("must_contain") or []:
                if needle not in content:
                    return [
                        failing(
                            _ORACLE,
                            fc.ARTIFACT_TRUTH_MISMATCH,
                            first_broken_link="finish -> required_content_present",
                            facts={"path": path, "missing_substring": needle},
                        )
                    ]

        # --- preview truth ---
        if preview_assert.get("required"):
            if not preview:
                return [
                    failing(
                        _ORACLE,
                        fc.FALSE_FINISH_PREVIEW_BROKEN,
                        first_broken_link="finish -> preview_health",
                        facts={"reason": "preview required but no preview evidence captured"},
                    )
                ]
            health = preview.get("health") or {}
            status = health.get("status")
            if status is not None and int(status) >= 400:
                return [
                    failing(
                        _ORACLE,
                        fc.FALSE_FINISH_PREVIEW_BROKEN,
                        first_broken_link="finish -> preview_health",
                        facts={"preview_health_status": status},
                    )
                ]
            preview_content = str(preview.get("content", ""))
            for needle in preview_assert.get("must_contain") or []:
                if needle not in preview_content:
                    return [
                        failing(
                            _ORACLE,
                            fc.PREVIEW_TRUTH_MISMATCH,
                            first_broken_link="finish -> preview_content",
                            facts={"missing_substring": needle},
                        )
                    ]

        return [passing(_ORACLE, facts={"terminal_status": term, "file_count": len(files)})]
