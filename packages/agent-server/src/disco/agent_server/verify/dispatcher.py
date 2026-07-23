"""Host-owned verifier dispatch by admitted target/check identity."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Protocol

from disco.core.loop import HostVerificationDeliverable
from disco.core.verification import unavailable_verification_result

VerifierKey = tuple[str, str, str]


class VerificationAdapter(Protocol):
    async def verify(self, deliverable: HostVerificationDeliverable) -> dict[str, Any]: ...


class HostVerifierDispatcher:
    """Exact registry; an absent adapter yields a typed unavailable receipt."""

    def __init__(
        self,
        adapters: Mapping[VerifierKey, VerificationAdapter],
        *,
        legacy_adapter: VerificationAdapter | None = None,
    ) -> None:
        self._adapters = dict(adapters)
        self._legacy_adapter = legacy_adapter

    async def verify(self, deliverable: HostVerificationDeliverable) -> dict[str, Any]:
        check = deliverable.verification_check
        if check is None:
            if self._legacy_adapter is not None:
                return await self._legacy_adapter.verify(deliverable)
            return self._unavailable(deliverable, "verification has no admitted check identity")
        adapter = self._adapters.get((check.issuer_id, check.receipt_kind, check.operation))
        if adapter is None:
            return self._unavailable(
                deliverable,
                "no registered target verifier owns "
                f"{check.issuer_id}/{check.receipt_kind}/{check.operation}",
            )
        return await adapter.verify(deliverable)

    async def bind_execution(
        self,
        deliverable: HostVerificationDeliverable,
    ) -> HostVerificationDeliverable:
        """Let the exact target adapter report an opaque runtime generation.

        The dispatcher does not infer simulator/device/desktop identity from a
        filename, framework, or unrelated Preview. Adapters that do not expose
        this optional seam retain the incoming binding; the finish gate separately
        requires that binding to match the check's admitted execution modality.
        """

        check = deliverable.verification_check
        if check is None:
            return deliverable
        adapter = self._adapters.get((check.issuer_id, check.receipt_kind, check.operation))
        binder = getattr(adapter, "bind_execution", None) if adapter is not None else None
        if binder is None:
            return deliverable
        bound = await binder(deliverable)
        if not isinstance(bound, HostVerificationDeliverable):
            raise TypeError("target verifier execution binder returned an invalid authority")
        original = deliverable.model_dump(mode="json", exclude={"execution_identity"})
        candidate = bound.model_dump(mode="json", exclude={"execution_identity"})
        if original != candidate:
            raise ValueError("target verifier execution binder changed non-execution authority")
        return bound

    @staticmethod
    def _unavailable(
        deliverable: HostVerificationDeliverable,
        reason: str,
    ) -> dict[str, Any]:
        receipt = unavailable_verification_result(
            deliverable=deliverable,
            reason=reason,
        )
        return {
            "passed": False,
            "verdict": "unavailable",
            "url": "",
            "summary": reason,
            "detail": reason,
            "next_action": "",
            "failures": [{"kind": "verifier_unavailable", "message": reason}],
            "failure_fingerprint": "target_verifier_unavailable",
            "verification_result": receipt.model_dump(mode="json"),
        }


__all__ = ["HostVerifierDispatcher", "VerificationAdapter", "VerifierKey"]
