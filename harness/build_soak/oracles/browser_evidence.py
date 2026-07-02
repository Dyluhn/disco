"""HARN-2 — the browser product-harness oracles.

These eight zero-opinion oracles judge the REAL product path (not just that the
engine wrote files) over the browser evidence HARN-1b captures into a single
``product_evidence`` dict. Each oracle reads its own slice; when the slice is absent
(a headless run, or HARN-1b not yet wired) it SKIPS — so these never affect headless
classification until a product-harness run supplies the evidence.

product_evidence (the HARN-1b dossier, all keys optional):
    {
      "browser_ws":   {"connections": int, "closes": [{"code": int, "reason": str}]},
      "lifecycle":    {"statuses": ["RUNNING", ...], "terminal": "FINISHED"|...},
      "sidecar":      {"stopped_at_terminal": bool, "provider_calls_after_terminal": int},
      "preview":      {"owner": "platform"|"model", "manual_port": bool,
                       "shown_to_user": bool, "url": str},
      "shown":        {"artifact_shown": bool, "preview_shown": bool},
      "verification": {"ready_for_verification_called": bool, "passed": bool},
      "export":       {"requested": bool, "download_present": bool, "download_bytes": int},
      "cleanup":      {"orphans": int, "workspace_released": bool, "scope"?: str},
    }
"""

from __future__ import annotations

from typing import Any

from .. import failure_codes as fc
from .schema import OracleResult, failing, passing, skipping

_TERMINAL_OK = frozenset({"FINISHED", "VERIFIED"})


def _slice(product_evidence: dict[str, Any] | None, key: str) -> dict[str, Any] | None:
    if not product_evidence:
        return None
    val = product_evidence.get(key)
    return val if isinstance(val, dict) else None


def _int(v: Any) -> int | None:
    """Safe int coercion: returns None for a missing/non-numeric/bool value so a
    malformed count can be handled fail-closed instead of crashing classification."""
    if isinstance(v, bool) or v is None:
        return None
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


class BrowserWSOracle:
    """The real browser opened (and kept) a build WebSocket — the 'silently dropped'
    bug class where the UI never connects yet the engine runs blind."""

    _NAME = "browser_ws"

    def check(self, *, product_evidence: dict[str, Any] | None = None) -> list[OracleResult]:
        ws = _slice(product_evidence, "browser_ws")
        if ws is None:
            return [skipping(self._NAME, reason="no browser_ws evidence (headless run)")]
        conns = _int(ws.get("connections"))
        if conns is None or conns <= 0:  # missing/malformed/zero → fail-closed
            return [
                failing(
                    self._NAME,
                    fc.BROWSER_WS_NOT_CONNECTED,
                    first_broken_link="build -> browser_ws_connected",
                    facts={"connections": ws.get("connections")},
                )
            ]
        return [passing(self._NAME, facts={"connections": conns})]


class LifecycleOracle:
    """The conversation status progressed to a clean terminal (no RUNNING-forever,
    no missing terminal)."""

    _NAME = "lifecycle"

    def check(self, *, product_evidence: dict[str, Any] | None = None) -> list[OracleResult]:
        lc = _slice(product_evidence, "lifecycle")
        if lc is None:
            return [skipping(self._NAME, reason="no lifecycle evidence (headless run)")]
        terminal = str(lc.get("terminal") or "")
        if terminal not in _TERMINAL_OK:
            return [
                failing(
                    self._NAME,
                    fc.LIFECYCLE_SEQUENCE_INVALID,
                    first_broken_link="build -> clean_terminal",
                    facts={"terminal": terminal or None, "statuses": lc.get("statuses")},
                )
            ]
        return [passing(self._NAME, facts={"terminal": terminal})]


class SidecarStopOracle:
    """The Pi sidecar (and provider calls) stopped at the terminal state — no runaway
    token burn."""

    _NAME = "sidecar_stop"

    def check(self, *, product_evidence: dict[str, Any] | None = None) -> list[OracleResult]:
        sc = _slice(product_evidence, "sidecar")
        if sc is None:
            return [skipping(self._NAME, reason="no sidecar evidence (headless run)")]
        calls_after = _int(sc.get("provider_calls_after_terminal", 0))
        # require an EXPLICIT stop + a well-formed zero post-terminal call count.
        if sc.get("stopped_at_terminal", False) is not True or calls_after is None or calls_after > 0:
            return [
                failing(
                    self._NAME,
                    fc.SIDECAR_NOT_STOPPED,
                    first_broken_link="terminal -> sidecar_stopped",
                    facts={
                        "stopped_at_terminal": sc.get("stopped_at_terminal"),
                        "provider_calls_after_terminal": sc.get("provider_calls_after_terminal"),
                    },
                )
            ]
        return [passing(self._NAME)]


