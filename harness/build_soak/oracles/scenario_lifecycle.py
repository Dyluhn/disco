"""Causal coverage oracles for governed lifecycle and context-pressure scenarios."""

from __future__ import annotations

from typing import Any

from .. import failure_codes as fc
from .schema import OracleResult, failing, passing, skipping

_LIFECYCLE = "ScenarioLifecycleOracle"
_CONTEXT = "ContextPressureOracle"


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
            if str(args.get("path") or "") != path:
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
