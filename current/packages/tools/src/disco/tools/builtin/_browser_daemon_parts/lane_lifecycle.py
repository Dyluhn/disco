"""Lane page-provisioning and generation-recycling, split out of ``BrowserState``.

``BrowserState`` owns the Playwright PROCESS (playwright/browser/context/
host_context; ``start()``/``stop()``). This module owns per-lane PAGE
lifecycle: lazily creating a lane's page, closing the host-verifier lane, and
recycling a lane for a new executor generation. None of the three operations
below (formerly ``BrowserState.ensure_lane_page`` / ``.close_lane`` /
``.reset_lane_for_generation``) has any caller outside ``_browser_daemon.py``
itself, so callers now reach them through ``state._lane_lifecycle`` with no
compatibility shim required.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .._browser_daemon import BrowserPageState, BrowserState


class LaneLifecycle:
    """Owns page-provisioning + generation-recycling for one ``BrowserState``."""

    def __init__(self, owner: BrowserState) -> None:
        self._owner = owner

    def ensure_page(self, lane_name: str) -> BrowserPageState:
        owner = self._owner
        lane = owner.lane(lane_name)
        if lane.page is None:
            if lane_name == "host_verifier":
                if owner.host_context is None:
                    owner.host_context = owner._new_context()
                context = owner.host_context
            else:
                context = owner.context
            if context is None:
                raise RuntimeError("Browser context not initialized")
            owner._bind_page(lane_name, context.new_page())
        return lane

    def close(self, lane_name: str) -> None:
        if lane_name != "host_verifier":
            raise ValueError("only the host verifier lane may be closed independently")
        owner = self._owner
        lane = owner.lane(lane_name)
        page, lane.page = lane.page, None
        context, owner.host_context = owner.host_context, None
        errors = []
        if page is not None:
            try:
                page.close()
            except Exception as exc:
                errors.append(exc)
        if context is not None:
            try:
                context.close()
            except Exception as exc:
                errors.append(exc)
        lane.reset_for_new_page()
        if errors:
            raise errors[0]

    def reset_for_generation(self, lane_name: str, generation: str | None) -> BrowserPageState:
        lane = self.ensure_page(lane_name)
        if lane.executor_generation in {None, generation}:
            lane.executor_generation = generation
            return lane
        # A new executor generation cannot inherit page/history/freshness from
        # its predecessor. Recreate only this fixed lane; the other lane remains
        # untouched. The host lane rotates its entire isolated context so no
        # origin state survives between verifier generations.
        if lane_name == "host_verifier":
            self.close("host_verifier")
            lane = self.ensure_page("host_verifier")
            lane.executor_generation = generation
            return lane
        old_page, lane.page = lane.page, None
        if old_page is not None:
            old_page.close()
        lane.reset_for_new_page()
        lane.executor_generation = generation
        return self.ensure_page(lane_name)
