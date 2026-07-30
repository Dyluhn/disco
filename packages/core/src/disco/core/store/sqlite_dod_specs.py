"""DoD spec storage delegate for SqliteEventStore.

Extracted from ``SqliteEventStore`` so the store class stays within its
architecture budget. This private static mixin retains the exact public
behavior/signatures of the inherited DoD spec methods; ``SqliteEventStore``
inherits them and exposes the same surface.

Behavior preserved exactly:

* All methods use the store's single sqlite3 connection (passed via ``self``).
* Write methods acquire ``self._write_lock`` + ``self._conn`` transaction
  context, matching the pre-extraction shape.
* ``set_dod_spec`` is WRITE-ONCE; ``replace_dod_spec`` enforces monotonic
  extension. See ``core/dod.py`` for the full immutability argument.
"""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from ..owners import DEFAULT_OWNER_ID

if TYPE_CHECKING:
    import sqlite3

    from ..dod import DoDSpec


class _DodSpecMixin:
    """Private static mixin: DoD spec storage (C1a).

    Inherited by ``SqliteEventStore``; not instantiated directly. All methods
    operate on ``self._conn`` and ``self._write_lock`` provided by the host
    class.
    """

    # Host-provided attributes (declared for type-checking; assigned by SqliteEventStore).
    _conn: sqlite3.Connection
    _write_lock: asyncio.Lock

    async def set_dod_spec(
        self, conversation_id: str, spec: DoDSpec, *, set_by: str = "system"
    ) -> DoDSpec:
        """Persist the DoD spec for a conversation. WRITE-ONCE: a second call
        raises `DoDSpecAlreadySet` and the original is preserved.

        `set_by` is an audit label (never consulted by the evaluator). We use
        a transaction (write-lock + sqlite txn) so a concurrent race between
        two `set_dod_spec` calls cannot interleave a half-written spec —
        the second caller sees the row and raises.

        The spec is serialized as a single JSON blob: predicates + meta
        round-trip atomically. Validation is the caller's job (build a
        `DoDSpec` via the Pydantic model); we don't re-validate on write
        beyond the JSON round-trip, because the spec is frozen upstream."""
        from ..dod import DoDSpec, DoDSpecAlreadySet  # local import: dod.py is a leaf

        if not isinstance(spec, DoDSpec):
            # Don't accept free-form dicts here — the storage shape is the
            # Pydantic model. Callers go through DoDSpec(predicates=...).
            raise TypeError(f"set_dod_spec expects a DoDSpec, got {type(spec).__name__}")
        payload = spec.to_json_dict()
        now = datetime.now(UTC).isoformat()
        async with self._write_lock:
            with self._conn:
                # Idempotency check inside the txn: if a row already exists,
                # raise without touching it. The PRIMARY KEY constraint would
                # also catch a naive double-insert, but checking first lets us
                # raise the precise exception (and not the sqlite IntegrityError).
                existing = self._conn.execute(
                    "SELECT 1 FROM dod_specs WHERE conversation_id = ?",
                    (conversation_id,),
                ).fetchone()
                if existing is not None:
                    raise DoDSpecAlreadySet(
                        f"DoD spec for {conversation_id!r} is already set; "
                        "the spec is write-once. Capture a new conversation "
                        "if the acceptance criteria changed."
                    )
                # Make sure the parent conversation row exists — the FK
                # constraint would otherwise reject the insert. (Auto-create
                # mirrors `create_conversation`'s "idempotent register" pattern
                # so a spec can be set at conversation creation time, before
                # the first event is appended.)
                self._conn.execute(
                    "INSERT OR IGNORE INTO conversations "
                    "(conversation_id, owner_id, created_at) "
                    "VALUES (?, ?, ?)",
                    (conversation_id, DEFAULT_OWNER_ID, datetime.now().isoformat()),
                )
                self._conn.execute(
                    "INSERT INTO dod_specs "
                    "(conversation_id, spec, set_at, set_by) "
                    "VALUES (?, ?, ?, ?)",
                    (
                        conversation_id,
                        json.dumps(payload),
                        now,
                        set_by,
                    ),
                )
        return spec

    async def get_dod_spec(self, conversation_id: str) -> DoDSpec | None:
        """Accessor. Returns the stored `DoDSpec` or `None` when no spec has
        been captured yet. A pure read; no copy, no wrapping, no mutation.

        The returned spec is the live Pydantic model — frozen, so even an
        in-process attempt to mutate the result is a `ValidationError`."""
        from ..dod import DoDSpec

        row = self._conn.execute(
            "SELECT spec FROM dod_specs WHERE conversation_id = ?",
            (conversation_id,),
        ).fetchone()
        if row is None:
            return None
        return DoDSpec.from_json_dict(json.loads(row["spec"]))

    async def get_external_dod_spec(self, conversation_id: str) -> DoDSpec | None:
        """Return the immutable external authority row, excluding legacy plan rows.

        Before H368's authority split, plan approval copied model predicates into
        ``dod_specs`` with ``set_by=system:plan_approval``. Keep those bytes for
        audit and compatibility through ``get_dod_spec``, but never reinterpret
        them as user/system/profile/harness requirements.
        """
        from ..dod import DoDSpec

        row = self._conn.execute(
            "SELECT spec, set_by FROM dod_specs WHERE conversation_id = ?",
            (conversation_id,),
        ).fetchone()
        if row is None or str(row["set_by"]).startswith("system:plan_approval"):
            return None
        return DoDSpec.from_json_dict(json.loads(row["spec"]))

    async def replace_dod_spec(
        self, conversation_id: str, spec: DoDSpec, *, actor: str = "system"
    ) -> DoDSpec:
        """Write-once BOOTSTRAP + MONOTONIC external replacement (v2).

        The spec can be EXTENDED by an external authority but never WEAKENED — the
        monotonic guard (`is_monotonic_extension`) is enforced HERE, in the
        store, so no caller can route around it. The contract:

          * No spec exists → bootstrap (same as `set_dod_spec`).
          * Spec exists AND `new` is a monotonic extension (only adds / renames
            within, never drops a committed deliverable) → UPDATE in place.
          * Spec exists AND `new` would WEAKEN it → raise `DoDSpecAlreadySet`
            (original preserved). Model-authored plan revisions are revision-scoped
            in the append-only event log and never call this method.

        `actor` is recorded as `set_by` on an accepted update (audit), but does
        NOT buy a weakening: even a human operator cannot drop a committed bar
        via this method (the user-facing relax path is a new conversation)."""
        from ..dod import DoDSpec, DoDSpecAlreadySet, is_monotonic_extension

        if not isinstance(spec, DoDSpec):
            raise TypeError(f"replace_dod_spec expects a DoDSpec, got {type(spec).__name__}")
        payload = spec.to_json_dict()
        now = datetime.now(UTC).isoformat()
        async with self._write_lock:
            with self._conn:
                row = self._conn.execute(
                    "SELECT spec FROM dod_specs WHERE conversation_id = ?",
                    (conversation_id,),
                ).fetchone()
                if row is None:
                    # Bootstrap inside the same txn (mirrors set_dod_spec).
                    self._conn.execute(
                        "INSERT OR IGNORE INTO conversations "
                        "(conversation_id, owner_id, created_at) VALUES (?, ?, ?)",
                        (conversation_id, DEFAULT_OWNER_ID, datetime.now().isoformat()),
                    )
                    self._conn.execute(
                        "INSERT INTO dod_specs "
                        "(conversation_id, spec, set_at, set_by) VALUES (?, ?, ?, ?)",
                        (conversation_id, json.dumps(payload), now, actor),
                    )
                    return spec
                existing = DoDSpec.from_json_dict(json.loads(row[0]))
                if not is_monotonic_extension(existing, spec):
                    raise DoDSpecAlreadySet(
                        f"DoD spec for {conversation_id!r} cannot be replaced: the new "
                        "spec would WEAKEN it (a committed deliverable was dropped without "
                        "an explicit rename). The acceptance bar can only be EXTENDED."
                    )
                self._conn.execute(
                    "UPDATE dod_specs SET spec = ?, set_at = ?, set_by = ? "
                    "WHERE conversation_id = ?",
                    (json.dumps(payload), now, actor, conversation_id),
                )
        return spec


__all__ = ["_DodSpecMixin"]
