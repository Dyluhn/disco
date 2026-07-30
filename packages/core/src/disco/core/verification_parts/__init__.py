"""Private implementation parts extracted from :mod:`disco.core.verification`.

This subpackage holds the cohesive private helpers that previously lived in the
single ``verification.py`` module.  Nothing here is part of the public API:
``verification.py`` remains the sole state-free public compatibility/export
facade and re-imports these names.  External callers must never import from
``verification_parts`` directly.
"""