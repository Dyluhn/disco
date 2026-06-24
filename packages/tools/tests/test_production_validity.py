"""EPIC H (§1.4/§9.3) — the sandbox production-validity gate.

The `process` backend runs builds as bare host subprocesses in the SHARED host PID +
network namespace — it is the source of the isolation incidents (a build `kill <pid>`
took down the agent-server). It is dev-only. The container backends (gvisor/local/
podman) each have their OWN PID + network namespace and ARE production-valid.

These hermetic tests prove (a) the `is_production_valid` flag is correct on every
backend, and (b) `require_production_valid_backend` REFUSES the process backend with an
actionable error while passing the container backends through unchanged.
"""

from __future__ import annotations

import pytest
from disco.tools.sandbox import (
    GvisorSandboxService,
    LocalSandboxService,
    PodmanSandboxService,
    ProcessSandboxService,
    ProductionValidityError,
    SandboxConfig,
    default_local_config,
    default_podman_config,
    require_production_valid_backend,
    service_from_config,
)


def test_process_backend_is_not_production_valid():
    assert ProcessSandboxService().is_production_valid is False


def test_container_backends_are_production_valid():
    # Constructed with injected fake clients = None is fine; the flag is a class attr,
    # read without touching any runtime.
    assert GvisorSandboxService(SandboxConfig()).is_production_valid is True
    assert LocalSandboxService(default_local_config()).is_production_valid is True
    assert PodmanSandboxService(default_podman_config()).is_production_valid is True


def test_local_inherits_production_valid_from_gvisor():
    # LocalSandboxService subclasses GvisorSandboxService — the flag must carry over.
    assert LocalSandboxService(default_local_config()).is_production_valid is True


def test_require_production_valid_refuses_process_backend():
    svc = ProcessSandboxService()
    with pytest.raises(ProductionValidityError) as exc:
        require_production_valid_backend(svc)
    msg = str(exc.value)
    # Actionable: names the offending backend AND the valid alternatives.
    assert "process" in msg
    assert "dev-only" in msg
    assert "Local" in msg and "Podman" in msg and "gVisor" in msg


def test_require_production_valid_passes_container_backends_through():
    for svc in (
        GvisorSandboxService(SandboxConfig()),
        LocalSandboxService(default_local_config()),
        PodmanSandboxService(default_podman_config()),
    ):
        assert require_production_valid_backend(svc) is svc  # unchanged, no raise


def test_gate_consumes_the_factory_selection():
    # The same factory the live builder uses → process backend → refused.
    process_svc = service_from_config(SandboxConfig(backend="process"))
    with pytest.raises(ProductionValidityError):
        require_production_valid_backend(process_svc)
    # …and a container backend from the SAME factory passes.
    gvisor_svc = service_from_config(SandboxConfig(backend="gvisor"))
    assert require_production_valid_backend(gvisor_svc) is gvisor_svc
