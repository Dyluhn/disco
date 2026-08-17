"""Completeness-sweep hardening (NEW beyond prior rounds):

P1 #1 — resource bounds must not be disablable via a mis-set CONFIG. The deployment
        MAXIMA (default_* and sidecar_*) are the host-protection ceiling, so a
        0/negative/NaN/inf value is a DISABLED cap (Docker reads 0 as "unlimited";
        a non-finite maximum makes resolve_bounds' clamp a no-op). Construction must
        reject it, and resolve_bounds refuses a non-finite/<=0 maximum at the last gate.

P1 #2 — filtered-egress setup (`_setup_filtered_egress`) runs BEFORE the guarded
        sandbox create, so a partial failure (connect/start/proxy inject) must not
        strand the internal network + proxy sidecar. The method now owns its cleanup
        on EVERY exception, on all three container backends (gVisor / local / podman).

P2 #3 — podman orphan reconciliation (`destroy_by_conversation`) must remove the
        labeled `disco-egr-*` egress networks too, not only the labeled containers —
        mirroring the gVisor path — else a crash/restart strands them.

H220 — startup reconciliation must also remove old detached product-labeled auxiliary
        networks/volumes when no sandbox container survives to supply a conversation ID.
"""

from __future__ import annotations

import math
from typing import Any

import pytest
from disco.tools.sandbox import (
    GvisorSandboxService,
    LocalSandboxService,
    PodmanSandboxService,
    SandboxConfig,
    SandboxError,
    SandboxSpec,
    default_local_config,
    default_podman_config,
)
from disco.tools.sandbox._container import reconcile_orphan_aux_resources, resolve_bounds
from pydantic import ValidationError

# ---------------------------------------------------------------------------
# P1 #1 — config validation rejects a non-finite / non-positive resource maximum.
# ---------------------------------------------------------------------------

_BOUND_FIELDS = [
    "default_cpu",
    "default_memory_mb",
    "default_pids_limit",
    "sidecar_cpu",
    "sidecar_memory_mb",
    "sidecar_pids_limit",
]
# 0 and negative apply to every field; NaN/inf only land on the FLOAT fields
# (pydantic's int coercion already rejects non-finite for the int fields, but we
# still assert the construction fails — by either the validator or the coercion).
_BAD_VALUES = [0, -1, -2.5, float("nan"), float("inf"), float("-inf")]


@pytest.mark.parametrize("field", _BOUND_FIELDS)
@pytest.mark.parametrize("bad", _BAD_VALUES)
def test_config_rejects_bad_resource_maximum_at_construction(field: str, bad: Any) -> None:
    """A 0/negative/NaN/inf configured maximum (sandbox OR sidecar) is rejected when
    the SandboxConfig is built — a mis-set deployment can never silently ship an
    unbounded sandbox or proxy sidecar."""
    # Rejected by our field validator (0/negative/non-finite) OR pydantic's int coercion
    # (non-finite → int) — either way construction does NOT succeed.
    with pytest.raises(ValidationError):
        SandboxConfig(**{field: bad})


def test_config_accepts_valid_positive_bounds() -> None:
    """Sanity: legitimate finite-positive maxima still construct fine (no false reject)."""
    cfg = SandboxConfig(
        default_cpu=2.0,
        default_memory_mb=1024,
        default_pids_limit=256,
        sidecar_cpu=0.5,
        sidecar_memory_mb=128,
        sidecar_pids_limit=64,
    )
    assert cfg.default_cpu == 2.0 and cfg.sidecar_pids_limit == 64


@pytest.mark.parametrize("bad_max", [0, -1.0, float("nan"), float("inf")])
def test_resolve_bounds_refuses_non_finite_or_nonpositive_maximum(bad_max: float) -> None:
    """resolve_bounds is the LAST gate before the runtime: even if a bad maximum somehow
    reaches it (config built bypassing validation), it refuses rather than passing an
    unbounded/invalid value to Docker. We bypass the field validators with model_construct
    to prove the runtime gate stands on its own."""
    cfg = SandboxConfig.model_construct(default_cpu=bad_max)  # type: ignore[arg-type]
    with pytest.raises(SandboxError, match="invalid"):
        resolve_bounds(SandboxSpec(cpu=1.0), cfg)


