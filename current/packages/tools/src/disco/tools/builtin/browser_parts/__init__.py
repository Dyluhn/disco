"""Interior parts of ``disco.tools.builtin.browser`` — split out to keep
``BrowserTool`` and the module itself under the architecture size budgets.

Nothing here is part of the public API; ``browser.py`` remains the single
import surface (``BrowserTool``, ``BrowserArgs``, ``BrowserUnavailableError``,
``BROWSER_UNAVAILABLE_MSG``, …). These modules take the constants/callables
they need as explicit parameters from ``browser.py``'s thin delegating
methods, rather than importing back from ``browser.py`` — this avoids a
parent/child import cycle and, for the handful of names a test monkeypatches
on the ``browser`` module object, guarantees the current (possibly patched)
value is read at call time.
"""

from __future__ import annotations
