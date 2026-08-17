"""Extracted implementation units for the ``verify_gates`` mixins.

Every module here holds pure functions (explicit arguments, no new mixins)
owned by one cohesive concern of host/browser/render verification. The
``verify_gates.py`` mixin methods delegate to these — the mixins remain the
composed public surface; the logic lives here exactly once.
"""

from __future__ import annotations
