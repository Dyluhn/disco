"""Frozen data tables extracted from :mod:`gen_closeout_acceptance_manifest`.

This package holds the cohesive frozen-data module that previously lived inline
in the single ``gen_closeout_acceptance_manifest.py``. Nothing here is part of
the public API: ``gen_closeout_acceptance_manifest.py`` remains the sole
state-free public compatibility/export facade and re-imports these names.
External callers must never import from ``closeout_manifest_parts`` directly.
"""
