"""REL-27 (F-27) — finish-time workspace sealability gate.

Sole owner of "is the current workspace sealable for delivery" policy at
finish time. Extracted from ``_FinalizeMixin.seal_gate_allows_finish`` in
``finalize.py``, which now delegates here.

Called immediately before EVERY affirmative FINISHED emission: the finish
battery's finalize, the completed-via-notify path, and the browserless
honest-static valve. Non-delivery terminals deliberately bypass it — forced
``noop_limit`` and the workflow-control ``workflow_skipped`` finish —
because neither claims a deliverable workspace, and the post-terminal
honestly-unsealed disclosure remains their backstop. (A future workflow
surface that DOES ship files must route through an affirmative site or add
its own gate call.)

Semantics:
  • no probe injected → True (legacy byte-identical);
  • probe sealable → True (streak reset);
  • probe blocking, streak < cap → emit a refusal naming the EXACT blocking
    entries and the remedy, return False (caller CONTINUEs — the constraint
    reaches the model BEFORE the last point it can act, unlike the
    post-terminal persistence note that F-27 exposed);
  • probe blocking, streak ≥ cap → loud ``unsealed_release`` marker +
    visible warning, then True: the commit-time strict seal will still
    refuse and disclose. Breaking the loop must not fabricate a seal;
  • probe error/timeout → True with a log line. The probe is an
    affordance; the commit-time seal remains the sole publication
    authority, and a broken probe must never cage a valid terminal.

The default ``finish_seal_timeout_s`` (150 s) deliberately exceeds the
Podman workspace-export timeout (120 s) so the probe reaches a verdict — or
the inner export's own timeout — rather than silently self-disabling on
exactly the large workspaces where an unsealable finish matters most.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

from ..common import (
    _FINISH_SEAL_CAP,
    _LOG,
    ConversationStatus,
    EventSource,
    LLMMessage,
    MessageEvent,
    StatusEvent,
)

if TYPE_CHECKING:
    from ...loop_facade_compat import _AgentLoopCompatibility as AgentLoop


def _format_blocking_entries(blocking: list[str]) -> str:
    """Render up to 8 blocking entries, noting how many more were omitted."""
    shown = "\n".join(f"  - {entry}" for entry in blocking[:8])
    if len(blocking) > 8:
        shown += f"\n  … and {len(blocking) - 8} more"
    return shown


async def _emit_seal_refusal(loop: AgentLoop, shown: str, blocking: list[str]) -> None:
    await loop._emit(
        MessageEvent(
            source=EventSource.ENVIRONMENT,
            message=LLMMessage(
                role="user",
                content=(
                    "<system-reminder>\n"
                    "You called finish, but the final workspace cannot be "
                    "sealed for delivery — these entries would be missing "
                    "from the saved/exported project:\n"
                    f"{shown}\n"
                    "The live preview resolves them, but only regular, "
                    "uniquely-linked files are preserved durably. Replace "
                    "each with real content (for a symlink: delete the "
                    "link and copy its target into place, e.g. "
                    "`rm <link> && cp -r <target> <link-path>`), then "
                    "finish again.\n"
                    "</system-reminder>"
                ),
            ),
            # `blocking` follows the loop-wide meta convention: a STRING
            # reason tag (signals.py treats any such message as a typed
            # blocking proof obligation). The entry list rides under its
            # own key.
            meta={
                "blocking": "finish_seal_refused",
                "seal_blocking": blocking[:32],
            },
        )
    )


async def _emit_seal_loud_release(loop: AgentLoop, shown: str, blocking: list[str]) -> None:
    # Cap reached: LOUD release — mirror the verify/browser valves. The
    # terminal stays reachable; the unsealed truth stays visible (marker +
    # human-facing warning now, strict-seal refusal + typed persistence
    # disclosure at commit).
    await loop._emit(
        StatusEvent(
            status=ConversationStatus.RUNNING,
            detail="unsealed_release",
        )
    )
    await loop._emit(
        MessageEvent(
            source=EventSource.ENVIRONMENT,
            message=LLMMessage(
                role="user",
                content=(
                    "⚠ Finished with an UNSEALABLE workspace after "
                    f"{loop._finish_seal_refusals} refusals — these "
                    "entries cannot be preserved in the durable project:\n"
                    f"{shown}\n"
                    "The saved/exported project will be missing them; "
                    "review the workspace."
                ),
            ),
            meta={
                "unsealed_release": True,
                "seal_blocking": blocking[:32],
            },
        )
    )


async def seal_gate_allows_finish(loop: AgentLoop) -> bool:
    """Run the REL-27 sealability probe and gate. See module docstring."""
    probe = loop._finish_sealability_probe
    if probe is None:
        return True
    try:
        result = await asyncio.wait_for(probe(), timeout=loop._finish_seal_timeout_s)
    except Exception:  # noqa: BLE001 — advisory probe; seal authority is at commit
        _LOG.warning(
            "finish sealability probe failed; the commit-time seal remains authoritative",
            exc_info=True,
        )
        return True
    if result.sealable:
        loop._finish_seal_refusals = 0
        return True

    blocking = [str(entry) for entry in result.blocking]
    shown = _format_blocking_entries(blocking)
    if loop._finish_seal_refusals < _FINISH_SEAL_CAP:
        loop._finish_seal_refusals += 1
        await _emit_seal_refusal(loop, shown, blocking)
        return False

    await _emit_seal_loud_release(loop, shown, blocking)
    loop._finish_seal_refusals = 0
    return True
