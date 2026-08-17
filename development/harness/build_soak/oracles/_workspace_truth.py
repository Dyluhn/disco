"""Workspace truth helpers for OutputTruthOracle.

Extracted from ``output_truth.py`` to keep the oracle module within the
architecture budget. These helpers handle workspace manifest normalization,
content mismatch collection, exact-paths checking, and destructive-elision
scanning.
"""

from __future__ import annotations

from typing import Any

from .. import failure_codes as fc
from ._elision import scan_for_destructive_elision
from .schema import OracleResult, failing
from .workspace_contract import (
    is_platform_managed_path,
    join_workspace_relative,
    normalized_workspace_relative_path,
)

_ORACLE = "OutputTruthOracle"
_NON_AUTHORITATIVE_PROOF = frozenset({"unproven_extended_stability", "unknown"})


def content_normalized(text: object, whitespace_re: Any) -> str:
    """Collapse whitespace runs and case for a CONTENT comparison."""
    return whitespace_re.sub(" ", str(text)).strip().casefold()


def nested_files(manifest: dict[str, Any] | None) -> dict[str, Any]:
    if not manifest:
        return {}
    nested = manifest.get("files")
    return nested if isinstance(nested, dict) else manifest


def normalize_workspace(manifest: dict[str, Any] | None) -> dict[str, str]:
    out: dict[str, str] = {}
    for path, val in nested_files(manifest).items():
        if isinstance(val, dict):
            out[str(path)] = str(val.get("content", ""))
        else:
            out[str(path)] = str(val)
    return out


def proof_for(manifest: dict[str, Any] | None, path: str) -> str | None:
    files = nested_files(manifest)
    entry = files.get(path)
    if entry is None:
        entry = files.get(path.lstrip("/"))
    if isinstance(entry, dict):
        proof = entry.get("proof")
        return str(proof) if proof is not None else None
    return None


def content_stable_for(manifest: dict[str, Any] | None, path: str) -> bool:
    files = nested_files(manifest)
    entry = files.get(path)
    if entry is None:
        entry = files.get(path.lstrip("/"))
    return isinstance(entry, dict) and entry.get("content_stable") is True


def _authoritative_mismatch(mismatch: dict[str, Any]) -> bool:
    return (
        mismatch.get("proof") not in _NON_AUTHORITATIVE_PROOF
        or mismatch.get("content_stable") is True
    )


def fold_content_mismatches(mismatches: list[dict[str, Any]]) -> OracleResult:
    """DETERMINISTIC PRECEDENCE fold over ALL workspace-content mismatches."""
    proven = [m for m in mismatches if _authoritative_mismatch(m)]
    first = mismatches[0]
    facts: dict[str, Any] = {
        "mismatches": mismatches,
        "proven_mismatch_count": len(proven),
        "unverified_mismatch_count": len(mismatches) - len(proven),
        "path": first.get("path"),
    }
    if "missing_substring" in first:
        facts["missing_substring"] = first["missing_substring"]
    if proven:
        return failing(
            _ORACLE,
            fc.ARTIFACT_TRUTH_MISMATCH,
            first_broken_link="finish -> required_content_present",
            facts=facts,
        )
    return failing(
        _ORACLE,
        fc.WORKSPACE_SNAPSHOT_UNVERIFIED,
        first_broken_link="finish -> snapshot_content_verifiable",
        facts=facts,
    )


def check_exact_paths(
    exact_paths: list[str],
    files: dict[str, str],
) -> OracleResult | None:
    """Check the exact product-file-set truth assertion."""
    expected = sorted(str(path) for path in exact_paths)
    actual = sorted(path for path in files if not is_platform_managed_path(path))
    missing_paths = sorted(set(expected) - set(actual))
    unexpected_paths = sorted(set(actual) - set(expected))
    if not missing_paths and not unexpected_paths:
        return None
    facts = {
        "expected_paths": expected,
        "missing_paths": missing_paths,
        "unexpected_paths": unexpected_paths,
    }
    if missing_paths:
        return failing(
            _ORACLE,
            fc.FALSE_FINISH_NO_OUTPUT,
            first_broken_link="finish -> exact_workspace_shape",
            facts=facts,
        )
    return failing(
        _ORACLE,
        fc.ARTIFACT_TRUTH_MISMATCH,
        first_broken_link="finish -> exact_workspace_shape",
        facts=facts,
    )


