"""Host-callable verifier probe helpers.

This module is intentionally in ``agent_server``: it owns the API-first verifier
runner and can depend on the lower tools layer, while core/tools must not import
server code. REL-1 host verification can reuse these helpers without driving an
LLM tool call.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal, Protocol

from disco.tools.verify.web_app_probe import (
    collect_web_app_probe,
)
from disco.tools.verify.web_app_probe import (
    compute_verdict as compute_web_app_verdict,
)


class AppProbeClient(Protocol):
    async def fetch_app(self, url: str) -> tuple[int, bytes] | None:
        """Fetch a deployment/preview URL and return ``(status, body)`` if reachable."""
        ...

    async def fetch_preview(self, cid: str) -> tuple[int, bytes] | None:
        """Fetch the conversation live preview and return ``(status, body)`` if reachable."""
        ...


@dataclass(frozen=True)
class AppProbeTarget:
    """One concrete app response the verifier should fetch."""

    kind: Literal["deployment_url", "preview"]
    label: str
    url: str = ""


def app_probe_targets(deliverables: list[dict[str, Any]]) -> list[AppProbeTarget]:
    """Collect app probe targets from located deliverables, without doing IO."""
    targets: list[AppProbeTarget] = []
    for d in (d for d in deliverables if d.get("kind") == "app"):
        url = str(d.get("deployment_url", ""))
        root = str(d.get("path", "")).strip("/")
        if url:
            targets.append(AppProbeTarget(kind="deployment_url", label=url, url=url))
        else:
            targets.append(AppProbeTarget(kind="preview", label=root or "preview"))
    return targets


async def validate_app_deliverables(
    deliverables: list[dict[str, Any]], *, client: AppProbeClient, cid: str
) -> list[str]:
    """A live-app handoff ("build me a website") must produce REAL, reachable output.

    If it has a deployment/preview URL, that URL must be 2xx + non-empty. If it has
    NO URL (a static site served via the client-assembled preview), probe the LIVE
    PREVIEW the user actually sees through its isolated path capability, so
    declared-but-empty output fails the same way it did before this extraction.
    """
    problems: list[str] = []
    for target in app_probe_targets(deliverables):
        if target.kind == "deployment_url":
            problem = app_body_problem(await client.fetch_app(target.url), label=target.label)
        else:
            problem = app_body_problem(await client.fetch_preview(cid), label=target.label)
        if problem:
            problems.append(problem)
    return problems


def app_body_problem(result: tuple[int, bytes] | None, *, label: str) -> str | None:
    """Judge a fetched app/preview response.

    The response must be reachable, 2xx, non-empty, and not a bare directory
    listing. The preview server can fall back to ``python3 -m http.server`` on the
    workspace root when there is no real app entry file; that returns a 200 non-
    empty directory listing, which is not a real app.
    """
    if result is None:
        return f"app deliverable not reachable: {label}"
    status, body = result
    if status == 503:
        return f"app deliverable preview not available (503): {label}"
    if not (200 <= status < 300):
        return f"app deliverable returned HTTP {status}: {label}"
    if not body:
        return f"app deliverable is empty: {label}"
    head = body[:4096].lower()
    if b"directory listing for" in head:
        return f"app deliverable is a bare directory listing, not a real app: {label}"
    return None


__all__ = [
    "AppProbeClient",
    "AppProbeTarget",
    "app_body_problem",
    "app_probe_targets",
    "collect_web_app_probe",
    "compute_web_app_verdict",
    "validate_app_deliverables",
]
