"""Private implementation parts extracted from :mod:`..engine` (`DeepResearchRun`).

``engine.py`` remains the sole public compatibility/export facade
(``DeepResearchRun``, ``ReportFromRun``) and re-imports what it needs from
these modules. Nothing here is part of the public API; external callers must
never import from ``_engine_parts`` directly.

Every function below that needs the live ``DeepResearchRun`` instance takes
it as an explicit first parameter (named ``run``) rather than being a method
on the class — this is what actually moves authority (and logical lines) out
of the class body, as opposed to a mixin (which would still expose the same
members on the instance and hide nothing from the AST class-size scanner).
The class keeps a thin delegator method at the original name/signature for
each extracted piece, so `DeepResearchRun`'s own call sites (and its `run()`
orchestration) are unchanged.

Modules:
- :mod:`_plan`     -- plan-bounding + gather-task dispatch (`prepare_plan`,
  `start_gather_tasks`, `start_one_steer_task`).
- :mod:`_drain`    -- the research-phase drain loop plus its single-purpose
  helpers (stop-condition check, D3 steer/inject checkpoints, leg-result
  await) and the research wall-clock reserve (`seconds_left`).
- :mod:`_adaptive` -- adaptive probe-batch execution between drains.
- :mod:`_compiler` -- the one post-research report path (outline → sections).
- :mod:`_finish`   -- report finalization (compile → summary → assemble).
- :mod:`_report`   -- final report data collection (`collect_report_data`).
"""

from __future__ import annotations
