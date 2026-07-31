"""Narrow typed evidence-source port for host web-app verification (DM-013).

The host verifier must not directly reach an :class:`HttpVerifyClient`, a
product-private success endpoint, or mutable workspace state.  This module
declares the narrow typed port through which a *client compatibility adapter*
may acquire **immutable evidence** (raw fetched bytes).  Only
:class:`HostWebAppVerifier` can produce the typed
:class:`~disco.core.verification.HostVerificationResult`; the adapter cannot
publish or upgrade a verdict.

The port is deliberately minimal: it exposes only the two evidence-acquisition
methods the executor-less fallback needs.  It does not expose conversation
creation, WebSocket exchange, event polling, artifact download, report export,
or any other client capability.  A client that implements
:class:`~disco.agent_server.verify.runner.AbstractVerifyClient` is wrapped by
:class:`ClientEvidenceAdapter` so the verifier never sees the full client
surface.
"""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable


@runtime_checkable
class EvidenceSource(Protocol):
    """Narrow typed port for acquiring immutable web-app evidence.

    A client compatibility adapter implements this port.  It may fetch raw
    bytes from a deployment URL or a conversation preview, but it **cannot**
    publish or upgrade a verdict.  Only
    :class:`~disco.agent_server.verify.host.HostWebAppVerifier` produces the
    typed :class:`~disco.core.verification.HostVerificationResult`.
    """

    async def fetch_app(self, url: str) -> tuple[int, bytes] | None:
        """Fetch a deployment/preview URL and return ``(status, body)`` if reachable."""
        ...

    async def fetch_preview(self, cid: str) -> tuple[int, bytes] | None:
        """Fetch the conversation live preview and return ``(status, body)`` if reachable."""
        ...


class ClientEvidenceAdapter:
    """State-free compatibility adapter wrapping a verify client.

    Wraps any object that provides ``fetch_app`` and ``fetch_preview`` (e.g.
    :class:`~disco.agent_server.verify.runner.AbstractVerifyClient`) so the
    host verifier sees only the narrow :class:`EvidenceSource` port.  The
    adapter holds no mutable state of its own and cannot publish or upgrade a
    verdict — it only forwards evidence acquisition.

    This preserves the existing ``client=`` constructor behaviour of
    :class:`~disco.agent_server.verify.host.HostWebAppVerifier`: callers that
    pass ``client=some_client`` get the same evidence path, but the verifier
    never directly reaches the full client surface.
    """

    __slots__ = ("_client",)

    def __init__(self, client: Any) -> None:
        self._client = client

    async def fetch_app(self, url: str) -> tuple[int, bytes] | None:
        return await self._client.fetch_app(url)

    async def fetch_preview(self, cid: str) -> tuple[int, bytes] | None:
        return await self._client.fetch_preview(cid)


def _ensure_evidence_source(client: Any | None) -> EvidenceSource | None:
    """Wrap a raw client in the compatibility adapter, or return None.

    If *client* is already a :class:`ClientEvidenceAdapter` (or a custom
    :class:`EvidenceSource` implementation that is not a raw client), it is
    returned as-is.  Otherwise it is wrapped in :class:`ClientEvidenceAdapter`
    so the verifier never sees the full client surface.

    The distinction is structural: a raw client (e.g.
    :class:`~disco.agent_server.verify.runner.HttpVerifyClient`) has many
    methods beyond ``fetch_app``/``fetch_preview``.  Wrapping it in
    :class:`ClientEvidenceAdapter` narrows the surface to just the
    :class:`EvidenceSource` port.  A caller that already provides a
    :class:`ClientEvidenceAdapter` or a custom :class:`EvidenceSource`
    implementation is not double-wrapped.
    """
    if client is None:
        return None
    if isinstance(client, ClientEvidenceAdapter):
        return client
    return ClientEvidenceAdapter(client)


__all__ = [
    "ClientEvidenceAdapter",
    "EvidenceSource",
    "_ensure_evidence_source",
]
