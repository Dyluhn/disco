"""Typed interior parts of the public-API authority.

``public_api.py`` sat at exactly 700 of its 700 logical-line budget and could
not accept one new line, so the member-level transition authority that Epic
10-D adds had nowhere to land.  The module is decomposed here along its real
seams: surface extraction, contract-byte checks, the accepted-authority delta
checks, and the member-level transition record.

Every module in this package carries change-controlled governance bytes and is
therefore a member of ``seal.PROTECTED`` in its own right — a hash-gated
wrapper over unsealed helpers is exactly the weakening ``seal.py``'s own
docstring warns about.

The parent keeps: the three commit/digest constants the adversarial suite
monkeypatches, every function that reads them, and the two public entry
points.  Anything a test resolves through the ``public_api`` module object
stays bound there.
"""

from __future__ import annotations
