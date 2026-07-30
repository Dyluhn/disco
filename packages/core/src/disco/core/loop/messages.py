"""Static reminder / message builders — pure helpers over the event log.

Compatibility facade: the implementation now lives in :mod:`message_rendering`,
the single bounded owner. This module re-exports every public/private name,
signature, sentinel, prompt byte, and ordering rule so consumers are
unchanged.

Extracted from `engine.py` (god-file decomposition, Wave 1). All pure module-
level functions — no loop or `self` state, no model calls, no emission:

* `_describe_llm_error` — render a provider/model error WITHOUT flattening it.
* `_workspace_paths_from_events` — the (mutated, read_only) working-set path
  lists, most-recent first; the snapshot + HS-03 recap both build on it.
* `_hs03_reground_message` — the HS-03 facts re-ground recap (an LLMMessage of
  goal / progress / files / constraints), FACTS ONLY.
* `_stuck_escape_reminder` — pick the rotating stuck-escape reminder for an
  attempt count from a small fixed pool.

The engine re-imports the four builders it calls; the cadence/temperature
constants that GATE them (`_HS03_REGROUND_INTERVAL`, `_STUCK_ESCAPE_TEMP`) and
the workspace-snapshot size caps (`_WS_*`) stay in engine.py — only the
constants used EXCLUSIVELY by the moved builders moved with them.
"""

from __future__ import annotations

from .message_rendering import (  # noqa: F401 — re-exported for back-compat
    _HS03_REGROUND_MAX_FILES,
    _HS03_REGROUND_SECTION_CHARS,
    _HS03_REGROUND_SENTINEL,
    _PLAN_EXPLORE_FORCE,
    _PLAN_EXPLORE_READ_CAP,
    _REPLAN_DIGEST_MAX_STEPS,
    _REPLAN_DIGEST_TITLE_CAP,
    _REPLAN_FRAMING,
    _STUCK_ESCAPE_REMINDER_POOL,
    _WORKFLOW_ROUTER_EXPLORE_FORCE,
    _describe_llm_error,
    _hs03_reground_message,
    _latest_user_instruction,
    _render_replan_plan_digest,
    _stuck_escape_reminder,
    _workspace_paths_from_events,
)