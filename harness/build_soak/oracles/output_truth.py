"""OutputTruthOracle (guidelines §11.5, §16).

If the run says it FINISHED, the claimed output must actually exist (§7 truth
hierarchy: a model/agent saying "the build worked" never overrides workspace /
preview truth).

Checks (only when the run reached a finished/verified terminal state AND the
scenario asserts output):
  * required workspace files exist             -> FALSE_FINISH_NO_OUTPUT
  * required file contents present (must_contain) -> ARTIFACT_TRUTH_MISMATCH
  * preview health passes (when required)      -> FALSE_FINISH_PREVIEW_BROKEN
  * required UI text proven by a current governed verifier receipt
    (legacy non-governed runs fall back to captured preview source)
                                                -> PREVIEW_TRUTH_MISMATCH
"""

from __future__ import annotations

import re
from typing import Any

from .. import failure_codes as fc
from ..events import terminal_status
from ._workspace_truth import (
    check_destructive_elision,
    check_exact_paths,
    check_file_existence,
    check_root_resolution_failure,
    collect_content_mismatches,
    content_normalized,
    fold_content_mismatches,
    normalize_workspace,
    resolve_declared_paths,
)
from .schema import OracleResult, failing, passing, skipping
from .workspace_contract import resolve_open_assertion_root

_ORACLE = "OutputTruthOracle"
_WHITESPACE_RUN_RE = re.compile(r"\s+")
_FINISHED_STATES = frozenset({"FINISHED", "VERIFIED"})


def _content_normalized(text: object) -> str:
    """Collapse whitespace runs and case for a CONTENT comparison."""
    return content_normalized(text, _WHITESPACE_RUN_RE)


def _verified_visible_text(
    verified_claim_results: list[dict[str, Any]],
    verified_observed_facts: list[dict[str, Any]],
) -> list[str]:
    """Return exact text values proven or observed by the adjudicated receipt."""
    claims = [
        expected
        for claim in verified_claim_results
        if isinstance(claim, dict)
        and claim.get("kind") == "visible_text"
        and claim.get("status") == "pass"
        and isinstance((expected := claim.get("expected")), str)
        and bool(expected)
    ]
    facts = [
        value
        for fact in verified_observed_facts
        if isinstance(fact, dict)
        and fact.get("kind") == "visible_text"
        and isinstance((value := fact.get("value")), str)
        and bool(value)
    ]
    return [*claims, *facts]


def _find_missing_preview_needles(
    preview_needles: list[Any],
    preview: dict[str, Any],
    verified_claim_results: list[dict[str, Any]] | None,
    verified_observed_facts: list[dict[str, Any]] | None,
) -> tuple[list[str], list[str], str]:
    """Find missing preview needles. Returns (missing, proven_text, evidence_basis)."""
    if verified_claim_results is not None:
        proven_text = _verified_visible_text(
            verified_claim_results,
            verified_observed_facts or [],
        )
        missing = [
            needle
            for needle in preview_needles
            if not any(
                _content_normalized(needle) in _content_normalized(value) for value in proven_text
            )
        ]
        return missing, proven_text, "current_governed_visible_text_claims"
    preview_content = str(preview.get("content", ""))
    missing = [
        needle
        for needle in preview_needles
        if _content_normalized(needle) not in _content_normalized(preview_content)
    ]
    return missing, [], "legacy_captured_preview_source"


def _check_preview_truth(
    preview_assert: dict[str, Any],
    preview: dict[str, Any] | None,
    verified_claim_results: list[dict[str, Any]] | None,
    verified_observed_facts: list[dict[str, Any]] | None,
) -> OracleResult | None:
    """Check preview health and required visible text."""
    if not preview_assert.get("required"):
        return None
    if not preview:
        return failing(
            _ORACLE,
            fc.FALSE_FINISH_PREVIEW_BROKEN,
            first_broken_link="finish -> preview_health",
            facts={"reason": "preview required but no preview evidence captured"},
        )
    health = preview.get("health") or {}
    status = health.get("status")
    if status is not None and int(status) >= 400:
        return failing(
            _ORACLE,
            fc.FALSE_FINISH_PREVIEW_BROKEN,
            first_broken_link="finish -> preview_health",
            facts={"preview_health_status": status},
        )
    preview_needles = preview_assert.get("must_contain") or []
    missing, proven_text, evidence_basis = _find_missing_preview_needles(
        preview_needles, preview, verified_claim_results, verified_observed_facts
    )
    if not missing:
        return None
    needle = missing[0]
    return failing(
        _ORACLE,
        fc.PREVIEW_TRUTH_MISMATCH,
        first_broken_link=(
            "finish -> preview_visible_text"
            if verified_claim_results is not None
            else "finish -> preview_content"
        ),
        facts={
            "missing_substring": needle,
            "evidence_basis": evidence_basis,
            "verified_visible_text": proven_text,
        },
    )


