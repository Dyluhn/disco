"""Resource-aware, claim-driven reliability campaign runner.

This is the outer harness.  It runs hermetic, live, and fresh-device suites,
rejects skipped/missing evidence, and records only post-fix passes against the
exact source revision that produced them.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import importlib
import json
import math
import os
import re
import signal
import sys
import time
import xml.etree.ElementTree as ET
from collections import Counter
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

# Keep the documented direct-script entrypoint hermetic. When Python executes
# ``harness/reliability/run.py`` it adds only that file's directory to sys.path,
# so absolute ``harness.*`` imports otherwise fail before argparse can handle
# even ``--list`` or ``--help``.
REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

# Load campaign modules only after the direct-script path is made importable.
# Dynamic imports keep the bootstrap ordering explicit without suppressing E402.
_resources = importlib.import_module("harness.build_soak.resources")
GIB = _resources.GIB
read_host_resources = _resources.read_host_resources

_matrix = importlib.import_module("harness.reliability.matrix")
PROOFS = _matrix.PROOFS
ReliabilityMatrix = _matrix.ReliabilityMatrix
Suite = _matrix.Suite
load_matrix = _matrix.load_matrix

_state = importlib.import_module("harness.reliability.state")
FAIL = _state.FAIL
INFRA = _state.INFRA
INVALID = _state.INVALID
PASS = _state.PASS
promotion_report = _state.promotion_report
record_campaign = _state.record_campaign
source_revision = _state.source_revision
state_transaction = _state.state_transaction

DEFAULT_MATRIX = Path(__file__).with_name("matrix.yaml")
_EXPECTED_PROVIDER_HOST_ENV = "DISCO_RELIABILITY_EXPECTED_PROVIDER_HOST"
_EXPECTED_PROVIDER_MODEL_ENV = "DISCO_RELIABILITY_EXPECTED_PROVIDER_MODEL"
_PROVIDER_CONVERSATION_MANIFEST_ENV = "DISCO_RELIABILITY_PROVIDER_CONVERSATION_MANIFEST"
_PROVIDER_LEDGER_REQUIRED_KEYS = {
    "ts",
    "host",
    "model",
    "has_tools",
    "conversation_id",
}
_PROVIDER_LEDGER_OPTIONAL_KEYS = {"purpose", "call_kind"}
_PROVIDER_LEDGER_LABEL_RE = re.compile(r"[a-z][a-z0-9_.:-]{0,63}\Z")


def _utc_now() -> str:
    return datetime.now(UTC).isoformat()


def _split_values(values: list[str] | None) -> set[str] | None:
    if not values:
        return None
    split = {part.strip() for value in values for part in value.split(",") if part.strip()}
    return split or None


def _expand(value: str, context: dict[str, str]) -> str:
    try:
        return value.format_map(context)
    except KeyError as exc:
        raise ValueError(f"unknown reliability matrix placeholder: {exc.args[0]}") from exc


class WeightedSuitePool:
    """Reserve variable per-suite memory while protecting desktop headroom."""

    def __init__(
        self,
        *,
        disk_path: Path,
        memory_reserve_bytes: int,
        disk_reserve_bytes: int,
        max_parallel: int,
        poll_s: float,
        wait_timeout_s: float,
    ) -> None:
        if min(memory_reserve_bytes, disk_reserve_bytes, max_parallel) <= 0:
            raise ValueError("resource reserves and parallelism must be positive")
        self.disk_path = disk_path
        self.memory_reserve_bytes = memory_reserve_bytes
        self.disk_reserve_bytes = disk_reserve_bytes
        self.max_parallel = max_parallel
        self.poll_s = poll_s
        self.wait_timeout_s = wait_timeout_s
        initial = read_host_resources(disk_path=disk_path)
        self.memory_budget = max(0, initial.memory_available_bytes - memory_reserve_bytes)
        self._active_memory = 0
        self._active_count = 0
        self._lock = asyncio.Lock()

    @asynccontextmanager
    async def slot(self, memory_bytes: int):
        if memory_bytes <= 0:
            raise ValueError("suite memory reservation must be positive")
        if memory_bytes > self.memory_budget:
            raise RuntimeError(
                "suite cannot fit without consuming the protected desktop memory reserve"
            )
        deadline = time.monotonic() + self.wait_timeout_s
        snapshot = None
        while snapshot is None:
            resources = read_host_resources(disk_path=self.disk_path)
            async with self._lock:
                memory_fits = self._active_memory + memory_bytes <= self.memory_budget
                live_memory_fits = (
                    resources.memory_available_bytes >= self.memory_reserve_bytes + memory_bytes
                )
                disk_fits = resources.disk_free_bytes >= self.disk_reserve_bytes
                count_fits = self._active_count < self.max_parallel
                if memory_fits and live_memory_fits and disk_fits and count_fits:
                    self._active_memory += memory_bytes
                    self._active_count += 1
                    snapshot = resources
            if snapshot is not None:
                break
            if time.monotonic() >= deadline:
                raise TimeoutError("suite resource admission timed out")
            await asyncio.sleep(self.poll_s)
        try:
            yield snapshot
        finally:
            async with self._lock:
                self._active_memory -= memory_bytes
                self._active_count -= 1


def _pytest_result(path: Path, *, exit_code: int, units: int) -> tuple[str, int, str]:
    if not path.is_file():
        return INFRA, 0, "pytest did not produce its required JUnit evidence"
    root = ET.parse(path).getroot()
    cases = list(root.iter("testcase"))
    failed = sum(
        1 for case in cases if case.find("failure") is not None or case.find("error") is not None
    )
    skipped = sum(1 for case in cases if case.find("skipped") is not None)
    passed = len(cases) - failed - skipped
    if failed:
        return FAIL, 0, f"{failed} pytest case(s) failed"
    if exit_code != 0:
        return INFRA, 0, f"pytest exited {exit_code} without a classified test failure"
    if skipped:
        return INVALID, 0, f"{skipped} pytest case(s) skipped; skipped evidence never counts"
    if not cases:
        return INVALID, 0, "pytest collected no cases"
    return PASS, units, f"{passed} pytest case(s) passed"


def _playwright_tests(report: dict[str, Any]) -> list[dict[str, Any]]:
    found: list[dict[str, Any]] = []

    def visit(node: Any) -> None:
        if not isinstance(node, dict):
            return
        tests = node.get("tests")
        if isinstance(tests, list):
            found.extend(test for test in tests if isinstance(test, dict))
        for key in ("suites", "specs"):
            children = node.get(key)
            if isinstance(children, list):
                for child in children:
                    visit(child)

    visit(report)
    return found


def _playwright_result(path: Path, *, exit_code: int, units: int) -> tuple[str, int, str]:
    if not path.is_file():
        return INFRA, 0, "Playwright did not produce its required JSON evidence"
    try:
        report = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        return INFRA, 0, f"Playwright JSON evidence is unreadable: {exc}"
    tests = _playwright_tests(report)
    passed = skipped = failed = 0
    for test in tests:
        results = test.get("results") or []
        statuses = [result.get("status") for result in results if isinstance(result, dict)]
        expected = test.get("expectedStatus", "passed")
        if expected == "skipped" or not statuses or statuses[-1] == "skipped":
            skipped += 1
        elif expected != "passed" or any(status != "passed" for status in statuses):
            failed += 1
        else:
            passed += 1
    top_errors = report.get("errors") if isinstance(report, dict) else None
    if failed or top_errors:
        return FAIL, 0, f"{failed} failed test(s), {len(top_errors or [])} top-level error(s)"
    if exit_code != 0:
        return INFRA, 0, f"Playwright exited {exit_code} without classified test failures"
    if skipped:
        return INVALID, 0, f"{skipped} Playwright test(s) skipped; skipped evidence never counts"
    if passed < units:
        return INVALID, 0, f"only {passed}/{units} required Playwright trials passed"
    return PASS, units, f"{passed} Playwright trial(s) passed"


def _vitest_result(path: Path, *, exit_code: int, units: int) -> tuple[str, int, str]:
    if not path.is_file():
        return INFRA, 0, "Vitest did not produce its required JSON evidence"
    try:
        report = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        return INFRA, 0, f"Vitest JSON evidence is unreadable: {exc}"
    failed = int(report.get("numFailedTests") or 0)
    pending = int(report.get("numPendingTests") or 0)
    passed = int(report.get("numPassedTests") or 0)
    total = int(report.get("numTotalTests") or (failed + pending + passed))
    if failed:
        return FAIL, 0, f"{failed} Vitest test(s) failed"
    if exit_code != 0:
        return INFRA, 0, f"Vitest exited {exit_code} without classified test failures"
    if pending:
        return INVALID, 0, f"{pending} Vitest test(s) skipped; skipped evidence never counts"
    if total <= 0 or passed <= 0:
        return INVALID, 0, "Vitest produced no passing test evidence"
    return PASS, units, f"{passed} Vitest test(s) passed"


def _build_soak_result(out: Path, *, exit_code: int, units: int) -> tuple[str, int, str]:
    summaries = sorted(out.rglob("batch-summary.json"), key=lambda path: path.stat().st_mtime)
    if not summaries:
        return INFRA, 0, "build soak produced no batch-summary.json"
    try:
        report = json.loads(summaries[-1].read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        return INFRA, 0, f"build-soak summary is unreadable: {exc}"
    runs = report.get("runs") or []
    statuses = Counter(str(run.get("status")) for run in runs if isinstance(run, dict))
    if statuses["FAIL"]:
        return FAIL, 0, f"{statuses['FAIL']} build-soak trial(s) failed"
    if statuses["INVALID_RUN"]:
        return INVALID, 0, f"{statuses['INVALID_RUN']} build-soak trial(s) invalid"
    if statuses["INFRA_FAILURE"]:
        return INFRA, 0, f"{statuses['INFRA_FAILURE']} build-soak infrastructure failure(s)"
    if exit_code != 0:
        return INFRA, 0, f"build soak exited {exit_code} without a classified failure"
    if statuses["PASS"] < units or len(runs) < units:
        return INVALID, 0, f"only {statuses['PASS']}/{units} required build trials passed"
    return PASS, units, f"{statuses['PASS']} build-soak trial(s) passed"


def _provider_evidence_result(
    path: Path,
    *,
    expected_host: str,
    expected_model: str,
    units: int,
    conversation_manifest_path: Path | None = None,
) -> tuple[str, int, str]:
    """Fail closed on absent, malformed, or fallback provider-call evidence."""

    manifest_conversations: frozenset[str] | None = None
    if conversation_manifest_path is not None:
        manifest_status, manifest_conversations, manifest_reason = (
            _provider_conversation_manifest_result(conversation_manifest_path)
        )
        if manifest_status != PASS:
            return manifest_status, 0, manifest_reason

    if not path.is_file():
        return INVALID, 0, "provider ledger was not produced"
    records: list[dict[str, Any]] = []
    try:
        for line_number, raw in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if not raw.strip():
                continue
            parsed = json.loads(raw)
            if not isinstance(parsed, dict):
                return INVALID, 0, f"provider ledger line {line_number} is not an object"
            records.append(parsed)
    except (OSError, json.JSONDecodeError) as exc:
        return INVALID, 0, f"provider ledger is unreadable: {type(exc).__name__}"
    if not records:
        return INVALID, 0, "provider ledger contains no calls"

    required_host = expected_host.strip().lower()
    required_model = expected_model.strip()
    if not required_host or not required_model:
        return INVALID, 0, "expected provider host/model is empty"

    conversations: set[str] = set()
    tool_conversations: set[str] = set()
    auxiliary_calls = 0
    for index, record in enumerate(records):
        record_status, normalized, record_reason = _provider_ledger_record_result(record, index)
        if record_status != PASS or normalized is None:
            return record_status, 0, record_reason
        host, model, has_tools, conversation_id = normalized
        if host != required_host:
            return (
                FAIL,
                0,
                f"provider fallback detected: host {host!r} != {required_host!r}",
            )
        if model != required_model:
            return (
                FAIL,
                0,
                f"provider fallback detected: model {model!r} != {required_model!r}",
            )
        if conversation_id is None:
            # Preflight and async title/summarizer calls carry no conversation
            # metadata and no tools. They still must use the exact provider, but
            # must not invalidate otherwise scoped driver evidence. A tool-bearing
            # call without a conversation remains invalid: it cannot be assigned
            # to a trial or checked for post-terminal runaway.
            if not has_tools:
                auxiliary_calls += 1
                continue
            return (
                INVALID,
                0,
                f"provider ledger record {index} has tools or unknown call type "
                "but no conversation_id",
            )
        conversations.add(conversation_id)
        if has_tools:
            tool_conversations.add(conversation_id)
    if manifest_conversations is not None and conversations != manifest_conversations:
        missing = sorted(manifest_conversations - conversations)
        extra = sorted(conversations - manifest_conversations)
        details: list[str] = []
        if missing:
            details.append(f"missing manifest conversation ID(s): {missing}")
        if extra:
            details.append(f"extra provider-ledger conversation ID(s): {extra}")
        return INVALID, 0, "; ".join(details)
    if manifest_conversations is not None:
        without_tools = sorted(manifest_conversations - tool_conversations)
        if without_tools:
            return (
                INVALID,
                0,
                f"manifest conversation ID(s) have no tool-bearing provider call: {without_tools}",
            )
    if len(conversations) < units:
        return (
            INVALID,
            0,
            f"provider ledger covers only {len(conversations)}/{units} required conversation(s)",
        )
    return (
        PASS,
        units,
        f"{len(records)} provider call(s) ({auxiliary_calls} auxiliary) across "
        f"{len(conversations)} conversation(s) "
        f"used {required_host}/{required_model}",
    )


def _provider_ledger_record_result(
    record: dict[str, Any], index: int
) -> tuple[str, tuple[str, str, bool, str | None] | None, str]:
    """Validate one sanitized provider record without coercing evidence types."""

    keys = set(record)
    missing = sorted(_PROVIDER_LEDGER_REQUIRED_KEYS - keys)
    unexpected = sorted(keys - _PROVIDER_LEDGER_REQUIRED_KEYS - _PROVIDER_LEDGER_OPTIONAL_KEYS)
    if missing or unexpected:
        return (
            INVALID,
            None,
            f"provider ledger record {index} has invalid fields "
            f"(missing={missing}, unexpected={unexpected})",
        )
    timestamp = record["ts"]
    if (
        not isinstance(timestamp, (int, float))
        or isinstance(timestamp, bool)
        or not math.isfinite(timestamp)
        or timestamp <= 0
    ):
        return INVALID, None, f"provider ledger record {index} has invalid ts"
    host = record["host"]
    if not isinstance(host, str) or not host or host != host.strip().lower():
        return INVALID, None, f"provider ledger record {index} has invalid host"
    model = record["model"]
    if not isinstance(model, str) or not model or model != model.strip():
        return INVALID, None, f"provider ledger record {index} has invalid model"
    has_tools = record["has_tools"]
    if not isinstance(has_tools, bool):
        return INVALID, None, f"provider ledger record {index} has invalid has_tools"
    conversation_id = record["conversation_id"]
    if conversation_id is not None and (
        not isinstance(conversation_id, str)
        or not conversation_id
        or conversation_id != conversation_id.strip()
    ):
        return INVALID, None, f"provider ledger record {index} has invalid conversation_id"
    for label in _PROVIDER_LEDGER_OPTIONAL_KEYS & keys:
        value = record[label]
        if not isinstance(value, str) or _PROVIDER_LEDGER_LABEL_RE.fullmatch(value) is None:
            return INVALID, None, f"provider ledger record {index} has invalid {label}"
    return PASS, (host, model, has_tools, conversation_id), ""


def _provider_conversation_manifest_result(
    path: Path,
) -> tuple[str, frozenset[str], str]:
    """Parse the runner-owned exact provider conversation scope manifest."""

    empty: frozenset[str] = frozenset()
    if not path.is_absolute():
        return INVALID, empty, "provider conversation manifest path is not absolute"
    if not path.is_file():
        return INVALID, empty, "provider conversation manifest was not produced"
    try:
        manifest = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return (
            INVALID,
            empty,
            f"provider conversation manifest is unreadable: {type(exc).__name__}",
        )
    if not isinstance(manifest, dict):
        return INVALID, empty, "provider conversation manifest must be an object"
    expected_keys = {"schema_version", "conversation_ids"}
    if set(manifest) != expected_keys:
        return (
            INVALID,
            empty,
            "provider conversation manifest must contain exactly "
            "schema_version and conversation_ids",
        )
    if manifest.get("schema_version") != 1 or isinstance(manifest.get("schema_version"), bool):
        return INVALID, empty, "provider conversation manifest schema_version must be 1"
    raw_ids = manifest.get("conversation_ids")
    if not isinstance(raw_ids, list) or not raw_ids:
        return (
            INVALID,
            empty,
            "provider conversation manifest conversation_ids must be a nonempty list",
        )
    conversation_ids: list[str] = []
    for index, value in enumerate(raw_ids):
        if not isinstance(value, str) or not value.strip() or value != value.strip():
            return (
                INVALID,
                empty,
                f"provider conversation manifest conversation_ids[{index}] "
                "must be an exact nonempty string",
            )
        conversation_ids.append(value)
    if len(set(conversation_ids)) != len(conversation_ids):
        return INVALID, empty, "provider conversation manifest contains duplicate IDs"
    parsed = frozenset(conversation_ids)
    return PASS, parsed, f"provider conversation manifest declares {len(parsed)} ID(s)"


def _fresh_device_result(path: Path, *, exit_code: int, units: int) -> tuple[str, int, str]:
    if not path.is_file():
        return INFRA, 0, "fresh-device harness produced no result evidence"
    try:
        report = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        return INFRA, 0, f"fresh-device result is unreadable: {exc}"
    status = report.get("status")
    if status == FAIL:
        return FAIL, 0, str(report.get("reason") or "fresh-device product check failed")
    if status != PASS or exit_code != 0:
        return INFRA, 0, str(report.get("reason") or f"fresh-device exit {exit_code}")
    fingerprint = report.get("device_fingerprint")
    if not isinstance(fingerprint, str) or re.fullmatch(r"[0-9a-f]{24}", fingerprint) is None:
        return INVALID, 0, "fresh-device evidence has no valid machine fingerprint"
    checks = report.get("checks") or []
    if not checks or any(check.get("status") != PASS for check in checks):
        return INVALID, 0, "fresh-device evidence has missing or non-passing checks"
    return PASS, units, f"{len(checks)} fresh-device checks passed"


def _fresh_device_fingerprint(path: Path) -> str | None:
    """Return only a harness-produced hardware identity, never an operator label."""

    try:
        report = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None
    fingerprint = report.get("device_fingerprint")
    if isinstance(fingerprint, str) and re.fullmatch(r"[0-9a-f]{24}", fingerprint):
        return fingerprint
    return None


async def _terminate_process_group(process: asyncio.subprocess.Process) -> None:
    if process.returncode is not None:
        return
    with contextlib.suppress(ProcessLookupError):
        os.killpg(process.pid, signal.SIGTERM)
    try:
        await asyncio.wait_for(process.wait(), timeout=10)
        return
    except TimeoutError:
        pass
    with contextlib.suppress(ProcessLookupError):
        os.killpg(process.pid, signal.SIGKILL)
    await process.wait()


def _suite_subprocess_environment(kind: str) -> dict[str, str]:
    env = os.environ.copy()
    if kind == "playwright":
        # Playwright deliberately sets FORCE_COLOR for its web server and
        # workers. Inheriting NO_COLOR as well makes Node warn on every child
        # process, so let Playwright own color policy for Playwright suites.
        env.pop("NO_COLOR", None)
    return env


def _structured_command(
    suite: Suite, command: list[str], suite_out: Path, env: dict[str, str]
) -> tuple[list[str], Path | None]:
    if any(Path(part).name in {"pytest", "pytest.exe"} for part in command):
        report = suite_out / "pytest.xml"
        command.append(f"--junitxml={report}")
        return command, report
    if suite.kind == "playwright":
        report = suite_out / "playwright.json"
        env["PLAYWRIGHT_JSON_OUTPUT_FILE"] = str(report)
        return command, report
    if suite.kind == "vitest":
        return command, suite_out / "vitest.json"
    if suite.kind == "fresh_device":
        return command, suite_out / "fresh-device-result.json"
    return command, None


def _configure_provider_evidence_environment(
    suite: Suite,
    suite_out: Path,
    env: dict[str, str],
) -> tuple[Path, Path | None]:
    """Own suite-private provider evidence paths and reject ambient manifests."""

    provider_ledger_path = (suite_out / "provider-ledger.jsonl").resolve()
    env.pop("DISCO_PROVIDER_LEDGER", None)
    env.pop(_PROVIDER_CONVERSATION_MANIFEST_ENV, None)
    conversation_manifest_path: Path | None = None
    if suite.provider_evidence:
        # Never inherit or share an ambient ledger across parallel suites. Each
        # campaign-owned stack writes an isolated, auditable call stream.
        env["DISCO_PROVIDER_LEDGER"] = str(provider_ledger_path)
    if suite.provider_conversation_manifest:
        conversation_manifest_path = (suite_out / "provider-conversations.json").resolve()
        env[_PROVIDER_CONVERSATION_MANIFEST_ENV] = str(conversation_manifest_path)
    return provider_ledger_path, conversation_manifest_path


async def _run_suite(
    suite: Suite,
    *,
    context: dict[str, str],
    campaign_out: Path,
    pool: WeightedSuitePool,
) -> dict[str, Any]:
    started_at = _utc_now()
    suite_out = campaign_out / suite.id
    suite_out.mkdir(parents=True, exist_ok=False)
    provider_env = (
        (_EXPECTED_PROVIDER_HOST_ENV, _EXPECTED_PROVIDER_MODEL_ENV)
        if suite.provider_evidence
        else ()
    )
    missing_env = [
        name for name in (*suite.requires_env, *provider_env) if not os.environ.get(name)
    ]
    base = {
        "suite_id": suite.id,
        "proof": suite.proof,
        "kind": suite.kind,
        "claims": list(suite.claims),
        "units_expected": suite.units,
        "units_passed": 0,
        "started_at": started_at,
        "output_dir": str(suite_out),
    }
    if missing_env:
        return {
            **base,
            "status": INVALID,
            "reason": f"missing required environment: {', '.join(missing_env)}",
            "finished_at": _utc_now(),
        }

    suite_context = {**context, "suite_out": str(suite_out)}
    try:
        cwd = Path(_expand(suite.cwd, suite_context))
        command = [_expand(part, suite_context) for part in suite.command]
    except ValueError as exc:
        return {**base, "status": INVALID, "reason": str(exc), "finished_at": _utc_now()}
    if not cwd.is_dir():
        return {
            **base,
            "status": INVALID,
            "reason": f"suite working directory does not exist: {cwd}",
            "finished_at": _utc_now(),
        }

    env = _suite_subprocess_environment(suite.kind)
    try:
        env.update({key: _expand(value, suite_context) for key, value in suite.environment.items()})
    except ValueError as exc:
        return {**base, "status": INVALID, "reason": str(exc), "finished_at": _utc_now()}
    env["DISCO_RELIABILITY_SUITE_OUT"] = str(suite_out)
    provider_ledger_path, provider_conversation_manifest_path = (
        _configure_provider_evidence_environment(suite, suite_out, env)
    )
    command, structured_path = _structured_command(suite, command, suite_out, env)
    log_path = suite_out / "suite.log"
    exit_code = -1
    timed_out = False
    try:
        async with pool.slot(int(suite.memory_gib * GIB)):
            print(f"[reliability] START {suite.id}: {' '.join(command)}")
            with log_path.open("wb") as log:
                process = await asyncio.create_subprocess_exec(
                    *command,
                    cwd=cwd,
                    env=env,
                    stdout=log,
                    stderr=asyncio.subprocess.STDOUT,
                    start_new_session=True,
                )
                try:
                    exit_code = await asyncio.wait_for(process.wait(), timeout=suite.timeout_s)
                except TimeoutError:
                    timed_out = True
                    await _terminate_process_group(process)
    except (OSError, RuntimeError, TimeoutError) as exc:
        result = {
            **base,
            "status": INFRA,
            "reason": f"suite could not run: {type(exc).__name__}: {exc}",
            "finished_at": _utc_now(),
            "command": command,
            "log_path": str(log_path),
        }
        print(f"[reliability] INFRA {suite.id}: {result['reason']}")
        return result

    if timed_out:
        status, units_passed, reason = (
            INFRA,
            0,
            f"suite process exceeded its {suite.timeout_s:g}s outer timeout",
        )
    elif structured_path and structured_path.name == "pytest.xml":
        status, units_passed, reason = _pytest_result(
            structured_path, exit_code=exit_code, units=suite.units
        )
    elif suite.kind == "playwright":
        status, units_passed, reason = _playwright_result(
            structured_path or Path(), exit_code=exit_code, units=suite.units
        )
    elif suite.kind == "vitest":
        status, units_passed, reason = _vitest_result(
            structured_path or Path(), exit_code=exit_code, units=suite.units
        )
    elif suite.kind == "build_soak":
        status, units_passed, reason = _build_soak_result(
            suite_out, exit_code=exit_code, units=suite.units
        )
    elif suite.kind == "fresh_device":
        status, units_passed, reason = _fresh_device_result(
            structured_path or Path(), exit_code=exit_code, units=suite.units
        )
    elif exit_code == 0:
        status, units_passed, reason = PASS, suite.units, "command completed successfully"
    else:
        status, units_passed, reason = FAIL, 0, f"command exited {exit_code}"

    provider_evidence: dict[str, Any] | None = None
    if suite.provider_evidence:
        provider_status, provider_units, provider_reason = _provider_evidence_result(
            provider_ledger_path,
            expected_host=os.environ.get(_EXPECTED_PROVIDER_HOST_ENV, ""),
            expected_model=os.environ.get(_EXPECTED_PROVIDER_MODEL_ENV, ""),
            units=suite.units,
            conversation_manifest_path=provider_conversation_manifest_path,
        )
        provider_evidence = {
            "status": provider_status,
            "reason": provider_reason,
            "units_passed": provider_units,
            "path": str(provider_ledger_path),
        }
        if provider_conversation_manifest_path is not None:
            provider_evidence["conversation_manifest_path"] = str(
                provider_conversation_manifest_path
            )
        if status == PASS and provider_status != PASS:
            status, units_passed, reason = provider_status, 0, provider_reason
        elif status != PASS and provider_status != PASS:
            reason = f"{reason}; provider evidence: {provider_reason}"

    result = {
        **base,
        "status": status,
        "reason": reason,
        "units_passed": units_passed,
        "finished_at": _utc_now(),
        "exit_code": exit_code,
        "command": command,
        "log_path": str(log_path),
    }
    if provider_evidence is not None:
        result["provider_evidence"] = provider_evidence
    if suite.proof == "fresh_device":
        result["fresh_device_id"] = _fresh_device_fingerprint(structured_path or Path())
    print(f"[reliability] {status} {suite.id}: {reason}")
    return result


def _selection_claims(suites: list[Suite]) -> set[str]:
    return {claim_id for suite in suites for claim_id in suite.claims}


def _print_matrix(matrix: ReliabilityMatrix, suites: list[Suite]) -> None:
    for suite in suites:
        claims = ", ".join(suite.claims)
        print(
            f"{suite.id:32} {suite.proof:12} {suite.kind:12} "
            f"units={suite.units:<4} memory={suite.memory_gib:g}GiB  {claims}"
        )


async def _amain(args: argparse.Namespace) -> int:
    repo = Path(__file__).resolve().parents[2]
    frontend = repo / "frontend"
    configured_python = (
        args.python
        or os.environ.get("DISCO_RELIABILITY_PYTHON")
        or os.environ.get("PMX_VENV_PY")
        or str(repo / ".venv" / "bin" / "python3")
    )
    project_python = Path(configured_python).expanduser()
    if not project_python.is_absolute():
        project_python = (repo / project_python).resolve()
    if not project_python.is_file() or not os.access(project_python, os.X_OK):
        print(
            "reliability campaign requires the repository Python environment; "
            f"not executable: {project_python}",
            file=sys.stderr,
        )
        return 2
    matrix = load_matrix(args.matrix)
    proofs = _split_values(args.proof)
    surfaces = _split_values(args.surface)
    suite_ids = _split_values(args.suite)
    if proofs:
        unknown = proofs - PROOFS
        if unknown:
            print(f"unknown proof type(s): {sorted(unknown)}", file=sys.stderr)
            return 2
    if surfaces:
        known_surfaces = {claim.surface for claim in matrix.claims.values()} | {"all"}
        unknown = surfaces - known_surfaces
        if unknown:
            print(f"unknown surface(s): {sorted(unknown)}", file=sys.stderr)
            return 2
        # The public CLI documents ``all`` as the wildcard. Do not pass it to
        # matrix intersection as a literal claim surface: that silently selects
        # only suites carrying an ``all`` claim and can produce a false full gate.
        if "all" in surfaces:
            surfaces = None
    if suite_ids:
        unknown = suite_ids - set(matrix.suites)
        if unknown:
            print(f"unknown suite id(s): {sorted(unknown)}", file=sys.stderr)
            return 2
    suites = matrix.select_suites(proofs=proofs, surfaces=surfaces, suite_ids=suite_ids)
    if not suites:
        print("selection matched no reliability suites", file=sys.stderr)
        return 2
    if args.list or args.dry_run:
        _print_matrix(matrix, suites)
        return 0

    revision, commit, dirty = source_revision(repo)
    if dirty and any(suite.proof == "fresh_device" for suite in suites):
        print(
            "fresh-device proof cannot certify an uncommitted source tree; commit the "
            "candidate so every clean machine can clone the exact recorded revision",
            file=sys.stderr,
        )
        return 2
    stamp = datetime.now(UTC).strftime("%Y%m%d_%H%M%S_%f")
    campaign_id = f"campaign_{stamp}_{revision[-12:].replace('.', '_')}"
    out_root = Path(args.out).expanduser().resolve()
    campaign_out = out_root / campaign_id
    campaign_out.mkdir(parents=True, exist_ok=False)
    started_at = _utc_now()
    initial = read_host_resources(disk_path=campaign_out)
    max_parallel = (
        min(len(suites), initial.cpu_count)
        if args.parallel_suites == "auto"
        else int(args.parallel_suites)
    )
    if max_parallel <= 0:
        print("--parallel-suites must be 'auto' or a positive integer", file=sys.stderr)
        return 2
    pool = WeightedSuitePool(
        disk_path=campaign_out,
        memory_reserve_bytes=int(args.memory_reserve_gib * GIB),
        disk_reserve_bytes=int(args.disk_reserve_gib * GIB),
        max_parallel=max_parallel,
        poll_s=args.resource_poll,
        wait_timeout_s=args.resource_wait_timeout,
    )
    print(
        f"[reliability] campaign {campaign_id}: {len(suites)} suite(s), up to "
        f"{max_parallel} concurrently; protecting {args.memory_reserve_gib:g} GiB RAM"
    )
    context = {
        "repo": str(repo),
        "frontend": str(frontend),
        "python": str(project_python),
        "commit": commit,
        "revision": revision,
    }
    results = await asyncio.gather(
        *(
            _run_suite(suite, context=context, campaign_out=campaign_out, pool=pool)
            for suite in suites
        )
    )
    finished_at = _utc_now()

    final_revision, _, _ = source_revision(repo)
    if final_revision != revision:
        for result in results:
            if result["status"] == PASS:
                result["status"] = INVALID
                result["units_passed"] = 0
                result["reason"] = "source tree changed during the campaign"

    state_path = Path(args.state).expanduser().resolve()
    with state_transaction(state_path) as state:
        record_campaign(
            state,
            campaign_id=campaign_id,
            revision=revision,
            commit=commit,
            dirty=dirty,
            started_at=started_at,
            finished_at=finished_at,
            results=results,
        )
        promotion = promotion_report(
            state, matrix, revision=revision, claim_ids=_selection_claims(suites)
        )
    report = {
        "schema_version": 1,
        "campaign_id": campaign_id,
        "revision": revision,
        "commit": commit,
        "dirty": dirty,
        "source_changed_during_campaign": final_revision != revision,
        "started_at": started_at,
        "finished_at": finished_at,
        "resource_policy": {
            "memory_reserve_gib": args.memory_reserve_gib,
            "disk_reserve_gib": args.disk_reserve_gib,
            "max_parallel_suites": max_parallel,
        },
        "results": results,
        "promotion": promotion,
    }
    report_path = campaign_out / "campaign-summary.json"
    report_path.write_text(json.dumps(report, indent=2, sort_keys=True), encoding="utf-8")
    counts = Counter(result["status"] for result in results)
    print(f"[reliability] report -> {report_path}")
    print(
        "[reliability] "
        + ", ".join(f"{status}={count}" for status, count in sorted(counts.items()))
        + f"; selected claims promoted={promotion['eligible']}"
    )
    if any(result["status"] != PASS for result in results):
        return 1
    if args.require_promotion and not promotion["eligible"]:
        return 4
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run Disco's claim-driven reliability campaign")
    parser.add_argument("--matrix", default=str(DEFAULT_MATRIX))
    parser.add_argument("--proof", action="append", help="hermetic, live, or fresh_device")
    parser.add_argument("--surface", action="append", help="build, agent, search, settings, or all")
    parser.add_argument("--suite", action="append", help="suite id; repeat or comma-separate")
    parser.add_argument("--list", action="store_true", help="list the selected suites")
    parser.add_argument("--dry-run", action="store_true", help="validate and print without running")
    parser.add_argument("--parallel-suites", default="auto")
    parser.add_argument(
        "--python",
        help="repository Python executable (default: .venv/bin/python3)",
    )
    parser.add_argument("--memory-reserve-gib", type=float, default=32.0)
    parser.add_argument("--disk-reserve-gib", type=float, default=10.0)
    parser.add_argument("--resource-poll", type=float, default=5.0)
    parser.add_argument("--resource-wait-timeout", type=float, default=1800.0)
    parser.add_argument(
        "--out", default=str(Path.home() / ".local/state/disco/reliability/campaigns")
    )
    parser.add_argument(
        "--state", default=str(Path.home() / ".local/state/disco/reliability/state.json")
    )
    parser.add_argument("--require-promotion", action="store_true")
    args = parser.parse_args(argv)
    try:
        if args.memory_reserve_gib <= 0 or args.disk_reserve_gib <= 0:
            raise ValueError("resource reserves must be positive")
        if args.resource_poll <= 0 or args.resource_wait_timeout <= 0:
            raise ValueError("resource timing values must be positive")
        if args.parallel_suites != "auto":
            int(args.parallel_suites)
        return asyncio.run(_amain(args))
    except (ValueError, OSError) as exc:
        print(f"reliability campaign configuration error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
