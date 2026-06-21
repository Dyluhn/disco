"""W13 — run the disco-verify live scenarios against a running stack and write dossiers.

Entry point::

    python -m disco.agent_server.verify.scenarios_run [--agent-base URL] [--only ID ...]

Each scenario is driven through the REAL HTTP/WS API (``run_scenario``), waits for a terminal
status, locates the delivered artifacts, runs the W14 validators, and writes a redacted
dossier under ``test-record/disco-verify/``. Exit code is non-zero if any scenario fails — so
this doubles as the nightly-VM gate's W13 step. The stack (app-server + agent-server + a live
driver model) must be reachable; on the VM 201 evidence host that is via the reverse tunnel.
"""

from __future__ import annotations

import argparse
import asyncio
import sys

from .runner import run_scenario
from .scenarios import app_from_build, missing_file_sandbox_error, slides_from_research_report

_SCENARIOS = {
    "slides_from_research_report": slides_from_research_report,
    "missing_file_sandbox_error": missing_file_sandbox_error,
    "app_from_build": app_from_build,
}


async def _run(agent_base: str, only: list[str]) -> int:
    selected = [s for k, s in _SCENARIOS.items() if not only or k in only]
    failures = 0
    for scenario in selected:
        result = await run_scenario(scenario, agent_base=agent_base)
        status = "PASS" if result.passed else "FAIL"
        print(
            f"[{status}] {scenario.id}: terminal={result.terminal_status} "
            f"problems={result.validator_problems} dossier={result.dossier_path}"
        )
        if not result.passed:
            failures += 1
    print(f"\n{len(selected) - failures}/{len(selected)} scenarios passed")
    return 1 if failures else 0


def main() -> None:
    ap = argparse.ArgumentParser(description="Run disco-verify live scenarios (W13).")
    ap.add_argument("--agent-base", default="http://127.0.0.1:8000")
    ap.add_argument("--only", nargs="*", default=[], help="scenario id(s) to run")
    args = ap.parse_args()
    sys.exit(asyncio.run(_run(args.agent_base, args.only)))


if __name__ == "__main__":
    main()
