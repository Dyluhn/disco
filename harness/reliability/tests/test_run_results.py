from __future__ import annotations

import json
from pathlib import Path

import pytest

from harness.reliability.matrix import Suite
from harness.reliability.run import (
    _build_soak_result,
    _configure_provider_evidence_environment,
    _fresh_device_fingerprint,
    _fresh_device_result,
    _playwright_result,
    _provider_conversation_manifest_result,
    _provider_evidence_result,
    _pytest_result,
    _suite_subprocess_environment,
)
from harness.reliability.state import FAIL, INVALID, PASS


def test_playwright_environment_drops_conflicting_no_color(
    monkeypatch,
) -> None:
    monkeypatch.setenv("NO_COLOR", "1")

    assert "NO_COLOR" not in _suite_subprocess_environment("playwright")
    assert _suite_subprocess_environment("generic")["NO_COLOR"] == "1"


def test_playwright_skip_is_invalid_not_pass(tmp_path: Path) -> None:
    report = {
        "suites": [
            {
                "specs": [
                    {
                        "tests": [
                            {
                                "expectedStatus": "skipped",
                                "results": [{"status": "skipped"}],
                            }
                        ]
                    }
                ]
            }
        ],
        "errors": [],
    }
    path = tmp_path / "report.json"
    path.write_text(json.dumps(report), encoding="utf-8")
    status, units, _ = _playwright_result(path, exit_code=0, units=1)
    assert (status, units) == (INVALID, 0)


def test_playwright_failed_attempt_cannot_retry_to_pass(tmp_path: Path) -> None:
    report = {
        "suites": [
            {
                "specs": [
                    {
                        "tests": [
                            {
                                "expectedStatus": "passed",
                                "results": [{"status": "failed"}, {"status": "passed"}],
                            }
                        ]
                    }
                ]
            }
        ],
        "errors": [],
    }
    path = tmp_path / "report.json"
    path.write_text(json.dumps(report), encoding="utf-8")
    status, units, _ = _playwright_result(path, exit_code=1, units=1)
    assert (status, units) == (FAIL, 0)


def test_build_soak_requires_every_declared_trial(tmp_path: Path) -> None:
    batch = tmp_path / "batch"
    batch.mkdir()
    (batch / "batch-summary.json").write_text(
        json.dumps({"runs": [{"status": "PASS"} for _ in range(9)]}), encoding="utf-8"
    )
    status, units, _ = _build_soak_result(tmp_path, exit_code=0, units=10)
    assert (status, units) == (INVALID, 0)


def _write_provider_ledger(path: Path, records: list[dict[str, object]]) -> None:
    complete = [
        {"ts": 1_700_000_000.0 + index, "has_tools": True, **record}
        for index, record in enumerate(records)
    ]
    path.write_text("".join(json.dumps(record) + "\n" for record in complete), encoding="utf-8")


def _write_provider_manifest(path: Path, conversation_ids: list[object]) -> None:
    path.write_text(
        json.dumps({"schema_version": 1, "conversation_ids": conversation_ids}),
        encoding="utf-8",
    )


def _provider_suite(*, conversation_manifest: bool, provider_evidence: bool = True) -> Suite:
    return Suite(
        id="provider-suite",
        proof="live",
        kind="playwright",
        cwd=".",
        timeout_s=1,
        memory_gib=1,
        units=1,
        command=("true",),
        claims=("claim",),
        requires_env=(),
        environment={},
        provider_evidence=provider_evidence,
        provider_conversation_manifest=conversation_manifest,
    )


def test_provider_evidence_requires_exact_host_model_and_trial_coverage(tmp_path: Path) -> None:
    path = tmp_path / "provider.jsonl"
    _write_provider_ledger(
        path,
        [
            {
                "host": "opencode.ai",
                "model": "deepseek-v4-flash",
                "conversation_id": f"conv_{index}",
            }
            for index in range(3)
        ],
    )

    assert _provider_evidence_result(
        path,
        expected_host="opencode.ai",
        expected_model="deepseek-v4-flash",
        units=3,
    )[:2] == (PASS, 3)


def test_provider_evidence_fails_hidden_fallback(tmp_path: Path) -> None:
    path = tmp_path / "provider.jsonl"
    _write_provider_ledger(
        path,
        [{"host": "fallback.example", "model": "other", "conversation_id": "conv_1"}],
    )

    status, units, reason = _provider_evidence_result(
        path,
        expected_host="opencode.ai",
        expected_model="deepseek-v4-flash",
        units=1,
    )
    assert (status, units) == (FAIL, 0)
    assert "provider fallback detected" in reason


