"""Truth oracles for live Search and Deep Research outputs — compatibility facade.

The actual validation logic now lives in :mod:`._search_oracles`:
* :mod:`._search_oracles._validators` — shared helpers and validators.
* :mod:`._search_oracles._answer` — grounded answer validation.
* :mod:`._search_oracles._report` — report event validation.
* :mod:`._search_oracles._connectivity` — connectivity probing and validation.

This module re-exports every public symbol so existing imports are unchanged.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from ._search_oracles._answer import validate_grounded_answer
from ._search_oracles._connectivity import probe_cited_sources
from ._search_oracles._report import validate_report_event
from ._search_oracles._validators import FAIL, PASS

__all__ = [
    "FAIL",
    "PASS",
    "probe_cited_sources",
    "validate_grounded_answer",
    "validate_report_event",
]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Validate live Search evidence")
    parser.add_argument("kind", choices=("answer", "report"))
    parser.add_argument("input", type=Path)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--allow-corpus-only", action="store_true")
    parser.add_argument("--probe-connectivity", action="store_true")
    parser.add_argument("--probe-timeout", type=float, default=15.0)
    args = parser.parse_args(argv)
    payload = json.loads(args.input.read_text(encoding="utf-8"))
    connectivity = (
        probe_cited_sources(payload, timeout_s=args.probe_timeout)
        if args.probe_connectivity
        else None
    )
    validate = validate_grounded_answer if args.kind == "answer" else validate_report_event
    result = validate(
        payload,
        require_web=not args.allow_corpus_only,
        connectivity=connectivity,
    )
    if connectivity is not None:
        result["connectivity"] = connectivity
    rendered = json.dumps(result, indent=2, sort_keys=True)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(rendered, encoding="utf-8")
    print(rendered)
    return 0 if result["status"] == PASS else 1


if __name__ == "__main__":
    raise SystemExit(main())
