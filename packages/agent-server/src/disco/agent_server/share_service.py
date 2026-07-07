"""Share export/import + revocable share-link issuance — extracted from `runtime.py`.

God-file decomposition (pure move, zero behavior change). The RP-06 share
surface (scrubbed bundle export, untrusted-bundle import, and the
`share_tokens` link issuer/lookup/revoke) moves out of runtime.py into a
`ShareService` collaborator constructed once in `ConversationRuntime`.

`ShareService` owns no state of its own; it is wired with the event store,
the runtime's `_surface_of` resolver (for the export provenance banner), and
the project-store getter. The two cross-cutting contract constants
(`_VALID_SURFACES`, `_IMPORT_MAX_EVENTS`) stay declared on
`ConversationRuntime` — `set_surface` / `_surface_of` use them too — so
`share_import` reaches them via a late-bound import (the same trick
`runtime_model_probe` uses for the shared probe cache) to keep a single
source of truth and avoid a module-load circular import.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

from disco.core import DEFAULT_OWNER_ID, Event
from disco.core.state import ConversationState
from disco.core.store.base import EventFilter


class ShareService:
    """Stateless share-surface logic, wired with the store + runtime resolvers."""

    def __init__(
        self,
        store: Any,
        surface_of: Callable[[str], str],
        project_store_getter: Callable[[], Any],
    ) -> None:
        self._store = store
        self._surface_of = surface_of
        self._project_store_getter = project_store_getter

    async def share_export(
        self,
        conversation_id: str,
        *,
        owner_id: str = DEFAULT_OWNER_ID,
        before_seq: int | None = None,
    ) -> dict[str, Any]:
        """Produce a scrubbed, versioned JSON bundle from a conversation event
        snapshot. The bundle is the canonical static-replay format (RP-00's
        cassette format, RP-06's static viewer) — the static viewer reads it
        directly with no WebSocket dependency, the harness re-runs against
        it as a deterministic event source. Both consumers get one projection
        of the same event log.

        Pure with respect to the event log: same log → same bundle bytes
        (modulo dict ordering, which we control via the dump mode). Scrubbed
        via `redaction.redact_event_payload` so credentials, tokens, and
        env-var dumps never leave the boundary.

        Returns a dict that `json.dumps` to a self-contained bundle:
          - `bundle_version`: int — the locked shape version. Bump on any
            backward-incompatible change to the bundle structure (viewers
            can refuse to render unknown versions cleanly).
          - `conversation_id`, `owner_id`, `exported_at`: provenance.
          - `surface`: the conversation's surface at export time.
          - `events`: list[dict] — the scrubbed event log, ascending seq.
          - `state`: dict — the reconstructed final state (drives the
            viewer's "finished at …" header and the "any gates open" badge).

        `before_seq` is the inclusive upper seq boundary captured on a share
        token. When supplied, events, state, last_seq, and cassette are all
        derived from events with seq <= before_seq; later appends remain absent.

        The order of operations matters: events are serialized via
        `event.model_dump(mode="json")` (ISO datetimes, enums by value) and
        ONLY THEN scrubbed — scrubbing the Pydantic model directly would
        risk mutating non-string fields and corrupting the schema.
        """
        # Provenance: conversation + surface + state at export time. The
        # surface name is the "this is a build" / "this is a deep report"
        # banner the viewer renders, so it MUST be present and trustworthy
        # (no surface marker in the log → we recover via the runtime's
        # `_surface_of` ladder, same as a fresh loop composition).
        summaries = await self._store.list_conversation_summaries(
            owner_id=owner_id, limit=500, cursor=None
        )
        row = next((s for s in summaries if s.conversation_id == conversation_id), None)
        if row is None:
            return {
                "ok": False,
                "reason": "conversation_not_found",
            }
        event_filter = (
            EventFilter(before_seq=before_seq + 1) if before_seq is not None else None
        )
        events = await self._store.get_events(conversation_id, event_filter)
        state = ConversationState.reconstruct(conversation_id, events)

        # Scrub the events. The redactor walks every text field of every
        # event payload; non-text fields (seq, id, timestamps, booleans)
        # pass through unchanged. The result IS the bundle's event log.
        from .redaction import redact_event_payload

        scrubbed_events: list[dict[str, Any]] = []
        for ev in events:
            payload = ev.model_dump(mode="json")
            scrubbed_events.append(redact_event_payload(payload))

        # Build the bundle. `bundle_version` is the locked contract; bump
        # on any backward-incompatible change (removing a field, changing
        # the redaction label format, etc.) and document the change in
        # the order's report.
        # D10: `cassette` is the single-source service-call projection (the
        # same row format the harness `Cassette` class produces) derived from
        # the SAME scrubbed events the viewer renders. The harness replay
        # path reads `bundle["cassette"]` via `Cassette.from_rows(...)` —
        # one projection, two readers, no divergent serializer.
        from harness.projection import project_cassette_rows

        cassette_rows = project_cassette_rows(scrubbed_events)
        bundle = {
            "bundle_version": 1,
            "conversation_id": conversation_id,
            "owner_id": row.owner_id,
            "surface": row.surface or self._surface_of(conversation_id),
            "title": row.title,
            "exported_at": datetime.now(UTC).isoformat(),
            "last_seq": state.last_seq,
            "state": {
                "execution_status": state.execution_status.value,
                "iteration": state.iteration,
                "last_seq": state.last_seq,
                "pending_action_id": state.pending_action_id,
                "pending_plan_id": state.pending_plan_id,
            },
            "events": scrubbed_events,
            "cassette": cassette_rows,
        }
        return {"ok": True, "bundle": bundle}

    async def share_import(
        self, bundle: Any, *, owner_id: str = DEFAULT_OWNER_ID
    ) -> dict[str, Any]:
        """Import an exported share bundle as a READ-ONLY local conversation (rp-06
        residue). A bundle is UNTRUSTED third-party data: fail-closed validation, an
        importer-minted cid (never trust the bundle's), re-scrub on ingest (exporter
        scrubbing is a claim, not a property — re-running the idempotent redactor
        protects this instance's future re-export), and an `origin="imported"` marker
        that the server edge enforces read-only against. Returns {ok, conversation_id}
        or {ok: False, reason} — the endpoint maps reason → 422. No partial imports."""
        import uuid as _uuid

        from disco.core import EventAdapter, migrate_event

        from .redaction import redact_event_payload

        # The surface allow-set + import cap stay on ConversationRuntime (also
        # used by set_surface / _surface_of) — single source of truth, reached
        # late-bound to avoid a module-load circular import.
        from .runtime import ConversationRuntime

        if not isinstance(bundle, dict):
            return {"ok": False, "reason": "bundle_not_an_object"}
        if bundle.get("bundle_version") != 1:
            return {"ok": False, "reason": "unsupported_bundle_version"}
        raw_events = bundle.get("events")
        if not isinstance(raw_events, list) or not raw_events:
            return {"ok": False, "reason": "bundle_has_no_events"}
        if len(raw_events) > ConversationRuntime._IMPORT_MAX_EVENTS:
            return {"ok": False, "reason": "bundle_too_large"}

        # Surface coerced into the known set (an attacker-set surface can't pick an
        # unhandled code path); title is text, truncated (React escapes it on render).
        surface = bundle.get("surface")
        if surface not in ConversationRuntime._VALID_SURFACES:
            surface = "build"
        raw_title = bundle.get("title")
        title = (raw_title[:200] if isinstance(raw_title, str) else None) or "(imported)"

        # Validate + RE-SCRUB every event. Reject the WHOLE bundle on the first bad
        # event (no partial import). Event ids are preserved (action/observation
        # pairing is by id); seqs are reassigned by the store under the fresh cid.
        events: list[Event] = []
        for raw in raw_events:
            if not isinstance(raw, dict):
                return {"ok": False, "reason": "malformed_event"}
            try:
                migrated = migrate_event(raw)
                validated = EventAdapter.validate_python(migrated)  # extra="forbid"
                rescrubbed = redact_event_payload(validated.model_dump(mode="json"))
                events.append(EventAdapter.validate_python(migrate_event(rescrubbed)))
            except Exception:  # noqa: BLE001 — any validation failure rejects the bundle
                return {"ok": False, "reason": "malformed_event"}

        cid = f"conv_{_uuid.uuid4().hex}"  # importer-minted — never trust bundle.conversation_id
        self._store.create_conversation(
            cid, owner_id=owner_id, title=title, surface=surface, origin="imported"
        )
        await self._store.append_many(cid, events)
        return {"ok": True, "conversation_id": cid}

    def create_share_link(
        self,
        conversation_id: str,
        *,
        owner_id: str = DEFAULT_OWNER_ID,
    ) -> dict[str, Any]:
        """DEPRECATED: synchronous scaffold kept off the hot path. The
        production issuer is `create_share_link_async` (it reads the live
        last_seq so the share_tokens row carries an accurate seq hint)."""
        import secrets

        token = secrets.token_urlsafe(16).replace("-", "a").replace("_", "b")[:22]
        return {"ok": True, "token": token, "owner_id": owner_id}

    async def create_share_link_async(
        self,
        conversation_id: str,
        *,
        owner_id: str = DEFAULT_OWNER_ID,
    ) -> dict[str, Any]:
        """Issue a revocable base62 token pointing at a conversation. The
        token is the URL slug for `/share/<token>`. The `share_tokens`
        table gives us:
          - cheap O(1) lookup on every viewer request
          - per-owner scoping (a token can only be revoked by its issuer)
          - revocation by `revoked_at` (a revoked row is invisible to lookups)

        16 random bytes → 22 base62 chars (62^22 ≈ 2^131) — enough to make
        enumeration infeasible; small enough to fit in a URL slug.
        Double-issuance is a no-op (the token PK is the random string and
        `INSERT OR IGNORE` is the cheap defense against a double-clicked
        "Share" button)."""
        import secrets

        # Confirm the conversation exists for this owner (and avoid
        # silently issuing tokens for unknown ids).
        summaries = await self._store.list_conversation_summaries(
            owner_id=owner_id, limit=500, cursor=None
        )
        row = next((s for s in summaries if s.conversation_id == conversation_id), None)
        if row is None:
            return {"ok": False, "reason": "conversation_not_found"}

        # Read the live last_seq so the share_tokens row can carry the
        # "bundle_seq" hint (re-exports reuse it; the UI surfaces a
        # "newer events available" badge when the live last_seq exceeds
        # the recorded one).
        state = await self._store.get_state(conversation_id)
        bundle_seq = state.last_seq

        token = secrets.token_urlsafe(16).replace("-", "a").replace("_", "b")[:22]
        self._store.create_share_token(
            token,
            conversation_id,
            owner_id,
            bundle_seq=bundle_seq,
        )
        return {
            "ok": True,
            "token": token,
            "conversation_id": conversation_id,
            "owner_id": owner_id,
            "bundle_seq": bundle_seq,
        }

    def lookup_share_link(self, token: str) -> dict | None:
        """Resolve a share token to its (conversation_id, owner_id) row.
        Returns None for missing OR revoked tokens — the two cases are
        intentionally conflated so a revoked link is indistinguishable
        from a never-issued one to a probe. The `share_tokens` table
        has the same idempotent semantics on `INSERT OR IGNORE` so a
        double-clicked "Share" never writes twice."""
        return self._store.lookup_share_token(token)

    def list_share_links(self, *, owner_id: str) -> list[dict]:
        """List the active (non-revoked) share links for one owner. The
        UI's "shared links" affordance consumes this; revoked links are
        filtered out — the user sees a clean active-only list."""
        return self._store.list_share_tokens(owner_id=owner_id)

    def revoke_share_link(self, token: str, *, owner_id: str) -> bool:
        """Revoke a share link. OWNER-SCOPED — only the issuer can revoke
        (the WHERE clause filters by both token AND owner_id). Returns
        True if a row was marked revoked, False otherwise. Idempotent: a
        second revoke call returns False (no row matched the
        `revoked_at IS NULL` clause)."""
        return self._store.revoke_share_token(token, owner_id=owner_id)