def test_provider_evidence_allows_exact_toolless_auxiliary_calls(tmp_path: Path) -> None:
    path = tmp_path / "provider.jsonl"
    _write_provider_ledger(
        path,
        [
            {
                "host": "opencode.ai",
                "model": "deepseek-v4-flash",
                "conversation_id": None,
                "has_tools": False,
            },
            {
                "host": "opencode.ai",
                "model": "deepseek-v4-flash",
                "conversation_id": "conv_1",
                "has_tools": True,
            },
        ],
    )

    status, units, reason = _provider_evidence_result(
        path,
        expected_host="opencode.ai",
        expected_model="deepseek-v4-flash",
        units=1,
    )

    assert (status, units) == (PASS, 1)
    assert "1 auxiliary" in reason


def test_provider_manifest_allows_exact_scope_and_toolless_auxiliary_calls(
    tmp_path: Path,
) -> None:
    ledger = tmp_path / "provider.jsonl"
    manifest = tmp_path / "provider-conversations.json"
    _write_provider_manifest(manifest, ["conv_build", "conv_agent"])
    _write_provider_ledger(
        ledger,
        [
            {
                "host": "opencode.ai",
                "model": "deepseek-v4-flash",
                "conversation_id": None,
                "has_tools": False,
            },
            {
                "host": "opencode.ai",
                "model": "deepseek-v4-flash",
                "conversation_id": "conv_build",
                "has_tools": True,
            },
            {
                "host": "opencode.ai",
                "model": "deepseek-v4-flash",
                "conversation_id": "conv_agent",
                "has_tools": True,
            },
        ],
    )

    status, units, reason = _provider_evidence_result(
        ledger,
        expected_host="opencode.ai",
        expected_model="deepseek-v4-flash",
        units=1,
        conversation_manifest_path=manifest,
    )

    assert (status, units) == (PASS, 1)
    assert "1 auxiliary" in reason
    assert "2 conversation(s)" in reason


def test_provider_manifest_is_runner_owned_absolute_and_suite_private(tmp_path: Path) -> None:
    suite_out = tmp_path / "campaign" / "provider-suite"
    suite_out.mkdir(parents=True)
    env = {
        "DISCO_PROVIDER_LEDGER": "/tmp/shared-ledger.jsonl",
        "DISCO_RELIABILITY_PROVIDER_CONVERSATION_MANIFEST": "/tmp/shared.json",
    }

    ledger, manifest = _configure_provider_evidence_environment(
        _provider_suite(conversation_manifest=True), suite_out, env
    )

    assert ledger.is_absolute()
    assert manifest is not None and manifest.is_absolute()
    assert ledger.parent == suite_out.resolve()
    assert manifest.parent == suite_out.resolve()
    assert env["DISCO_PROVIDER_LEDGER"] == str(ledger)
    assert env["DISCO_RELIABILITY_PROVIDER_CONVERSATION_MANIFEST"] == str(manifest)


def test_nonprovider_suite_drops_all_ambient_provider_evidence(tmp_path: Path) -> None:
    env = {
        "DISCO_PROVIDER_LEDGER": "/tmp/shared-ledger.jsonl",
        "DISCO_RELIABILITY_PROVIDER_CONVERSATION_MANIFEST": "/tmp/shared.json",
    }
    _, manifest = _configure_provider_evidence_environment(
        _provider_suite(conversation_manifest=False, provider_evidence=False), tmp_path, env
    )

    assert manifest is None
    assert "DISCO_PROVIDER_LEDGER" not in env
    assert "DISCO_RELIABILITY_PROVIDER_CONVERSATION_MANIFEST" not in env


def test_provider_evidence_rejects_host_substring_fallback(tmp_path: Path) -> None:
    ledger = tmp_path / "provider.jsonl"
    _write_provider_ledger(
        ledger,
        [
            {
                "host": "evil-opencode.ai.example",
                "model": "deepseek-v4-flash",
                "conversation_id": "conv_1",
            }
        ],
    )

    status, units, reason = _provider_evidence_result(
        ledger,
        expected_host="opencode.ai",
        expected_model="deepseek-v4-flash",
        units=1,
    )

    assert (status, units) == (FAIL, 0)
    assert "provider fallback detected" in reason


