"""ContractOracle (guidelines §10 / §16 — the scenario-contract layer).

Runs after harness validity. It validates that the scenario's DECLARED
requirements are well-formed and that the evidence needed to adjudicate them is
present — i.e. that the run can be judged against the contract the scenario
states. It does NOT yet judge whether the product satisfied the contract (that is
the event-chain / revision / output-truth oracles); it guards against silently
"passing" a run whose contract we could never actually check.

Examples of an unsatisfiable contract → INVALID_RUN (SCENARIO_CONTRACT_UNSATISFIABLE):
  * the scenario asserts workspace file truth but no workspace manifest was captured;
  * the scenario asserts preview truth but no preview health/screenshot was captured;
  * the scenario declares followups requiring plan revision but no expected final
    revision (the revision oracle would have nothing to compare against).

The scenario is a plain machine-readable dict (guidelines §14) — never interpreted
by an LLM.
"""

from __future__ import annotations

from typing import Any

from .. import failure_codes as fc
from .schema import OracleResult, failing, passing, skipping
from .workspace_contract import validate_exact_paths

_ORACLE = "ContractOracle"


def _fail_unsatisfiable(link: str, reason: str, **extra: Any) -> list[OracleResult]:
    return [
        failing(
            _ORACLE,
            fc.SCENARIO_CONTRACT_UNSATISFIABLE,
            first_broken_link=link,
            facts={"reason": reason, **extra},
        )
    ]


def _validate_receipt_kinds(governed: dict[str, Any]) -> bool:
    """Validate required_receipt_kinds is a non-empty unique list of strings."""
    kinds = governed.get("required_receipt_kinds")
    return (
        isinstance(kinds, list)
        and bool(kinds)
        and all(isinstance(kind, str) and bool(kind) for kind in kinds)
        and len(set(kinds)) == len(kinds)
    )


def _validate_claim_kinds(governed: dict[str, Any]) -> bool:
    """Validate required_claim_kinds is a non-empty dict of positive int counts."""
    kinds = governed.get("required_claim_kinds")
    return (
        isinstance(kinds, dict)
        and bool(kinds)
        and all(
            isinstance(kind, str) and bool(kind) and type(count) is int and count > 0
            for kind, count in kinds.items()
        )
    )


def _validate_execution_modalities(governed: dict[str, Any]) -> bool:
    """Validate exactly one of required_execution_modality/modalities is declared."""
    if isinstance(governed.get("required_execution_modality"), str):
        return (
            bool(governed["required_execution_modality"])
            and "required_execution_modalities" not in governed
        )
    if "required_execution_modality" not in governed:
        modalities = governed.get("required_execution_modalities")
        return (
            isinstance(modalities, list)
            and bool(modalities)
            and all(isinstance(m, str) and bool(m) for m in modalities)
            and len(set(modalities)) == len(modalities)
        )
    return False


def _validate_governed_verification(governed: Any) -> list[OracleResult] | None:
    """Validate the governed_verification assertion. Returns failures or None."""
    if governed is None:
        return None
    allowed_governed = {
        "required",
        "route",
        "composition_authority",
        "delivery_mode",
        "required_receipt_kinds",
        "required_claim_kinds",
        "required_execution_modality",
        "required_execution_modalities",
        "require_agent_view_binding",
        "require_workspace_epoch",
    }
    if not isinstance(governed, dict) or not set(governed) <= allowed_governed:
        return _fail_unsatisfiable(
            "scenario_contract -> governed_verification",
            "governed_verification must declare an exact typed "
            "target/receipt/claim/execution policy",
        )
    valid = (
        governed.get("required") is True
        and isinstance(governed.get("route"), str)
        and bool(governed["route"])
        and isinstance(governed.get("composition_authority"), str)
        and bool(governed["composition_authority"])
        and governed.get("delivery_mode") in {"interactive", "artifact"}
        and _validate_receipt_kinds(governed)
        and _validate_claim_kinds(governed)
        and _validate_execution_modalities(governed)
        and type(governed.get("require_agent_view_binding", True)) is bool
        and type(governed.get("require_workspace_epoch", False)) is bool
    )
    if not valid:
        return _fail_unsatisfiable(
            "scenario_contract -> governed_verification",
            "governed_verification must declare an exact typed "
            "target/receipt/claim/execution policy",
        )
    return None


