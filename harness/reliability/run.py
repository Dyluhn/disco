"""Resource-aware, claim-driven reliability campaign runner.

Compatibility entry point; bounded owners live under ``reliability._runner``.
"""

# Imports intentionally follow the direct-script path bootstrap below.
# ruff: noqa: E402

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from harness.reliability._runner.campaign import (
    _amain,
    _print_matrix,
    _selection_claims,
)
from harness.reliability._runner.common import _expand, _split_values, _utc_now
from harness.reliability._runner.provider_evidence import (
    _provider_conversation_manifest_result,
    _provider_evidence_result,
    _provider_ledger_record_result,
    _read_private_regular_evidence_file,
)
from harness.reliability._runner.resource_pool import (
    GIB,
    WeightedSuitePool,
    read_host_resources,
)
from harness.reliability._runner.result_evidence import (
    _build_soak_result,
    _fresh_device_fingerprint,
    _fresh_device_result,
    _playwright_result,
    _playwright_tests,
    _pytest_result,
    _select_build_soak_summary,
    _vitest_result,
)
from harness.reliability._runner.suite_execution import (
    _close_subprocess_reader,
    _configure_provider_evidence_environment,
    _process_group_exists,
    _run_suite,
    _sanitize_suite_log,
    _stream_sanitized_suite_log,
    _structured_command,
    _suite_log_integrity_reason,
    _suite_subprocess_environment,
    _SuiteLogRenderer,
    _terminate_process_group,
    _wait_for_process_leader,
)
from harness.reliability.matrix import PROOFS, ReliabilityMatrix, Suite, load_matrix
from harness.reliability.state import (
    FAIL,
    INFRA,
    INVALID,
    PASS,
    promotion_report,
    record_campaign,
    source_revision,
    state_transaction,
)

DEFAULT_MATRIX = Path(__file__).with_name("matrix.yaml")

__all__ = [
    "FAIL",
    "GIB",
    "INFRA",
    "INVALID",
    "PASS",
    "PROOFS",
    "ReliabilityMatrix",
    "Suite",
    "WeightedSuitePool",
    "_SuiteLogRenderer",
    "_amain",
    "_build_soak_result",
    "_close_subprocess_reader",
    "_configure_provider_evidence_environment",
    "_expand",
    "_fresh_device_fingerprint",
    "_fresh_device_result",
    "_playwright_result",
    "_playwright_tests",
    "_print_matrix",
    "_process_group_exists",
    "_provider_conversation_manifest_result",
    "_provider_evidence_result",
    "_provider_ledger_record_result",
    "_pytest_result",
    "_read_private_regular_evidence_file",
    "_run_suite",
    "_sanitize_suite_log",
    "_select_build_soak_summary",
    "_selection_claims",
    "_split_values",
    "_stream_sanitized_suite_log",
    "_structured_command",
    "_suite_log_integrity_reason",
    "_suite_subprocess_environment",
    "_terminate_process_group",
    "_utc_now",
    "_vitest_result",
    "_wait_for_process_leader",
    "load_matrix",
    "main",
    "promotion_report",
    "read_host_resources",
    "record_campaign",
    "source_revision",
    "state_transaction",
]


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
    parser.add_argument(
        "--seed-base",
        type=int,
        default=None,
        help="nonnegative integer seed base for live-build-shapes (commissioned per campaign)",
    )
    args = parser.parse_args(argv)
    try:
        if args.memory_reserve_gib <= 0 or args.disk_reserve_gib <= 0:
            raise ValueError("resource reserves must be positive")
        if args.resource_poll <= 0 or args.resource_wait_timeout <= 0:
            raise ValueError("resource timing values must be positive")
        if args.parallel_suites != "auto":
            int(args.parallel_suites)
        if args.seed_base is not None and args.seed_base < 0:
            raise ValueError("--seed-base must be nonnegative")
        return asyncio.run(_amain(args))
    except (ValueError, OSError) as exc:
        print(f"reliability campaign configuration error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
