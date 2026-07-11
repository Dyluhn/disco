"""EPIC H (P1) — deployment config is the resource MAXIMUM, not a fallback.

The old `spec.X or cfg.default_X` resolution let a model-influenced SandboxSpec set ANY
value: `pids=0` read as "unset" but a raw 0 to Docker means UNLIMITED pids (the model
could DISABLE the fork-bomb cap), and `cpu=64` sailed above the deployment's intent.
`resolve_bounds` makes the config the hard ceiling — a spec may TIGHTEN a bound but can
never loosen it above the max nor disable a limit; negatives are rejected.
"""

from __future__ import annotations

import pytest
from disco.tools.sandbox import SandboxConfig, SandboxError, SandboxSpec
from disco.tools.sandbox._container import resolve_bounds


def _cfg(**kw) -> SandboxConfig:
    base = {
        "default_cpu": 4.0,
        "default_memory_mb": 2048,
        "default_pids_limit": 512,
        "default_disk_mb": 4096,
    }
    base.update(kw)
    return SandboxConfig(**base)


def test_unset_sentinel_resolves_to_the_configured_default():
    # A spec leaving cpu/mem/pids at 0 (the unset sentinel) → the deployment default/max.
    cpu, mem, pids, disk = resolve_bounds(
        SandboxSpec(cpu=0, memory_mb=0, pids=0, disk_mb=0), _cfg()
    )
    assert (cpu, mem, pids, disk) == (4.0, 2048, 512, 4096)


def test_pids_zero_never_means_unlimited():
    # The core finding: pids=0 must NOT pass 0 (Docker "unlimited") through — it resolves
    # to the configured cap so the fork-bomb guard can't be disabled by the spec.
    _cpu, _mem, pids, _disk = resolve_bounds(SandboxSpec(pids=0), _cfg(default_pids_limit=256))
    assert pids == 256


def test_spec_may_tighten_below_the_max():
    # Requesting LESS than the deployment max is honored (a spec can tighten).
    cpu, mem, pids, disk = resolve_bounds(
        SandboxSpec(cpu=1.0, memory_mb=512, pids=64, disk_mb=1024), _cfg()
    )
    assert (cpu, mem, pids, disk) == (1.0, 512, 64, 1024)


@pytest.mark.parametrize(
    "spec_kw,expected",
    [
        ({"pids": 100_000}, ("pids", 512)),
        ({"cpu": 64.0}, ("cpu", 4.0)),
        ({"memory_mb": 1_000_000}, ("memory_mb", 2048)),
        ({"disk_mb": 1_000_000}, ("disk_mb", 4096)),
    ],
)
def test_above_max_is_clamped_down(spec_kw, expected):
    # A model spec can NEVER raise a limit above the deployment maximum — it's clamped.
    field, want = expected
    cpu, mem, pids, disk = resolve_bounds(SandboxSpec(**spec_kw), _cfg())
    got = {"cpu": cpu, "memory_mb": mem, "pids": pids, "disk_mb": disk}[field]
    assert got == want


@pytest.mark.parametrize(
    "spec_kw", [{"pids": -1}, {"cpu": -2.0}, {"memory_mb": -512}, {"disk_mb": -1}]
)
def test_negative_values_are_rejected(spec_kw):
    # A negative resource value is invalid and rejected (never smuggled to the runtime).
    with pytest.raises(SandboxError, match="invalid"):
        resolve_bounds(SandboxSpec(**spec_kw), _cfg())
