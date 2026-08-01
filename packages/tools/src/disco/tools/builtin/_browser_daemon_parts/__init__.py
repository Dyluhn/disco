"""Interior parts of ``disco.tools.builtin._browser_daemon`` — split out to
keep ``BrowserState``, ``BrowserHandler``, and the module itself under the
architecture size budgets.

``_browser_daemon.py`` remains the single import surface and the sole owner
of every module-level global a test can monkeypatch (``state``,
``WORKSPACE_ROOT``, ``SCREENSHOT_DIR``, ``_live_headed``, ``sync_playwright``,
…). Modules here take those as explicit parameters from the parent's thin
delegating methods rather than importing them back — this avoids a
parent/child import cycle and guarantees the current (possibly monkeypatched)
value is read at call time, not one captured at import time.
"""

from __future__ import annotations
