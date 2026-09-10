"""``BrowserHandler._dispatch_parsed`` split into a real per-command dispatch
table, plus the shared preflight/sync/capture steps around it.

The original was a single 322-logical-line / mccabe-66 if/elif chain over
every browser action. Relocating that blob does not reduce its cyclomatic
complexity, so this module genuinely decomposes it: each action gets its own
named handler function (each independently under the callable budget), keyed
by a lookup table, plus small shared helpers for the preflight checks, the
epoch sync/reload dance, and the common post-action capture. ``handler``
(the live ``BrowserHandler`` instance) and ``state`` (the live
``BrowserState`` singleton, or a test's fake) are threaded through as
parameters read fresh from ``_browser_daemon.py``'s own module globals at
each call, so a test that monkeypatches ``daemon.state`` is still honored.
"""

from __future__ import annotations

import base64
import os
from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from .dom_signals import _count_visible_semantic_elements, _visible_dom_text

if TYPE_CHECKING:
    from .._browser_daemon import BrowserHandler, BrowserPageState


@dataclass
class _ActionCtx:
    """Per-call bundle threaded through one action's handler + capture step.

    ``sync_performed`` and ``live_headed`` are mutated in place by a handler
    (``back`` can upgrade ``sync_performed``; ``live_start`` can upgrade
    ``live_headed``) — the caller reads them back off this same object.
    """

    handler: BrowserHandler
    page: Any
    lane: BrowserPageState
    params: dict[str, Any]
    action: str
    lane_name: str
    generation: str
    nonce: str
    requested_epoch: int | None
    sync_performed: bool
    pending_epoch: bool
    click_timeout_ms: int
    page_kind: Callable[[Any], str]
    live_view: Any
    live_headed: bool


def _render_not_ready_error(handler, state, lane_name, generation, nonce, requested_epoch):
    return handler._error(
        "browser_daemon_unavailable",
        "renderer_unavailable",
        state.render_error or "Browser renderer not initialized",
        handler._freshness(lane_name, generation, nonce, requested_epoch, False),
    )


def _handle_close_lane_action(handler, state, lane_name, generation, nonce, requested_epoch):
    if lane_name != "host_verifier":
        return handler._error(
            "freshness_protocol_invalid",
            "invalid_lane_close",
            "the agent browser lane cannot be closed by this operation",
            handler._freshness(lane_name, generation, nonce, requested_epoch, False),
        )
    state._lane_lifecycle.close("host_verifier")
    return {
        "ok": True,
        "freshness": handler._freshness(lane_name, generation, nonce, requested_epoch, False),
    }


def _reset_lane_and_page(handler, state, lane_name, generation, nonce, requested_epoch):
    """Returns (lane, page, error) — error is not None iff page is None."""
    lane = state._lane_lifecycle.reset_for_generation(lane_name, generation)
    page = lane.page
    if page is None:
        return lane, None, handler._error(
            "browser_daemon_unavailable",
            "renderer_unavailable",
            "Browser renderer not initialized",
            handler._freshness(lane_name, generation, nonce, requested_epoch, False),
        )
    return lane, page, None


def _epoch_regression_error(handler, lane, lane_name, generation, nonce, requested_epoch):
    if lane.synchronized_epoch is not None and (
        requested_epoch is None or requested_epoch < lane.synchronized_epoch
    ):
        return handler._error(
            "freshness_protocol_invalid",
            "epoch_regression",
            "workspace epoch regressed",
            handler._freshness(lane_name, generation, nonce, requested_epoch, False),
        )
    return None


def _apply_viewport(page, params) -> None:
    vw = params.get("viewport_width")
    vh = params.get("viewport_height")
    if type(vw) is int and type(vh) is int:
        page.set_viewport_size({"width": vw, "height": vh})


def _sync_action_precondition_error(
    handler, page, action, lane_name, generation, nonce, requested_epoch, *, sync_actions, page_kind
):
    if action in sync_actions and page_kind(getattr(page, "url", "")) == "uninitialized":
        return handler._error(
            "browser_session_uninitialized",
            "missing_loaded_page",
            "browser page has no loaded document; navigate before this action",
            handler._freshness(lane_name, generation, nonce, requested_epoch, False),
        )
    return None


