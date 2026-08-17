"""Turn a CAPTURED-observations JSON (from the live Playwright product-harness spec) into a
classifiable dossier and classify it — the bridge between the TS capture and the Python
classifier (P1B-LIVE-3b durable automation).

The Playwright spec (`build-artifact-runtime-smoke.spec.ts`) drives/observes a real build and
writes a single capture file::

    {
      "product_evidence": { ...the 6+ slices the UI run observed... },
      "provider_records": [ {"host": "...", "model": "..."}, ... ],
      "events":           [ ...the conversation event log... ],
      "autonomous":       true,            # the disco-kernel default drive mode
      "run_id":           "...", "scenario_id": "static_site_smoke"
    }

This reads that file, writes the dossier (via the existing `write_dossier`, threading
`autonomous`), classifies it against STATIC_SITE_SMOKE, prints the classification JSON, and
exits non-zero unless the status is PASS — so the spec can assert on the process result.
No fabricated evidence: every value comes from the capture file.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

from harness.product_build import (
    EXPORT_SMOKE,
    STATIC_SITE_EDIT_GOVERNED,
    STATIC_SITE_SMOKE,
    STATIC_SITE_SMOKE_DEEPSEEK_DIRECT,
    STATIC_SITE_SMOKE_GOVERNED,
    STATIC_SMOKE_STRICT,
    classify_dossier,
    write_dossier,
)

# The product scenarios this bridge can adjudicate, keyed by id. The capture file names which
# one it ran (`scenario_id`); an UNKNOWN id raises (never silently falls back to a laxer
# scenario — e.g. a typo'd export run must NOT be classified as the no-export static smoke).
_SCENARIOS = {
    s.id: s
    for s in (
        STATIC_SITE_SMOKE,
        STATIC_SITE_SMOKE_DEEPSEEK_DIRECT,
        STATIC_SITE_SMOKE_GOVERNED,
        STATIC_SITE_EDIT_GOVERNED,
        STATIC_SMOKE_STRICT,
        EXPORT_SMOKE,
    )
}


def classify_capture(capture_path: str | Path, dossier_dir: str | Path) -> dict[str, Any]:
    """Read a capture, assemble its dossier, and classify its named scenario.

    Raises KeyError or ValueError if a required field is missing, malformed, or
    names an unknown scenario. Capture bugs must surface loudly, never silently pass.
    """
    cap = json.loads(Path(capture_path).read_text(encoding="utf-8"))
    # The capture MUST name its scenario explicitly. A MISSING (or unknown) scenario_id raises —
    # never default to the laxer static scenario, or an export capture that forgot the field would
    # silently classify with NO export required (a fail-open PASS).
    scenario_id = cap.get("scenario_id")
    scenario = _SCENARIOS.get(scenario_id) if scenario_id else None
    if scenario is None:
        known_scenarios = sorted(_SCENARIOS)
        raise ValueError(
            f"capture must name a known scenario_id; got {scenario_id!r}; known: {known_scenarios}"
        )
    write_dossier(
        dossier_dir,
        product_evidence=cap["product_evidence"],
        provider_records=cap["provider_records"],
        events=cap["events"],
        run_id=cap.get("run_id", ""),
        scenario_id=scenario_id,
        autonomous=bool(cap.get("autonomous", False)),
    )
    return classify_dossier(dossier_dir, scenario)


def main(argv: list[str]) -> int:
    if len(argv) < 3:
        print("usage: classify_captured <capture.json> <dossier_dir>", file=sys.stderr)
        return 2
    result = classify_capture(argv[1], argv[2])
    # stdout is the machine-readable verdict the spec parses; nothing else goes to stdout.
    print(json.dumps(result, sort_keys=True))
    return 0 if result.get("status") == "PASS" else 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main(sys.argv))
