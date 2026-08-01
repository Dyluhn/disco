"""Direction data table: the 22 catalog `DesignDirection` records.

Extracted from ``directions`` to keep that module's facade under the module
logical-line budget. The 22 records are themselves partitioned into three
``_catalog_*`` chunks (each independently under the module-size cap) and
concatenated here, in original catalog order, into the single ``DIRECTIONS``
tuple every caller imports.
"""

from __future__ import annotations

from typing import Final

from ..directions import DesignDirection
from ._catalog_a import _CATALOG_A
from ._catalog_b import _CATALOG_B
from ._catalog_c import _CATALOG_C

DIRECTIONS: Final[tuple[DesignDirection, ...]] = (*_CATALOG_A, *_CATALOG_B, *_CATALOG_C)