def test_resolve_bounds_rejects_non_finite_spec_value() -> None:
    """A NaN/inf spec value is rejected before the clamp (min(NaN, max) is order-dependent
    and could otherwise smuggle a non-finite value to the runtime)."""
    cfg = SandboxConfig()
    with pytest.raises(SandboxError, match="invalid"):
        resolve_bounds(SandboxSpec(cpu=float("nan")), cfg)
    assert math.isfinite(cfg.default_cpu)  # the config itself is fine; the SPEC was bad


# ---------------------------------------------------------------------------
# P1 #2 — filtered-egress setup cleans up its partial creation on failure
# (gVisor / local / podman). We inject a failure at `sidecar.start()` — after BOTH
# the internal network and the sidecar exist — and assert both are torn down.
# ---------------------------------------------------------------------------


class _LeakNet:
    """A fake internal network that records whether it was removed."""

    def __init__(self, name: str) -> None:
        self.name = name
        self.id = "net-" + name
        self.removed = False
        self.connected: list[Any] = []

    def connect(self, container: Any, aliases: Any = None) -> None:
        self.connected.append(container)

    def remove(self) -> None:
        self.removed = True


class _LeakSidecar:
    """A fake sidecar whose .start() raises — the mid-setup failure point."""

    def __init__(self, name: str) -> None:
        self.name = name
        self.removed = False
        self.started = False
        self.attrs: dict = {}

    def start(self) -> None:
        raise RuntimeError("simulated sidecar start failure mid-setup")

    def remove(self, force: bool = False) -> None:
        self.removed = True


class _LeakNetworks:
    def __init__(self) -> None:
        self.created: list[_LeakNet] = []

    def create(self, name: str, **kwargs: Any) -> _LeakNet:
        net = _LeakNet(name)
        self.created.append(net)
        return net


class _LeakClient:
    """Minimal docker/podman-py stand-in for the egress-setup leak test: a network
    create that succeeds + a container create that succeeds but whose start() fails."""

    def __init__(self) -> None:
        self.networks = _LeakNetworks()
        self.containers = self
        self.created: list[_LeakSidecar] = []

    def create(self, **kwargs: Any) -> _LeakSidecar:
        sc = _LeakSidecar(kwargs.get("name", "sidecar"))
        self.created.append(sc)
        return sc


def _assert_no_leak(client: _LeakClient) -> None:
    assert len(client.networks.created) == 1, "the internal network should have been created"
    assert len(client.created) == 1, "the sidecar should have been created"
    assert client.networks.created[0].removed is True, "leaked network — cleanup not invoked"
    assert client.created[0].removed is True, "leaked sidecar — cleanup not invoked"


def test_gvisor_filtered_egress_setup_failure_leaves_no_leak() -> None:
    svc = GvisorSandboxService(SandboxConfig(), client=_LeakClient())
    client = _LeakClient()
    spec = SandboxSpec(egress_allow=frozenset({"api.example.com"}))
    with pytest.raises(RuntimeError, match="simulated sidecar start failure"):
        svc._setup_filtered_egress(client, spec, "sbx_test", "conv1")
    _assert_no_leak(client)


def test_local_filtered_egress_setup_failure_leaves_no_leak() -> None:
    # The local tier reuses the parent gVisor `_setup_filtered_egress` unchanged, so the
    # cleanup-on-failure must hold here too (same method, different service class).
    svc = LocalSandboxService(default_local_config(), client=_LeakClient())
    client = _LeakClient()
    spec = SandboxSpec(egress_allow=frozenset({"api.example.com"}))
    with pytest.raises(RuntimeError, match="simulated sidecar start failure"):
        svc._setup_filtered_egress(client, spec, "sbx_test", "conv1")
    _assert_no_leak(client)


