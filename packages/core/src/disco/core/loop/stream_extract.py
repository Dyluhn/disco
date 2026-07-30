"""Best-effort extraction of a string field's value out of a *partial* JSON
object — a tool call's `arguments` as it streams in.

Compatibility facade: the implementation now lives in :mod:`partial_stream`.
This module re-exports the public function so existing import paths remain
unchanged.
"""

from __future__ import annotations

from .partial_stream import (
    _ESCAPES as _ESCAPES,
)
from .partial_stream import (
    _WS as _WS,
)
from .partial_stream import (
    extract_partial_string_field as extract_partial_string_field,
)
