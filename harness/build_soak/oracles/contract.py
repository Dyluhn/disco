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

        missing: list[str] = []

        lifecycle = scenario.get("lifecycle") or {}
        if lifecycle:
            allowed_lifecycle = {
                "pause_resume_at",
                "restart_after_terminal",
                "restore_version",
                "export_download",
            }
            if not isinstance(lifecycle, dict) or not set(lifecycle).issubset(allowed_lifecycle):
                return [
                    failing(
                        _ORACLE,
                        fc.SCENARIO_CONTRACT_UNSATISFIABLE,
                        first_broken_link="scenario_contract -> lifecycle",
                        facts={"reason": "lifecycle contains an unsupported action"},
                    )
                ]
            if lifecycle.get("pause_resume_at") not in (None, "after_first_file_write"):
                return [
                    failing(
                        _ORACLE,
                        fc.SCENARIO_CONTRACT_UNSATISFIABLE,
                        first_broken_link="scenario_contract -> lifecycle.pause_resume_at",
                        facts={"reason": "only after_first_file_write is supported"},
                    )
                ]
            if lifecycle.get("restore_version") not in (None, "oldest", "previous"):
                return [
                    failing(
                        _ORACLE,
                        fc.SCENARIO_CONTRACT_UNSATISFIABLE,
                        first_broken_link="scenario_contract -> lifecycle.restore_version",
                        facts={"reason": "restore_version must be oldest or previous"},
                    )
                ]
            for field in ("restart_after_terminal", "export_download"):
                if field in lifecycle and type(lifecycle[field]) is not bool:
                    return [
                        failing(
                            _ORACLE,
                            fc.SCENARIO_CONTRACT_UNSATISFIABLE,
                            first_broken_link=f"scenario_contract -> lifecycle.{field}",
                            facts={"reason": f"{field} must be boolean"},
                        )
                    ]
        import_fixture = scenario.get("import_fixture")
        if import_fixture is not None:
            fixture_files = (
                import_fixture.get("files") if isinstance(import_fixture, dict) else None
            )
            valid_fixture_files = isinstance(fixture_files, dict) and bool(fixture_files)
            if valid_fixture_files:
                for path, contents in fixture_files.items():
                    generated = (
                        isinstance(contents, dict)
                        and set(contents) == {"line_template", "count"}
                        and isinstance(contents.get("line_template"), str)
                        and type(contents.get("count")) is int
                        and 1 <= int(contents["count"]) <= 100_000
                    )
                    if not isinstance(path, str) or not path or not (
                        isinstance(contents, str) or generated
                    ):
                        valid_fixture_files = False
                        break
            if (
                not isinstance(import_fixture, dict)
                or set(import_fixture) != {"filename", "files"}
                or not isinstance(import_fixture.get("filename"), str)
                or not str(import_fixture.get("filename")).endswith(".zip")
                or not valid_fixture_files
            ):
                return [
                    failing(
                        _ORACLE,
                        fc.SCENARIO_CONTRACT_UNSATISFIABLE,
                        first_broken_link="scenario_contract -> import_fixture",
                        facts={
                            "reason": (
                                "import_fixture must declare filename and text or bounded "
                                "line_template/count files"
                            )
                        },
                    )
                ]
        if (lifecycle or import_fixture is not None) and "product_evidence" not in present:
            missing.append("product_evidence (scenario drives lifecycle/import actions)")

        # workspace truth requires a workspace manifest. ``exact_paths`` is an
        # opt-in closed product-file set; ordinary ``files`` assertions retain
        # their open-set semantics.
        workspace = assertions.get("workspace") or {}
        exact_paths: list[str] | None = None
        if "exact_paths" in workspace:
            exact_paths, exact_error = validate_exact_paths(workspace.get("exact_paths"))
            if exact_error is not None:
                return [
                    failing(
                        _ORACLE,
                        fc.SCENARIO_CONTRACT_UNSATISFIABLE,
                        first_broken_link="scenario_contract -> workspace.exact_paths",
                        facts={"reason": exact_error},
                    )
                ]
            declared_specs = workspace.get("files") or []
            if not isinstance(declared_specs, list) or not all(
                isinstance(spec, dict) and isinstance(spec.get("path"), str) and bool(spec["path"])
                for spec in declared_specs
            ):
                return [
                    failing(
                        _ORACLE,
                        fc.SCENARIO_CONTRACT_UNSATISFIABLE,
                        first_broken_link="scenario_contract -> workspace.files",
                        facts={
                            "reason": (
                                "workspace.files must be a list of objects with a "
                                "non-empty string path when exact_paths is declared"
                            )
                        },
                    )
                ]
            declared_paths = [spec["path"] for spec in declared_specs]
            exact_set = set(exact_paths or [])
            outside = sorted(path for path in declared_paths if path not in exact_set)
            if outside:
                return [
                    failing(
                        _ORACLE,
                        fc.SCENARIO_CONTRACT_UNSATISFIABLE,
                        first_broken_link="scenario_contract -> workspace.file_set_coherence",
                        facts={
                            "reason": (
                                "every workspace.files[].path must belong to workspace.exact_paths"
                            ),
                            "outside_exact_paths": outside,
                        },
                    )
                ]
        if (workspace.get("files") or exact_paths is not None) and "workspace" not in present:
            missing.append("workspace_manifest (scenario asserts workspace truth)")

        # preview truth requires preview health/screenshot evidence.
        preview = assertions.get("preview") or {}
        if preview.get("required") and "preview" not in present:
            missing.append("preview_evidence (scenario asserts preview.required)")

        # tool-scope truth (§11.7 WRITE_TOOL_ALLOWED_IN_PLANNING — a disallowed tool
        # must not be CALLABLE, not merely un-advertised) needs the runner-captured
        # per-turn tool scope. A scenario that asserts tool_scope therefore REQUIRES
        # that evidence; if it is missing the run cannot be adjudicated against the
        # contract and must FAIL-CLOSED to INVALID_RUN — never silently PASS via a
        # SKIP. (The event-only WRITE_TOOL_ATTEMPTED_IN_PLANNING check is separate
        # and still runs.)
        tool_scope_assert = assertions.get("tool_scope") or {}
        if tool_scope_assert:
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
                return [
                    failing(
                        _ORACLE,
                        fc.SCENARIO_CONTRACT_UNSATISFIABLE,
                        first_broken_link="scenario_contract -> tool_scope",
                        facts={
                            "reason": (
                                "assertions.tool_scope must declare planning_disallows "
                                "and/or execution_disallows"
                            )
                        },
                    )
                ]
            for field, value in (
                ("planning_disallows", planning_disallows),
                ("execution_disallows", execution_disallows),
            ):
                if value is not None and not _valid_names(value):
                    return [
                        failing(
                            _ORACLE,
                            fc.SCENARIO_CONTRACT_UNSATISFIABLE,
                            first_broken_link=f"scenario_contract -> tool_scope.{field}",
                            facts={
                                "reason": (
                                    f"assertions.tool_scope.{field} must be a non-empty "
                                    "unique list of tool names"
                                )
                            },
                        )
                    ]
        if tool_scope_assert and "tool_scope" not in present:
            missing.append("tool_scope_capture (scenario asserts assertions.tool_scope)")

        # H191: validate the opt-in shape here.  Whether the product actually ran
        # a passing verifier is adjudicated later as a PRODUCT result; only missing
        # bytes for an observation that did run are a harness-validity INVALID.
        browser_verification = assertions.get("browser_verification")
        if browser_verification is not None:
            if (
                not isinstance(browser_verification, dict)
                or set(browser_verification) != {"required"}
                or (browser_verification.get("required") is not True)
            ):
                return [
                    failing(
                        _ORACLE,
                        fc.SCENARIO_CONTRACT_UNSATISFIABLE,
                        first_broken_link="scenario_contract -> browser_verification.required",
                        facts={
                            "reason": (
                                "assertions.browser_verification must contain only required: true"
                            )
                        },
                    )
                ]
        context_pressure = assertions.get("context_pressure")
        if context_pressure is not None:
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
                return [
                    failing(
                        _ORACLE,
                        fc.SCENARIO_CONTRACT_UNSATISFIABLE,
                        first_broken_link="scenario_contract -> context_pressure",
                        facts={
                            "reason": (
                                "context_pressure requires path, min_distinct_offsets >= 2, "
                                "max_reads_per_offset >= 1, and boolean require_compaction"
                            )
                        },
                    )
                ]
        if missing:
            return [
                failing(
                    _ORACLE,
                    fc.SCENARIO_CONTRACT_UNSATISFIABLE,
                    first_broken_link="scenario_contract -> required_evidence",
                    facts={"missing_evidence": missing},
                )
            ]

        # Revision contract well-formedness: followups requiring a revision need an
        # expected final revision to compare against.
        followups = scenario.get("followups") or []
        revisions = assertions.get("revisions") or {}
        requires_rev = any(f.get("requires_plan_revision") for f in followups)
        if requires_rev and "expected_final_plan_revision" not in revisions:
            return [
                failing(
                    _ORACLE,
                    fc.SCENARIO_CONTRACT_UNSATISFIABLE,
                    first_broken_link="scenario_contract -> revisions.expected_final_plan_revision",
                    facts={
                        "reason": (
                            "followups require_plan_revision but no "
                            "assertions.revisions.expected_final_plan_revision declared"
                        )
                    },
                )
            ]

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
