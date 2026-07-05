"""Shared constants, request models, and helper functions for the agent-server
route modules.

Extracted verbatim from `create_app` (which rebuilt these on every call). Behaviour
is identical — the closures' captured deps (`store` / `runtime`) are now threaded as
explicit parameters, and the constants are plain module-level values.
"""

from __future__ import annotations

import contextlib
import re
import unicodedata
from pathlib import Path
from typing import TYPE_CHECKING, Literal

from disco.core import (
    DEFAULT_OWNER_ID,
)
from disco.core.appkit import BuildBrief
from disco.core.context.artifact_projection import artifact_paths_from_events
from disco.core.store.sqlite import SqliteEventStore
from fastapi import HTTPException
from pydantic import BaseModel

# The user/context turn constructors live in a non-route module so the kernel
# seam need not depend on the route layer (Disco Pi campaign, finding #3). They
# are re-exported here so the existing `routes._common` import path keeps working.
from ..build_messages import (  # noqa: F401 — re-exported for the existing import path
    _build_brief_message,
    _context_message,
    _user_message,
)

if TYPE_CHECKING:
    from ..runtime import ConversationRuntime

# ---- upload limits (files route) ----------------------------------------------
_MAX_FILE_BYTES = 25 * 1024 * 1024      # 25 MB per file
_MAX_FILES_PER_REQUEST = 20
_MAX_CONV_BYTES = 100 * 1024 * 1024    # 100 MB per conversation (uploads/ total)

# ---- misc route constants -----------------------------------------------------
_MAX_SESSION_TAIL_CHARS = 100_000

_WORKSPACE_PREFIXES = (".pmx/screenshots/", ".pmx/plots/")

# Content-type is pinned to the extension (never sniffed) and every type is
# served as an ATTACHMENT — we never parse or inline-render the bytes
# server-side, so an agent overwriting the declared file post-hoc can't turn
# this into a render/parse exploit.  Adding a new format here is safe as long
# as it stays an attachment (no inline rendering).
_ARTIFACT_TYPES = {
    ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    ".pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    ".pdf": "application/pdf",
    ".html": "text/html; charset=utf-8",
    ".md": "text/markdown; charset=utf-8",
    ".mp3": "audio/mpeg",
    ".wav": "audio/wav",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".csv": "text/csv; charset=utf-8",
}

_AUDIO_MEDIA_TYPES = {".mp3": "audio/mpeg", ".md": "text/markdown; charset=utf-8"}


def _sanitize_name(raw: str) -> str | None:
    """Return a safe filename for uploads/, or None if the result is empty."""
    name = Path(raw).name            # kills traversal (../../etc/passwd → passwd)
    name = unicodedata.normalize("NFC", name)
    name = name.lstrip(".")          # strip leading dots (dotfiles)
    name = re.sub(r"\s+", "-", name.strip())  # strip surrounding whitespace, runs → hyphen
    return name or None


class CreateConversationBody(BaseModel):
    owner_id: str = DEFAULT_OWNER_ID
    space_id: str | None = None
    title: str | None = None
    # "research" (read-only, ungated) | "build" / "agent" (agent + tools + gate;
    # identical machinery, different framing) | "deep_research" (plan→iterate→report).
    # A Literal so a junk surface 422s at the edge rather than persisting a DB label
    # the runtime then coerces to a toolless research loop (the DC-05 half-state).
    surface: Literal["research", "build", "agent", "deep_research"] = "research"
    model_override: str | None = None  # pin the driver model (catalogue key) for this convo
    autonomous: bool = False  # headless/unattended: no ask_user, auto-approve plan, clean forfeit
    quiet: bool = False  # suppress pre-plan assistant prose; plan/actions still render
    # weak-model assist tier. None ⇒ default from the probed model (local→on, cloud→off);
    # True/False ⇒ explicit per-conversation override.
    assist: bool | None = None
    # Deep Research depth tier ("quick" | "standard_deep" | "exhaustive"). The UI's
    # depth picker sends it here; the runtime reads it via _depth_for. Without
    # wiring it through, every run silently used the standard_deep default.
    depth_tier: str | None = None
    # A4: Deep Research iterative grounding. When True the engine re-searches
    # weakly-grounded claims and re-checks (up to 3 rounds). The UI's toggle sends
    # it here; the runtime reads it via _iterative_for. False ⇒ standard run.
    iterative: bool = False
    # --- Shared schema points pre-seeded for the runthru-v2 fan-out (each is wired
    # by its owning wave; default = OFF / byte-identical to today until wired). ---
    # DR-3 (Track B §11.4): bias research toward recent sources. None ⇒ no change.
    recency_window: Literal["month", "week"] | None = None
    # §1.5: per-export/-conversation brand theme for reports + decks. None ⇒ Disco default.
    brand_theme: str | None = None
    # C6 (Track C §3.6): low-friction artifact authoring (NeverConfirm + INTERACTIVE +
    # artifact_scope) on the build-like machinery. False ⇒ normal build loop.
    artifact_mode: bool = False
    # EPIC F: AppKit build — enforce the STRICT phase-based tool allowlist (raw
    # free-form tools barred; only the validated app_* mutators + reads/probes; a
    # HIGH-risk request_custom_build escape hatch). False ⇒ normal build loop.
    appkit_mode: bool = False
    # Spaces: persistent named corpora to ground research / deep_research. The
    # runtime pins these per conversation and the retrieval engine scopes through
    # RetrievalRequest.corpus_ids.
    space_ids: list[str] = []
    # Per-query research source selection. Empty means use the Settings default.
    sources: list[str] = []