def _maybe_synchronize_lane(
    handler,
    page,
    lane,
    action,
    lane_name,
    generation,
    nonce,
    requested_epoch,
    *,
    pending_epoch,
    sync_actions,
    page_kind,
):
    """Returns (sync_performed, error)."""
    if not (action in sync_actions and pending_epoch):
        return False, None
    if page_kind(getattr(page, "url", "")) == "local_preview":
        try:
            lane.clear_diagnostics()
            page.reload(wait_until="load")
            page.wait_for_timeout(500)
        except Exception:
            return False, handler._error(
                "freshness_sync_failed",
                "reload_failed",
                "local preview could not be synchronized",
                handler._freshness(lane_name, generation, nonce, requested_epoch, False),
            )
        # Acknowledge immediately after reload. A later action failure
        # must not cause the next recovery action to reload again.
        lane.synchronized_epoch = requested_epoch
        return True, None
    # Workspace bytes cannot affect a literal non-loopback page.
    # Acknowledge without reloading it so external browsing state is
    # never destroyed by local mutations.
    lane.synchronized_epoch = requested_epoch
    return False, None


# ---------------------------------------------------------------------------
# Per-action handlers. Each returns a complete response dict (terminal) or
# None (proceed to the common post-action capture step below).
# ---------------------------------------------------------------------------


def _handle_navigate(ctx: _ActionCtx, state):
    return ctx.handler._navigate(
        ctx.page,
        ctx.lane,
        ctx.params.get("url"),
        lane_name=ctx.lane_name,
        generation=ctx.generation,
        nonce=ctx.nonce,
        requested_epoch=ctx.requested_epoch,
    )


def _handle_screenshot(ctx: _ActionCtx, state):
    return None  # Just take a screenshot at the end.


def _handle_click(ctx: _ActionCtx, state):
    freshness = ctx.handler._freshness(
        ctx.lane_name, ctx.generation, ctx.nonce, ctx.requested_epoch, ctx.sync_performed
    )
    locator, error = ctx.handler._action_locator(
        ctx.page, ctx.handler._selector_for(ctx.params), freshness
    )
    if error is not None:
        return error
    assert locator is not None
    try:
        locator.click(timeout=ctx.click_timeout_ms)
    except Exception:
        return ctx.handler._error(
            "browser_action_failed",
            "interaction_blocked",
            "browser click could not be completed",
            freshness,
        )
    try:
        ctx.page.wait_for_timeout(500)
    except Exception:
        # Settling is observation latency after a dispatched click, not part of
        # actionability. The common capture below will still fail honestly if
        # the page itself is no longer readable.
        pass
    return None


def _handle_press(ctx: _ActionCtx, state):
    key = ctx.params.get("key", "")
    if not key:
        return ctx.handler._error(
            "browser_action_failed",
            "interaction_blocked",
            "key required for press",
            ctx.handler._freshness(
                ctx.lane_name, ctx.generation, ctx.nonce, ctx.requested_epoch, ctx.sync_performed
            ),
        )
    try:
        ctx.page.keyboard.press(key)
        ctx.page.wait_for_timeout(500)
    except Exception:
        return ctx.handler._error(
            "browser_action_failed",
            "interaction_blocked",
            "browser key press could not be completed",
            ctx.handler._freshness(
                ctx.lane_name, ctx.generation, ctx.nonce, ctx.requested_epoch, ctx.sync_performed
            ),
        )
    return None


# PROD-2: a fill request is a LIST. Testing a signup form one input per model
# turn ("Submitted a web form" x6: address, city, ZIP, card, expiry, CVC) turned
# a five-minute site into an hour of round trips. One call now carries every
# known field; each one is still resolved and validated on its own, and a
# failure names WHICH field failed and which were already applied, so the model
# can resume instead of re-deriving the whole form.
_FILLABLE_ELEMENT_JS = """el => {
  const tag = el.tagName.toLowerCase();
  if (tag === 'textarea') return true;
  if (tag === 'input') {
    const type = (el.getAttribute('type') || 'text').toLowerCase();
    return !['button', 'submit', 'reset', 'image', 'file'].includes(type);
  }
  return el.isContentEditable === true;
}"""


