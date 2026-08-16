from __future__ import annotations

import asyncio
import copy
import json
import os
import sys
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace

import pytest

from harness.build_soak.resources import GIB, HostResources
from harness.reliability._runner import resource_pool
from harness.reliability._runner.suite_execution import _adjudicate_provider_evidence
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
    _run_suite,
    _sanitize_suite_log,
    _stream_sanitized_suite_log,
    _suite_subprocess_environment,
)
from harness.reliability.state import FAIL, INFRA, INVALID, PASS


class _ImmediatePool:
    @asynccontextmanager
    async def slot(self, _memory_bytes: int):
        yield None


@pytest.mark.asyncio
async def test_suite_pool_waits_when_only_current_memory_pressure_blocks_fit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    low = HostResources(128 * GIB, 40 * GIB, 16, 100 * GIB)
    recovered = HostResources(128 * GIB, 60 * GIB, 16, 100 * GIB)
    readings = iter((low, recovered))
    monkeypatch.setattr(resource_pool, "read_host_resources", lambda **_kwargs: next(readings))

    pool = resource_pool.WeightedSuitePool(
        disk_path=tmp_path,
        memory_reserve_bytes=32 * GIB,
        disk_reserve_bytes=10 * GIB,
        max_parallel=1,
        poll_s=0.001,
        wait_timeout_s=1,
    )

    async with pool.slot(18 * GIB) as admitted:
        assert admitted == recovered


def _generic_suite(suite_id: str, command: tuple[str, ...], cwd: Path) -> Suite:
    return Suite(
        id=suite_id,
        proof="hermetic",
        kind="generic",
        cwd=str(cwd),
        timeout_s=30,
        memory_gib=1,
        units=1,
        command=command,
        claims=("claim",),
        requires_env=(),
        environment={},
        provider_evidence=False,
        provider_conversation_manifest=False,
        provider_conversation_count=None,
    )


def test_playwright_environment_drops_conflicting_no_color(
    monkeypatch,
) -> None:
    monkeypatch.setenv("NO_COLOR", "1")

    assert "NO_COLOR" not in _suite_subprocess_environment("playwright")
    assert _suite_subprocess_environment("generic")["NO_COLOR"] == "1"


def test_suite_log_canonicalizes_terminal_and_binary_controls_atomically(
    tmp_path: Path,
) -> None:
    path = tmp_path / "suite.log"
    prefix = b"a" * (64 * 1024 - 1)
    path.write_bytes(
        prefix
        + "Ж🙂".encode()
        + b" plain\x1b[31mred\x1b[0m\x00tail\tkept\r\n"
        + b"bare\roverwrite\x7f\x9b\xc2\x9b\xff"
        + "\u202espoof".encode()
    )

    _sanitize_suite_log(path)

    rendered = path.read_bytes()
    assert rendered.startswith(prefix + "Ж🙂".encode())
    assert b"plain\\x1b[31mred\\x1b[0m\\x00tail\tkept\r\n" in rendered
    assert b"bare\\x0doverwrite\\x7f\\x9b\\x9b\\xff\\u202espoof" in rendered
    assert rendered.decode("utf-8")
    assert b"\x1b" not in rendered
    assert b"\x00" not in rendered
    assert b"\x7f" not in rendered
    assert b"\x9b" not in rendered
    assert path.stat().st_mode & 0o777 == 0o600
    assert list(tmp_path.glob(".suite.log.sanitize-*")) == []


