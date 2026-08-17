"""Private implementation parts extracted from :mod:`verify_export_track1_closeout`.

This subpackage holds the cohesive private helpers that previously lived in the
single ``verify_export_track1_closeout.py`` module. Nothing here is part of the
public API: ``verify_export_track1_closeout.py`` remains the sole state-free
public compatibility/export facade (its ``main`` retains the exact argv/exit-code
contract) and re-imports these names. External callers must never import from
``closeout_verify_parts`` directly.
"""
