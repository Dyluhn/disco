#!/usr/bin/env python3
"""Out-of-session watchdog. Fires every 15 minutes until the campaign is DONE.

This exists because I stopped working at a clean boundary and narrated it as
progress. It is a catch, not a driver: work must move forward without it. Its
only job is to make an unjustified stop loud and impossible to miss.

It runs detached from the Claude Code session (setsid + nohup), so killing or
compacting the session does not kill it. Every interval it appends a nudge to
`.claude/runtime/watchdog-nudge.json`; `post_tool_review.py` surfaces any
unacknowledged nudge as injected context, which is what actually reaches the
model.

It exits ONLY when docs/governance/CAMPAIGN-STATUS.md truthfully asserts the
completion contract, using the same strict standalone-line check as the Stop
gate (fenced/quoted mentions do not count).
"""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from _governance import (  # noqa: E402
    completion_contract_satisfied,
    runtime_dir,
)

INTERVAL_SECONDS = 15 * 60
NUDGE_FILE = "watchdog-nudge.json"
LOG_FILE = "watchdog.log"


def main() -> int:
    root = Path(os.environ.get("CLAUDE_PROJECT_DIR", Path(__file__).resolve().parents[2]))
    rt = runtime_dir(root)
    nudge_path = rt / NUDGE_FILE
    log_path = rt / LOG_FILE
    started = int(time.time())
    fired = 0

    def log(message: str) -> None:
        with log_path.open("a", encoding="utf-8") as handle:
            handle.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} {message}\n")

    log(f"watchdog started pid={os.getpid()} interval={INTERVAL_SECONDS}s root={root}")

    while True:
        time.sleep(INTERVAL_SECONDS)
        if completion_contract_satisfied(root):
            log("completion contract satisfied — watchdog exiting")
            try:
                nudge_path.unlink()
            except OSError:
                pass
            return 0
        fired += 1
        elapsed = int(time.time()) - started
        payload = {
            "fired_at_epoch": int(time.time()),
            "fired_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            "count": fired,
            "elapsed_seconds": elapsed,
            "acknowledged": False,
            "message": (
                f"WATCHDOG #{fired} ({elapsed // 60} min since start). The campaign "
                "completion contract is NOT satisfied. If you are not actively "
                "executing a campaign action right now, you have stopped without "
                "justification — resume the current epic immediately. Do not write a "
                "status report instead of working."
            ),
        }
        tmp = nudge_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        os.replace(tmp, nudge_path)
        log(f"nudge #{fired} written")


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        raise SystemExit(0) from None
