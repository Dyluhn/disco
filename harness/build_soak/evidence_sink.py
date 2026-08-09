"""FilesystemEvidenceSink — the single owner of dossier/result retention.

Writes facts and does not select verdicts.  Existing classifier/oracle/promotion
code stays where it is and is called after retention exactly as today.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from . import failure_codes as fc
from .adapters.disco_api import (
    BrowserEvidenceCollectionError,
    CollectedRun,
    InfraProbeError,
    validate_browser_evidence_relpath,
)
from .evidence import EvidenceManifest, compute_evidence_hashes, sha256_file, write_manifest
from .product_evidence import (
    PRODUCT_EVIDENCE_NAME,
    PROVIDER_LEDGER_NAME,
    write_product_evidence,
    write_provider_ledger,
)

CLASSIFICATION_NAME = "classification.json"
_BROWSER_EVIDENCE_COLLECTION_ERROR_NAME = "browser-evidence-collection-error.json"


def _events_jsonl(events: list[dict[str, Any]]) -> str:
    """One canonical full-event dict per line."""
    lines: list[str] = []
    for row in events:
        payload = row.get("payload")
        if isinstance(payload, str):
            try:
                payload = json.loads(payload)
            except (ValueError, TypeError):
                payload = row
        elif not isinstance(payload, dict):
            payload = row
        lines.append(json.dumps(payload, sort_keys=True))
    return "\n".join(lines) + ("\n" if lines else "")


def _timeline_md(scenario: dict[str, Any], run: CollectedRun) -> str:
    lines = [
        f"# Build Soak timeline — {scenario.get('id')}",
        "",
        f"- conversation: `{run.conversation_id}`",
        f"- prompt: {scenario.get('prompt')!r}",
        "",
        "## Steps",
    ]
    lines += [f"{i + 1}. {step}" for i, step in enumerate(run.timeline)]
    return "\n".join(lines) + "\n"


class FilesystemEvidenceSink:
    """Retain facts without selecting a verdict."""

    def _write_dossier_files(
        self,
        base: Path,
        conv: Path,
        scenario: dict[str, Any],
        run: CollectedRun,
    ) -> dict[str, Any] | None:
        """Write the per-run dossier files; return the browser collection error."""
        (base / "prompt.txt").write_text(str(scenario.get("prompt", "")), encoding="utf-8")
        scenario_path = base / "scenario.json"
        scenario_path.write_text(
            json.dumps(scenario, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
        (base / "followups.json").write_text(
            json.dumps(scenario.get("followups") or [], indent=2), encoding="utf-8"
        )
        (base / "timeline.md").write_text(_timeline_md(scenario, run), encoding="utf-8")
        (conv / "events.jsonl").write_text(_events_jsonl(run.events), encoding="utf-8")
        (conv / "state.initial.json").write_text(
            json.dumps(run.state_initial, indent=2, sort_keys=True), encoding="utf-8"
        )
        (conv / "state.final.json").write_text(
            json.dumps(run.state_final, indent=2, sort_keys=True), encoding="utf-8"
        )
        (conv / "workspace-manifest.json").write_text(
            json.dumps(run.workspace_manifest, indent=2, sort_keys=True), encoding="utf-8"
        )
        self._write_browser_evidence(conv, run)
        browser_collection_error = run.browser_evidence_collection_error
        if browser_collection_error is not None:
            (conv / _BROWSER_EVIDENCE_COLLECTION_ERROR_NAME).write_text(
                json.dumps(browser_collection_error, indent=2, sort_keys=True) + "\n",
                encoding="utf-8",
            )
        self._write_preview(conv, run)
        if run.inspect_trace is not None:
            (conv / "inspect-trace.json").write_text(
                json.dumps(run.inspect_trace, indent=2, sort_keys=True), encoding="utf-8"
            )
        (conv / "thrash-monitor.json").write_text(
            json.dumps(run.thrash_monitor, indent=2, sort_keys=True), encoding="utf-8"
        )
        return browser_collection_error

    def _write_browser_evidence(self, conv: Path, run: CollectedRun) -> None:
        """Write browser evidence bytes to the conversation directory."""
        browser_evidence_root = (conv / "browser-evidence").resolve()
        for rel, data in sorted(run.browser_evidence.items()):
            validate_browser_evidence_relpath(rel)
            evidence_path = browser_evidence_root / rel
            evidence_path.parent.mkdir(parents=True, exist_ok=True)
            if not evidence_path.resolve(strict=False).is_relative_to(browser_evidence_root):
                raise BrowserEvidenceCollectionError(
                    "browser evidence dossier destination escapes its conversation directory",
                    {"path": rel},
                )
            evidence_path.write_bytes(data)

    def _write_preview(self, conv: Path, run: CollectedRun) -> None:
        """Write the preview dossier files when a preview was captured."""
        if run.preview is None:
            return
        preview_dir = conv / "preview"
        preview_dir.mkdir(exist_ok=True)
        (preview_dir / "health.json").write_text(
            json.dumps(run.preview.get("health") or {}, indent=2, sort_keys=True), encoding="utf-8"
        )
        (preview_dir / "served.html").write_text(
            str(run.preview.get("content") or ""), encoding="utf-8"
        )
        (preview_dir / "metadata.json").write_text(
            json.dumps(
                {
                    "available": bool(run.preview.get("available")),
                    "runtime_available": bool(run.preview.get("runtime_available")),
                    "runtime_availability_status": run.preview.get("runtime_availability_status"),
                    "source": str(run.preview.get("source") or "unrecorded"),
                    **(
                        {"failure_stage": run.preview["failure_stage"]}
                        if run.preview.get("failure_stage")
                        in {"mint", "validation", "redemption", "fetch"}
                        else {}
                    ),
                },
                indent=2,
                sort_keys=True,
            ),
            encoding="utf-8",
        )

    def _build_evidence_files(
        self,
        run: CollectedRun,
        conv_rel: str,
        browser_collection_error: dict[str, Any] | None,
        _pe: Any,
        provider_ledger: list[dict[str, Any]] | None,
    ) -> dict[str, str]:
        """Build the evidence-file map for the hash lock."""
        evidence_files = {
            "scenario.json": "scenario.json",
            "events.jsonl": f"{conv_rel}/events.jsonl",
            "state.initial.json": f"{conv_rel}/state.initial.json",
            "state.final.json": f"{conv_rel}/state.final.json",
            "workspace-manifest.json": f"{conv_rel}/workspace-manifest.json",
        }
        for rel in sorted(run.browser_evidence):
            label = f"browser-evidence/{rel}"
            evidence_files[label] = f"{conv_rel}/{label}"
        if browser_collection_error is not None:
            evidence_files[_BROWSER_EVIDENCE_COLLECTION_ERROR_NAME] = (
                f"{conv_rel}/{_BROWSER_EVIDENCE_COLLECTION_ERROR_NAME}"
            )
        if _pe:
            evidence_files[PRODUCT_EVIDENCE_NAME] = f"{conv_rel}/{PRODUCT_EVIDENCE_NAME}"
        if run.inspect_trace is not None:
            evidence_files["inspect-trace.json"] = f"{conv_rel}/inspect-trace.json"
        if provider_ledger is not None:
            evidence_files[PROVIDER_LEDGER_NAME] = f"{conv_rel}/{PROVIDER_LEDGER_NAME}"
        evidence_files["thrash-monitor.json"] = f"{conv_rel}/thrash-monitor.json"
        if run.preview is not None:
            evidence_files["preview/health.json"] = f"{conv_rel}/preview/health.json"
            evidence_files["preview/served.html"] = f"{conv_rel}/preview/served.html"
            evidence_files["preview/metadata.json"] = f"{conv_rel}/preview/metadata.json"
        return evidence_files

    def assemble_dossier(
        self,
        out_root: str | Path,
        run_id: str,
        scenario: dict[str, Any],
        run: CollectedRun,
        *,
        model: str | None,
        autonomous: bool,
        commit: str = "",
        repo_revision: str = "",
        repo_dirty: bool = False,
        seed: int | None = None,
        mode: str = "api",
        kernel: str = "disco",
        started_at: str | None = None,
        provider_ledger: list[dict[str, Any]] | None = None,
    ) -> Path:
        """Write the dossier and freeze it under the evidence lock."""
        base = Path(out_root) / run_id
        conv = base / "conversations" / run.conversation_id
        conv.mkdir(parents=True, exist_ok=True)

        scenario_path = base / "scenario.json"
        browser_collection_error = self._write_dossier_files(base, conv, scenario, run)

        _pe = getattr(run, "product_evidence", None)
        if _pe:
            write_product_evidence(conv, _pe, strict=False)
        if provider_ledger is not None:
            write_provider_ledger(conv, provider_ledger)

        conv_rel = f"conversations/{run.conversation_id}"
        evidence_files = self._build_evidence_files(
            run, conv_rel, browser_collection_error, _pe, provider_ledger
        )
        provider_assertion = (scenario.get("assertions") or {}).get("provider") or {}
        provider = (
            str(provider_assertion.get("require_host_substr") or "")
            if isinstance(provider_assertion, dict)
            else ""
        )
        if not started_at:
            for event in run.events:
                candidate = event.get("created_at") or event.get("timestamp")
                if isinstance(candidate, str) and candidate:
                    started_at = candidate
                    break
        manifest = EvidenceManifest(
            run_id=run_id,
            scenario_id=str(scenario.get("id")),
            scenario_sha256=sha256_file(scenario_path),
            seed=seed,
            repo_commit=commit,
            repo_revision=repo_revision,
            repo_dirty=repo_dirty,
            model=model or "",
            provider=provider,
            autonomous=autonomous,
            surface=str(scenario.get("surface") or "build"),
            kernel=kernel,
            mode=mode,
            started_at=started_at or datetime.now(UTC).isoformat(),
            finished_at=datetime.now(UTC).isoformat(),
            evidence_files={"events": f"{conv_rel}/events.jsonl", **evidence_files},
            evidence_hashes=compute_evidence_hashes(base, evidence_files),
        )
        write_manifest(base, manifest)
        return base

    def record_infra_failure(
        self,
        out_root: str | Path,
        run_id: str,
        scenario: dict[str, Any],
        exc: InfraProbeError,
    ) -> dict[str, Any]:
        """Write an INFRA_FAILURE record (the run never created a conversation)."""
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
            (
                "# INFRA_FAILURE (pre-create)\n\n"
                f"signature: {exc.signature_id}\ndetail: {exc.detail}\n"
            ),
            encoding="utf-8",
        )
        return record

    def record_invalid_run(
        self,
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
        """Write an INVALID_RUN record when the harness could not collect complete evidence."""
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

    def record_finish_unsealable(
        self,
        out_root: str | Path,
        run_id: str,
        scenario: dict[str, Any],
        reason: str,
        *,
        facts: dict[str, Any] | None = None,
        conversation_id: str | None = None,
        timeline_markdown: str | None = None,
    ) -> dict[str, Any]:
        """Write the product-FAIL record for a confirmed FINISHED build whose final
        strict seal the PRODUCT refused on deterministic content."""
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

    def write_batch_summary(
        self,
        path: Path,
        summary: dict[str, Any],
    ) -> Path:
        """Write the batch summary JSON."""
        path.write_text(json.dumps(summary, indent=2, sort_keys=True), encoding="utf-8")
        return path
