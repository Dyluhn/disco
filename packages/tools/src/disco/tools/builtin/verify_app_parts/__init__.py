"""Private implementation parts for the ``verify_web_app`` tool.

This package is an internal extraction seam: ``verify_app.py`` is the
state-free compatibility facade that re-exports the public names and owns the
``VerifyWebAppTool`` class, while the cohesive trusted-component folding,
preview-target detection, game-interaction, and render logic lives in the
private modules here. Nothing outside this package should import the
``_``-prefixed modules directly — they are implementation details.

Tools are evidence producers, not verdict authorities: the output here is
immutable evidence bound for later host verification. The target probes never
manufacture or upgrade a typed ``HostVerificationResult``.
"""