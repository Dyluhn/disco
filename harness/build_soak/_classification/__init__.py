"""Cohesive bounded owners for the deterministic classifier (guidelines §13, §16).

The classifier is a pure function of durable evidence. This subpackage splits the
formerly monolithic ``classify.py`` into focused owners:

* :mod:`._tool_scope_proof` — frozen inspect-trace tool-scope extraction.
* :mod:`._browser_proof` — strict browser-verification path extraction.
* :mod:`._dossier_loader` — frozen run-folder loading and evidence assembly.
* :mod:`._pipeline` — the ordered oracle fold (first-broken-link wins).
* :mod:`._no_fluke` — the §17 no-fluke replay policy.

``classify.py`` remains as a thin compatibility facade re-exporting every public
symbol so existing imports are unchanged.
"""
