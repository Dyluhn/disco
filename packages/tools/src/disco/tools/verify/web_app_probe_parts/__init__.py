"""Private implementation parts for the ``verify_web_app`` structured verdict.

This package is an internal extraction seam: ``web_app_probe.py`` is the
state-free compatibility facade that re-exports the public names, while the
cohesive classification / fingerprint / probe-collection / verdict logic lives
in the private modules here. Nothing outside this package should import the
``_``-prefixed modules directly — they are implementation details.
"""