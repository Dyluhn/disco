"""Private implementation parts extracted from
:mod:`disco.tools.builtin.verify_appkit_app`.

This subpackage holds the collaborators (`SandboxReader`, `PreviewLifecycle`),
the check batteries (`checks.py`), the constants they share, and the sealed
digest computation that previously all lived in the single
`verify_appkit_app.py` module. Nothing here is part of the public API:
`verify_appkit_app.py` remains the sole import surface and re-exports every
name external code/tests require. External callers must never import from
`verify_appkit_parts` directly.
"""

from __future__ import annotations
