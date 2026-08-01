"""Private implementation parts extracted from :mod:`disco.tools.builtin.design_lint`.

This subpackage holds the cohesive rule-family modules, scanning helpers, and the
pure-engine driver that previously lived in the single ``design_lint.py`` module.
Nothing here is part of the public API: ``design_lint.py`` remains the sole
public compatibility/export facade and re-imports these names. External callers
must never import from ``design_lint_parts`` directly.
"""
