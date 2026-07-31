"""Internal decomposition parts for `finish/common.py`.

Every name defined under this subpackage is imported back into
`disco.core.loop.finish.common`'s module globals so the `common.py`
`__all__` re-export surface (consumed via `from .common import *` by
`content_gates.py` and `verify_gates.py`) is unchanged. These modules are
an implementation detail of `common.py` — nothing outside `finish/`
should import from here directly.
"""

from __future__ import annotations
