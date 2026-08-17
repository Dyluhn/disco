"""Secrets, capabilities & mediation — tool-sandbox-contract.md §6.

The absolute rule (BoD Principle 4 / §17.1): **no secret is ever readable from
inside the sandbox.** Secrets live only in the orchestrator's `SecretsStore`.
Tools that need a credential receive a scoped, short-lived *capability* — a
handle to an orchestrator-mediated action — never the credential. The privileged
call runs orchestrator-side; only its result crosses into the tool.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Protocol, runtime_checkable


class CapabilityDenied(Exception):
    """Raised when a tool invokes a capability it was not granted (§6 rule 4)."""


@runtime_checkable
class SecretsStore(Protocol):
    """[CONTRACT] Orchestrator-held secrets. v1 is in-memory/file-encrypted; the
    interface is what keeps a real secrets manager a swap. Read ONLY by
    capability handlers (orchestrator-side), never by tools."""

    def get(self, name: str) -> str | None: ...


class InMemorySecretsStore:
    """v1 [INTERIOR] secrets store. (File-encrypted variant deferred; the
    `SecretsStore` interface is the seam.)"""

    def __init__(self, secrets: dict[str, str] | None = None) -> None:
        self._secrets = dict(secrets or {})

    def get(self, name: str) -> str | None:
        return self._secrets.get(name)

    def set(self, name: str, value: str) -> None:
        self._secrets[name] = value


# A capability handler is an orchestrator-side coroutine that performs the
# privileged action. It may close over the SecretsStore — the secret stays in
# the closure, never reaching the tool/sandbox.
CapabilityHandler = Callable[..., Awaitable[object]]


class CapabilitySet:
    """[CONTRACT] Scoped grants handed to a tool via ToolContext — NOT secrets.
    `call(name, **kwargs)` runs the orchestrator-mediated action; the credential
    stays orchestrator-side. A tool requesting an ungranted capability gets
    `CapabilityDenied` (the executor maps it to a `denied` ToolResult)."""

    def __init__(self, granted: frozenset[str], handlers: dict[str, CapabilityHandler]) -> None:
        self._granted = granted
        self._handlers = handlers

    def has(self, name: str) -> bool:
        return name in self._granted and name in self._handlers

    async def call(self, capability: str, **kwargs: object) -> object:
        if not self.has(capability):
            raise CapabilityDenied(capability)
        return await self._handlers[capability](**kwargs)


class CapabilityBroker:
    """Orchestrator-side registry of capability handlers. Grants scoped
    CapabilitySets to tools (only the capabilities a tool declares in
    `uses_capabilities`, intersected with what's registered). The kill switch
    calls `revoke_all()` to drop every grant (§6.4)."""

    def __init__(self) -> None:
        self._handlers: dict[str, CapabilityHandler] = {}
        self._revoked = False

    def register(self, name: str, handler: CapabilityHandler) -> None:
        if self._revoked:
            raise RuntimeError("capability broker has been revoked")
        self._handlers[name] = handler

    def grant(self, capabilities: frozenset[str]) -> CapabilitySet:
        if self._revoked:
            return CapabilitySet(frozenset(), {})  # all grants dropped
        return CapabilitySet(capabilities, self._handlers)

    def revoke_all(self) -> None:
        """Kill-switch hook: drop all capabilities (§6.4)."""
        self._revoked = True
        # CapabilitySets intentionally share this mapping. Clear it in place so
        # contexts issued before kill are revoked too; replacing the mapping
        # would leave their old handlers callable.
        self._handlers.clear()