@pytest.mark.parametrize(
    "record",
    [
        {"host": "opencode.ai", "model": "deepseek-v4-flash", "conversation_id": "conv_1"},
        {
            "ts": 1.0,
            "host": "opencode.ai",
            "model": "deepseek-v4-flash",
            "has_tools": True,
            "conversation_id": 123,
        },
        {
            "ts": float("nan"),
            "host": "opencode.ai",
            "model": "deepseek-v4-flash",
            "has_tools": True,
            "conversation_id": "conv_1",
        },
        {
            "ts": 1.0,
            "host": "opencode.ai",
            "model": "deepseek-v4-flash",
            "has_tools": "yes",
            "conversation_id": "conv_1",
        },
        {
            "ts": 1.0,
            "host": "opencode.ai",
            "model": "deepseek-v4-flash",
            "has_tools": True,
            "conversation_id": "conv_1",
            "payload": "must not be retained",
        },
        {
            "ts": 1.0,
            "host": "opencode.ai",
            "model": "deepseek-v4-flash",
            "has_tools": True,
            "conversation_id": "conv_1",
            "purpose": "prompt text is not a bounded label",
        },
    ],
)
def test_provider_evidence_rejects_malformed_record_schema(
    tmp_path: Path, record: dict[str, object]
) -> None:
    ledger = tmp_path / "provider.jsonl"
    ledger.write_text(json.dumps(record) + "\n", encoding="utf-8")

    assert _provider_evidence_result(
        ledger,
        expected_host="opencode.ai",
        expected_model="deepseek-v4-flash",
        units=1,
    )[:2] == (INVALID, 0)


def test_provider_manifest_missing_is_invalid(tmp_path: Path) -> None:
    ledger = tmp_path / "provider.jsonl"
    _write_provider_ledger(
        ledger,
        [
            {
                "host": "opencode.ai",
                "model": "deepseek-v4-flash",
                "conversation_id": "conv_build",
            }
        ],
    )

    status, units, reason = _provider_evidence_result(
        ledger,
        expected_host="opencode.ai",
        expected_model="deepseek-v4-flash",
        units=1,
        conversation_manifest_path=tmp_path / "missing.json",
    )

    assert (status, units) == (INVALID, 0)
    assert "was not produced" in reason


@pytest.mark.parametrize(
    ("content", "reason_fragment"),
    [
        ("not-json", "unreadable"),
        ("[]", "must be an object"),
        ('{"schema_version": 1}', "must contain exactly"),
        ('{"schema_version": true, "conversation_ids": ["conv_1"]}', "must be 1"),
        ('{"schema_version": 1, "conversation_ids": []}', "nonempty list"),
        ('{"schema_version": 1, "conversation_ids": [null]}', "nonempty string"),
        ('{"schema_version": 1, "conversation_ids": [" conv_1"]}', "nonempty string"),
    ],
)
def test_provider_manifest_malformed_is_invalid(
    tmp_path: Path,
    content: str,
    reason_fragment: str,
) -> None:
    manifest = tmp_path / "provider-conversations.json"
    manifest.write_text(content, encoding="utf-8")

    status, conversation_ids, reason = _provider_conversation_manifest_result(manifest)

    assert status == INVALID
    assert conversation_ids == frozenset()
    assert reason_fragment in reason


def test_provider_manifest_duplicate_id_is_invalid(tmp_path: Path) -> None:
    manifest = tmp_path / "provider-conversations.json"
    _write_provider_manifest(manifest, ["conv_1", "conv_1"])

    status, _, reason = _provider_conversation_manifest_result(manifest)

    assert status == INVALID
    assert "duplicate IDs" in reason


def test_provider_manifest_missing_ledger_id_is_invalid(tmp_path: Path) -> None:
    ledger = tmp_path / "provider.jsonl"
    manifest = tmp_path / "provider-conversations.json"
    _write_provider_manifest(manifest, ["conv_build", "conv_agent"])
    _write_provider_ledger(
        ledger,
        [
            {
                "host": "opencode.ai",
                "model": "deepseek-v4-flash",
                "conversation_id": "conv_build",
            }
        ],
    )

    status, units, reason = _provider_evidence_result(
        ledger,
        expected_host="opencode.ai",
        expected_model="deepseek-v4-flash",
        units=1,
        conversation_manifest_path=manifest,
    )

    assert (status, units) == (INVALID, 0)
    assert "missing manifest conversation ID" in reason
    assert "conv_agent" in reason


def test_provider_manifest_extra_ledger_id_is_invalid(tmp_path: Path) -> None:
    ledger = tmp_path / "provider.jsonl"
    manifest = tmp_path / "provider-conversations.json"
    _write_provider_manifest(manifest, ["conv_build", "conv_agent"])
    _write_provider_ledger(
        ledger,
        [
            {
                "host": "opencode.ai",
                "model": "deepseek-v4-flash",
                "conversation_id": conversation_id,
            }
            for conversation_id in ("conv_build", "conv_agent", "conv_unclaimed")
        ],
    )

    status, units, reason = _provider_evidence_result(
        ledger,
        expected_host="opencode.ai",
        expected_model="deepseek-v4-flash",
        units=1,
        conversation_manifest_path=manifest,
    )

    assert (status, units) == (INVALID, 0)
    assert "extra provider-ledger conversation ID" in reason
    assert "conv_unclaimed" in reason


