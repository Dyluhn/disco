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
    preflight_build_sandbox_backend,
    process_sandbox_dev_opt_out_enabled,
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


# ---------------------------------------------------------------------------
# P0 — the FAIL-CLOSED Build/soak preflight (the inversion of the old fail-open opt-in)
# ---------------------------------------------------------------------------
def test_preflight_refuses_process_backend_by_default():
    # Fail-closed: with NO dev opt-out, the Build/soak preflight refuses the process
    # backend (the inversion — protection is the default, not an opt-in the operator
    # might forget). `allow_process_dev=False` makes the default explicit + hermetic.
    with pytest.raises(ProductionValidityError, match="dev-only"):
        preflight_build_sandbox_backend(ProcessSandboxService(), allow_process_dev=False)


def test_preflight_dev_opt_out_re_enables_process_backend():
    # The explicit dev opt-out re-permits the unisolated process backend (plain local dev).
    svc = ProcessSandboxService()
    assert preflight_build_sandbox_backend(svc, allow_process_dev=True) is svc


def test_preflight_container_backends_always_pass():
    # A container backend passes regardless of the opt-out flag (it IS production-valid).
    for opt in (True, False):
        for svc in (
            GvisorSandboxService(SandboxConfig()),
            LocalSandboxService(default_local_config()),
            PodmanSandboxService(default_podman_config()),
        ):
            assert preflight_build_sandbox_backend(svc, allow_process_dev=opt) is svc


def test_dev_opt_out_env_is_fail_closed(monkeypatch):
    # The opt-out reads ONLY an explicit truthy value; unset / anything else = NO.
    monkeypatch.delenv("DISCO_ALLOW_PROCESS_SANDBOX_FOR_DEV", raising=False)
    assert process_sandbox_dev_opt_out_enabled() is False
    monkeypatch.setenv("DISCO_ALLOW_PROCESS_SANDBOX_FOR_DEV", "0")
    assert process_sandbox_dev_opt_out_enabled() is False
    monkeypatch.setenv("DISCO_ALLOW_PROCESS_SANDBOX_FOR_DEV", "nope")
    assert process_sandbox_dev_opt_out_enabled() is False
    for truthy in ("1", "true", "YES", "on"):
        monkeypatch.setenv("DISCO_ALLOW_PROCESS_SANDBOX_FOR_DEV", truthy)
        assert process_sandbox_dev_opt_out_enabled() is True
    # default-resolution path (no explicit arg) honors the env, fail-closed
    monkeypatch.delenv("DISCO_ALLOW_PROCESS_SANDBOX_FOR_DEV", raising=False)
    with pytest.raises(ProductionValidityError):
        preflight_build_sandbox_backend(ProcessSandboxService())


# ---------------------------------------------------------------------------
# P1 #4 — the process host-signal refusal is BEST-EFFORT dev-only, NOT containment.
# The production path never relies on it (it refuses the backend outright).
# ---------------------------------------------------------------------------
def test_host_signal_refusal_is_documented_best_effort_not_containment():
    from disco.tools.sandbox.process import process_backend_signal_command_violation

    doc = (process_backend_signal_command_violation.__doc__ or "").lower()
    assert "best-effort" in doc
    assert "not containment" in doc
    assert "bypassable" in doc  # explicitly acknowledges it's trivially evadable


def test_signal_refusal_call_site_does_not_overclaim_containment():
    # [P2] The ENFORCEMENT call site (ProcessSandboxInstance.exec_shell) must describe the
    # host-signal refusal as best-effort dev-only, NOT as "additional containment" — the
    # real containment is the P0 production-validity gate refusing the process backend.
    import inspect

    from disco.tools.sandbox.process import ProcessSandboxInstance

    src = inspect.getsource(ProcessSandboxInstance.exec_shell)
    # isolate the Bug-19 call-site comment block (up to the actual refusal call)
    head, _sep, _rest = src.partition("signal_why = process_backend_signal_command_violation")
    _b19, _s, block = head.partition("# Bug 19")
    block = block.lower()
    assert "additional containment" not in block  # the round-1 over-claim is gone
    assert "best-effort" in block
    assert "dev-only" in block


def test_production_path_never_relies_on_the_signal_refusal():
    # The REAL containment: production-validity refuses the process backend, so a
    # prod/soak build is never on the shared-host box that the refusal would "protect".
    assert ProcessSandboxService().is_production_valid is False
    with pytest.raises(ProductionValidityError):
        preflight_build_sandbox_backend(ProcessSandboxService(), allow_process_dev=False)