@pytest.mark.asyncio
async def test_suite_log_is_sanitized_before_the_child_stream_finishes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "suite.log"
    reader = asyncio.StreamReader()

    def _forbid_path_chmod(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("suite log permissions must be bound to the open descriptor")

    monkeypatch.setattr(os, "chmod", _forbid_path_chmod)
    task = asyncio.create_task(_stream_sanitized_suite_log(reader, path))

    reader.feed_data(b"running\x1b[31m\rrewrite")
    await asyncio.sleep(0)

    assert path.read_bytes() == b"running\\x1b[31m\\x0drewrite"
    reader.feed_eof()
    await task
    assert path.stat().st_mode & 0o777 == 0o600


@pytest.mark.asyncio
async def test_suite_log_descendant_pipe_holder_is_bounded_and_killed(tmp_path: Path) -> None:
    child_pid = tmp_path / "child.pid"
    code = (
        "import pathlib,subprocess,sys; "
        "child=subprocess.Popen([sys.executable,'-c','import time; time.sleep(30)'], "
        "stdout=sys.stdout,stderr=sys.stderr); "
        f"pathlib.Path({str(child_pid)!r}).write_text(str(child.pid)); "
        "print('leader exited')"
    )
    campaign_out = tmp_path / "campaign"
    campaign_out.mkdir()

    result = await _run_suite(
        _generic_suite("pipe-holder", (sys.executable, "-c", code), tmp_path),
        context={},
        campaign_out=campaign_out,
        pool=_ImmediatePool(),  # type: ignore[arg-type]
    )

    assert result["status"] == "INFRA"
    assert "descendant retained the output pipe" in result["reason"]
    pid = int(child_pid.read_text())
    with pytest.raises(ProcessLookupError):
        os.kill(pid, 0)


@pytest.mark.asyncio
async def test_suite_log_path_replacement_is_infra_not_retained_evidence(tmp_path: Path) -> None:
    replacement = tmp_path / "replacement.log"
    replacement.write_text("untrusted replacement", encoding="utf-8")
    code = (
        "import os,pathlib,time\n"
        "log=pathlib.Path(os.environ['DISCO_RELIABILITY_SUITE_OUT'])/'suite.log'\n"
        "deadline=time.monotonic()+2\n"
        "while not log.exists() and time.monotonic()<deadline:\n"
        "    time.sleep(0.01)\n"
        "if not log.exists():\n"
        "    raise RuntimeError('suite log was never created')\n"
        "log.unlink()\n"
        f"log.symlink_to({str(replacement)!r})\n"
        "print('leader exited')\n"
    )
    campaign_out = tmp_path / "campaign"
    campaign_out.mkdir()

    result = await _run_suite(
        _generic_suite("log-swap", (sys.executable, "-c", code), tmp_path),
        context={},
        campaign_out=campaign_out,
        pool=_ImmediatePool(),  # type: ignore[arg-type]
    )

    assert result["status"] == "INFRA"
    assert "suite log" in result["reason"]
    assert "streamed evidence inode" in result["reason"] or "aliased path" in result["reason"]


@pytest.mark.asyncio
async def test_suite_success_with_silent_background_descendant_is_infra_and_killed(
    tmp_path: Path,
) -> None:
    child_pid = tmp_path / "silent-child.pid"
    code = (
        "import pathlib,subprocess,sys; "
        "child=subprocess.Popen([sys.executable,'-c','import time; time.sleep(30)'], "
        "stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL); "
        f"pathlib.Path({str(child_pid)!r}).write_text(str(child.pid)); "
        "print('leader exited')"
    )
    campaign_out = tmp_path / "campaign"
    campaign_out.mkdir()

    result = await _run_suite(
        _generic_suite("silent-descendant", (sys.executable, "-c", code), tmp_path),
        context={},
        campaign_out=campaign_out,
        pool=_ImmediatePool(),  # type: ignore[arg-type]
    )

    assert result["status"] == "INFRA"
    assert "descendant process group remained alive" in result["reason"]
    pid = int(child_pid.read_text())
    with pytest.raises(ProcessLookupError):
        os.kill(pid, 0)


@pytest.mark.asyncio
async def test_suite_cancellation_kills_process_group_and_keeps_safe_log(tmp_path: Path) -> None:
    child_pid = tmp_path / "leader.pid"
    code = (
        "import os,pathlib,time; "
        f"pathlib.Path({str(child_pid)!r}).write_text(str(os.getpid())); "
        "print('running\\x1b[31m', flush=True); time.sleep(30)"
    )
    campaign_out = tmp_path / "campaign"
    campaign_out.mkdir()
    task = asyncio.create_task(
        _run_suite(
            _generic_suite("cancelled", (sys.executable, "-c", code), tmp_path),
            context={},
            campaign_out=campaign_out,
            pool=_ImmediatePool(),  # type: ignore[arg-type]
        )
    )
    for _ in range(200):
        retained = campaign_out / "cancelled" / "suite.log"
        if child_pid.exists() and retained.exists() and b"\\x1b" in retained.read_bytes():
            break
        await asyncio.sleep(0.01)
    assert child_pid.exists()
    assert b"\\x1b" in retained.read_bytes()

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    pid = int(child_pid.read_text())
    with pytest.raises(ProcessLookupError):
        os.kill(pid, 0)
    assert b"\x1b" not in retained.read_bytes()
    assert b"\\x1b" in retained.read_bytes()


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
    path.chmod(0o600)


def _write_provider_manifest(path: Path, conversation_ids: list[object]) -> None:
    path.write_text(
        json.dumps({"schema_version": 1, "conversation_ids": conversation_ids}),
        encoding="utf-8",
    )
    path.chmod(0o600)


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
        provider_conversation_count=2 if conversation_manifest else None,
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


def test_provider_evidence_can_prove_pass_subset_without_rejecting_invalid_cell(
    tmp_path: Path,
) -> None:
    path = tmp_path / "provider.jsonl"
    _write_provider_ledger(
        path,
        [
            {
                "host": "opencode.ai",
                "model": "deepseek-v4-flash",
                "conversation_id": conversation_id,
            }
            for conversation_id in ("conv_pass", "conv_invalid")
        ],
    )

    assert _provider_evidence_result(
        path,
        expected_host="opencode.ai",
        expected_model="deepseek-v4-flash",
        units=1,
        required_conversation_ids=frozenset({"conv_pass"}),
    )[:2] == (PASS, 1)

    status, units, reason = _provider_evidence_result(
        path,
        expected_host="opencode.ai",
        expected_model="deepseek-v4-flash",
        units=1,
        required_conversation_ids=frozenset({"conv_missing"}),
    )
    assert (status, units) == (INVALID, 0)
    assert "missing 1 required PASS conversation" in reason


def test_provider_fallback_taints_even_when_suite_was_already_invalid(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ledger = tmp_path / "provider.jsonl"
    _write_provider_ledger(
        ledger,
        [
            {
                "host": "fallback.example",
                "model": "other",
                "conversation_id": "conv_1",
            }
        ],
    )
    monkeypatch.setenv("DISCO_RELIABILITY_EXPECTED_PROVIDER_HOST", "opencode.ai")
    monkeypatch.setenv("DISCO_RELIABILITY_EXPECTED_PROVIDER_MODEL", "deepseek-v4-flash")
    suite = _provider_suite(conversation_manifest=False)
    launch = SimpleNamespace(
        provider_ledger_path=ledger,
        provider_conversation_manifest_path=None,
    )

    status, units, _, _ = _adjudicate_provider_evidence(
        suite, launch, INVALID, 0, "harness evidence invalid"
    )

    assert (status, units) == (FAIL, 0)


def test_provider_evidence_accepts_complete_sanitized_request_shape(tmp_path: Path) -> None:
    path = tmp_path / "provider.jsonl"
    shape = {
        "request_id": "req_0123456789abcdef0123456789abcdef",
        "driver_context_window": 128000,
        "model_repair_attempt": 1,
        "stream": False,
        "max_output_tokens": None,
        "canonical_payload_bytes": 500,
        "messages_json_bytes": 200,
        "tools_json_bytes": 250,
        "message_count": 3,
        "tool_count": 2,
        "system_message_count": 1,
        "user_message_count": 1,
        "assistant_message_count": 1,
        "tool_message_count": 0,
        "image_count": 0,
        "image_url_chars": 0,
    }
    _write_provider_ledger(
        path,
        [
            {
                "host": "opencode.ai",
                "model": "deepseek-v4-flash",
                "conversation_id": "conv_1",
                **shape,
            }
        ],
    )

    assert _provider_evidence_result(
        path,
        expected_host="opencode.ai",
        expected_model="deepseek-v4-flash",
        units=1,
    )[:2] == (PASS, 1)


def test_provider_evidence_rejects_partial_request_shape(tmp_path: Path) -> None:
    path = tmp_path / "provider.jsonl"
    _write_provider_ledger(
        path,
        [
            {
                "host": "opencode.ai",
                "model": "deepseek-v4-flash",
                "conversation_id": "conv_1",
                "request_id": "req_0123456789abcdef0123456789abcdef",
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
    assert "incomplete request shape" in reason


def test_provider_ledger_requires_private_regular_file(tmp_path: Path) -> None:
    path = tmp_path / "provider.jsonl"
    _write_provider_ledger(
        path,
        [
            {
                "host": "opencode.ai",
                "model": "deepseek-v4-flash",
                "conversation_id": "conv_1",
            }
        ],
    )
    path.chmod(0o644)

    status, units, reason = _provider_evidence_result(
        path,
        expected_host="opencode.ai",
        expected_model="deepseek-v4-flash",
        units=1,
    )

    assert (status, units) == (INVALID, 0)
    assert "permissions must be 0600" in reason


def test_provider_ledger_reads_the_validated_descriptor_across_path_swap(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ledger = tmp_path / "provider.jsonl"
    replacement = tmp_path / "replacement.jsonl"
    _write_provider_ledger(
        ledger,
        [
            {
                "host": "opencode.ai",
                "model": "deepseek-v4-flash",
                "conversation_id": "conv_1",
            }
        ],
    )
    _write_provider_ledger(
        replacement,
        [
            {
                "host": "fallback.example",
                "model": "other",
                "conversation_id": "conv_1",
            }
        ],
    )
    real_open = os.open
    swapped = False

    def _open_and_swap(file: os.PathLike[str] | str, flags: int, *args: int) -> int:
        nonlocal swapped
        descriptor = real_open(file, flags, *args)
        if not swapped and Path(file) == ledger:
            swapped = True
            ledger.unlink()
            ledger.symlink_to(replacement)
        return descriptor

    monkeypatch.setattr(os, "open", _open_and_swap)

    assert _provider_evidence_result(
        ledger,
        expected_host="opencode.ai",
        expected_model="deepseek-v4-flash",
        units=1,
    )[:2] == (PASS, 1)


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
    ledger.chmod(0o600)

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
    manifest.chmod(0o600)

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


def test_provider_manifest_requires_private_regular_file(tmp_path: Path) -> None:
    manifest = tmp_path / "provider-conversations.json"
    _write_provider_manifest(manifest, ["conv_1"])
    manifest.chmod(0o644)

    status, _, reason = _provider_conversation_manifest_result(manifest)
    assert status == INVALID
    assert "permissions must be 0600" in reason

    manifest.unlink()
    target = tmp_path / "target.json"
    _write_provider_manifest(target, ["conv_1"])
    manifest.symlink_to(target)
    status, _, reason = _provider_conversation_manifest_result(manifest)
    assert status == INVALID
    assert "regular non-symlink" in reason


def test_provider_manifest_reads_the_validated_descriptor_across_path_swap(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manifest = tmp_path / "provider-conversations.json"
    replacement = tmp_path / "replacement.json"
    _write_provider_manifest(manifest, ["conv_1"])
    replacement.write_text("not valid manifest JSON", encoding="utf-8")
    replacement.chmod(0o600)
    real_open = os.open
    swapped = False

    def _open_and_swap(file: os.PathLike[str] | str, flags: int, *args: int) -> int:
        nonlocal swapped
        descriptor = real_open(file, flags, *args)
        if not swapped and Path(file) == manifest:
            swapped = True
            manifest.unlink()
            manifest.symlink_to(replacement)
        return descriptor

    monkeypatch.setattr(os, "open", _open_and_swap)

    status, conversation_ids, _ = _provider_conversation_manifest_result(manifest)

    assert status == PASS
    assert conversation_ids == frozenset({"conv_1"})


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
    assert "missing 1 manifest conversation ID" in reason
    assert "conv_agent" not in reason


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
    assert "extra 1 provider-ledger conversation ID" in reason
    assert "conv_unclaimed" not in reason


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
    assert "conv_agent" not in reason


def test_provider_manifest_success_requires_declared_conversation_count(tmp_path: Path) -> None:
    ledger = tmp_path / "provider.jsonl"
    manifest = tmp_path / "provider-conversations.json"
    _write_provider_manifest(manifest, ["conv_build"])
    _write_provider_ledger(
        ledger,
        [
            {
                "host": "opencode.ai",
                "model": "deepseek-v4-flash",
                "conversation_id": "conv_build",
                "has_tools": True,
            }
        ],
    )

    status, units, reason = _provider_evidence_result(
        ledger,
        expected_host="opencode.ai",
        expected_model="deepseek-v4-flash",
        units=1,
        conversation_manifest_path=manifest,
        expected_conversations=2,
    )

    assert (status, units) == (INVALID, 0)
    assert "1/2 required successful-suite conversation" in reason
    assert "conv_build" not in reason


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


def _valid_fresh_device_report() -> dict:
    fingerprint = "0123456789abcdef01234567"
    project_digests = {"project-a": "a" * 64, "project-b": "b" * 64}
    snapshot_project_digests = {
        **project_digests,
        "project-edge": "c" * 64,
    }
    return {
        "status": PASS,
        "device_fingerprint": fingerprint,
        "device_label": "operator-label",
        "checks": [
            {
                "id": "pristine-device",
                "status": PASS,
                "detail": "pristine",
                "evidence": {
                    "device_label": "operator-label",
                    "fingerprint": fingerprint,
                    "engine": "podman",
                    "containers": 0,
                    "images": 0,
                    "volumes": 0,
                    "checked_ports": [8088, 8800, 8000],
                    "cpu_count": 8,
                    "memory_available_bytes": 8_000_000_000,
                    "disk_free_bytes": 20_000_000_000,
                },
            },
            {
                "id": "clean-clone",
                "status": PASS,
                "detail": "cloned",
                "evidence": {"base_commit": "base", "upgrade_commit": "upgrade"},
            },
            {
                "id": "install-and-boot",
                "status": PASS,
                "detail": "installed",
                "evidence": {"ui": "http://127.0.0.1:8088", "image_ids": ["sha256:base"]},
            },
            {
                "id": "first-pair-and-config",
                "status": PASS,
                "detail": "paired",
                "evidence": {},
            },
            {
                "id": "model-and-internet",
                "status": PASS,
                "detail": "verified",
                "evidence": {},
            },
            {
                "id": "build-search-export",
                "status": PASS,
                "detail": "exported",
                "evidence": {"project_count": 2, "project_digests": dict(project_digests)},
            },
            {
                "id": "cold-restart",
                "status": PASS,
                "detail": "restarted",
                "evidence": {
                    "readiness_seconds": 30.0,
                    "data_digest": "c" * 64,
                    "project_digests": dict(project_digests),
                },
            },
            {
                "id": "upgrade-in-place",
                "status": PASS,
                "detail": "upgraded",
                "evidence": {
                    "readiness_seconds": 120.0,
                    "stable_data_digest": "d" * 64,
                    "project_digests": dict(project_digests),
                    "snapshot_project_digests": dict(snapshot_project_digests),
                    "base_commit": "base",
                    "upgrade_commit": "upgrade",
                    "candidate_front_door": {
                        "app": "http://127.0.0.1:8088/svc/app",
                        "agent": "http://127.0.0.1:8088/svc/agent",
                    },
                    "image_ids": ["sha256:base", "sha256:upgrade"],
                },
            },
            {
                "id": "backup-restore",
                "status": PASS,
                "detail": "restored",
                "evidence": {
                    "readiness_seconds": 180.0,
                    "backup_digest": "e" * 64,
                    "project_digests": dict(snapshot_project_digests),
                    "project_count": 3,
                },
            },
            {
                "id": "uninstall",
                "status": PASS,
                "detail": "removed",
                "evidence": {},
            },
        ],
    }


def test_fresh_device_pass_requires_hardware_fingerprint(tmp_path: Path) -> None:
    path = tmp_path / "fresh-device-result.json"
    report = _valid_fresh_device_report()
    report.pop("device_fingerprint")
    path.write_text(json.dumps(report), encoding="utf-8")
    assert _fresh_device_result(path, exit_code=0, units=1)[:2] == (INVALID, 0)
    assert _fresh_device_fingerprint(path) is None

    path.write_text("[]", encoding="utf-8")
    assert _fresh_device_result(path, exit_code=0, units=1)[:2] == (INFRA, 0)
    assert _fresh_device_fingerprint(path) is None


def test_fresh_device_uses_harness_fingerprint_not_label(tmp_path: Path) -> None:
    path = tmp_path / "fresh-device-result.json"
    report = _valid_fresh_device_report()
    report["device_label"] = "same-machine-renamed"
    path.write_text(json.dumps(report), encoding="utf-8")
    assert _fresh_device_result(path, exit_code=0, units=1)[:2] == (PASS, 1)
    assert _fresh_device_fingerprint(path) == "0123456789abcdef01234567"


@pytest.mark.parametrize(
    "mutation",
    [
        "missing-phase",
        "wrong-order",
        "malformed-check",
        "extra-evidence",
        "bad-digest",
        "changed-project",
        "missing-edge-project",
        "extra-snapshot-project",
        "excessive-readiness",
        "invalid-candidate-front-door",
    ],
)
def test_fresh_device_refuses_incomplete_or_malformed_phase_evidence(
    mutation: str, tmp_path: Path
) -> None:
    report = copy.deepcopy(_valid_fresh_device_report())
    checks = report["checks"]
    if mutation == "missing-phase":
        checks.pop()
    elif mutation == "wrong-order":
        checks[0], checks[1] = checks[1], checks[0]
    elif mutation == "malformed-check":
        checks[0] = "not-an-object"
    elif mutation == "extra-evidence":
        checks[3]["evidence"]["invented"] = True
    elif mutation == "bad-digest":
        checks[6]["evidence"]["data_digest"] = "not-a-digest"
    elif mutation == "changed-project":
        checks[8]["evidence"]["project_digests"]["project-b"] = "f" * 64
    elif mutation == "missing-edge-project":
        checks[7]["evidence"]["snapshot_project_digests"].pop("project-edge")
    elif mutation == "extra-snapshot-project":
        checks[7]["evidence"]["snapshot_project_digests"]["project-unbound"] = "f" * 64
    elif mutation == "invalid-candidate-front-door":
        checks[7]["evidence"]["candidate_front_door"]["agent"] = (
            "http://127.0.0.1:9099/svc/agent"
        )
    else:
        checks[7]["evidence"]["readiness_seconds"] = 901
    path = tmp_path / f"fresh-device-{mutation}.json"
    path.write_text(json.dumps(report), encoding="utf-8")

    assert _fresh_device_result(path, exit_code=0, units=1)[:2] == (INVALID, 0)
