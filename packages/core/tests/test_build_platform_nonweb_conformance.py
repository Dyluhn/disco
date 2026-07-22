from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest
from disco.core.build_platform import (
    SYNTHETIC_CAPABILITIES,
    SYNTHETIC_PROFILE_ID,
    ComponentIntent,
    build_synthetic_registry,
    resolve_synthetic_conformance,
)


class _SyntheticHost:
    """Test-only host effect mediator for the packaged declarative intents."""

    def __init__(self, workspace: Path) -> None:
        self.workspace = workspace
        self.executed: list[str] = []
        self.preview_result: str | None = None
        self.package_digest: str | None = None

    def execute(self, intent: ComponentIntent) -> None:
        self.executed.append(intent.operation)
        job_dir = self.workspace / "job"
        job_dir.mkdir(exist_ok=True)
        if intent.operation == "job.author_spec":
            (job_dir / "spec.json").write_text(
                json.dumps({"operation": "uppercase", "input": "fixture.txt"}),
                encoding="utf-8",
            )
        elif intent.operation == "job.build_fixture":
            fixture = (self.workspace / "fixture.txt").read_text(encoding="utf-8")
            result = fixture.upper()
            (job_dir / "result.txt").write_text(result, encoding="utf-8")
            (job_dir / "task.manifest").write_text(
                json.dumps({"artifact": "job/result.txt", "operation": "uppercase"}),
                encoding="utf-8",
            )
        elif intent.operation == "job.preview_fixture":
            self.preview_result = (job_dir / "result.txt").read_text(encoding="utf-8")
        elif intent.operation == "job.verify_output":
            expected = (self.workspace / "fixture.txt").read_text(encoding="utf-8").upper()
            assert (job_dir / "result.txt").read_text(encoding="utf-8") == expected
        elif intent.operation == "job.package_bundle":
            payload = b"\n".join(
                path.read_bytes() for path in sorted(job_dir.iterdir()) if path.is_file()
            )
            self.package_digest = f"sha256:{hashlib.sha256(payload).hexdigest()}"
        else:  # pragma: no cover - makes the host fail closed on a new effect
            raise AssertionError(f"unmediated/unknown intent: {intent.operation}")


def test_packaged_nonweb_conformance_lifecycle(tmp_path: Path) -> None:
    composition = resolve_synthetic_conformance()
    assert composition.profile.id == SYNTHETIC_PROFILE_ID
    assert composition.blocked_operations == ()
    assert composition.target_plan.delivery.shape == "data.job_bundle"
    assert composition.target_plan.delivery.entry.reference == "job/task.manifest"
    assert composition.target_plan.preview.modality == "dry_run"
    assert composition.target_plan.preview.policy.required is False

    serialized = composition.model_dump_json()
    for forbidden in ("http", "browser", "index.html", "port_listening"):
        assert forbidden not in serialized.casefold()

    (tmp_path / "fixture.txt").write_text("hello batch\n", encoding="utf-8")
    host = _SyntheticHost(tmp_path)
    for intent in composition.construction.intents:
        host.execute(intent)
    for intent in composition.target_plan.intents:
        host.execute(intent)
    for intent in composition.target_plan.preview.intents:
        host.execute(intent)
    for check in composition.target_plan.verifier.checks:
        host.execute(check.intent)
    assert composition.target_plan.package is not None
    for intent in composition.target_plan.package.intents:
        host.execute(intent)

    assert host.executed == [
        "job.author_spec",
        "job.build_fixture",
        "job.preview_fixture",
        "job.verify_output",
        "job.package_bundle",
    ]
    assert host.preview_result == "HELLO BATCH\n"
    assert host.package_digest is not None
    assert (tmp_path / "job" / "task.manifest").is_file()


def test_optional_executable_preview_blocks_alone_when_host_lacks_it() -> None:
    composition = resolve_synthetic_conformance(
        host_capabilities=SYNTHETIC_CAPABILITIES - {"process.execute"}
    )
    assert composition.blocked("preview")
    assert not composition.blocked("construct")
    assert not composition.blocked("target")
    assert not composition.blocked("verify")
    assert not composition.blocked("package")


def test_nonweb_target_verifier_rejects_corrupted_target_output(tmp_path: Path) -> None:
    composition = resolve_synthetic_conformance()
    (tmp_path / "fixture.txt").write_text("hello batch\n", encoding="utf-8")
    host = _SyntheticHost(tmp_path)
    for intent in composition.construction.intents:
        host.execute(intent)
    for intent in composition.target_plan.intents:
        host.execute(intent)
    (tmp_path / "job" / "result.txt").write_text("CORRUPTED\n", encoding="utf-8")

    with pytest.raises(AssertionError):
        for check in composition.target_plan.verifier.checks:
            host.execute(check.intent)

    assert "job.verify_output" in host.executed
    assert "job.package_bundle" not in host.executed


def test_synthetic_registration_uses_public_registry_without_central_switch() -> None:
    registry = build_synthetic_registry()
    assert registry.profile(SYNTHETIC_PROFILE_ID) is not None
    assert [choice.id for choice in registry.profile_choices()] == [SYNTHETIC_PROFILE_ID]
    assert registry.engine(registry.profile(SYNTHETIC_PROFILE_ID).engine) is not None  # type: ignore[union-attr]
