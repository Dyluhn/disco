"""``BrowserTool.run``'s request/response orchestration, split out of the class.

``run`` was mccabe-25 / 120-logical-lines. Decomposed into named steps that
mirror the wire protocol's own phases (ensure daemon -> resolve identity ->
submit job -> parse response -> validate freshness -> finalize), each
independently under the callable budget. ``tool`` (the live ``BrowserTool``
instance) is threaded through so calls like ``tool._classified_failure(...)``
still resolve through the actual class/instance at call time.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

from .._outcomes import fail_outcome
from .daemon_transport import current_daemon_identity

if TYPE_CHECKING:
    import re

    from ..anatomy import ToolContext, ToolOutcome
    from ..browser import BrowserArgs, BrowserTool


async def _resolve_daemon_identity(
    tool: BrowserTool, ctx: ToolContext, daemon_url: str, *, token_re: re.Pattern[str]
) -> tuple[str | None, ToolOutcome | None]:
    expected_daemon_id, identity_failure = await current_daemon_identity(
        tool, ctx, daemon_url, token_re=token_re
    )
    identity_required = (
        ctx.browser_workspace_epoch is not None or ctx.browser_lane == "host_verifier"
    )
    if expected_daemon_id is None and identity_required:
        return None, identity_failure or tool._freshness_protocol_failure(
            "daemon identity was unavailable"
        )
    if expected_daemon_id is not None:
        tool._pin_daemon_identity(
            ctx.browser_generation,
            expected_daemon_id,
            allow_rotation=True,
        )
    return expected_daemon_id, None


async def _submit_job(
    args: BrowserArgs,
    ctx: ToolContext,
    daemon_url: str,
    expected_daemon_id: str | None,
    request_nonce: str,
    *,
    vision_mode: Callable[[], bool],
):
    job: dict[str, Any] = {
        "action": args.action,
        "url": args.url,
        "index": args.index,
        # W6: CSS-selector and text alternatives to index for click.
        "selector": args.selector,
        "click_text": args.click_text,
        "text": args.text,
        "key": args.key,
        "full_page": args.full_page,
        "viewport_width": args.viewport_width,
        "viewport_height": args.viewport_height,
        # W6 V5: include b64 screenshot when vision is enabled (local
        # driver OR escalation model configured), not just DRIVER_VISION.
        "include_screenshot_b64": (ctx.browser_capture_screenshot_b64 or vision_mode()),
        # BF1: host-authored coherence metadata. None denotes the
        # executor's initial epoch zero and is never an acknowledgement.
        "workspace_epoch": ctx.browser_workspace_epoch,
        "executor_generation": ctx.browser_generation,
        "browser_lane": ctx.browser_lane,
        "request_nonce": request_nonce,
    }
    if expected_daemon_id is not None:
        job["expected_daemon_instance_id"] = expected_daemon_id
    # The two fixed lane-specific request paths prevent a host-verifier
    # request from replacing an agent request between write and curl,
    # without leaking one file per action. Keep the legacy job.json
    # mirror for diagnostics and compatibility; it is never dispatched.
    job_path = f"/workspace/.pmx/job-{ctx.browser_lane}.json"
    encoded_job = json.dumps(job).encode("utf-8")
    await ctx.sandbox.write_file(job_path, encoded_job)
    await ctx.sandbox.write_file("/workspace/.pmx/job.json", encoded_job)

    return await ctx.sandbox.exec_shell(
        f"curl -s -X POST {daemon_url} -d @{job_path}",
        timeout_s=ctx.timeout_s,
    )


def _parse_response(tool: BrowserTool, res) -> tuple[dict[str, Any] | None, ToolOutcome | None]:
    if res.exit_code != 0:
        return None, tool._classified_failure(
            "browser_daemon_unavailable",
            "transport_failed",
            f"browser daemon request failed (exit {res.exit_code}):"
            f" {str(res.stderr).strip()[:160]}",
        )
    try:
        data = json.loads(res.stdout)
    except (TypeError, ValueError):
        return None, tool._freshness_protocol_failure("daemon response was not valid JSON")
    if not isinstance(data, dict):
        return None, tool._freshness_protocol_failure("daemon response was not an object")
    return data, None


def _validate_and_classify(
    tool: BrowserTool,
    data: dict[str, Any],
    ctx: ToolContext,
    request_nonce: str,
    expected_daemon_id: str | None,
    *,
    failure_recipe: str,
) -> ToolOutcome | None:
    freshness_error = tool._freshness_response_error(
        data,
        ctx=ctx,
        request_nonce=request_nonce,
        expected_daemon_id=expected_daemon_id,
    )
    if freshness_error is not None:
        unmasked = tool._daemon_failure_despite_missing_freshness(data, freshness_error)
        if unmasked is not None:
            return unmasked
        return tool._freshness_protocol_failure(freshness_error)
    if data["ok"] is False:
        # The daemon's own classification reaches the agent intact. Any
        # resulting lane staleness is DISCLOSED beside it rather than
        # replacing it — the failure is the finding, and the daemon
        # re-synchronizes on the next sync action.
        failure_structured: dict[str, Any] = {
            **data,
            "lane_synchronized": not tool._action_failure_is_stale(data, ctx),
        }
        return fail_outcome(
            f"browser error: {str(data.get('error') or 'unknown error')[:512]}"
            f"\n{failure_recipe}",
            structured=failure_structured,
        )
    return None


async def _finalize_success(
    tool: BrowserTool,
    ctx: ToolContext,
    data: dict[str, Any],
    *,
    max_console_lines: int,
    max_network_lines: int,
    tool_outcome: Callable[..., ToolOutcome],
) -> ToolOutcome:
    content = tool._render_observation(data)
    structured: dict[str, Any] = data
    # CXT-5: console/network rendering is capped (max_console_lines /
    # max_network_lines); without a recover path those omitted entries
    # would be DESTRUCTIVELY lost (re-running the browser is expensive).
    # Mirror shell HS-01: spill the FULL diagnostics to a workspace file
    # and name it (prose pointer + structured field) so nothing is lost.
    console = data.get("console") or []
    network = data.get("network") or []
    if len(console) > max_console_lines or len(network) > max_network_lines:
        spill_path = f".disco-spill-browser-{uuid.uuid4().hex}.json"
        full = json.dumps({"console": console, "network": network}, indent=2)
        try:
            await ctx.sandbox.write_file(spill_path, full.encode("utf-8"))
            content += (
                f"\n[full browser diagnostics ({len(console)} console, "
                f"{len(network)} network entries) at {spill_path} — "
                f"file_read it for the omitted entries]"
            )
            structured = {**data, "diagnostics_spill_path": spill_path}
        except Exception:
            # spill failed — DO NOT claim recoverability. Carry the full
            # diagnostics in the structured payload (not lost) and flag the
            # truncation as non-recoverable so it can't be mistaken for clean.
            content += (
                f"\n[NOTE: {len(console)} console / {len(network)} network entries; "
                f"diagnostics spill failed — full entries are in this tool's structured "
                f"payload; re-run the browser to regenerate]"
            )
            structured = {
                **data,
                "diagnostics_spill_failed": True,
                "diagnostics_full": {"console": console, "network": network},
            }
    return tool_outcome(success=True, content=content, structured=structured)


async def run(
    tool: BrowserTool,
    args: BrowserArgs,
    ctx: ToolContext,
    *,
    daemon_url_default: str,
    token_re: re.Pattern[str],
    vision_mode: Callable[[], bool],
    failure_recipe: str,
    max_console_lines: int,
    max_network_lines: int,
    browser_unavailable_error: type[Exception],
    tool_outcome: Callable[..., ToolOutcome],
) -> ToolOutcome:
    """Former ``BrowserTool.run``."""
    assert ctx.sandbox is not None
    try:
        daemon_url = await tool._ensure_daemon(ctx) or daemon_url_default
        expected_daemon_id, identity_failure = await _resolve_daemon_identity(
            tool, ctx, daemon_url, token_re=token_re
        )
        if identity_failure is not None:
            return identity_failure
        request_nonce = uuid.uuid4().hex

        res = await _submit_job(
            args, ctx, daemon_url, expected_daemon_id, request_nonce, vision_mode=vision_mode
        )
        data, parse_error = _parse_response(tool, res)
        if parse_error is not None:
            return parse_error
        assert data is not None

        outcome = _validate_and_classify(
            tool, data, ctx, request_nonce, expected_daemon_id, failure_recipe=failure_recipe
        )
        if outcome is not None:
            return outcome

        return await _finalize_success(
            tool,
            ctx,
            data,
            max_console_lines=max_console_lines,
            max_network_lines=max_network_lines,
            tool_outcome=tool_outcome,
        )
    except browser_unavailable_error as e:
        # ROOT-3 — terminal, non-retryable: this backend has no browser. Carry a
        # structured flag so verify_web_app can degrade gracefully, and a clearly
        # worded error so the agent skips browser-based verification.
        unavailable: dict[str, Any] = {"browser_unavailable": True}
        if e.startup_diagnostic:
            unavailable["startup_diagnostic"] = e.startup_diagnostic
        return fail_outcome(str(e), structured=unavailable)
    except Exception as e:
        return tool._classified_failure(
            "browser_daemon_unavailable",
            "unexpected_client_error",
            f"browser tool error: {type(e).__name__}",
        )
