"""Private implementation parts for the heavy (render-it) artifact validators.

This package is an internal extraction seam: ``heavy_validators.py`` is the
state-free compatibility facade that re-exports the public names, while the
cohesive OPC structural pre-check and the pptx/pdf render validators live in
the private modules here. Nothing outside this package should import the
``_``-prefixed modules directly — they are implementation details.

These validators RENDER the artifact (LibreOffice for decks, poppler for PDFs)
rather than only reading metadata, so they catch corruption a mime/format check
misses. They produce EVIDENCE (a list of problem strings), not verdicts: they
never manufacture or upgrade a typed ``HostVerificationResult``.
"""