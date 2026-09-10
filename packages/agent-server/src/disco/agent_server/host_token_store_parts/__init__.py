"""Cohesive private implementation parts extracted from ``host_token_store``.

``host_token_store.py`` remains the sole public compatibility/export facade
(``HostTokenStore``, ``HostTokenRecord``, ``HostTokenError``, ``TokenStoreClosed``,
``TokenKind``) and re-imports the names it needs from these modules. Nothing
here is part of the public API; external callers must never import from
``host_token_store_parts`` directly.

Modules:
- :mod:`_model`       — shared constants, exception classes, and the
  ``HostTokenRecord`` dataclass (kept free of any dependency on
  ``HostTokenStore`` itself so every other part can import it without a
  circular import back to the parent facade).
- :mod:`_crypto`      — selector/verifier byte generation + digesting.
- :mod:`_origins`     — return-origin canonicalization/validation.
- :mod:`_schema`      — SQLite schema creation/migration.
- :mod:`_records`     — row -> ``HostTokenRecord`` marshalling/validation.
- :mod:`_credentials` — ``mint``/``verify`` and the wire-format token parsers.
- :mod:`_rotation`    — ``rotate``/``finish_rotation``/``revoke``/listing.
"""

from __future__ import annotations