def _check_workspace_truth(
    workspace_assert: dict[str, Any],
    workspace_manifest: dict[str, Any] | None,
    verified_artifact_paths: list[str] | None,
) -> tuple[OracleResult | None, dict[str, str], str | None, dict[str, Any] | None]:
    """Check workspace file truth. Returns (error, files, assertion_root, root_error)."""
    files = normalize_workspace(workspace_manifest)
    exact_paths = workspace_assert.get("exact_paths")

    if isinstance(exact_paths, list):
        result = check_exact_paths(exact_paths, files)
        if result is not None:
            return result, files, None, None

    file_specs = workspace_assert.get("files") or []
    declared_paths, path_error = resolve_declared_paths(file_specs)
    if path_error is not None:
        return path_error, files, None, None

    assertion_root, root_error = resolve_open_assertion_root(
        present_paths=files,
        declared_paths=declared_paths,
        verified_artifact_paths=verified_artifact_paths,
    )
    if assertion_root is None and root_error and root_error.get("reason"):
        return (
            failing(
                _ORACLE,
                fc.WORKSPACE_SNAPSHOT_UNVERIFIED,
                first_broken_link="governed_artifact -> output_contract_root",
                facts=root_error,
            ),
            files,
            None,
            root_error,
        )

    if assertion_root is None:
        result = check_root_resolution_failure(declared_paths, files, root_error)
        if result is not None:
            return result, files, None, root_error

    existence_error = check_file_existence(
        file_specs, files, assertion_root, root_error, declared_paths
    )
    if existence_error is not None:
        return existence_error, files, assertion_root, root_error

    mismatches = collect_content_mismatches(
        file_specs, files, workspace_manifest, assertion_root, _WHITESPACE_RUN_RE
    )
    if mismatches:
        return fold_content_mismatches(mismatches), files, assertion_root, root_error

    elision_error = check_destructive_elision(
        file_specs, files, assertion_root, bool(workspace_assert.get("allow_elision"))
    )
    if elision_error is not None:
        return elision_error, files, assertion_root, root_error

    return None, files, assertion_root, root_error


class OutputTruthOracle:
    def check(
        self,
        events: list[dict[str, Any]],
        *,
        scenario: dict[str, Any] | None = None,
        workspace_manifest: dict[str, Any] | None = None,
        preview: dict[str, Any] | None = None,
        verified_claim_results: list[dict[str, Any]] | None = None,
        verified_observed_facts: list[dict[str, Any]] | None = None,
        verified_artifact_paths: list[str] | None = None,
    ) -> list[OracleResult]:
        assertions = (scenario or {}).get("assertions") or {}
        workspace_assert = assertions.get("workspace") or {}
        preview_assert = assertions.get("preview") or {}
        exact_paths = workspace_assert.get("exact_paths")
        wants_output = (
            bool(workspace_assert.get("files"))
            or exact_paths is not None
            or bool(preview_assert.get("required"))
        )

        if not wants_output:
            return [skipping(_ORACLE, reason="scenario asserts no output truth")]

        term = terminal_status(events)
        terminal_expected = (scenario or {}).get("assertions", {}).get("terminal_status_in")
        finished = term in _FINISHED_STATES or (
            bool(terminal_expected) and term in set(terminal_expected)
        )
        if not finished:
            return [
                failing(
                    _ORACLE,
                    fc.BUILD_DID_NOT_FINISH,
                    first_broken_link="required_finish -> terminal_status",
                    facts={
                        "terminal_status": term,
                        "terminal_status_in": terminal_expected,
                        "reason": "run required to finish + deliver output but never reached "
                        "a finished/required terminal",
                    },
                )
            ]

        ws_error, files, assertion_root, _root_error = _check_workspace_truth(
            workspace_assert, workspace_manifest, verified_artifact_paths
        )
        if ws_error is not None:
            return [ws_error]

        preview_error = _check_preview_truth(
            preview_assert, preview, verified_claim_results, verified_observed_facts
        )
        if preview_error is not None:
            return [preview_error]

        return [
            passing(
                _ORACLE,
                facts={
                    "terminal_status": term,
                    "file_count": len(files),
                    "assertion_root": assertion_root,
                },
            )
        ]
