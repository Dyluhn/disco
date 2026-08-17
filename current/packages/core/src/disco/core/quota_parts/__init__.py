"""Private implementation parts extracted from :mod:`disco.core.quota`.

This subpackage holds the cohesive private collaborators that previously
lived inline in the single ``quota.py`` module: dependency-free leaf identity/
validation helpers and value types (``_types``), connection lifecycle
(``_connection``), ``quota_configs`` CRUD (``_config_store``), and
``quota_reservations`` admission/usage accounting (``_reservation_ledger``).
``_types`` depends on nothing else here; every other module — including
``quota.py`` itself — imports from it, never from each other's parent, so the
import graph has one direction and no cycle. Nothing here is part of the
public API: ``quota.py`` remains the sole public entry point and re-imports
the names every caller has always used. External callers must never import
from ``quota_parts`` directly.
"""
