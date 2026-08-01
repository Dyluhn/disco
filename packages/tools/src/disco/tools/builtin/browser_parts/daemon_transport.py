"""Daemon transport glue split out of ``BrowserTool``.

Health checks, daemon-identity resolution, failure classification, and the
host-verifier lane close path. These were plain methods on ``BrowserTool``
that pushed its class-logical size over budget; none of them read instance
state, so they are free functions here. Where a call needs a classmethod
still owned by ``BrowserTool`` (``_pin_daemon_identity``, which mutates the
class-level identity-pin ledger), the live ``tool``/``cls`` object is passed
in and used at call time, so a test that monkeypatches the class attribute is
still honored.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    import re

    from ..anatomy import ToolContext, ToolOutcome
    from ..browser import BrowserTool


async def process_daemon_url(ctx: ToolContext, *, daemon_port_path: str) -> str | None:
    """Former ``BrowserTool._process_daemon_url``."""
    assert ctx.sandbox is not None
    if getattr(ctx.sandbox, "shares_host_network", False) is not True:
        return None
    try:
        raw = await ctx.sandbox.read_file(daemon_port_path)
        port = int(raw.decode("ascii").strip())
    except (FileNotFoundError, OSError, UnicodeDecodeError, ValueError):
        return None
    if not 1 <= port <= 65535:
        return None
    return f"http://127.0.0.1:{port}"


async def daemon_healthy(ctx: ToolContext, daemon_url: str) -> bool:
    """Former ``BrowserTool._daemon_healthy``."""
    assert ctx.sandbox is not None
    res = await ctx.sandbox.exec_shell(f"curl -sf {daemon_url}/health", timeout_s=5)
    if res.exit_code != 0:
        return False
    if getattr(ctx.sandbox, "shares_host_network", False) is not True:
        return True
    workspace = getattr(ctx.sandbox, "workspace_path", None)
    if not workspace:
        return True
    expected = hashlib.sha256(str(workspace).encode()).hexdigest()
    return res.stdout.strip() == expected


def classified_failure(
    error_class: str, error_reason: str, detail: str, *, failure_recipe: str
) -> ToolOutcome:
    """Former ``BrowserTool._classified_failure``."""
    from .._outcomes import fail_outcome

    bounded = str(detail or error_reason)[:512]
    return fail_outcome(
        f"{bounded}\n{failure_recipe}",
        structured={
            "ok": False,
            "error_class": error_class,
            "error_reason": error_reason,
        },
    )


def freshness_protocol_failure(detail: str, *, failure_recipe: str) -> ToolOutcome:
    """Former ``BrowserTool._freshness_protocol_failure``."""
    from .._outcomes import fail_outcome

    bounded = str(detail or "invalid browser freshness response")[:240]
    return fail_outcome(
        f"browser freshness protocol error: {bounded}\n{failure_recipe}",
        structured={
            "ok": False,
            "error_class": "freshness_protocol_invalid",
            "error_reason": "invalid_acknowledgement",
        },
    )


def daemon_failure_despite_missing_freshness(
    data: dict[str, Any], freshness_error: str, *, failure_recipe: str
) -> ToolOutcome | None:
    """Former ``BrowserTool._daemon_failure_despite_missing_freshness``.

    A declared daemon failure without ANY acknowledgement keeps its verdict.

    Counted P1 2026-07-28 (p4_ff_react_continue seed 600041): the daemon's
    internal-error path replied ok:false WITHOUT a freshness dict; this
    client reported "freshness acknowledgement schema mismatch" instead,
    destroying the daemon's real `browser_daemon_unavailable` verdict and
    telling the agent its well-formed call was malformed — with "do not
    retry the identical call" advice pointing at alternatives that rode the
    same dead daemon. Two such maskings shared one signature and the run
    was correctly thrash-stopped; the oracle was right, the report was
    wrong. Same P7 family as the 2026-07-27 navigate waiver in
    ``browser_parts.freshness``, one layer earlier.

    The unmasking is deliberately NARROWER than the ok:false path for
    well-formed acknowledgements: it applies only when the daemon did not
    emit an acknowledgement AT ALL yet did classify its own failure. A
    PRESENT acknowledgement with wrong keys or wrong values keeps failing
    closed — a wrong daemon / nonce / epoch may not even be answering this
    request — and ok:true NEVER bypasses freshness, so stale success
    evidence stays refused. The anomaly is disclosed beside the verdict,
    never swallowed.
    """
    from .._outcomes import fail_outcome

    if data.get("ok") is not False:
        return None
    if isinstance(data.get("freshness"), dict):
        return None
    error_text = str(data.get("error") or "").strip()
    if not error_text:
        return None
    # The generic recipe says "do not retry the identical call" — on THIS
    # path that advice is wrong and was exactly what stranded the live
    # run: the daemon heals its own transport, so one retry is the sane
    # recovery for an unavailable daemon. Every other failure path keeps
    # the standard recipe unchanged.
    if str(data.get("error_class") or "") == "browser_daemon_unavailable":
        recovery = (
            "The browser daemon recovers its own stack after a transport "
            "failure: retry this call once; if it still fails, check the "
            "preview with preview_status / preview_logs."
        )
    else:
        recovery = failure_recipe
    return fail_outcome(
        f"browser error: {error_text[:512]}"
        f"\n[freshness unverified: {freshness_error}]"
        f"\n{recovery}",
        structured={**data, "freshness_unverified": freshness_error},
    )


def action_failure_is_stale(data: dict[str, Any], ctx: ToolContext) -> bool:
    """Former ``BrowserTool._action_failure_is_stale``.

    Whether a classified action failure left the lane unsynchronized.

    Disclosed rather than enforced: the failure itself is the finding, and a
    lane that never loaded the requested document is legitimately behind. The
    daemon re-synchronizes on the next sync action, so this is diagnostic
    context for the agent, not a verdict.
    """
    raw = data.get("freshness")
    if not isinstance(raw, dict) or ctx.browser_workspace_epoch is None:
        return False
    return raw.get("synchronized_epoch") != ctx.browser_workspace_epoch


def pin_daemon_identity(
    cls: type[BrowserTool],
    generation: str,
    daemon_id: str,
    *,
    allow_rotation: bool = False,
    limit: int,
) -> bool:
    """Former ``BrowserTool._pin_daemon_identity``."""
    current = cls._daemon_identity_pins.get(generation)
    if current is not None and current != daemon_id and not allow_rotation:
        return False
    cls._daemon_identity_pins[generation] = daemon_id
    cls._daemon_identity_pins.move_to_end(generation)
    while len(cls._daemon_identity_pins) > limit:
        cls._daemon_identity_pins.popitem(last=False)
    return True


async def current_daemon_identity(
    tool: BrowserTool,
    ctx: ToolContext,
    daemon_url: str,
    *,
    token_re: re.Pattern[str],
) -> tuple[str | None, ToolOutcome | None]:
    """Former ``BrowserTool._current_daemon_identity``."""
    assert ctx.sandbox is not None
    response = await ctx.sandbox.exec_shell(
        f"curl -sf {daemon_url}/identity", timeout_s=min(ctx.timeout_s, 5)
    )
    if response.exit_code != 0:
        return None, tool._classified_failure(
            "browser_daemon_unavailable",
            "identity_transport_failed",
            f"browser daemon identity request failed (exit {response.exit_code})",
        )
    daemon_id = str(response.stdout).strip()
    if token_re.fullmatch(daemon_id) is None:
        return None, tool._freshness_protocol_failure("invalid daemon identity response")
    return daemon_id, None


async def close_host_verifier_lane(
    tool: BrowserTool,
    ctx: ToolContext,
    *,
    daemon_url_default: str,
    daemon_port_path: str,
    token_re: re.Pattern[str],
) -> bool:
    """Former ``BrowserTool.close_host_verifier_lane``.

    Best-effort close of the one fixed host-verifier page.

    This never starts a daemon merely to close it and can never target the
    agent lane. HostWebAppVerifier serializes callers around this operation.
    """
    if ctx.browser_lane != "host_verifier" or ctx.sandbox is None:
        return False
    daemon_url = (
        await process_daemon_url(ctx, daemon_port_path=daemon_port_path) or daemon_url_default
    )
    if not await daemon_healthy(ctx, daemon_url):
        return False
    expected_daemon_id, identity_failure = await current_daemon_identity(
        tool, ctx, daemon_url, token_re=token_re
    )
    if expected_daemon_id is None or identity_failure is not None:
        return False
    tool._pin_daemon_identity(
        ctx.browser_generation,
        expected_daemon_id,
        allow_rotation=True,
    )
    request_nonce = uuid.uuid4().hex
    job = {
        "action": "_close_lane",
        "workspace_epoch": ctx.browser_workspace_epoch,
        "executor_generation": ctx.browser_generation,
        "browser_lane": "host_verifier",
        "request_nonce": request_nonce,
        "expected_daemon_instance_id": expected_daemon_id,
    }
    job_path = f"/workspace/.pmx/job-{ctx.browser_lane}.json"
    await ctx.sandbox.write_file(job_path, json.dumps(job).encode("utf-8"))
    response = await ctx.sandbox.exec_shell(
        f"curl -s -X POST {daemon_url} -d @{job_path}",
        timeout_s=min(ctx.timeout_s, 10),
    )
    if response.exit_code != 0:
        return False
    try:
        data = json.loads(response.stdout)
    except (TypeError, ValueError):
        return False
    return isinstance(data, dict) and data.get("ok") is True