def _fill_requests(params: dict[str, Any]) -> list[dict[str, Any]]:
    """The ordered fill requests: the batch when present, else the single field."""

    batch = params.get("fields")
    if isinstance(batch, list) and batch:
        return [request for request in batch if isinstance(request, dict)]
    return [
        {
            "index": params.get("index"),
            "selector": params.get("selector", ""),
            "text": params.get("text", ""),
        }
    ]


def _fill_field_label(request: dict[str, Any], position: int, total: int) -> str:
    if request.get("selector"):
        target = repr(request["selector"])
    elif request.get("index") is not None:
        target = f"index {request['index']}"
    else:
        target = "no target"
    return f"field {position} of {total} ({target})"


def _fill_progress(applied: list[str]) -> str:
    """Name the fields already written, so a partial fill is not a mystery."""

    if not applied:
        return "; no field was filled"
    return f"; already filled: {', '.join(applied)}"


def _fill_error(ctx: _ActionCtx, reason, detail, freshness, label, applied):
    return ctx.handler._error(
        "browser_action_failed",
        reason,
        f"{detail} for {label}{_fill_progress(applied)}",
        freshness,
    )


def _handle_fill(ctx: _ActionCtx, state):
    freshness = ctx.handler._freshness(
        ctx.lane_name, ctx.generation, ctx.nonce, ctx.requested_epoch, ctx.sync_performed
    )
    requests = _fill_requests(ctx.params)
    applied: list[str] = []
    for position, request in enumerate(requests, start=1):
        label = _fill_field_label(request, position, len(requests))
        if request.get("index") is None and not request.get("selector"):
            return _fill_error(
                ctx, "selector_not_found", "Index or selector required", freshness, label, applied
            )
        locator, error = ctx.handler._action_locator(
            ctx.page, ctx.handler._selector_for(request), freshness
        )
        if error is not None:
            # `_action_locator` reports the failure class truthfully but has no
            # idea which field it was resolving; say so rather than returning a
            # bare "target was not found" for a six-field call.
            error["error"] = f"{error.get('error', '')} for {label}{_fill_progress(applied)}"[:512]
            return error
        assert locator is not None
        try:
            fillable = locator.evaluate(_FILLABLE_ELEMENT_JS)
        except Exception:
            fillable = None
        if fillable is False:
            return _fill_error(
                ctx,
                "interaction_blocked",
                "target is not a fillable input, textarea, or contenteditable element",
                freshness,
                label,
                applied,
            )
        try:
            locator.fill(request.get("text", ""))
        except Exception:
            return _fill_error(
                ctx,
                "interaction_blocked",
                "browser fill could not be completed",
                freshness,
                label,
                applied,
            )
        applied.append(label)
    return None


def _handle_submit(ctx: _ActionCtx, state):
    index = ctx.params.get("index")
    if index is None:
        return ctx.handler._error(
            "browser_action_failed",
            "selector_not_found",
            "Index required for submit",
            ctx.handler._freshness(
                ctx.lane_name, ctx.generation, ctx.nonce, ctx.requested_epoch, ctx.sync_performed
            ),
        )
    freshness = ctx.handler._freshness(
        ctx.lane_name, ctx.generation, ctx.nonce, ctx.requested_epoch, ctx.sync_performed
    )
    locator, error = ctx.handler._action_locator(
        ctx.page, ctx.handler._selector_for(ctx.params), freshness
    )
    if error is not None:
        return error
    assert locator is not None
    try:
        tag_name = locator.evaluate("el => el.tagName.toLowerCase()")
        if tag_name in ("input", "textarea"):
            locator.focus()
            ctx.page.keyboard.press("Enter")
        else:
            locator.click(timeout=ctx.click_timeout_ms)
        ctx.page.wait_for_timeout(500)
    except Exception:
        return ctx.handler._error(
            "browser_action_failed",
            "interaction_blocked",
            "browser submit could not be completed",
            freshness,
        )
    return None


