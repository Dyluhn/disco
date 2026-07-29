"""Cohesive bounded owners for build-soak efficiency observability.

This subpackage splits the formerly monolithic ``efficiency.py`` into focused
owners:

* :mod:`._readers` — evidence readers and helpers.
* :mod:`._record` — the per-run efficiency record builder.
* :mod:`._aggregate` — batch aggregation and baseline comparison.
* :mod:`._report` — human-readable report rendering.
* :mod:`._live` — bounded live progress counters.

``efficiency.py`` remains as a thin compatibility facade re-exporting every
public symbol so existing imports are unchanged.
"""
