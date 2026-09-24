"""One terminal truth projection for normal, synthetic, and notify completion."""

from __future__ import annotations

from ...events import ConversationStatus, Event, StatusEvent, VerifierVerdictEvent
from ._shell_diagnostics import _terminal_shell_content


def _unverified_release_final_message(events: list[Event]) -> str:
    """The host-owned final message for an unverified release, RENDERED.

    GROUNDED FEEDBACK constraints 1 and 4: this replaces the model's own closing
    summary, so it is the last thing the reader sees — and it used to be one
    canned paragraph that never said WHICH verification was missing, though the
    verdict log holds exactly that. It now names the host verdict on record (or
    states that none was produced), which is the same source the release marker
    itself is derived from, so message and marker cannot drift.
    """
    latest = next(
        (event for event in reversed(events) if isinstance(event, VerifierVerdictEvent)),
        None,
    )
    if latest is None:
        observed = "No host verifier verdict was produced at all for this run."
    else:
        verdict = (latest.verdict or "none").strip() or "none"
        observed = (
            f"The last host verifier verdict on record is {verdict!r} (verified={latest.verified})."
        )
    return (
        "⚠ UNVERIFIED FINAL RESULT: Required host/browser verification did not pass "
        "or could not run. Browser rendering remains UNVERIFIED, and the deliverable "
        "may be INCOMPLETE. Any model-authored verification claim is superseded by "
        f"this host-owned result. {observed}"
    )


def _unverified_release_active(events: list[Event]) -> bool:
    """Whether finalization is under the current unverified-release marker.

    The marker is a RUNNING status scoped to the current finish attempt. A later
    lifecycle transition makes an old marker stale, and a later passing host
    verdict explicitly supersedes it even when a stop-hook kept the same run
    segment alive. This is deliberately event-only: finalization must not spend
    another provider call asking the model to correct its pre-gate summary.
    """
    active = False
    for event in events:
        if isinstance(event, StatusEvent):
            active = (
                event.status == ConversationStatus.RUNNING and event.detail == "unverified_release"
            )
        elif (
            active
            and isinstance(event, VerifierVerdictEvent)
            and event.verified
            and event.verdict == "pass"
        ):
            active = False
    return active


def _terminal_content(events: list[Event], content: str) -> tuple[str, dict[str, bool]]:
    unverified = _unverified_release_active(events)
    if unverified:
        content = _unverified_release_final_message(events)
    content, shell_caveat = _terminal_shell_content(events, content, host_warning=unverified)
    meta: dict[str, bool] = {}
    if unverified or shell_caveat:
        meta["host_owned_terminal_warning"] = True
    if unverified:
        meta["unverified_release"] = True
    if shell_caveat:
        meta["shell_diagnostics_advisory"] = True
    return content, meta
