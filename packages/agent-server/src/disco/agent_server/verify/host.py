"""REL-1c host-owned verifier.

Runs the web-app diagnostics from the host/runtime path without emitting an
agent ``ActionEvent``. The returned payload is intentionally shaped like the
existing ``verify_web_app`` structured verdict so core can compare host-vs-inline
results without depending on agent-server internals.
"""

from __future__ import annotations

import logging
from typing import Any

from disco.core.loop import HostVerificationDeliverable
from disco.tools.builtin.verify_app import (
    VerifyWebAppArgs,
    VerifyWebAppTool,
)
from disco.tools.builtin.verify_app import (
    compute_verdict as verify_app_compute_verdict,
)

from .probe import app_body_problem, collect_web_app_probe, compute_web_app_verdict

_LOG = logging.getLogger(__name__)


class HostWebAppVerifier:
    """Host-side web verifier for REL-1 shadow mode.

    The preferred path reuses ``VerifyWebAppTool`` directly with a host-built
    tool context from the runtime's executor. This drives the same browser/probe
    logic as the inline tool call but does not append an ``ActionEvent``.

    A small client fallback exists for verifier tests and executor-less hosts; it
    uses the REL-1a app-response helpers plus the shared verdict builder.
    """

    def __init__(self, executor: Any | None = None, *, client: Any | None = None) -> None:
        self._executor = executor
        self._client = client

    async def verify(self, deliverable: HostVerificationDeliverable) -> dict[str, Any]:
        ctx_builder: Any = getattr(self._executor, "_build_context", None)
        if ctx_builder is not None:
            try:
                ctx = await ctx_builder(VerifyWebAppTool.definition)
                outcome = await VerifyWebAppTool().run(
                    VerifyWebAppArgs(url=deliverable.deployment_url or ""),
                    ctx,
                )
                if outcome.success and outcome.structured:
                    return dict(outcome.structured)
                return self._unavailable_verdict(
                    deliverable,
                    outcome.error or outcome.content or "host verifier produced no verdict",
                )
            except Exception as exc:  # noqa: BLE001 — shadow verifier degrades to telemetry
                _LOG.warning(
                    "host web verifier failed for %s:%s",
                    deliverable.conversation_id,
                    deliverable.artifact_path,
                    exc_info=True,
                )
                return self._unavailable_verdict(deliverable, str(exc))

        if self._client is not None:
            return await self._verify_via_client(deliverable)

        return self._unavailable_verdict(deliverable, "no host verifier context available")

    async def _verify_via_client(self, deliverable: HostVerificationDeliverable) -> dict[str, Any]:
        client = self._client
        if client is None:  # caller guards; keep the method total for the checker
            return self._unavailable_verdict(deliverable, "no client")
        url = deliverable.deployment_url or (
            f"/conversations/{deliverable.conversation_id}/preview-app/"
        )
        if deliverable.deployment_url:
            fetched = await client.fetch_app(deliverable.deployment_url)
            label = deliverable.deployment_url
        else:
            fetched = await client.fetch_preview(deliverable.conversation_id)
            label = deliverable.artifact_path or "preview"

        reachable = fetched is not None
        status, body = fetched if fetched is not None else (0, b"")
        problem = app_body_problem(fetched, label=label)
        structured = {
            "title": "",
            "text": body[:4096].decode("utf-8", "replace"),
            "elements": [],
            "console": [],
            "network": [],
            "screenshot_path": "",
        }
        # Touch the extracted collector explicitly so this fallback composes the
        # same diagnostics normalizer the browser path uses.
        collect_web_app_probe(structured)
        verdict = compute_web_app_verdict(
            url=url,
            reachable=reachable,
            http_status=int(status or 0),
            structured=structured,
            meaningful=problem is None,
        )
        if problem:
            verdict = dict(verdict)
            verdict["passed"] = False
            verdict["verdict"] = "fail"
            verdict["summary"] = problem
            verdict["next_action"] = "Serve a reachable, non-empty web app preview."
        return verdict

    @staticmethod
    def _unavailable_verdict(
        deliverable: HostVerificationDeliverable, detail: str
    ) -> dict[str, Any]:
        verdict = verify_app_compute_verdict(
            url=deliverable.deployment_url or "",
            reachable=False,
            http_status=0,
            structured=None,
            meaningful=False,
        )
        verdict = dict(verdict)
        verdict["verdict"] = "unavailable"
        verdict["summary"] = f"host verifier unavailable: {detail}"
        verdict["next_action"] = ""
        verdict["failure_fingerprint"] = "host_verifier_unavailable"
        return verdict


__all__ = ["HostWebAppVerifier"]
