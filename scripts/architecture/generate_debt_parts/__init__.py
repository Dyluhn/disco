"""Data tables lifted out of ``generate_debt.py`` to keep it under its own cap.

``generate_debt.py`` enforces the 700-logical-line module budget that the whole
campaign is measured against, and it had five lines of headroom left before
Epic 11. Its four sub-epic seals each register a new resolved-ID set, so the
generator would have breached the very budget it enforces.

Only frozen DATA moved here — no parsing, reconstruction or emission logic
crosses this boundary, so the generator's behavior is unchanged by construction.
Every module in this package is in ``seal.PROTECTED``: a sealed wrapper over
unsealed helpers is the weakening ``seal.py``'s own docstring warns about.
"""
