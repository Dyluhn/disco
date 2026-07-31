"""Private implementation parts extracted from ``export_track1_candidate_receipt``.

This subpackage holds the cohesive private helpers that previously lived in the
single ``export_track1_candidate_receipt.py`` module. Nothing here is part of a
public API: ``export_track1_candidate_receipt.py`` remains the sole entry point
and argv contract, and re-imports these names so every existing module-level
name still resolves as an attribute of that module. External callers must
never import from ``track1_receipt_parts`` directly.
"""