def _handle_back(ctx: _ActionCtx, state):
    try:
        ctx.page.go_back()
        ctx.page.wait_for_timeout(500)
        # History may restore an old cached local document. Reload that
        # destination once so a successful back cannot fabricate same-
        # epoch freshness.
        if ctx.pending_epoch and ctx.page_kind(ctx.page.url) == "local_preview":
            ctx.lane.clear_diagnostics()
            ctx.page.reload(wait_until="load")
            ctx.page.wait_for_timeout(500)
            ctx.sync_performed = True
        ctx.lane.synchronized_epoch = ctx.requested_epoch
    except Exception:
        return ctx.handler._error(
            "browser_action_failed",
            "navigation_failed",
            "browser history navigation failed",
            ctx.handler._freshness(
                ctx.lane_name, ctx.generation, ctx.nonce, ctx.requested_epoch, ctx.sync_performed
            ),
        )
    return None


def _handle_console_view(ctx: _ActionCtx, state):
    return {
        "ok": True,
        "url": ctx.page.url,
        "title": ctx.page.title(),
        "console": ctx.lane.console_logs,
        "network": ctx.lane.network_fails,
        "elements": [],
        "text": "",
        "screenshot_path": None,
        "freshness": ctx.handler._freshness(
            ctx.lane_name, ctx.generation, ctx.nonce, ctx.requested_epoch, ctx.sync_performed
        ),
    }


def _handle_live_start(ctx: _ActionCtx, state):
    if ctx.live_view is None:
        return {"ok": False, "error": "live_view module not available"}
    # Ensure Xvfb + VNC stack is up
    ok = ctx.live_view.ensure_live()
    if not ok:
        return {"ok": False, "error": "Failed to start live view stack"}
    # If browser is headless, restart it headed on :1
    if not ctx.live_headed:
        if state.browser is not None:
            try:
                state.browser.close()
            except Exception:
                pass
        if state.playwright is not None:
            try:
                state.playwright.stop()
            except Exception:
                pass
        state.start(display=":1")
        state._lane_lifecycle.reset_for_generation(ctx.lane_name, ctx.generation)
        ctx.live_headed = True
    return {
        "ok": True,
        "novnc_port": 6080,
        "display": ":1",
        "freshness": ctx.handler._freshness(
            ctx.lane_name, ctx.generation, ctx.nonce, ctx.requested_epoch, ctx.sync_performed
        ),
    }


def _handle_live_touch(ctx: _ActionCtx, state):
    # Heartbeat from the frontend while the live view is open — refresh the idle
    # watchdog so an actively-watched session is not reaped after 600s.
    if ctx.live_view is not None:
        ctx.live_view.touch()
    return {
        "ok": True,
        "live": ctx.live_view.is_live() if ctx.live_view is not None else False,
        "freshness": ctx.handler._freshness(
            ctx.lane_name, ctx.generation, ctx.nonce, ctx.requested_epoch, ctx.sync_performed
        ),
    }


def _handle_live_stop(ctx: _ActionCtx, state):
    if ctx.live_view is not None:
        ctx.live_view.teardown()
    return {
        "ok": True,
        "freshness": ctx.handler._freshness(
            ctx.lane_name, ctx.generation, ctx.nonce, ctx.requested_epoch, ctx.sync_performed
        ),
    }


_ACTION_HANDLERS: dict[str, Callable[[_ActionCtx, Any], dict[str, Any] | None]] = {
    "navigate": _handle_navigate,
    "screenshot": _handle_screenshot,
    "click": _handle_click,
    "press": _handle_press,
    "fill": _handle_fill,
    "submit": _handle_submit,
    "back": _handle_back,
    "console_view": _handle_console_view,
    "live_start": _handle_live_start,
    "live_touch": _handle_live_touch,
    "live_stop": _handle_live_stop,
}


