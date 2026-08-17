"""Frozen Build Soak runner constants shared by bounded owners."""

from __future__ import annotations

from pathlib import Path

_DEFAULT_BASE_URL = "http://127.0.0.1:8000"

_DEFAULT_OUT = "test-record/build-soak"

_SCENARIOS = Path(__file__).resolve().parents[1] / "scenarios.yaml"

_BATCH_SUMMARY_NAME = "batch-summary.json"

_MAX_GATES = 8  # bound the approve loop so a gate flap can't spin forever

_MAX_RESUMES = 3  # bound PAUSED-resume so an actionless-paused build can't spin forever

_MAX_CLARIFY = 3  # bound clarify/confirm answers so an endlessly-asking model is let go (Bug 17)

_MAX_DECISION = 3  # bound AWAITING_USER_DECISION auto-resolutions (mirror _MAX_CLARIFY)

_GENERIC_CLARIFY_ANSWER = (
    "Use your best judgment and proceed with sensible, conventional defaults. "
    "Do not ask further clarifying questions; build the most reasonable version."
)

_DEFAULT_INACTIVITY_S = 180.0

_DEFAULT_HARD_CAP_S = 1200.0

_TRIGGER_AFTER_FIRST_FILE_WRITE = "after_first_file_write"

_TRIGGER_AFTER_TERMINAL = "after_terminal"

_BROWSER_EVIDENCE_COLLECTION_ERROR_NAME = "browser-evidence-collection-error.json"