class SendMessageBody(BaseModel):
    content: str
    # AppKit EPIC B: advisory presence flag. The persisted brief is recomputed
    # server-side from content; client-supplied fields are never trusted.
    build_brief: BuildBrief | None = None


class UpdateSettingsBody(BaseModel):
    """Patch a PRE-CREATED conversation's pre-kick settings (runthru-v2 ROOT-1).
    The build surface pre-creates a cid on mount with defaults; the user's later
    model pick / autonomous / assist choice is applied here right before the loop
    is kicked (the loop caches these at first kick). None = leave unchanged.

    model_override and assist are applied via apply_settings_change (atomic
    pristine check + per-cid lock). autonomous is applied separately (no gate)."""

    model_override: str | None = None
    autonomous: bool | None = None
    quiet: bool | None = None
    # Order C: weak-model assist tier toggle. None ⇒ leave unchanged; True/False
    # ⇒ explicit per-conversation override (mirrors CreateConversationBody.assist).
    assist: bool | None = None


def _reject_if_imported(store: SqliteEventStore, conversation_id: str) -> None:
    """Imported share-bundle conversations are READ-ONLY — they hold untrusted
    third-party events, so reviving them would feed an attacker's content to the
    agent with this instance's tools/credentials. Every loop-kicking / mutating
    endpoint rejects them at the server EDGE (409); the UI hiding the affordance
    is defense-in-depth, never the boundary."""
    if store.conversation_origin(conversation_id) == "imported":
        raise HTTPException(status_code=409, detail={"reason": "imported_read_only"})


async def _declared_artifacts(store: SqliteEventStore, conversation_id: str) -> set[str]:
    """The set of workspace-relative paths this conversation EMITTED as results.
    The download jail: only emitted artifacts are reachable, never arbitrary
    workspace paths (user code, secrets, uploads).

    Sources collected per tool:
      sheet_generate   — structured["filename"]
      slides_generate  — structured["filename"]  (same key)
      image_generate   — structured["path"]
      audio_overview   — structured["mp3_path"] + structured["transcript_path"]
      DeliverableEvent artifact_kind="files" — e.path

    [REL-2a] The projection logic now lives in the single-source core helper
    ``artifact_paths_from_events`` so the download jail and the artifact-manifest fold can never
    drift. This wrapper preserves the original fail-soft behavior (any store/projection error →
    empty set, never a 500).
    """
    out: set[str] = set()
    with contextlib.suppress(Exception):
        out = artifact_paths_from_events(await store.get_events(conversation_id))
    return out


def make_preview_upstream_resolver(runtime: ConversationRuntime | None):
    """DC-01: build the `{cid8}-{port}.localhost → sandbox upstream` resolver the
    HostPreviewProxyMiddleware calls. Returned closure captures `runtime` (None in
    wire-only tests → always resolves to None)."""

    async def _preview_upstream_resolver(cid8: str, port: int) -> str | None:
        if runtime is None:
            return None
        return await runtime.wake_for_preview(cid8, port)

    return _preview_upstream_resolver
