"""Origin-approval authority for the config store — a thin facade over
`OriginApprovalStore`, parameterized by the config file's path.

Split out of `ConfigStore` (PY-0459): an approval registry is not a config
file writer — `app_server/origin_approval_wiring.py` already treats it as a
separate concern. `ConfigStore` exposes an instance of this class as the
plain attribute `approvals` (not a `@property` — see `config_sections.py`
for why that distinction matters to the public-method count).
"""

from __future__ import annotations

from pathlib import Path

from ..origin_approvals import OriginApprovalStore
from .secrets import SecretStore


class ConfigOriginApprovals:
    """Origin-approval registry for one config file's URLs, keyed by purpose.

    Each call builds a fresh `OriginApprovalStore` scoped to the config
    path — identical behaviour to when these were `ConfigStore` methods."""

    def __init__(self, path: Path) -> None:
        self._path = path

    def approval_store(self, *, secret_store: SecretStore | None = None) -> OriginApprovalStore:
        return OriginApprovalStore(config_path=self._path, secret_store=secret_store)

    def origin_approved(
        self,
        url: str,
        purpose: str,
        secret_ref: str | None = "",
        *,
        secret_store: SecretStore | None = None,
    ) -> bool:
        return self.approval_store(secret_store=secret_store).is_approved(url, purpose, secret_ref)

    def approve_origin(
        self,
        url: str,
        purpose: str,
        secret_ref: str | None = "",
        *,
        secret_store: SecretStore | None = None,
    ) -> None:
        self.approval_store(secret_store=secret_store).approve(url, purpose, secret_ref)

    def replace_origin_purpose(
        self,
        url: str,
        purpose: str,
        secret_refs: tuple[str, ...] = ("",),
        *,
        secret_store: SecretStore | None = None,
    ) -> None:
        self.approval_store(secret_store=secret_store).replace_purpose(url, purpose, secret_refs)

    def revoke_origin_purpose(
        self,
        purpose: str,
        *,
        secret_store: SecretStore | None = None,
    ) -> int:
        return self.approval_store(secret_store=secret_store).revoke_purpose(purpose)