def _validate_lifecycle(lifecycle: Any) -> list[OracleResult] | None:
    """Validate the lifecycle assertion. Returns failures or None."""
    if not lifecycle:
        return None
    allowed_lifecycle = {
        "pause_resume_at",
        "restart_after_terminal",
        "restore_version",
        "export_download",
    }
    if not isinstance(lifecycle, dict) or not set(lifecycle).issubset(allowed_lifecycle):
        return _fail_unsatisfiable(
            "scenario_contract -> lifecycle",
            "lifecycle contains an unsupported action",
        )
    if lifecycle.get("pause_resume_at") not in (None, "after_first_file_write"):
        return _fail_unsatisfiable(
            "scenario_contract -> lifecycle.pause_resume_at",
            "only after_first_file_write is supported",
        )
    if lifecycle.get("restore_version") not in (None, "oldest", "previous"):
        return _fail_unsatisfiable(
            "scenario_contract -> lifecycle.restore_version",
            "restore_version must be oldest or previous",
        )
    for field in ("restart_after_terminal", "export_download"):
        if field in lifecycle and type(lifecycle[field]) is not bool:
            return _fail_unsatisfiable(
                f"scenario_contract -> lifecycle.{field}",
                f"{field} must be boolean",
            )
    return None


def _validate_fixture_files(fixture_files: Any) -> bool:
    """Validate the files dict inside an import_fixture."""
    if not isinstance(fixture_files, dict) or not fixture_files:
        return False
    for path, contents in fixture_files.items():
        generated = (
            isinstance(contents, dict)
            and set(contents) == {"line_template", "count"}
            and isinstance(contents.get("line_template"), str)
            and type(contents.get("count")) is int
            and 1 <= int(contents["count"]) <= 100_000
        )
        if not isinstance(path, str) or not path or not (isinstance(contents, str) or generated):
            return False
    return True


def _validate_import_fixture(import_fixture: Any) -> list[OracleResult] | None:
    """Validate the import_fixture assertion. Returns failures or None."""
    if import_fixture is None:
        return None
    fixture_files = import_fixture.get("files") if isinstance(import_fixture, dict) else None
    valid_fixture_files = _validate_fixture_files(fixture_files)
    if (
        not isinstance(import_fixture, dict)
        or set(import_fixture) != {"filename", "files"}
        or not isinstance(import_fixture.get("filename"), str)
        or not str(import_fixture.get("filename")).endswith(".zip")
        or not valid_fixture_files
    ):
        return _fail_unsatisfiable(
            "scenario_contract -> import_fixture",
            "import_fixture must declare filename and text or bounded line_template/count files",
        )
    return None


def _validate_workspace_exact_paths(workspace: dict[str, Any]) -> list[OracleResult] | None:
    """Validate workspace.exact_paths coherence. Returns failures or None."""
    if "exact_paths" not in workspace:
        return None
    exact_paths, exact_error = validate_exact_paths(workspace.get("exact_paths"))
    if exact_error is not None:
        return _fail_unsatisfiable("scenario_contract -> workspace.exact_paths", exact_error)
    declared_specs = workspace.get("files") or []
    if not isinstance(declared_specs, list) or not all(
        isinstance(spec, dict) and isinstance(spec.get("path"), str) and bool(spec["path"])
        for spec in declared_specs
    ):
        return _fail_unsatisfiable(
            "scenario_contract -> workspace.files",
            "workspace.files must be a list of objects with a non-empty string "
            "path when exact_paths is declared",
        )
    declared_paths = [spec["path"] for spec in declared_specs]
    exact_set = set(exact_paths or [])
    outside = sorted(path for path in declared_paths if path not in exact_set)
    if outside:
        return _fail_unsatisfiable(
            "scenario_contract -> workspace.file_set_coherence",
            "every workspace.files[].path must belong to workspace.exact_paths",
            outside_exact_paths=outside,
        )
    return None


def _validate_tool_scope_assertion(assertions: dict[str, Any]) -> list[OracleResult] | None:
    """Validate the tool_scope assertion. Returns failures or None."""
    tool_scope_assert = assertions.get("tool_scope") or {}
    if not tool_scope_assert:
        return None
    planning_disallows = tool_scope_assert.get("planning_disallows")
    execution_disallows = tool_scope_assert.get("execution_disallows")

    def _valid_names(value: Any) -> bool:
        return (
            isinstance(value, list)
            and bool(value)
            and all(isinstance(name, str) and name for name in value)
            and len(set(value)) == len(value)
        )

    if planning_disallows is None and execution_disallows is None:
        return _fail_unsatisfiable(
            "scenario_contract -> tool_scope",
            "assertions.tool_scope must declare planning_disallows and/or execution_disallows",
        )
    for field, value in (
        ("planning_disallows", planning_disallows),
        ("execution_disallows", execution_disallows),
    ):
        if value is not None and not _valid_names(value):
            return _fail_unsatisfiable(
                f"scenario_contract -> tool_scope.{field}",
                f"assertions.tool_scope.{field} must be a non-empty unique list of tool names",
            )
    return None


def _validate_browser_verification(assertions: dict[str, Any]) -> list[OracleResult] | None:
    """Validate the browser_verification assertion. Returns failures or None."""
    browser_verification = assertions.get("browser_verification")
    if browser_verification is None:
        return None
    if (
        not isinstance(browser_verification, dict)
        or set(browser_verification) != {"required"}
        or (browser_verification.get("required") is not True)
    ):
        return _fail_unsatisfiable(
            "scenario_contract -> browser_verification.required",
            "assertions.browser_verification must contain only required: true",
        )
    return None