def test_podman_filtered_egress_setup_failure_leaves_no_leak() -> None:
    svc = PodmanSandboxService(
        default_podman_config(), client=_LeakClient(), cli_runner=lambda argv, t: (0, b"", b"")
    )
    client = _LeakClient()
    spec = SandboxSpec(egress_allow=frozenset({"api.example.com"}))
    with pytest.raises(RuntimeError, match="simulated sidecar start failure"):
        svc._setup_filtered_egress(client, spec, "sbx_test", "conv1")
    _assert_no_leak(client)


# ---------------------------------------------------------------------------
# P1 (sidecar runtime gate) — the sidecar resource caps go through a runtime LAST GATE
# right before the create, so a POST-construction mutation of a hot-mutable SandboxConfig
# (`cfg.sidecar_cpu = 0`) cannot smuggle an UNLIMITED sidecar cap to Docker/Podman. The
# construction @field_validator does NOT cover this (ConfigDict has no assignment
# validation, on purpose, for Settings hot-apply). We mutate AFTER construction and assert
# the sidecar create path REFUSES (raises SandboxError) before any network/sidecar exists,
# on gVisor/local AND podman; a valid cap still gets past the gate.
# ---------------------------------------------------------------------------

_SIDECAR_CAP_FIELDS = ["sidecar_cpu", "sidecar_memory_mb", "sidecar_pids_limit"]
# 0 + negative read as UNLIMITED at the runtime; NaN/inf are non-finite. setattr on the
# hot-mutable config bypasses pydantic (no assignment validation) so each lands verbatim.
_BAD_SIDECAR_VALUES = [0, -1, -2.5, float("nan"), float("inf"), float("-inf")]


def _mutated_cfg(make_cfg: Any, field: str, bad: Any) -> SandboxConfig:
    """Build a VALID config, then hot-mutate one sidecar cap AFTER construction — exactly
    the path the construction @field_validator cannot guard (ConfigDict, no assign-validate)."""
    cfg = make_cfg()
    setattr(cfg, field, bad)  # bypasses validation by design (hot-apply); reaches the runtime
    assert getattr(cfg, field) is bad or getattr(cfg, field) == bad  # the mutation stuck
    return cfg


@pytest.mark.parametrize("field", _SIDECAR_CAP_FIELDS)
@pytest.mark.parametrize("bad", _BAD_SIDECAR_VALUES)
def test_gvisor_sidecar_cap_mutation_refused_at_runtime(field: str, bad: Any) -> None:
    cfg = _mutated_cfg(SandboxConfig, field, bad)
    svc = GvisorSandboxService(SandboxConfig(), client=_LeakClient())
    client = _LeakClient()
    svc._cfg = cfg  # the mutated config is what the create path reads
    spec = SandboxSpec(egress_allow=frozenset({"api.example.com"}))
    with pytest.raises(SandboxError, match="sidecar cap"):
        svc._setup_filtered_egress(client, spec, "sbx_test", "conv1")
    # The gate fires BEFORE any network/sidecar is created — nothing stranded.
    assert client.networks.created == [], "gate must refuse before creating the egress network"
    assert client.created == [], "gate must refuse before creating the sidecar"


@pytest.mark.parametrize("field", _SIDECAR_CAP_FIELDS)
@pytest.mark.parametrize("bad", _BAD_SIDECAR_VALUES)
def test_local_sidecar_cap_mutation_refused_at_runtime(field: str, bad: Any) -> None:
    # Local tier reuses the gVisor `_setup_filtered_egress` unchanged — the runtime gate
    # must hold here too (same method, different service class / config).
    cfg = _mutated_cfg(default_local_config, field, bad)
    svc = LocalSandboxService(default_local_config(), client=_LeakClient())
    client = _LeakClient()
    svc._cfg = cfg
    spec = SandboxSpec(egress_allow=frozenset({"api.example.com"}))
    with pytest.raises(SandboxError, match="sidecar cap"):
        svc._setup_filtered_egress(client, spec, "sbx_test", "conv1")
    assert client.networks.created == [] and client.created == []


