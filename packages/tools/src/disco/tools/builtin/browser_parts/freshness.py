"""Freshness-acknowledgement validation, split out of ``BrowserTool``.

``_freshness_response_error`` was a single 65-logical-line / mccabe-38 chain
of sequential checks. Split into named validators, each independently under
the callable budget, run in the SAME order as the original so the first
failing check still wins — this is a faithful decomposition, not a
relocation: each helper below owns real branching that used to live inline.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    import re

    from ...anatomy import ToolContext
    from ..browser import BrowserTool


def _ok_flag_error(data: dict[str, Any]) -> str | None:
    if type(data.get("ok")) is not bool:
        return "daemon success flag was not boolean"
    return None


def _is_legacy_unversioned_exempt(ctx: ToolContext, raw: Any) -> bool:
    # Compatibility for old, epoch-zero test doubles and third-party
    # sandbox shims. A pending mutation or host-verifier lane never accepts
    # an unversioned response; the shipped daemon always emits the protocol.
    return raw is None and ctx.browser_workspace_epoch is None and ctx.browser_lane == "agent"


def _schema_error(
    raw: Any, *, freshness_keys: frozenset[str]
) -> tuple[dict[str, Any] | None, str | None]:
    """Return ``(validated, error)``; ``validated`` is None only with an error.

    Returns the proven mapping rather than just a verdict: the extraction moved
    this shape check out of ``_freshness_response_error``, so the downstream
    validators can no longer see the ``isinstance`` that establishes their
    ``dict[str, Any]`` parameter. Handing back the proven value keeps that
    guarantee expressible without a cast, matching
    ``_daemon_identity_error``'s existing ``(value, error)`` shape.
    """
    if not isinstance(raw, dict) or set(raw) != freshness_keys:
        return None, "freshness acknowledgement schema mismatch"
    if raw.get("schema_version") != 1:
        return None, "freshness acknowledgement version mismatch"
    return raw, None


def _daemon_identity_error(
    cls: type[BrowserTool],
    ctx: ToolContext,
    raw: dict[str, Any],
    expected_daemon_id: str | None,
    *,
    token_re: re.Pattern[str],
) -> tuple[str | None, str | None]:
    """Return ``(daemon_id, error)``; ``daemon_id`` is None only with an error."""
    daemon_id = raw.get("daemon_instance_id")
    if not isinstance(daemon_id, str) or token_re.fullmatch(daemon_id) is None:
        return None, "invalid daemon instance identity"
    if expected_daemon_id is not None:
        if daemon_id != expected_daemon_id:
            return None, "daemon instance identity mismatch"
    elif (
        pinned_daemon_id := cls._daemon_identity_pins.get(ctx.browser_generation)
    ) is not None and pinned_daemon_id != daemon_id:
        return None, "daemon instance identity changed without a live preflight"
    return daemon_id, None


def _protocol_fields_error(
    ctx: ToolContext, raw: dict[str, Any], request_nonce: str, *, browser_lanes: frozenset[str]
) -> str | None:
    if raw.get("executor_generation") != ctx.browser_generation:
        return "executor generation mismatch"
    if raw.get("lane") != ctx.browser_lane or raw.get("lane") not in browser_lanes:
        return "browser lane mismatch"
    if raw.get("request_nonce") != request_nonce:
        return "request nonce mismatch"
    return None


def _epoch_shape_error(
    ctx: ToolContext, raw: dict[str, Any]
) -> tuple[str | None, int | None, bool | None]:
    """Validate requested/synchronized/sync_performed shape.

    Returns ``(error, synchronized, performed)`` — ``synchronized``/
    ``performed`` are only meaningful when ``error`` is None.
    """
    requested = raw.get("requested_epoch")
    expected = ctx.browser_workspace_epoch
    if requested != expected or type(requested) is not type(expected):
        return "requested workspace epoch mismatch", None, None
    synchronized = raw.get("synchronized_epoch")
    if synchronized is not None and (
        type(synchronized) is not int
        or synchronized <= 0
        or expected is None
        or synchronized > expected
    ):
        return "invalid synchronized workspace epoch", None, None
    performed = raw.get("sync_performed")
    if type(performed) is not bool:
        return "invalid synchronization flag", None, None
    if performed and synchronized != expected:
        return "performed synchronization did not acknowledge the requested epoch", None, None
    return None, synchronized, performed


def _epoch_acknowledgement_error(
    data: dict[str, Any], ctx: ToolContext, synchronized: int | None
) -> str | None:
    expected = ctx.browser_workspace_epoch
    if data["ok"] is True and expected is not None and synchronized != expected:
        return "successful browser action did not acknowledge the requested epoch"
    # A failed NAVIGATION deliberately does not have to claim synchronization.
    #
    # Counted-promotion failure 2026-07-27 (p4_ff_node_pause seed 400025): a
    # `navigate` whose `page.goto` raised was rejected here as a PROTOCOL
    # error, which DESTROYED the daemon's real, already-classified failure
    # ("browser navigation failed" — nothing listening at that URL) and told
    # the agent the protocol was broken instead. The agent then had nothing
    # actionable to route around, and two such maskings a full recovery apart
    # shared one error signature and adjudicated as TOOL_ERROR_THRASH.
    #
    # The demand was unsatisfiable by construction: `navigate` is not in the
    # daemon's `_SYNC_ACTIONS`, so its lane epoch is assigned only AFTER a
    # successful `goto`. A navigation that never loaded a document cannot
    # honestly claim to have synchronized to anything.
    #
    # The waiver is NARROW, and deliberately so. Every action in the daemon's
    # `_SYNC_ACTIONS` (click / press / fill / submit / screenshot /
    # console_view) runs the pre-sync block BEFORE acting, so if one of those
    # fails with a stale epoch the daemon really did skip synchronization —
    # a genuine protocol violation that must still be caught. `navigate` is
    # the only action outside that block, so it is the only one that can
    # honestly fail unsynchronized.
    #
    # Every other invariant still holds — schema, daemon identity, executor
    # generation, lane, nonce, and the REQUESTED epoch must all match, so a
    # daemon operating on the wrong epoch is still caught. The staleness is
    # not swallowed either: `action_failure_is_stale` reports it so the
    # caller discloses an unsynchronized lane.
    if (
        data["ok"] is False
        and data.get("error_class") == "browser_action_failed"
        and data.get("error_reason") != "navigation_failed"
        and expected is not None
        and synchronized != expected
    ):
        return "browser action failure did not acknowledge the requested epoch"
    return None


def _epoch_fields_error(data: dict[str, Any], ctx: ToolContext, raw: dict[str, Any]) -> str | None:
    error, synchronized, _performed = _epoch_shape_error(ctx, raw)
    if error is not None:
        return error
    return _epoch_acknowledgement_error(data, ctx, synchronized)


def _page_kind_error(raw: dict[str, Any]) -> str | None:
    if raw.get("page_kind") not in {"local_preview", "external", "uninitialized"}:
        return "invalid browser page kind"
    return None


def response_error(
    cls: type[BrowserTool],
    data: dict[str, Any],
    *,
    ctx: ToolContext,
    request_nonce: str,
    expected_daemon_id: str | None = None,
    freshness_keys: frozenset[str],
    token_re: re.Pattern[str],
    browser_lanes: frozenset[str],
) -> str | None:
    """Former ``BrowserTool._freshness_response_error``."""
    error = _ok_flag_error(data)
    if error is not None:
        return error
    raw = data.get("freshness")
    if _is_legacy_unversioned_exempt(ctx, raw):
        return None
    validated, error = _schema_error(raw, freshness_keys=freshness_keys)
    if validated is None:
        return error
    daemon_id, error = _daemon_identity_error(
        cls, ctx, validated, expected_daemon_id, token_re=token_re
    )
    if error is not None:
        return error
    error = _protocol_fields_error(ctx, validated, request_nonce, browser_lanes=browser_lanes)
    if error is not None:
        return error
    error = _epoch_fields_error(data, ctx, validated)
    if error is not None:
        return error
    error = _page_kind_error(validated)
    if error is not None:
        return error
    # Direct validators without a live preflight establish continuity only
    # after every acknowledgement invariant is proven; malformed responses
    # cannot poison the bounded generation pin. `_daemon_identity_error`
    # guarantees a non-None id whenever it reports no error (its docstring);
    # the explicit check makes that documented invariant provable here.
    if expected_daemon_id is None and daemon_id is not None:
        cls._pin_daemon_identity(ctx.browser_generation, daemon_id)
    return None
