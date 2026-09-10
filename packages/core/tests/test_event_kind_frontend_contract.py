"""Cross-language contract: the frontend has a UI disposition for every backend
event kind.

The backend (events.py :: EventKind) and the frontend (a hand-maintained TS
mirror, no codegen) drift independently. The historical failure mode: the backend
appends an event kind the frontend has no render path for, and it's silently
dropped (the RP-13 follow-up answer was generated server-side but never shown).

This test pins the two together: the frontend's KNOWN_EVENT_KINDS list (in
current/frontend/src/lib/eventDisposition.ts) must equal the backend EventKind value set.
Add a backend kind → this goes red until the frontend classifies it (rendered or
suppressed). The companion vitest (eventDisposition.test.ts) enforces that every
listed kind actually has a disposition entry.
"""

from __future__ import annotations

import re
from pathlib import Path

from disco.core.events import EventKind

_REPO_ROOT = Path(__file__).resolve().parents[4]
_DISPOSITION_TS = _REPO_ROOT / "current" / "frontend" / "src" / "lib" / "eventDisposition.ts"


def _frontend_known_kinds() -> set[str]:
    """Parse KNOWN_EVENT_KINDS = [ "...", ... ] out of the TS source."""
    text = _DISPOSITION_TS.read_text(encoding="utf-8")
    m = re.search(r"KNOWN_EVENT_KINDS\s*=\s*\[(.*?)\]\s*as const", text, re.DOTALL)
    assert m, "could not find KNOWN_EVENT_KINDS array in eventDisposition.ts"
    return set(re.findall(r'"([a-z0-9_]+)"', m.group(1)))


def test_frontend_classifies_every_backend_event_kind() -> None:
    backend = {e.value for e in EventKind}
    frontend = _frontend_known_kinds()

    missing_in_frontend = backend - frontend
    extra_in_frontend = frontend - backend

    assert not missing_in_frontend, (
        "backend EventKind(s) with NO frontend disposition — they would be "
        f"silently dropped by the UI: {sorted(missing_in_frontend)}. "
        "Classify them in current/frontend/src/lib/eventDisposition.ts (rendered or suppressed)."
    )
    assert not extra_in_frontend, (
        "frontend KNOWN_EVENT_KINDS lists kind(s) the backend no longer emits: "
        f"{sorted(extra_in_frontend)}. Remove them from eventDisposition.ts."
    )
