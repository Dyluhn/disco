"""W3 C-1 — the sandbox backend is a fail-CLOSED allowlist.

Before: `backend` was a free `str`; `service_from_config` fell THROUGH to
`ProcessSandboxService` (host execution) for any unknown value, and the wire DTO
accepted garbage. A typo or a poisoned config write silently downgraded the
isolation boundary to the host. Now:

  * the dispatch RAISES on an unknown backend (never a silent host downgrade);
  * the runtime + wire models constrain `backend` to a `Literal` (construction /
    422 rejection);
  * the PERSISTED model coerces an unknown value to the most-isolated backend
    (`gvisor`) so a corrupt config never crashes the whole config load.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError


def test_dispatch_fails_closed_on_unknown_backend():
    from disco.tools.sandbox import service_from_config
    from disco.tools.sandbox.config import SandboxConfig

    # Bypass field validation (model_construct) to simulate a value that somehow
    # reached the dispatch — it must RAISE, not resolve to ProcessSandboxService.
    cfg = SandboxConfig.model_construct(backend="garbage")
    with pytest.raises(ValueError, match="unknown sandbox backend"):
        service_from_config(cfg)


def test_dispatch_process_requires_explicit_backend():
    from disco.tools.sandbox import service_from_config
    from disco.tools.sandbox.config import SandboxConfig
    from disco.tools.sandbox.process import ProcessSandboxService

    # `process` is reachable ONLY by naming it explicitly (still gated on the
    # Build path by the dev opt-out) — never by fall-through.
    svc = service_from_config(SandboxConfig(backend="process"))
    assert isinstance(svc, ProcessSandboxService)


def test_runtime_config_rejects_unknown_backend():
    from disco.tools.sandbox.config import SandboxConfig

    with pytest.raises(ValidationError):
        SandboxConfig(backend="garbage")
    # …but every valid backend constructs fine.
    for b in ("gvisor", "local", "podman", "process"):
        assert SandboxConfig(backend=b).backend == b


def test_wire_dto_rejects_unknown_backend():
    from disco.app_server.config.dtos import SandboxConfigDTO

    with pytest.raises(ValidationError):
        SandboxConfigDTO(
            backend="garbage", docker_socket="", podman_url="", runtime="",
            image="", workspace_root="/tmp/x", connections={},
        )


def test_persisted_settings_coerce_unknown_to_gvisor_not_host():
    from disco.core.llm.config import SandboxSettings

    # A corrupt/legacy persisted backend must NOT raise (that would nuke the whole
    # config on load) and must NOT become `process` (host). It coerces to gvisor.
    s = SandboxSettings.model_validate({"backend": "e2b-legacy"})
    assert s.backend == "gvisor"
    # …AND the OCI runtime is forced to runsc, so it's ACTUALLY gVisor-isolated,
    # not a plain-runc container mislabelled gVisor.
    assert s.runtime == "runsc"
    # A valid value (and its runtime) is untouched.
    podman = SandboxSettings.model_validate({"backend": "podman", "runtime": "crun"})
    assert podman.backend == "podman"
    assert podman.runtime == "crun"
