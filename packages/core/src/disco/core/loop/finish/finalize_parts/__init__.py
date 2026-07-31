"""Internal decomposition of ``finalize.py``'s ``_FinalizeMixin``.

Every module here owns exactly one cohesive slice of finish-time policy
(verify-receipt reuse eligibility, plan-verifier preflight, seal-gate
evaluation, finish-step verify disposition) as plain functions taking
explicit arguments. Not a public package: only ``..finalize`` imports from
it, and each mixin method that used to hold this logic is now a thin
delegator into the matching module here — the logic itself lives in exactly
one place.
"""

from __future__ import annotations
