"""Implementation modules for :mod:`harness.research_harness`.

Split out of the old monolithic ``research_harness.py`` to clear its module
size cap (>700 logical) and five callables' complexity caps
(``check_invariants``, ``normalize_report``, ``phase_for``,
``Observation._collect``, ``_main_async``, ``ReplayTransport._legacy_projection``
— all were over the mccabe-15 limit).  Each module owns one concern; none of
the split changes behaviour:

- ``_observe``    — request model, redaction, phase classification, Observation
- ``_transports`` — fake / replay / live wire transports
- ``_checks``     — report normalization and the run invariants
- ``_run``        — run/batch orchestration, rendering, artifact writing
- ``_cli``        — argument parsing and the module entrypoint

``research_harness.py`` remains the stable import surface; import the original
names from there, not from these modules.
"""