@pytest.mark.parametrize("field", _SIDECAR_CAP_FIELDS)
@pytest.mark.parametrize("bad", _BAD_SIDECAR_VALUES)
def test_podman_sidecar_cap_mutation_refused_at_runtime(field: str, bad: Any) -> None:
    cfg = _mutated_cfg(default_podman_config, field, bad)
    svc = PodmanSandboxService(
        default_podman_config(), client=_LeakClient(), cli_runner=lambda argv, t: (0, b"", b"")
    )
    client = _LeakClient()
    svc._cfg = cfg
    spec = SandboxSpec(egress_allow=frozenset({"api.example.com"}))
    with pytest.raises(SandboxError, match="sidecar cap"):
        svc._setup_filtered_egress(client, spec, "sbx_test", "conv1")
    assert client.networks.created == [] and client.created == []


def test_gvisor_valid_sidecar_caps_pass_the_runtime_gate() -> None:
    """A legitimate (default) sidecar cap is NOT a false reject: the gate lets it through to
    the create, proven by reaching the injected `start()` failure (RuntimeError, not the
    gate's SandboxError)."""
    svc = GvisorSandboxService(SandboxConfig(), client=_LeakClient())
    client = _LeakClient()
    spec = SandboxSpec(egress_allow=frozenset({"api.example.com"}))
    with pytest.raises(RuntimeError, match="simulated sidecar start failure"):
        svc._setup_filtered_egress(client, spec, "sbx_test", "conv1")
    assert len(client.created) == 1, "a valid sidecar cap must get past the gate to create"


def test_podman_valid_sidecar_caps_pass_the_runtime_gate() -> None:
    svc = PodmanSandboxService(
        default_podman_config(), client=_LeakClient(), cli_runner=lambda argv, t: (0, b"", b"")
    )
    client = _LeakClient()
    spec = SandboxSpec(egress_allow=frozenset({"api.example.com"}))
    with pytest.raises(RuntimeError, match="simulated sidecar start failure"):
        svc._setup_filtered_egress(client, spec, "sbx_test", "conv1")
    assert len(client.created) == 1, "a valid sidecar cap must get past the gate to create"


# ---------------------------------------------------------------------------
# P2 #3 — podman destroy_by_conversation removes labeled egress networks too.
# ---------------------------------------------------------------------------

_CONV = "conv-xyz"
_LABEL = "disco.conversation_id"


class _ReconNet:
    def __init__(self, name: str, labels: dict[str, str]) -> None:
        self.name = name
        self.id = "id-" + name
        self.labels = labels
        self.removed = False

    def remove(self) -> None:
        self.removed = True


class _ReconContainer:
    def __init__(self, name: str, labels: dict[str, str]) -> None:
        self.name = name
        self.id = "id-" + name
        self.labels = labels
        self.stopped = False
        self.removed = False
        self.remove_kwargs: dict[str, Any] | None = None

    def stop(self, timeout: Any = None) -> None:
        self.stopped = True

    def remove(self, force: bool = False, v: bool = False) -> None:
        self.removed = True
        self.remove_kwargs = {"force": force, "v": v}


class _ReconVolume:
    def __init__(self, name: str, labels: dict[str, str]) -> None:
        self.name = name
        self.id = "id-" + name
        self.labels = labels
        self.removed = False
        self.remove_kwargs: dict[str, Any] | None = None

    def remove(self, force: bool = False) -> None:
        self.removed = True
        self.remove_kwargs = {"force": force}


class _ReconNetworksMgr:
    def __init__(self, nets: list[_ReconNet]) -> None:
        self._nets = nets

    def list(self, filters: dict[str, str] | None = None) -> list[_ReconNet]:
        label = (filters or {}).get("label", "")
        key, _, val = label.partition("=")
        return [n for n in self._nets if n.labels.get(key) == val]


class _ReconContainersMgr:
    def __init__(self, containers: list[_ReconContainer]) -> None:
        self._containers = containers

    def list(self, all: bool = False, filters: dict[str, str] | None = None) -> list:  # noqa: A002
        label = (filters or {}).get("label", "")
        key, _, val = label.partition("=")
        return [c for c in self._containers if c.labels.get(key) == val]