# ---------------------------------------------------------------------------
# Common post-action capture (navigate/screenshot/click/press/fill/submit/back
# on success all fall through to this shared block).
# ---------------------------------------------------------------------------


def _capture_elements_and_text(
    handler, page, *, max_text, capture_retry_max, capture_retry_interval_ms
):
    # B-F: capture-before-render race — read elements + text, but retry up to
    # capture_retry_max times until the page has SOMETHING (text or elements),
    # so a late SPA hydration (mount > the fixed settle) is not returned as a
    # permanent empty observation.
    elements: list[str] = []
    text = ""
    for attempt in range(capture_retry_max):
        elements = handler._get_elements(page)
        text = page.evaluate(
            "() => (document.body.innerText || document.body.textContent || '').trim()"
        ).strip()[:max_text]
        if text or elements:
            break
        # Don't sleep after the final read — a genuinely-blank page returns
        # empty immediately at the cap rather than burning a trailing wait.
        if attempt < capture_retry_max - 1:
            page.wait_for_timeout(capture_retry_interval_ms)
    return elements, text


def _take_screenshot(state, page, action, params, *, workspace_root, screenshot_dir):
    state.screenshot_seq += 1
    os.makedirs(screenshot_dir, exist_ok=True)
    screenshot_rel_path = f".pmx/screenshots/{state.screenshot_seq:04d}-{action}.png"
    screenshot_full_path = os.path.join(workspace_root, screenshot_rel_path)
    page.screenshot(path=screenshot_full_path, full_page=params.get("full_page", False))
    return screenshot_rel_path, screenshot_full_path


def _capture_common_result(
    ctx: _ActionCtx,
    state,
    *,
    workspace_root,
    screenshot_dir,
    max_text,
    capture_retry_max,
    capture_retry_interval_ms,
):
    handler = ctx.handler
    page = ctx.page
    lane = ctx.lane
    elements, text = _capture_elements_and_text(
        handler,
        page,
        max_text=max_text,
        capture_retry_max=capture_retry_max,
        capture_retry_interval_ms=capture_retry_interval_ms,
    )
    screenshot_rel_path, screenshot_full_path = _take_screenshot(
        state,
        page,
        ctx.action,
        ctx.params,
        workspace_root=workspace_root,
        screenshot_dir=screenshot_dir,
    )

    # AppKit EPIC G: surface the data-appkit-section markers present in the
    # RENDERED DOM so verify_appkit_app's section_coverage check can prove each
    # AppSpec section actually mounted. Additive + harmless to normal builds
    # (an app with no markers returns []). Port-scope miss found live 2026-07-03:
    # the verifier read `appkit_sections` but nothing ever emitted it here.
    try:
        appkit_sections = page.evaluate(
            "() => Array.from(document.querySelectorAll('[data-appkit-section]'))"
            ".map(e => e.getAttribute('data-appkit-section'))"
        )
    except Exception:
        appkit_sections = []
    try:
        canvas_count = page.evaluate("() => document.querySelectorAll('canvas').length")
    except Exception:
        canvas_count = 0
    visible_semantic_elements = _count_visible_semantic_elements(page)
    dom_text = _visible_dom_text(page)

    res = {
        "ok": True,
        "url": page.url,
        "title": page.title(),
        "console": lane.console_logs,
        "network": lane.network_fails,
        "elements": elements,
        "text": text,
        "appkit_sections": appkit_sections,
        "canvas_count": canvas_count,
        # Exact authored text from rendered, non-hidden text nodes.
        # Unlike innerText, this is not rewritten by CSS text-transform,
        # so exact-content claims retain their case while still requiring
        # structured browser visibility/geometry evidence.
        "visible_dom_text": dom_text,
        # Captured from the Playwright main-document Response event, not
        # from page JavaScript (which can shadow document.contentType).
        "document_content_type": lane.document_content_type,
        "visible_semantic_elements": visible_semantic_elements,
        "screenshot_path": screenshot_rel_path,
        "freshness": handler._freshness(
            ctx.lane_name, ctx.generation, ctx.nonce, ctx.requested_epoch, ctx.sync_performed
        ),
    }

    if ctx.params.get("include_screenshot_b64"):
        with open(screenshot_full_path, "rb") as f:
            b64 = base64.b64encode(f.read()).decode("utf-8")
        res["screenshot_b64"] = b64

    return res


