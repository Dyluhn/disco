"""Extracted, non-mixin implementation units for the content/DoD finish gates.

Every module here is a leaf: none of them import from `..content_gates` (the
mixin module), so `content_gates.py` can safely import from this package
without a cycle. Pure functions with explicit arguments are preferred over
new mixin/base-class members — see the package-level split rationale in
`..content_gates`.
"""

from __future__ import annotations
