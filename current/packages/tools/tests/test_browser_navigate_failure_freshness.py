"""A failed browser action must surface its REAL error, not a protocol error.

Counted-promotion failure 2026-07-27 (`p4_ff_node_pause` seed 400025). The daemon
correctly classified a navigation failure — nothing was listening at the URL the
agent had been handed — as `browser_action_failed / navigation_failed`. The
client then discarded that and returned
"browser action failure did not acknowledge the requested epoch", so the agent
was told the protocol was broken and never learned the address was unreachable.

The demand was unsatisfiable by construction: `navigate` is not in the daemon's
`_SYNC_ACTIONS`, so the lane epoch is assigned only AFTER a successful `goto`. A
navigation that loaded no document cannot honestly claim synchronization.

What must still fail is everything that indicates a daemon operating on the wrong
authority — those cases are pinned below alongside the fix.
"""

from __future__ import annotations

from typing import Any

from disco.tools import ToolContext
from disco.tools.builtin.browser import BrowserTool

_NONCE = "e" * 32
_DAEMON = "a" * 32
_GENERATION = "f" * 32


def _ctx(*, epoch: int | None = 3) -> ToolContext:
    return ToolContext(
        sandbox=None,
        workspace_path=".",
        timeout_s=1,
        capabilities=None,
        owner_id="owner",
        conversation_id="conversation",
        browser_workspace_epoch=epoch,
        browser_generation=_GENERATION,
        browser_lane="agent",
    )


def _response(
    *,
    ok: bool,
    synchronized_epoch: Any,
    requested_epoch: Any = 3,
    error_class: str | None = None,
    error_reason: str = "navigation_failed",
    sync_performed: bool = False,
) -> dict[str, Any]:
    data: dict[str, Any] = {
        "ok": ok,
        "freshness": {
            "schema_version": 1,
            "daemon_instance_id": _DAEMON,
            "executor_generation": _GENERATION,
            "lane": "agent",
            "request_nonce": _NONCE,
            "requested_epoch": requested_epoch,
            "synchronized_epoch": synchronized_epoch,
            "sync_performed": sync_performed,
            "page_kind": "local_preview",
        },
    }
    if error_class is not None:
        data.update(
            {
                "error": "browser navigation failed: could not load http://127.0.0.1:44889",
                "error_class": error_class,
                "error_reason": error_reason,
            }
        )
    return data


def _error_for(data: dict[str, Any], ctx: ToolContext | None = None) -> str | None:
    BrowserTool._daemon_identity_pins.pop(_GENERATION, None)
    return BrowserTool._freshness_response_error(
        data, ctx=ctx or _ctx(), request_nonce=_NONCE, expected_daemon_id=_DAEMON
    )


# ---- the regression -------------------------------------------------------


def test_a_failed_navigation_is_no_longer_masked_as_a_protocol_error():
    # The exact shape the daemon emits when `page.goto` raises: the lane never
    # reached the requested epoch because no document loaded.
    data = _response(ok=False, synchronized_epoch=None, error_class="browser_action_failed")
    assert _error_for(data) is None


def test_a_stale_lane_epoch_on_a_failed_action_is_also_accepted():
    # Older daemons leave the PREVIOUS epoch in place rather than clearing it.
    data = _response(ok=False, synchronized_epoch=2, error_class="browser_action_failed")
    assert _error_for(data) is None


def test_the_failure_is_reported_as_unsynchronized_so_staleness_is_not_swallowed():
    stale = _response(ok=False, synchronized_epoch=None, error_class="browser_action_failed")
    fresh = _response(ok=False, synchronized_epoch=3, error_class="browser_action_failed")
    assert BrowserTool._action_failure_is_stale(stale, _ctx()) is True
    assert BrowserTool._action_failure_is_stale(fresh, _ctx()) is False


# ---- what must STILL fail --------------------------------------------------


def test_a_SUCCESSFUL_action_must_still_acknowledge_the_epoch():
    # This is the invariant that stops stale content being served as fresh.
    data = _response(ok=True, synchronized_epoch=2)
    assert _error_for(data) == "successful browser action did not acknowledge the requested epoch"


def test_a_claimed_synchronization_must_still_match():
    data = _response(
        ok=False, synchronized_epoch=2, error_class="browser_action_failed", sync_performed=True
    )
    assert _error_for(data) == "performed synchronization did not acknowledge the requested epoch"


def test_a_wrong_REQUESTED_epoch_is_still_a_protocol_error():
    # Waiving the synchronized claim must not waive the requested one: a daemon
    # operating on a different epoch than we asked for is still a violation.
    data = _response(
        ok=False, synchronized_epoch=None, requested_epoch=99, error_class="browser_action_failed"
    )
    assert _error_for(data) == "requested workspace epoch mismatch"


def test_a_wrong_executor_generation_is_still_a_protocol_error():
    data = _response(ok=False, synchronized_epoch=None, error_class="browser_action_failed")
    data["freshness"]["executor_generation"] = "b" * 32
    assert _error_for(data) == "executor generation mismatch"


def test_a_wrong_daemon_identity_is_still_a_protocol_error():
    data = _response(ok=False, synchronized_epoch=None, error_class="browser_action_failed")
    data["freshness"]["daemon_instance_id"] = "c" * 32
    assert _error_for(data) == "daemon instance identity mismatch"


def test_a_replayed_nonce_is_still_a_protocol_error():
    data = _response(ok=False, synchronized_epoch=None, error_class="browser_action_failed")
    data["freshness"]["request_nonce"] = "d" * 32
    assert _error_for(data) == "request nonce mismatch"


def test_a_schema_mismatch_is_still_a_protocol_error():
    data = _response(ok=False, synchronized_epoch=None, error_class="browser_action_failed")
    del data["freshness"]["page_kind"]
    assert _error_for(data) == "freshness acknowledgement schema mismatch"


def test_a_failed_SYNC_action_with_a_stale_epoch_is_STILL_a_protocol_error():
    # The waiver is narrow on purpose. click / press / fill / submit / screenshot /
    # console_view all run the daemon's pre-sync block BEFORE acting, so one of
    # those failing on a stale epoch means synchronization really was skipped.
    # `navigate` is the only action outside that block.
    for reason in ("interaction_blocked", "selector_not_found", "timeout"):
        data = _response(
            ok=False,
            synchronized_epoch=2,
            error_class="browser_action_failed",
            error_reason=reason,
        )
        assert (
            _error_for(data) == "browser action failure did not acknowledge the requested epoch"
        ), reason


def test_a_synchronized_epoch_ahead_of_the_request_is_still_invalid():
    data = _response(ok=False, synchronized_epoch=99, error_class="browser_action_failed")
    assert _error_for(data) == "invalid synchronized workspace epoch"