class _ReconVolumesMgr:
    def __init__(self, volumes: list[_ReconVolume]) -> None:
        self._volumes = volumes

    def list(self, filters: dict[str, str] | None = None) -> list[_ReconVolume]:
        label = (filters or {}).get("label", "")
        key, _, val = label.partition("=")
        return [v for v in self._volumes if v.labels.get(key) == val]

    def get(self, name: str) -> _ReconVolume:
        for volume in self._volumes:
            if volume.name == name:
                return volume
        raise KeyError(name)


class _ReconClient:
    def __init__(
        self,
        containers: list[_ReconContainer],
        nets: list[_ReconNet],
        volumes: list[_ReconVolume] | None = None,
    ) -> None:
        self.containers = _ReconContainersMgr(containers)
        self.networks = _ReconNetworksMgr(nets)
        self.volumes = _ReconVolumesMgr(volumes or [])


async def test_podman_destroy_by_conversation_removes_labeled_egress_networks() -> None:
    """A crash/restart with filtered podman boxes leaves the labeled `disco-egr-*`
    network behind unless destroy_by_conversation removes it. This proves the podman
    path now mirrors gVisor's labeled-network cleanup."""
    sbx = _ReconContainer("disco-sbx-1", {_LABEL: _CONV})
    sidecar = _ReconContainer("disco-egr-1", {_LABEL: _CONV})
    egr_net = _ReconNet("disco-egr-1", {_LABEL: _CONV})
    other_net = _ReconNet("disco-egr-other", {_LABEL: "different-conv"})
    ws_vol = _ReconVolume("disco-ws-1", {})  # old in-flight volumes predate volume labels
    other_vol = _ReconVolume("disco-ws-other", {_LABEL: "different-conv"})
    client = _ReconClient([sbx, sidecar], [egr_net, other_net], [ws_vol, other_vol])
    svc = PodmanSandboxService(default_podman_config(), client=client)

    await svc.destroy_by_conversation(_CONV)

    # The labeled containers are gone...
    assert sbx.removed and sidecar.removed
    assert sbx.remove_kwargs == {"force": True, "v": True}
    assert sidecar.remove_kwargs == {"force": True, "v": True}
    # ...AND the labeled egress network is removed (the P2 fix).
    assert egr_net.removed is True, "labeled egress network was stranded (P2 leak)"
    # ...AND the derived workspace volume name is removed (REL-6 finding #4).
    assert ws_vol.removed is True, "workspace volume was stranded (REL-6 leak)"
    assert ws_vol.remove_kwargs == {"force": True}
    # A network for a DIFFERENT conversation is left untouched.
    assert other_net.removed is False
    assert other_vol.removed is False


class _GcResource:
    def __init__(self, name: str, attrs: dict[str, Any]) -> None:
        self.name = self.id = name
        self.attrs = attrs
        self.labels = attrs.get("Labels") or attrs.get("labels") or {}
        self.remove_calls: list[tuple[tuple[Any, ...], dict[str, Any]]] = []

    def reload(self) -> None:
        return None

    def remove(self, *args: Any, **kwargs: Any) -> None:
        self.remove_calls.append((args, kwargs))


class _GcManager:
    def __init__(self, resources: list[Any], *, fail: bool = False) -> None:
        self.resources = resources
        self.fail = fail

    def list(self, *args: Any, **kwargs: Any) -> list[Any]:
        if self.fail:
            raise RuntimeError("inventory unavailable")
        return list(self.resources)


class _GcClient:
    def __init__(
        self,
        *,
        containers: list[Any],
        networks: list[_GcResource],
        volumes: list[_GcResource],
        fail_containers: bool = False,
    ) -> None:
        self.containers = _GcManager(containers, fail=fail_containers)
        self.networks = _GcManager(networks)
        self.volumes = _GcManager(volumes)


def _gc_network(
    name: str,
    *,
    created: str = "2000-01-01T00:00:00+00:00",
    labels: dict[str, str] | None = None,
    attached: bool = False,
) -> _GcResource:
    return _GcResource(
        name,
        {
            "created": created,
            "labels": {_LABEL: _CONV} if labels is None else labels,
            "containers": {"attached": {}} if attached else {},
        },
    )


