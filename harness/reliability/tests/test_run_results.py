from __future__ import annotations

import json
from pathlib import Path

from harness.reliability.run import (
    _build_soak_result,
    _fresh_device_fingerprint,
    _fresh_device_result,
    _playwright_result,
    _pytest_result,
)
from harness.reliability.state import FAIL, INVALID, PASS


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
