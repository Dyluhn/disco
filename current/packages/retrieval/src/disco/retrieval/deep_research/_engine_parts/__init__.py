"""Private implementation parts for :mod:`..engine` (`DeepResearchRun`).

``engine.py`` remains the sole public compatibility/export facade
(``DeepResearchRun``, ``ReportFromRun``). Nothing here is part of the public
API; external callers must never import from ``_engine_parts`` directly.

Modules:
- :mod:`_report` -- final report data collection (`collect_report_data`):
  cited/reviewed passage split + discovery-hit dedupe.

The v1 probe-pipeline parts (`_plan`, `_drain`, `_adaptive`, `_compiler`,
`_finish`) were removed by the v2 agentic-loop rework (PKG-35): research
control flow now lives in :mod:`..agent`, report writing in :mod:`..writer`.
"""

from __future__ import annotations
