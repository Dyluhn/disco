"""Development-harness view of the single-source event-log → cassette projection.

The implementation lives in the installed runtime package
:mod:`disco.core.share_projection` so production share export never imports
this development-only package. This module delegates to it without any
fallback: if the runtime helper is missing, the import fails loudly rather
than silently producing a divergent projection.
"""

from __future__ import annotations

from disco.core.share_projection import (
    _EXTRACT_TOOL_NAMES as _EXTRACT_TOOL_NAMES,
)
from disco.core.share_projection import (
    _LLM_TOOL_NAMES as _LLM_TOOL_NAMES,
)
from disco.core.share_projection import (
    _SEARCH_TOOL_NAMES as _SEARCH_TOOL_NAMES,
)
from disco.core.share_projection import (
    cassette_key as cassette_key,
)
from disco.core.share_projection import (
    project_cassette_rows as project_cassette_rows,
)

__all__ = [
    "cassette_key",
    "project_cassette_rows",
]