def collect_content_mismatches(
    file_specs: list[dict[str, Any]],
    files: dict[str, str],
    workspace_manifest: dict[str, Any] | None,
    assertion_root: str | None,
    whitespace_re: Any,
) -> list[dict[str, Any]]:
    """Collect content mismatches across all asserted file specs."""
    mismatches: list[dict[str, Any]] = []
    for spec in file_specs:
        raw_path = spec.get("path")
        if raw_path is None:
            continue
        path = normalized_workspace_relative_path(raw_path)
        if path is None:
            continue
        resolved_path = (
            join_workspace_relative(assertion_root, path) if assertion_root is not None else None
        )
        if resolved_path is None or resolved_path not in files:
            continue
        content = files[resolved_path]
        proof = proof_for(workspace_manifest, resolved_path)
        stable = content_stable_for(workspace_manifest, resolved_path)
        for needle in spec.get("must_contain") or []:
            if content_normalized(needle, whitespace_re) not in content_normalized(
                content, whitespace_re
            ):
                mismatches.append(
                    {
                        "path": path,
                        "resolved_path": resolved_path,
                        "check": "must_contain",
                        "missing_substring": needle,
                        "proof": proof,
                        "content_stable": stable,
                    }
                )
        for needle in spec.get("must_not_contain") or []:
            if content_normalized(needle, whitespace_re) in content_normalized(
                content, whitespace_re
            ):
                mismatches.append(
                    {
                        "path": path,
                        "resolved_path": resolved_path,
                        "check": "must_not_contain",
                        "forbidden_substring": needle,
                        "proof": proof,
                        "content_stable": stable,
                    }
                )
        exact = spec.get("equals")
        if exact is not None and content != str(exact):
            mismatches.append(
                {
                    "path": path,
                    "resolved_path": resolved_path,
                    "check": "equals",
                    "proof": proof,
                    "content_stable": stable,
                }
            )
    return mismatches


def check_destructive_elision(
    file_specs: list[dict[str, Any]],
    files: dict[str, str],
    assertion_root: str | None,
    allow_elision: bool,
) -> OracleResult | None:
    """CXT-5: destructive-elision scan over final deliverables."""
    if allow_elision:
        return None
    for spec in file_specs:
        raw_path = spec.get("path")
        if raw_path is None:
            continue
        path = normalized_workspace_relative_path(raw_path)
        if path is None or assertion_root is None:
            continue
        resolved_path = join_workspace_relative(assertion_root, path)
        if resolved_path not in files:
            continue
        hits = scan_for_destructive_elision(files[resolved_path])
        if hits:
            return failing(
                _ORACLE,
                fc.DESTRUCTIVE_ELISION,
                first_broken_link="finish -> deliverable_recoverable",
                facts={
                    "path": path,
                    "resolved_path": resolved_path,
                    "offending_lines": hits[:5],
                },
            )
    return None


def resolve_declared_paths(
    file_specs: list[dict[str, Any]],
) -> tuple[list[str], OracleResult | None]:
    """Normalize declared paths. Returns (paths, error_result_or_none)."""
    declared_paths: list[str] = []
    for spec in file_specs:
        raw_path = spec.get("path")
        if raw_path is None:
            continue
        path = normalized_workspace_relative_path(raw_path)
        if path is None:
            return [], failing(
                _ORACLE,
                fc.WORKSPACE_SNAPSHOT_UNVERIFIED,
                first_broken_link="scenario_contract -> workspace_file_path",
                facts={
                    "reason": (
                        "declared output path is not a normalized POSIX workspace-relative path"
                    ),
                    "declared_path": raw_path,
                },
            )
        declared_paths.append(path)
    return declared_paths, None


def check_file_existence(
    file_specs: list[dict[str, Any]],
    files: dict[str, str],
    assertion_root: str | None,
    root_error: dict[str, Any] | None,
    declared_paths: list[str],
) -> OracleResult | None:
    """Check that every declared path exists under the resolved root."""
    for spec in file_specs:
        raw_path = spec.get("path")
        if raw_path is None:
            continue
        path = normalized_workspace_relative_path(raw_path)
        if path is None:
            continue
        resolved_path = (
            join_workspace_relative(assertion_root, path) if assertion_root is not None else None
        )
        if resolved_path is None or resolved_path not in files:
            facts: dict[str, Any] = {
                "required_path": path,
                "present_paths": sorted(files),
            }
            if root_error:
                facts.update(root_error)
            return failing(
                _ORACLE,
                fc.FALSE_FINISH_NO_OUTPUT,
                first_broken_link="finish -> required_file_exists",
                facts=facts,
            )
    return None


def check_root_resolution_failure(
    declared_paths: list[str],
    files: dict[str, str],
    root_error: dict[str, Any] | None,
) -> OracleResult | None:
    """Handle the case where no candidate root satisfies every declared path."""
    absent = [
        path
        for path in declared_paths
        if not any(
            join_workspace_relative(root, path) in files
            for root in (*(root_error or {}).get("verified_candidate_roots", ()), ".")
        )
    ]
    facts: dict[str, Any] = {
        "reason": "no candidate root satisfies every declared output path",
        "missing_declared_paths": absent,
        "present_declared_paths": [p for p in declared_paths if p not in absent],
        "declared_paths": list(declared_paths),
        "present_paths": sorted(files),
    }
    if root_error:
        facts.update({k: v for k, v in root_error.items() if k not in facts})
    if absent:
        facts["required_path"] = absent[0]
    return failing(
        _ORACLE,
        fc.FALSE_FINISH_NO_OUTPUT if absent else fc.WORKSPACE_SNAPSHOT_UNVERIFIED,
        first_broken_link=(
            "finish -> required_file_exists"
            if absent
            else "governed_artifact -> output_contract_root"
        ),
        facts=facts,
    )
