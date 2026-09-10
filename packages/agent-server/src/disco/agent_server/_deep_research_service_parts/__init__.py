"""Extraction target for ``DeepResearchService`` (PKG-11-RETRIEVAL wave 1).

Each sibling module here owns one phase-collaborator of the service
(dispatch, execution, follow-up, stream). The orchestrating sequence for
each phase stays a method on ``DeepResearchService`` itself — only the
branchy/long inner computations move here — so instance-level monkeypatches
(``monkeypatch.setattr(rt.deep_research, "_execute_deep_research", ...)``)
keep resolving exactly as before. The old ``plan`` sibling (decompose →
PlanEvent → approval) was removed in v2 (PKG-35): Deep Research is gateless.
"""

from __future__ import annotations
