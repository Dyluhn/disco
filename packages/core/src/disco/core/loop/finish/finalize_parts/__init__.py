"""Internal decomposition of ``finalize.py``'s ``_FinalizeMixin``.

Active modules here own cohesive finish-time policy as plain functions taking
explicit arguments. ``plan_verifier_preflight`` is a documented compatibility
exception: model plan conditions are advisory now, so production finish no
longer delegates to it.
"""

from __future__ import annotations
