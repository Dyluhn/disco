"""The narrow endpoint contract the Stripe exploit checks actually require.

The checks talk to *a booted Worker endpoint*: they issue authenticated HTTP
requests and read D1 row counts back. Two concrete implementations satisfy that
and both are used in production paths — :class:`~.workerd_lifecycle._WorkerdApp`
(workerd directly) and :class:`~.probe_script._MiniflareRuntimeProbe` (the
Miniflare runtime probe that routes the configured bus URL through a host-owned
outbound adapter). Neither is a subclass of the other.

Binding the checks to this Protocol rather than to one concrete class is what
makes that substitution honest: a check that only issues requests and counts
rows should not claim to need workerd's process lifecycle, filesystem layout or
dev-log surface. ``_check_secret_absence`` genuinely does need those
(``app_dir``/``bundle_dir``/``dev_log_path``) and therefore keeps the concrete
``_WorkerdApp`` type.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Protocol, runtime_checkable


@runtime_checkable
class WorkerEndpoint(Protocol):
    """A booted Worker that answers authenticated requests and D1 counts."""

    async def request_async(
        self,
        method: str,
        path: str,
        body: bytes | None = None,
        headers: Mapping[str, str] | None = None,
        token: str | None = None,
    ) -> tuple[int, str]: ...

    async def post_json_async(
        self,
        path: str,
        obj: Mapping[str, object],
        *,
        token: str | None = None,
    ) -> tuple[int, str]: ...

    async def get_async(self, path: str, *, token: str | None = None) -> tuple[int, str]: ...

    async def d1_count_async(self, table: str) -> int: ...