def _validate_context_pressure(assertions: dict[str, Any]) -> list[OracleResult] | None:
    """Validate the context_pressure assertion. Returns failures or None."""
    context_pressure = assertions.get("context_pressure")
    if context_pressure is None:
        return None
    if (
        not isinstance(context_pressure, dict)
        or not isinstance(context_pressure.get("path"), str)
        or not context_pressure.get("path")
        or type(context_pressure.get("min_distinct_offsets", 2)) is not int
        or int(context_pressure.get("min_distinct_offsets", 2)) < 2
        or type(context_pressure.get("max_reads_per_offset", 2)) is not int
        or int(context_pressure.get("max_reads_per_offset", 2)) < 1
        or type(context_pressure.get("require_compaction")) is not bool
    ):
        return _fail_unsatisfiable(
            "scenario_contract -> context_pressure",
            "context_pressure requires path, min_distinct_offsets >= 2, "
            "max_reads_per_offset >= 1, and boolean require_compaction",
        )
    return None


def _validate_revision_contract(scenario: dict[str, Any]) -> list[OracleResult] | None:
    """Validate that followups requiring revision have an expected final revision."""
    followups = scenario.get("followups") or []
    revisions = (scenario.get("assertions") or {}).get("revisions") or {}
    requires_rev = any(f.get("requires_plan_revision") for f in followups)
    if requires_rev and "expected_final_plan_revision" not in revisions:
        return _fail_unsatisfiable(
            "scenario_contract -> revisions.expected_final_plan_revision",
            "followups require_plan_revision but no "
            "assertions.revisions.expected_final_plan_revision declared",
        )
    return None


def _compute_missing_evidence(
    scenario: dict[str, Any],
    assertions: dict[str, Any],
    present: set[str],
) -> list[str]:
    """Compute the list of missing required evidence categories."""
    missing: list[str] = []
    if (scenario.get("lifecycle") or scenario.get("import_fixture") is not None) and (
        "product_evidence" not in present
    ):
        missing.append("product_evidence (scenario drives lifecycle/import actions)")

    workspace = assertions.get("workspace") or {}
    if (workspace.get("files") or "exact_paths" in workspace) and "workspace" not in present:
        missing.append("workspace_manifest (scenario asserts workspace truth)")

    preview = assertions.get("preview") or {}
    if preview.get("required") and "preview" not in present:
        missing.append("preview_evidence (scenario asserts preview.required)")

    if assertions.get("tool_scope") and "tool_scope" not in present:
        missing.append("tool_scope_capture (scenario asserts assertions.tool_scope)")
    return missing


def _run_contract_validations(
    scenario: dict[str, Any],
    assertions: dict[str, Any],
) -> list[OracleResult] | None:
    """Run all assertion validations. Returns failures or None if all pass."""
    result = _validate_governed_verification(assertions.get("governed_verification"))
    if result is not None:
        return result
    result = _validate_lifecycle(scenario.get("lifecycle") or {})
    if result is not None:
        return result
    result = _validate_import_fixture(scenario.get("import_fixture"))
    if result is not None:
        return result
    result = _validate_workspace_exact_paths(assertions.get("workspace") or {})
    if result is not None:
        return result
    for validator in (
        _validate_tool_scope_assertion,
        _validate_browser_verification,
        _validate_context_pressure,
    ):
        result = validator(assertions)
        if result is not None:
            return result
    return _validate_revision_contract(scenario)


class ContractOracle:
    def check(
        self,
        scenario: dict[str, Any] | None,
        *,
        available_evidence: set[str] | None = None,
    ) -> list[OracleResult]:
        """`available_evidence` names the evidence categories the run actually
        captured (e.g. {"events", "workspace", "preview", "tool_scope"}). When
        None, only "events" is assumed present (the deterministic, no-live-spend
        slice always has the event log)."""
        if not scenario:
            return [skipping(_ORACLE, reason="no scenario contract supplied")]
        present = available_evidence if available_evidence is not None else {"events"}
        assertions = scenario.get("assertions") or {}

        result = _run_contract_validations(scenario, assertions)
        if result is not None:
            return result

        missing = _compute_missing_evidence(scenario, assertions, present)
        if missing:
            return _fail_unsatisfiable(
                "scenario_contract -> required_evidence",
                "missing required evidence",
                missing_evidence=missing,
            )

        followups = scenario.get("followups") or []
        return [
            passing(
                _ORACLE,
                facts={
                    "scenario_id": scenario.get("id"),
                    "followup_count": len(followups),
                    "evidence_present": sorted(present),
                },
            )
        ]
