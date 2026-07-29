"""Bounded Build Soak records owner."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .. import failure_codes as fc
from ..adapters.disco_api import InfraProbeError
from ..classify import CLASSIFICATION_NAME
from ..ports import ProductClient


def _infra_failure_record(
    out_root: str | Path,
    run_id: str,
    scenario: dict[str, Any],
    exc: InfraProbeError,
) -> dict[str, Any]:
    """Write a §9-compliant INFRA_FAILURE record (the run never created a
    conversation). This is the ONLY non-product outcome the runner emits."""
    base = Path(out_root) / run_id
    base.mkdir(parents=True, exist_ok=True)
    record = {
        "status": fc.INFRA_FAILURE,
        "severity": fc.NONE,
        "code": exc.signature_id,
        "first_broken_link": "pre_create_probe -> agent_server/provider",
        "scenario_id": scenario.get("id"),
        "run_id": run_id,
        "conversation_id": None,
        "facts": exc.detail,
        "accepted_by": "oracle",
        "agent_comments_ignored_for_adjudication": True,
        "stage": "before_conversation_creation",
    }
    (base / CLASSIFICATION_NAME).write_text(
        json.dumps(record, indent=2, sort_keys=True), encoding="utf-8"
    )
    (base / "timeline.md").write_text(
        f"# INFRA_FAILURE (pre-create)\n\nsignature: {exc.signature_id}\ndetail: {exc.detail}\n",
        encoding="utf-8",
    )
    return record


def _invalid_run_record(
    out_root: str | Path,
    run_id: str,
    scenario: dict[str, Any],
    reason: str,
    *,
    code: str = "RUN_INTERRUPTED",
    first_broken_link: str = "drive -> evidence_collection",
    facts: dict[str, Any] | None = None,
    conversation_id: str | None = None,
    timeline_markdown: str | None = None,
) -> dict[str, Any]:
    """Write an INVALID_RUN record (§8) when the harness could not collect complete
    evidence / obtain a verdict to adjudicate — e.g. the agent-server became UNREACHABLE
    mid-run (RUN_INTERRUPTED), or the terminal wait was cut off by the hard cap while the
    build was STILL PROGRESSING (RUN_TIMEOUT_WHILE_PROGRESSING, Bug 15). This is NOT a
    product FAIL (we can't prove a product outcome) and NOT INFRA_FAILURE (pre-create only,
    §9); it blocks promotion AND signals §17 to re-run rather than recording a false fail."""
    base = Path(out_root) / run_id
    base.mkdir(parents=True, exist_ok=True)
    record_facts: dict[str, Any] = {"reason": reason, **(facts or {})}
    record = {
        "status": fc.INVALID_RUN,
        "severity": fc.NONE,
        "code": code,
        "first_broken_link": first_broken_link,
        "scenario_id": scenario.get("id"),
        "run_id": run_id,
        "conversation_id": conversation_id,
        "facts": record_facts,
        "required_evidence_present": False,
        "accepted_by": "oracle",
        "agent_comments_ignored_for_adjudication": True,
    }
    (base / CLASSIFICATION_NAME).write_text(
        json.dumps(record, indent=2, sort_keys=True), encoding="utf-8"
    )
    (base / "timeline.md").write_text(
        timeline_markdown or f"# INVALID_RUN ({code})\n\nreason: {reason}\n",
        encoding="utf-8",
    )
    return record


def _invalidation_conversation_id(
    facts: dict[str, Any] | None, client: ProductClient
) -> str | None:
    """Resolve the failed run's exact conversation ID for the evidence freeze.

    The exception's own facts win (they are bound to the failing collection);
    the client's last-created conversation is the fallback for exceptions that
    carry none. Never guesses across runs: ``last_conversation_id`` is reset by
    ``run_once`` before each create.
    """

    raw = (facts or {}).get("conversation_id")
    if isinstance(raw, str) and raw:
        return raw
    return client.last_conversation_id


def _finish_unsealable_fail_record(
    out_root: str | Path,
    run_id: str,
    scenario: dict[str, Any],
    reason: str,
    *,
    facts: dict[str, Any] | None = None,
    conversation_id: str | None = None,
    timeline_markdown: str | None = None,
) -> dict[str, Any]:
    """Write the F-27 product-FAIL record: a CONFIRMED FINISHED build whose final
    strict seal the PRODUCT refused on deterministic content (typed
    ``seal_incomplete_content`` disclosure). Unlike `_invalid_run_record` this IS
    an adjudicated product outcome — the required evidence (the product's own
    typed disclosure on the durable log) is present, the failure counts, and §17
    must NOT re-run it."""
    base = Path(out_root) / run_id
    base.mkdir(parents=True, exist_ok=True)
    record_facts: dict[str, Any] = {"reason": reason, **(facts or {})}
    record = {
        "status": fc.FAIL,
        "severity": fc.severity_for(fc.FINISH_UNSEALABLE_CONTENT),
        "code": fc.FINISH_UNSEALABLE_CONTENT,
        "first_broken_link": "finish_accepted -> final_seal_refused_on_content",
        "scenario_id": scenario.get("id"),
        "run_id": run_id,
        "conversation_id": conversation_id,
        "facts": record_facts,
        "required_evidence_present": True,
        "accepted_by": "oracle",
        "agent_comments_ignored_for_adjudication": True,
    }
    (base / CLASSIFICATION_NAME).write_text(
        json.dumps(record, indent=2, sort_keys=True), encoding="utf-8"
    )
    (base / "timeline.md").write_text(
        timeline_markdown or f"# FAIL ({fc.FINISH_UNSEALABLE_CONTENT})\n\nreason: {reason}\n",
        encoding="utf-8",
    )
    return record
