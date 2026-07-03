"""FinishGate facade and finish-path compatibility exports."""

from __future__ import annotations

from typing import TYPE_CHECKING

from .common import *
from .common import (
    _DICTATED_CONTENT_REFUSAL_CAP,
    _DOD_REFUSAL_CAP,
    _EXECUTION_NUDGE,
    _EXECUTION_NUDGE_CAP,
    _FINISH_VERIFY_CAP,
    _PREVIEW_PORTS,
    _DoDWorkspaceUnavailable,
    _app_verify_command,
    _browser_content_meaningful,
    _browser_verified,
    _is_web_deliverable,
    _last_productive_seq,
    _latest_browser_error,
    _latest_browser_structured,
    _latest_verify_verdict,
    _preview_key,
    _prior_verify_marker_fp,
    _static_verify_command,
    _url_targets_preview,
)
from .content_gates import _ContentGateMixin
from .finalize import _FinalizeMixin
from .verify_gates import (
    _BrowserVerifyGateMixin,
    _ExportRenderGateMixin,
    _FinishVerifyMixin,
    _HostVerifyGateMixin,
    _RenderVerifyGateMixin,
)

if TYPE_CHECKING:
    from ..engine import AgentLoop


class FinishGate(
    _FinalizeMixin,
    _ContentGateMixin,
    _FinishVerifyMixin,
    _HostVerifyGateMixin,
    _BrowserVerifyGateMixin,
    _ExportRenderGateMixin,
    _RenderVerifyGateMixin,
):
    """Thin facade preserving the original FinishGate public API."""

    def __init__(self, loop: AgentLoop) -> None:
        self._loop = loop