def test_provider_manifest_requires_tool_bearing_call_for_every_exact_id(
    tmp_path: Path,
) -> None:
    ledger = tmp_path / "provider.jsonl"
    manifest = tmp_path / "provider-conversations.json"
    _write_provider_manifest(manifest, ["conv_build", "conv_agent"])
    _write_provider_ledger(
        ledger,
        [
            {
                "host": "opencode.ai",
                "model": "deepseek-v4-flash",
                "conversation_id": "conv_build",
                "has_tools": True,
            },
            {
                "host": "opencode.ai",
                "model": "deepseek-v4-flash",
                "conversation_id": "conv_agent",
                "has_tools": False,
            },
        ],
    )

    status, units, reason = _provider_evidence_result(
        ledger,
        expected_host="opencode.ai",
        expected_model="deepseek-v4-flash",
        units=1,
        conversation_manifest_path=manifest,
    )

    assert (status, units) == (INVALID, 0)
    assert "no tool-bearing provider call" in reason
    assert "conv_agent" in reason


def test_provider_evidence_rejects_unscoped_tool_calls(tmp_path: Path) -> None:
    path = tmp_path / "provider.jsonl"
    _write_provider_ledger(
        path,
        [
            {
                "host": "opencode.ai",
                "model": "deepseek-v4-flash",
                "conversation_id": None,
                "has_tools": True,
            }
        ],
    )

    status, units, reason = _provider_evidence_result(
        path,
        expected_host="opencode.ai",
        expected_model="deepseek-v4-flash",
        units=1,
    )

    assert (status, units) == (INVALID, 0)
    assert "no conversation_id" in reason


def test_provider_evidence_rejects_absent_or_under_scoped_ledger(tmp_path: Path) -> None:
    missing = tmp_path / "missing.jsonl"
    assert _provider_evidence_result(
        missing,
        expected_host="opencode.ai",
        expected_model="deepseek-v4-flash",
        units=1,
    )[:2] == (INVALID, 0)

    path = tmp_path / "provider.jsonl"
    _write_provider_ledger(
        path,
        [
            {
                "host": "opencode.ai",
                "model": "deepseek-v4-flash",
                "conversation_id": "conv_only",
            }
        ],
    )
    assert _provider_evidence_result(
        path,
        expected_host="opencode.ai",
        expected_model="deepseek-v4-flash",
        units=2,
    )[:2] == (INVALID, 0)


def test_pytest_skip_is_invalid(tmp_path: Path) -> None:
    path = tmp_path / "pytest.xml"
    path.write_text(
        '<testsuite tests="2"><testcase name="ok"/><testcase name="no"><skipped/></testcase>'
        "</testsuite>",
        encoding="utf-8",
    )
    status, units, _ = _pytest_result(path, exit_code=0, units=1)
    assert (status, units) == (INVALID, 0)


def test_structured_all_pass_counts_declared_units(tmp_path: Path) -> None:
    path = tmp_path / "pytest.xml"
    path.write_text(
        '<testsuite tests="2"><testcase name="a"/><testcase name="b"/></testsuite>',
        encoding="utf-8",
    )
    assert _pytest_result(path, exit_code=0, units=7)[:2] == (PASS, 7)


def test_fresh_device_pass_requires_hardware_fingerprint(tmp_path: Path) -> None:
    path = tmp_path / "fresh-device-result.json"
    path.write_text(
        json.dumps(
            {
                "status": PASS,
                "device_label": "operator-can-change-this",
                "checks": [{"id": "install", "status": PASS}],
            }
        ),
        encoding="utf-8",
    )
    assert _fresh_device_result(path, exit_code=0, units=1)[:2] == (INVALID, 0)
    assert _fresh_device_fingerprint(path) is None


def test_fresh_device_uses_harness_fingerprint_not_label(tmp_path: Path) -> None:
    path = tmp_path / "fresh-device-result.json"
    path.write_text(
        json.dumps(
            {
                "status": PASS,
                "device_fingerprint": "0123456789abcdef01234567",
                "device_label": "same-machine-renamed",
                "checks": [{"id": "install", "status": PASS}],
            }
        ),
        encoding="utf-8",
    )
    assert _fresh_device_result(path, exit_code=0, units=1)[:2] == (PASS, 1)
    assert _fresh_device_fingerprint(path) == "0123456789abcdef01234567"
