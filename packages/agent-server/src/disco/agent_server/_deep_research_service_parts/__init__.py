"""Extraction target for ``DeepResearchService`` (PKG-11-RETRIEVAL wave 1).

Each sibling module here owns one phase-collaborator of the service
(dispatch, plan-proposal, execution, follow-up). The orchestrating sequence
for each phase stays a method on ``DeepResearchService`` itself — only the
branchy/long inner computations move here — so instance-level monkeypatches
(``monkeypatch.setattr(rt.deep_research, "_execute_deep_research", ...)``) and the
module-level ``decompose_query`` patch point in ``deep_research_service.py``
keep resolving exactly as before.
"""

from __future__ import annotations
