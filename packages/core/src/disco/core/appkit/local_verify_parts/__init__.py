"""Private implementation parts for :mod:`disco.core.appkit.local_verify`.

This package is an internal extraction seam: ``local_verify.py`` remains the
sole state-free public module — every currently-importable name stays defined
at that exact module path with an unchanged signature. The cohesive,
branch-heavy sub-logic those functions delegate to lives here, reached only
through a LOCAL (function-body-scoped) import from the owning
``local_verify.py`` function — never a module-level import — so nothing here
ever appears on ``local_verify``'s public surface.

Nothing outside ``local_verify.py`` should import these `_`-prefixed modules
directly — they are implementation details of the owning functions, not an
independent API.
"""