class PreviewOwnershipOracle:
    """The PLATFORM owned the preview port/URL — the model never hand-served a port."""

    _NAME = "preview_ownership"

    def check(self, *, product_evidence: dict[str, Any] | None = None) -> list[OracleResult]:
        pv = _slice(product_evidence, "preview")
        if pv is None:
            return [skipping(self._NAME, reason="no preview evidence (headless run)")]
        # fail-closed: require the owner to be EXPLICITLY "platform" (a missing/unknown
        # owner cannot prove the platform owned the port) and manual_port not set.
        if pv.get("owner") != "platform" or pv.get("manual_port", False) is True:
            return [
                failing(
                    self._NAME,
                    fc.PREVIEW_OWNERSHIP_VIOLATION,
                    first_broken_link="preview -> platform_owned",
                    facts={"owner": pv.get("owner"), "manual_port": pv.get("manual_port")},
                )
            ]
        return [passing(self._NAME)]


class ShowToUserOracle:
    """The artifact/preview was actually SHOWN to the user — file existence is not the
    same as the UI presenting it."""

    _NAME = "show_to_user"

    def check(self, *, product_evidence: dict[str, Any] | None = None) -> list[OracleResult]:
        shown = _slice(product_evidence, "shown")
        if shown is None:
            return [skipping(self._NAME, reason="no shown evidence (headless run)")]
        if not (shown.get("artifact_shown", False) or shown.get("preview_shown", False)):
            return [
                failing(
                    self._NAME,
                    fc.ARTIFACT_NOT_SHOWN_TO_USER,
                    first_broken_link="finish -> shown_to_user",
                    facts={k: bool(shown.get(k, False)) for k in ("artifact_shown", "preview_shown")},
                )
            ]
        return [passing(self._NAME)]


class VerificationGateOracle:
    """Completion went THROUGH the host-owned verification finalizer (ready_for_*_
    verification), not a model self-declared finish."""

    _NAME = "verification_gate"

    def check(self, *, product_evidence: dict[str, Any] | None = None) -> list[OracleResult]:
        vf = _slice(product_evidence, "verification")
        if vf is None:
            return [skipping(self._NAME, reason="no verification evidence (headless run)")]
        # fail-closed: require BOTH flags explicitly True.
        if vf.get("ready_for_verification_called", False) is not True or vf.get("passed", False) is not True:
            return [
                failing(
                    self._NAME,
                    fc.VERIFICATION_GATE_BYPASSED,
                    first_broken_link="finish -> verification_gate",
                    facts={
                        "ready_for_verification_called": bool(vf.get("ready_for_verification_called", False)),
                        "passed": bool(vf.get("passed", False)),
                    },
                )
            ]
        return [passing(self._NAME)]


class ExportDownloadOracle:
    """When an export was requested, a real download was delivered (non-empty)."""

    _NAME = "export_download"

    def check(self, *, product_evidence: dict[str, Any] | None = None) -> list[OracleResult]:
        ex = _slice(product_evidence, "export")
        if ex is None:
            return [skipping(self._NAME, reason="no export evidence (headless run)")]
        if not ex.get("requested", False):
            return [skipping(self._NAME, reason="no export requested")]
        nbytes = _int(ex.get("download_bytes", 0))
        if ex.get("download_present", False) is not True or nbytes is None or nbytes <= 0:
            return [
                failing(
                    self._NAME,
                    fc.EXPORT_DOWNLOAD_MISSING,
                    first_broken_link="export -> download_delivered",
                    facts={
                        "download_present": ex.get("download_present"),
                        "download_bytes": ex.get("download_bytes"),
                    },
                )
            ]
        return [passing(self._NAME, facts={"download_bytes": nbytes})]


class CleanupOracle:
    """No orphan container/sandbox/preview survived the terminal state."""

    _NAME = "cleanup"

    def check(self, *, product_evidence: dict[str, Any] | None = None) -> list[OracleResult]:
        cu = _slice(product_evidence, "cleanup")
        if cu is None:
            return [skipping(self._NAME, reason="no cleanup evidence (headless run)")]
        orphans = _int(cu.get("orphans", 0))
        # fail-closed: malformed orphan count, any orphan, or a non-explicit release.
        if orphans is None or orphans > 0 or cu.get("workspace_released", False) is not True:
            return [
                failing(
                    self._NAME,
                    fc.WORKSPACE_NOT_CLEANED,
                    first_broken_link="terminal -> resources_released",
                    facts={
                        "orphans": cu.get("orphans"),
                        "workspace_released": cu.get("workspace_released"),
                        "scope": cu.get("scope"),
                    },
                )
            ]
        return [passing(self._NAME, facts={"orphans": 0, "scope": cu.get("scope")})]


# The ordered family the classifier runs (each SKIPs without its evidence slice).
BROWSER_EVIDENCE_ORACLES = (
    BrowserWSOracle,
    LifecycleOracle,
    SidecarStopOracle,
    PreviewOwnershipOracle,
    ShowToUserOracle,
    VerificationGateOracle,
    ExportDownloadOracle,
    CleanupOracle,
)