def _gc_volume(
    name: str,
    *,
    created: str = "2000-01-01T00:00:00+00:00",
    labels: dict[str, str] | None = None,
    mount_count: int = 0,
) -> _GcResource:
    return _GcResource(
        name,
        {
            "CreatedAt": created,
            "Labels": {_LABEL: _CONV} if labels is None else labels,
            "MountCount": mount_count,
        },
    )


def test_startup_reconciliation_removes_only_owned_old_detached_auxiliaries() -> None:
    current_net = _gc_network("disco-egr-orphan")
    legacy_net = _gc_network("pmx-egr-legacy", labels={"pmx.conversation_id": _CONV})
    fresh_net = _gc_network("disco-egr-fresh", created="2100-01-01T00:00:00+00:00")
    unlabeled_net = _gc_network("disco-egr-unlabeled", labels={})
    attached_net = _gc_network("disco-egr-attached", attached=True)
    live_net = _gc_network("disco-egr-live")
    user_net = _gc_network("user-network")

    current_volume = _gc_volume("disco-ws-orphan")
    legacy_volume = _gc_volume("pmx-ws-legacy", labels={"pmx.conversation_id": _CONV})
    fresh_volume = _gc_volume("disco-ws-fresh", created="2100-01-01T00:00:00+00:00")
    unlabeled_volume = _gc_volume("disco-ws-unlabeled", labels={})
    mounted_volume = _gc_volume("disco-ws-mounted", mount_count=1)
    live_volume = _gc_volume("disco-ws-live")
    user_volume = _gc_volume("user-volume")
    live_container = _ReconContainer("disco-sbx-live", {_LABEL: _CONV})

    client = _GcClient(
        containers=[live_container],
        networks=[
            current_net,
            legacy_net,
            fresh_net,
            unlabeled_net,
            attached_net,
            live_net,
            user_net,
        ],
        volumes=[
            current_volume,
            legacy_volume,
            fresh_volume,
            unlabeled_volume,
            mounted_volume,
            live_volume,
            user_volume,
        ],
    )

    assert reconcile_orphan_aux_resources(
        client,
        workspace_volume_prefix="disco-ws",
        now_s=2_000_000_000,
    ) == (2, 2)
    assert current_net.remove_calls == [((), {})]
    assert legacy_net.remove_calls == [((), {})]
    assert current_volume.remove_calls == [((), {})]
    assert legacy_volume.remove_calls == [((), {})]
    for retained in (
        fresh_net,
        unlabeled_net,
        attached_net,
        live_net,
        user_net,
        fresh_volume,
        unlabeled_volume,
        mounted_volume,
        live_volume,
        user_volume,
    ):
        assert retained.remove_calls == []


def test_startup_reconciliation_fails_closed_without_container_inventory() -> None:
    network = _gc_network("disco-egr-orphan")
    volume = _gc_volume("disco-ws-orphan")
    client = _GcClient(
        containers=[],
        networks=[network],
        volumes=[volume],
        fail_containers=True,
    )

    assert reconcile_orphan_aux_resources(
        client,
        workspace_volume_prefix="disco-ws",
        now_s=2_000_000_000,
    ) == (0, 0)
    assert network.remove_calls == []
    assert volume.remove_calls == []


@pytest.mark.parametrize("backend", ["gvisor", "podman"])
async def test_list_live_instances_reconciles_owned_orphan_auxiliaries(backend: str) -> None:
    network = _gc_network("disco-egr-public-boundary")
    volume = _gc_volume("disco-ws-public-boundary")
    client = _GcClient(containers=[], networks=[network], volumes=[volume])
    if backend == "gvisor":
        service = GvisorSandboxService(SandboxConfig(), client=client)
    else:
        service = PodmanSandboxService(default_podman_config(), client=client)

    assert await service.list_live_instances() == []
    assert network.remove_calls == [((), {})]
    assert volume.remove_calls == [((), {})]