def _build_action_ctx(
    handler,
    page,
    lane,
    params,
    action,
    lane_name,
    generation,
    nonce,
    requested_epoch,
    sync_performed,
    pending_epoch,
    *,
    click_timeout_ms,
    page_kind,
    live_view,
    live_headed,
) -> _ActionCtx:
    return _ActionCtx(
        handler=handler,
        page=page,
        lane=lane,
        params=params,
        action=action,
        lane_name=lane_name,
        generation=generation,
        nonce=nonce,
        requested_epoch=requested_epoch,
        sync_performed=sync_performed,
        pending_epoch=pending_epoch,
        click_timeout_ms=click_timeout_ms,
        page_kind=page_kind,
        live_view=live_view,
        live_headed=live_headed,
    )


def dispatch_parsed(
    handler,
    state,
    action,
    params,
    lane_name,
    generation,
    nonce,
    requested_epoch,
    *,
    live_view,
    live_headed,
    workspace_root,
    screenshot_dir,
    max_text,
    click_timeout_ms,
    capture_retry_max,
    capture_retry_interval_ms,
    sync_actions,
    page_kind,
) -> tuple[dict[str, Any], bool]:
    """Former ``BrowserHandler._dispatch_parsed``.

    Returns ``(response, live_headed)`` — the caller (the thin delegate in
    ``_browser_daemon.py``, the only place that can rebind the module global)
    writes ``live_headed`` back onto ``_live_headed``.
    """
    if not state.render_ready:
        return (
            _render_not_ready_error(handler, state, lane_name, generation, nonce, requested_epoch),
            live_headed,
        )

    if action == "_close_lane":
        return (
            _handle_close_lane_action(
                handler, state, lane_name, generation, nonce, requested_epoch
            ),
            live_headed,
        )

    lane, page, error = _reset_lane_and_page(
        handler, state, lane_name, generation, nonce, requested_epoch
    )
    if error is not None:
        return error, live_headed

    error = _epoch_regression_error(handler, lane, lane_name, generation, nonce, requested_epoch)
    if error is not None:
        return error, live_headed

    _apply_viewport(page, params)

    pending_epoch = requested_epoch is not None and requested_epoch != lane.synchronized_epoch

    error = _sync_action_precondition_error(
        handler, page, action, lane_name, generation, nonce, requested_epoch,
        sync_actions=sync_actions, page_kind=page_kind,
    )
    if error is not None:
        return error, live_headed

    sync_performed, error = _maybe_synchronize_lane(
        handler, page, lane, action, lane_name, generation, nonce, requested_epoch,
        pending_epoch=pending_epoch, sync_actions=sync_actions, page_kind=page_kind,
    )
    if error is not None:
        return error, live_headed

    handler_fn = _ACTION_HANDLERS.get(action)
    if handler_fn is None:
        return (
            handler._error(
                "browser_action_failed",
                "unknown_action",
                "Unknown browser action",
                handler._freshness(lane_name, generation, nonce, requested_epoch, sync_performed),
            ),
            live_headed,
        )

    ctx = _build_action_ctx(
        handler, page, lane, params, action, lane_name, generation, nonce, requested_epoch,
        sync_performed, pending_epoch,
        click_timeout_ms=click_timeout_ms, page_kind=page_kind,
        live_view=live_view, live_headed=live_headed,
    )
    result = handler_fn(ctx, state)
    if result is not None:
        return result, ctx.live_headed

    return (
        _capture_common_result(
            ctx,
            state,
            workspace_root=workspace_root,
            screenshot_dir=screenshot_dir,
            max_text=max_text,
            capture_retry_max=capture_retry_max,
            capture_retry_interval_ms=capture_retry_interval_ms,
        ),
        ctx.live_headed,
    )
