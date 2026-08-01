"""Private implementation parts for :mod:`disco.core.appkit.worker_inspect`.

This package is an internal extraction seam: ``worker_inspect.py`` remains the
sole state-free public module — every currently-importable name (including the
`_`-prefixed helpers tests reach through or monkeypatch) stays defined at that
exact module path with an unchanged signature. The cohesive, branch-heavy
sub-logic these functions delegate to lives here, reached only through a
LOCAL (function-body-scoped) import from the owning ``worker_inspect.py``
function — never a module-level import — so nothing here ever appears on
``worker_inspect``'s public surface.

Nothing outside ``worker_inspect.py`` should import these `_`-prefixed
modules directly — they are implementation details of the owning functions,
not an independent API.
"""
