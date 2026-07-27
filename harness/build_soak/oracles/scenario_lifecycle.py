"""Causal coverage oracles for governed lifecycle and context-pressure scenarios."""

from __future__ import annotations

from typing import Any

from disco.core.view import summary_rejection_reason
from disco.core.workspace_paths import strip_redundant_workspace_prefix

from .. import failure_codes as fc
from .schema import OracleResult, failing, passing, skipping

_LIFECYCLE = "ScenarioLifecycleOracle"
_CONTEXT = "ContextPressureOracle"


def _same_workspace_file(observed: Any, declared: str) -> bool:
    """Compare which FILE was read, not how the agent happened to spell it.

    `/workspace/catalog.txt` and `catalog.txt` are the same file by the product's
    own documented contract (`strip_redundant_workspace_prefix`), and the agent is
    free to use either. Comparing the raw strings made this oracle measure a
    spelling instead of the property it exists to check.

    Context seed 460000 read catalog.txt at offsets 1, 9996 and 19996 — three
    distinct offsets, exactly what `min_distinct_offsets: 3` asks for — and every
    read was discarded because the agent wrote `/workspace/catalog.txt`. The oracle
    then reported `read_count: 0` and failed the run for not paging a file it had
    paged correctly. Using the product's own helper keeps the two from drifting.
    """

    if not isinstance(observed, str) or not observed:
        return False
    return strip_redundant_workspace_prefix(observed) == strip_redundant_workspace_prefix(declared)


def _status(event: dict[str, Any]) -> str:
    value = event.get("status")
    if isinstance(value, dict):
        value = value.get("value")
    return str(value or "")


def _slice(product_evidence: dict[str, Any] | None, key: str) -> dict[str, Any] | None:
    if not isinstance(product_evidence, dict):
        return None
    value = product_evidence.get(key)
    return value if isinstance(value, dict) else None


class ScenarioLifecycleOracle:
    """Require each scenario-directed lifecycle action to be causally observed."""

    def check(
        self,
        events: list[dict[str, Any]],
        *,
        scenario: dict[str, Any] | None,
        product_evidence: dict[str, Any] | None,
    ) -> list[OracleResult]:
        scenario = scenario or {}
        lifecycle = scenario.get("lifecycle") or {}
        imported = isinstance(scenario.get("import_fixture"), dict)
        if not lifecycle and not imported:
            return [skipping(_LIFECYCLE, reason="scenario declares no lifecycle action")]

        facts: dict[str, Any] = {}
        failed: list[str] = []
        statuses = [(int(event.get("seq", -1)), _status(event)) for event in events]

        if imported:
            evidence = _slice(product_evidence, "import")
            ok = evidence is not None and evidence.get("accepted") is True
            facts["import"] = evidence
            if not ok:
                failed.append("import")

        if lifecycle.get("pause_resume_at"):
            evidence = _slice(product_evidence, "pause_resume")
            paused = [seq for seq, status in statuses if status == "PAUSED"]
            resumed = any(
                status == "RUNNING" and paused and seq > paused[0] for seq, status in statuses
            )
            ok = evidence is not None and evidence.get("ok") is True and bool(paused) and resumed
            facts["pause_resume"] = {
                "evidence": evidence,
                "paused_seqs": paused,
                "resumed_after_pause": resumed,
            }
            if not ok:
                failed.append("pause_resume")

        if lifecycle.get("restart_after_terminal"):
            evidence = _slice(product_evidence, "restart")
            ok = (
                evidence is not None
                and evidence.get("ok") is True
                and evidence.get("before_status") == evidence.get("after_status")
                and isinstance(evidence.get("before_digest"), str)
                and evidence.get("before_digest") == evidence.get("after_digest")
            )
            facts["restart"] = evidence
            if not ok:
                failed.append("restart")

        if lifecycle.get("restore_version"):
            evidence = _slice(product_evidence, "rollback")
            durable = any(event.get("kind") == "workspace_restored" for event in events)
            ok = evidence is not None and evidence.get("ok") is True and durable
            facts["rollback"] = {"evidence": evidence, "durable_event": durable}
            if not ok:
                failed.append("rollback")

        if failed:
            # A lifecycle action the HARNESS could not attempt is not a product
            # sequence defect. `_restart_isolated_stack` reports exactly this
            # when the disposable stack's private control is unbound, and the
            # 2026-07-27 counted failure recorded it as the build platform
            # failing a restart lifecycle while the build itself FINISHED twice.
            # `run.py` now refuses to start such a lane; this keeps the dossier
            # truthful if one is ever discovered mid-run.
            unavailable = sorted(
                action
                for action in failed
                if str((facts.get(action) or {}).get("reason") or "").endswith(
                    "control_unavailable"
                )
            )
            if unavailable and len(unavailable) == len(failed):
                return [
                    failing(
                        _LIFECYCLE,
                        fc.LIFECYCLE_CONTROL_UNAVAILABLE,
                        first_broken_link="harness_control -> lifecycle_action",
                        facts={
                            "failed_actions": failed,
                            "unattempted_actions": unavailable,
                            **facts,
                        },
                    )
                ]
            return [
                failing(
                    _LIFECYCLE,
                    fc.LIFECYCLE_SEQUENCE_INVALID,
                    first_broken_link="scenario_action -> durable_lifecycle_evidence",
                    facts={"failed_actions": failed, **facts},
                )
            ]
        return [passing(_LIFECYCLE, facts=facts)]


class ContextPressureOracle:
    """Prove paged reads, distinct ranges, compaction, and bounded reread behavior."""

    def check(
        self,
        events: list[dict[str, Any]],
        *,
        scenario: dict[str, Any] | None,
    ) -> list[OracleResult]:
        config = ((scenario or {}).get("assertions") or {}).get("context_pressure")
        if not isinstance(config, dict):
            return [skipping(_CONTEXT, reason="no context-pressure assertion")]
        path = str(config.get("path") or "")
        min_offsets = int(config.get("min_distinct_offsets", 2))
        max_reads_per_offset = int(config.get("max_reads_per_offset", 2))
        require_compaction = config.get("require_compaction") is True

        reads: list[tuple[int, str]] = []
        range_hints = 0
        action_calls: dict[str, tuple[int, str]] = {}
        for event in events:
            if event.get("kind") != "action":
                continue
            call = event.get("tool_call") or {}
            if call.get("tool_name") != "file_read":
                continue
            args = call.get("arguments") or {}
            if not _same_workspace_file(args.get("path"), path):
                continue
            raw_offset = args.get("offset")
            offset = "0" if raw_offset is None else str(raw_offset)
            seq = int(event.get("seq", -1))
            reads.append((seq, offset))
            call_id = call.get("call_id")
            if isinstance(call_id, str) and call_id:
                action_calls[call_id] = (seq, offset)

        for event in events:
            if event.get("kind") != "observation":
                continue
            result = event.get("tool_result") or {}
            call_id = result.get("call_id")
            if call_id not in action_calls:
                continue
            content = str(result.get("content") or "")
            if "read more with offset=" in content or "HEAD-ONLY under context pressure" in content:
                range_hints += 1

        offsets = sorted({offset for _seq, offset in reads})
        offset_counts = {
            offset: sum(1 for _seq, seen in reads if seen == offset) for offset in offsets
        }
        compactions = sum(1 for event in events if event.get("kind") == "condensation")
        # Counting condensations proves the mechanism RAN; it says nothing about
        # what it stored. Seed 460000 persisted 22 summaries that were raw
        # tool-call protocol residue -- replayed to the model as conversation --
        # while this oracle happily reported `compaction_count: 32`. Adjudicate
        # with the PRODUCT's own predicate so the harness cannot keep a private,
        # staler idea of what protocol markup looks like and agree with the bug.
        unusable = [
            {"seq": event.get("seq"), "reason": reason}
            for event in events
            if event.get("kind") == "condensation"
            and isinstance(event.get("summary"), str)
            and (reason := summary_rejection_reason(event["summary"])) is not None
        ]
        if unusable:
            return [
                failing(
                    _CONTEXT,
                    fc.CONDENSATION_SUMMARY_UNUSABLE,
                    first_broken_link="condensation -> persisted_summary",
                    facts={
                        "path": path,
                        "compaction_count": compactions,
                        "unusable_summary_count": len(unusable),
                        "unusable_summaries": unusable[:10],
                    },
                )
            ]
        ok = (
            len(offsets) >= min_offsets
            and range_hints >= 1
            and all(count <= max_reads_per_offset for count in offset_counts.values())
            and (not require_compaction or compactions >= 1)
        )
        facts = {
            "path": path,
            "read_count": len(reads),
            "distinct_offsets": offsets,
            "offset_counts": offset_counts,
            "max_reads_per_offset": max_reads_per_offset,
            "range_hint_count": range_hints,
            "compaction_count": compactions,
            "required_compaction": require_compaction,
        }
        if not ok:
            return [
                failing(
                    _CONTEXT,
                    fc.CONTEXT_PRESSURE_NOT_OBSERVED,
                    first_broken_link="large_read -> range_receipts_and_compaction",
                    facts=facts,
                )
            ]
        return [passing(_CONTEXT, facts=facts)]
