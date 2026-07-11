"""Podman sandbox backend — hermetic (mocked podman-py client + CLI runner).

Proves the session model, sealing, limits-in-create, NEVER-PULL, timeout reporting,
typed-error paths, file round-trip (put_archive write + CLI cat/ls read), and no
env leak — all offline. The real isolation + limits-bite-through-the-socket is live.
"""

from __future__ import annotations

import io
import tarfile

import pytest
from disco.tools.anatomy import Capability
from disco.tools.sandbox import (
    PodmanSandboxService,
    SandboxConfig,
    SandboxError,
    SandboxSpec,
    SandboxUnavailableError,
)


class FakeContainer:
    def __init__(self, create_kwargs: dict, fs: dict[str, bytes]) -> None:
        self.create_kwargs = create_kwargs
        self.fs = fs  # shared with the CLI runner (write here, read there)
        self.started = self.stopped = self.removed = False
        self.attrs: dict = {}  # populated by reload() — E8 sidecar needs Networks
        # a queue of fake (exit_code, stdout, stderr) for exec_run; consumed FIFO.
        self.exec_results: list[tuple[int, bytes, bytes]] = []
        self.exec_calls: list[list[str]] = []
        self.remove_kwargs: dict | None = None
        # podman-py Container.name — the sidecar setup reads it to build the
        # `podman --url … exec <name> …` CLI argv. The fake's `name` follows
        # the create-kwarg convention (matches the real podman-py .name).
        self.name = create_kwargs.get("name", "")

    def start(self):
        self.started = True

    def reload(self):  # no-op: the fake leaves attrs alone (callers set NetworkSettings)
        pass

    def exec_run(self, cmd, demux=False, workdir=None, detach=False):
        # The sidecar's one-shot setup (resolv.conf + proxy launch) goes through
        # the CLI runner in the REAL podman backend (per E8 _sidecar_cli_run), so
        # the fake's podman-py `exec_run` path is only exercised by hermetic
        # tests that want to assert it. We accept the call and return a default
        # success — tests that need a specific exit/stdout push into exec_results.
        self.exec_calls.append(cmd)
        if self.exec_results:
            return self.exec_results.pop(0)
        return (0, b"", b"")

    def put_archive(self, path, data):
        with tarfile.open(fileobj=io.BytesIO(data)) as tar:
            for m in tar.getmembers():
                f = tar.extractfile(m)
                self.fs[path.rstrip("/") + "/" + m.name] = f.read() if f else b""
        return True

    def stop(self, timeout=None):
        self.stopped = True

    def remove(self, force=False, v=False):
        self.removed = True
        self.remove_kwargs = {"force": force, "v": v}


class _Images:
    def __init__(self, has: bool) -> None:
        self._has = has
        self.pull_called = False

    def exists(self, tag):
        return self._has

    def pull(self, *a, **k):  # MUST never be called
        self.pull_called = True
        raise AssertionError("backend attempted a registry pull")


class _Volumes:
    def __init__(self) -> None:
        self.created: list[str] = []
        self.objects: dict[str, FakeVolume] = {}

    def create(self, name, labels=None, **options):
        self.created.append(name)
        vol = FakeVolume(name, labels or {}, options)
        self.objects[name] = vol
        return vol

    def list(self, filters=None):
        label = (filters or {}).get("label")
        if not label:
            return list(self.objects.values())
        key, _, val = label.partition("=")
        return [vol for vol in self.objects.values() if vol.labels.get(key) == val]

    def get(self, name):
        return self.objects[name]


class FakeVolume:
    def __init__(self, name: str, labels: dict[str, str], options: dict) -> None:
        self.name = name
        self.id = "vol-" + name
        self.labels = labels
        self.options = options
        self.removed = False
        self.remove_kwargs: dict | None = None

    def remove(self, force=False):
        self.removed = True
        self.remove_kwargs = {"force": force}


class FakePodmanNetwork:
    """Mirrors podman-py Network: connect() joins a container, remove() tears it down."""

    def __init__(self, name: str, **attrs) -> None:
        self.name = name
        self.attrs = attrs
        self.connected: list = []
        self.removed = False

    def connect(self, container, aliases=None):
        self.connected.append((container, aliases))

    def remove(self):
        self.removed = True


class _FakeNetworks:
    def __init__(self) -> None:
        self.created: list[FakePodmanNetwork] = []

    def create(self, name, **kwargs):
        net = FakePodmanNetwork(name, **kwargs)
        self.created.append(net)
        return net


class FakePodmanClient:
    def __init__(self, *, has_image: bool = True, fs: dict | None = None) -> None:
        self.images = _Images(has_image)
        self.volumes = _Volumes()
        self.networks = _FakeNetworks()
        self.containers = self
        self.fs = fs if fs is not None else {}
        self.last: FakeContainer | None = None
        self.created: list[FakeContainer] = []  # every container create()'d (sidecar + sandbox)

    def ping(self):
        return True

    def create(self, **kwargs):
        c = FakeContainer(kwargs, self.fs)
        self.created.append(c)
        self.last = c
        return c


class FakeCli:
    """Stands in for `podman --url … exec …`. cat/ls/mkdir against the shared FS;
    queued results for shell (timeout-wrapped) commands."""

    def __init__(self, fs: dict[str, bytes]) -> None:
        self.fs = fs
        self.results: list[tuple[int, bytes, bytes]] = []
        self.calls: list[list[str]] = []

    def __call__(self, argv, timeout):
        self.calls.append(argv)
        rest = argv[5:]  # after ["podman","--url",url,"exec",name]
        if rest[0] == "python3" and "DISCO_READ_" in rest[2]:
            path = rest[3].rstrip("/") + "/" + rest[4]
            return (
                (0, self.fs[path], b"")
                if path in self.fs
                else (44, b"", b"DISCO_READ_MISSING:no file")
            )
        if rest[0] == "cat":
            path = rest[-1]
            return (0, self.fs[path], b"") if path in self.fs else (1, b"", b"no file")
        if rest[0] == "ls":
            prefix = rest[-1].rstrip("/") + "/"
            names = sorted(
                {p[len(prefix) :].split("/")[0] for p in self.fs if p.startswith(prefix)}
            )
            return (0, ("\n".join(names) + "\n").encode() if names else b"", b"")
        if rest[0] == "mkdir":
            return (0, b"", b"")
        if rest[0] == "stat":
            path = rest[-1]
            return (
                (0, f"{len(self.fs[path])}\n".encode(), b"")
                if path in self.fs
                else (1, b"", b"no file")
            )
        if self.results:
            return self.results.pop(0)
        return (0, b"ok\n", b"")


def _svc(has_image: bool = True) -> tuple[PodmanSandboxService, FakePodmanClient, FakeCli]:
    fs: dict[str, bytes] = {}
    client = FakePodmanClient(has_image=has_image, fs=fs)
    cli = FakeCli(fs)
    svc = PodmanSandboxService(
        SandboxConfig(backend="podman", runtime="crun"), client=client, cli_runner=cli
    )
    return svc, client, cli


async def test_create_exec_close_over_the_socket():
    svc, client, cli = _svc()
    inst = await svc.create(SandboxSpec(), owner_id="local", conversation_id="c")
    kw = client.last.create_kwargs
    assert kw["command"] == ["sleep", "infinity"] and client.last.started is True
    # per-run named volume bound to /workspace
    (vol,) = client.volumes.created
    assert kw["volumes"][vol] == {"bind": "/workspace", "mode": "rw"}
    # exec goes through the CLI native remote (podman --url … exec)
    r = await inst.exec_shell("echo hi", timeout_s=10)
    assert r.stdout == "ok\n" and r.timed_out is False
    assert cli.calls[-1][:5] == ["podman", "--url", svc._cli_url, "exec", f"disco-sbx-{inst.id}"]
    assert cli.calls[-1][5:7] == ["python3", "-c"]
    assert "echo hi" in cli.calls[-1]
    await inst.destroy()
    assert client.last.stopped and client.last.removed
    assert client.last.remove_kwargs == {"force": True, "v": True}
    assert client.volumes.objects[vol].removed is True
    assert client.volumes.objects[vol].labels == {"disco.conversation_id": "c"}
    assert "size=4096m" in client.volumes.objects[vol].options["driver_opts"]["o"]


async def test_never_pulls_when_image_absent():
    svc, client, _cli = _svc(has_image=False)
    with pytest.raises(SandboxUnavailableError, match="never pulls"):
        await svc.create(SandboxSpec(), owner_id="o", conversation_id="c")
    assert client.images.pull_called is False and client.last is None


async def test_sealed_default_open_when_granted():
    svc, client, _ = _svc()
    await svc.create(SandboxSpec(), owner_id="o", conversation_id="c")
    assert client.last.create_kwargs["network_mode"] == "none"
    await svc.create(
        SandboxSpec(permitted=frozenset({Capability.NETWORK})), owner_id="o", conversation_id="c"
    )
    assert client.last.create_kwargs["network_mode"] == "bridge"
    assert next(iter(client.last.create_kwargs["networks"])).startswith("disco-egr-")
    assert client.networks.created[-1].attrs.get("internal") is True


async def test_filtered_podman_spec_uses_proxied_egress_not_sealed():
    # E8 wiring: a filtered podman spec now gets a PROXIED network (allowlist
    # sidecar on an internal net) — NOT the old fail-safe `network_mode="none"`
    # seal that was gVisor-only. This is the regression guard for the local
    # podman backend; the formal E8 acceptance test (`test_e8_*.py`) is the
    # full new-test contract.
    svc, client, _ = _svc()
    await svc.create(
        SandboxSpec(egress_allow=frozenset({"api.example.com"})), owner_id="o", conversation_id="c"
    )
    # 1) NOT the old fail-safe seal (it's bridge-mode netns, not network_mode="none").
    assert client.last.create_kwargs.get("network_mode") != "none"
    # 2) the sandbox IS on the internal no-NAT net (proxied route out).
    networks = client.last.create_kwargs.get("networks") or {}
    assert networks and next(iter(networks)).startswith("disco-egr-")
    # 3) the allowlist proxy env is present (defense in depth atop the no-route net).
    env = client.last.create_kwargs["environment"]
    assert env["HTTPS_PROXY"].startswith("http://") and "8888" in env["HTTPS_PROXY"]


async def test_limits_and_no_env_leak_in_create(monkeypatch):
    monkeypatch.setenv("PMX_FAKE_SECRET", "sk-do-not-leak")
    # EPIC H (P1): config is the MAXIMUM. cpu=2.0 is within this deployment's ceiling
    # (default_cpu=4.0) so it flows through; above-max clamping is its own regression test.
    fs: dict[str, bytes] = {}
    client = FakePodmanClient(has_image=True, fs=fs)
    cli = FakeCli(fs)
    cfg = SandboxConfig(backend="podman", runtime="crun", default_cpu=4.0)
    svc = PodmanSandboxService(cfg, client=client, cli_runner=cli)
    await svc.create(SandboxSpec(cpu=2.0, memory_mb=512), owner_id="o", conversation_id="c")
    kw = client.last.create_kwargs
    assert kw["mem_limit"] == "512m"  # the limit goes through the socket create
    assert kw["cpu_quota"] == 200_000 and kw["cpu_period"] == 100_000
    # EPIC H host-protection: pids cap (cgroup pids.max) carried on the socket create.
    assert kw["pids_limit"] == 512  # spec unset → config default
    assert kw.get("pid_mode") != "host"  # own PID namespace, never the host's
    assert kw["environment"] == {}  # no host env into the box


async def test_timeout_reported_and_file_round_trip():
    svc, _client, cli = _svc()
    inst = await svc.create(SandboxSpec(), owner_id="o", conversation_id="c")
    # write (put_archive) -> read/list (CLI), jailed
    await inst.write_file("sub/a.txt", b"hello")
    assert await inst.read_file("sub/a.txt") == b"hello"
    assert "sub" in await inst.list_dir(".")
    with pytest.raises(SandboxError):
        await inst.read_file("../../etc/passwd")
    # timeout → reported (CLI returns 124), partial output preserved
    cli.results = [(124, b"partial\n", b"")]
    res = await inst.exec_shell("sleep 999", timeout_s=1)
    assert res.timed_out is True and res.exit_code == 124 and res.stdout == "partial\n"


async def test_podman_unreachable_is_typed_error():
    # bogus UNIX socket → fails fast (an ssh:// url would block on the connect),
    # exercising the same _client() error-mapping path.
    cfg = SandboxConfig(backend="podman", podman_url="http+unix:///nonexistent/podman.sock")
    svc = PodmanSandboxService(cfg, cli_runner=lambda a, t: (0, b"", b""))
    with pytest.raises(SandboxUnavailableError, match="Podman unreachable"):
        await svc.create(SandboxSpec(), owner_id="o", conversation_id="c")


def test_config_is_podman_and_crun_and_cli_url():
    from disco.tools.sandbox import default_podman_config

    cfg = default_podman_config()
    assert cfg.backend == "podman" and cfg.runtime == "crun"
    svc = PodmanSandboxService(cfg)
    assert svc._cli_url.startswith("ssh://") and not svc._cli_url.startswith("http+")


def test_inspect_state_retries_transient_inspect_failure(monkeypatch):
    """The re-verify inspect RETRIES when the inspect COMMAND itself fails (rc!=0):
    under concurrent load `podman inspect` is intermittently refused, and a single
    failed inspect would conservatively type a LIVE container dead → a needless
    recreate. A retry that then succeeds recovers the real (running) verdict."""
    import disco.tools.sandbox.podman as pod

    monkeypatch.setattr(pod.time, "sleep", lambda _s: None)  # no real backoff in tests
    calls = {"n": 0}

    def flaky(_args, _timeout):
        calls["n"] += 1
        if calls["n"] < 3:
            return (125, b"", b"connection refused")  # the inspect command fails
        return (0, b"status=running OOMKilled=false exit=0 reason=", b"")

    inst = pod.PodmanSandboxInstance.__new__(pod.PodmanSandboxInstance)
    inst._runner, inst._name, inst._cli_url = flaky, "c-flaky", "ssh://x"
    status, _reason = inst._inspect_state()
    assert status == "running", "the retry must recover the live verdict"
    assert calls["n"] == 3  # retried until the inspect succeeded


def test_inspect_state_gives_up_after_retry_budget(monkeypatch):
    """If the inspect keeps failing, it gives up as 'reason-unavailable' after the
    bounded retry budget — never an unbounded loop. A SUCCESSFUL inspect reporting a
    terminal status returns immediately (a real verdict is not retried)."""
    import disco.tools.sandbox.podman as pod

    monkeypatch.setattr(pod.time, "sleep", lambda _s: None)
    calls = {"n": 0}

    def always_fail(_args, _timeout):
        calls["n"] += 1
        return (125, b"", b"refused")

    inst = pod.PodmanSandboxInstance.__new__(pod.PodmanSandboxInstance)
    inst._runner, inst._name, inst._cli_url = always_fail, "c-dead", "ssh://x"
    status, reason = inst._inspect_state()
    assert status == "" and reason == "reason-unavailable"
    assert calls["n"] == pod._INSPECT_RETRIES  # exactly the budget, no more

    one = {"n": 0}

    def exited(_args, _timeout):
        one["n"] += 1
        return (0, b"status=exited OOMKilled=false exit=1 reason=boom", b"")

    inst2 = pod.PodmanSandboxInstance.__new__(pod.PodmanSandboxInstance)
    inst2._runner, inst2._name, inst2._cli_url = exited, "c-exited", "ssh://x"
    status2, _r2 = inst2._inspect_state()
    assert status2 == "exited" and one["n"] == 1  # a real verdict is NOT retried


# ---------------------------------------------------------------------------
# P-B — FIX6 parity: inbound sidecar preview forwarder + inherited expose_port.
# Mirrors the gVisor FIX6 unit proofs (test_fix6_inbound_forward.py) for podman.
# ---------------------------------------------------------------------------


def _filtered_spec() -> SandboxSpec:
    return SandboxSpec(egress_allow=frozenset({"api.example.com"}))


def _wire_sandbox_ip(client: FakePodmanClient, ip: str) -> None:
    """Make the SANDBOX container report an internal-net IP under the egress net
    (the sidecar — `disco-egr-…` — is created first, so its name is the net name).
    The forwarder reads this to target `host:PORT -> <sandbox_ip>:PORT`."""
    orig = client.create

    def _create(**kwargs):
        c = orig(**kwargs)
        if kwargs.get("name", "").startswith("disco-sbx-"):
            egr = next(x for x in client.created if x.name.startswith("disco-egr-"))
            c.attrs = {"NetworkSettings": {"Networks": {egr.name: {"IPAddress": ip}}}}
        return c

    client.create = _create  # type: ignore[method-assign]


async def test_filtered_sidecar_published_with_preview_ports_sandbox_not():
    from disco.tools.sandbox._container import loopback_port_bindings

    svc, client, _ = _svc()
    await svc.create(_filtered_spec(), owner_id="o", conversation_id="c")
    # The SIDECAR (disco-egr-…, created first) carries the published preview set —
    # it's the only member on bridge that CAN publish; the sandbox is internal-only.
    sidecar = client.created[0]
    assert sidecar.name.startswith("disco-egr-")
    assert sidecar.create_kwargs.get("ports") == loopback_port_bindings(podman=True)
    assert sidecar.create_kwargs.get("cap_drop") == ["ALL"]
    assert sidecar.create_kwargs.get("no_new_privileges") is True
    # The SANDBOX publishes NOTHING (containment).
    assert "ports" not in client.last.create_kwargs


async def test_podman_sealed_host_service_box_has_capability_only_relay():
    fs: dict[str, bytes] = {}
    client = FakePodmanClient(fs=fs)
    cli = FakeCli(fs)
    cfg = SandboxConfig(
        backend="podman",
        runtime="crun",
        host_service_upstream="https://agent.internal:8443",
    )
    svc = PodmanSandboxService(cfg, client=client, cli_runner=cli)
    inst = await svc.create(
        SandboxSpec(host_services=True), owner_id="o", conversation_id="c"
    )
    sidecar, sandbox = client.created
    net = client.networks.created[0]
    assert net.attrs["internal"] is True
    assert sidecar.create_kwargs["ports"] is None
    assert sandbox.create_kwargs["networks"] == {net.name: {}}
    assert "/capability_relay.py" in fs
    assert inst.host_service_relay_url == f"http://{net.name}:3211"
    await inst.destroy()
    assert sidecar.removed and net.removed


async def test_podman_relay_failure_fails_creation_and_cleans_sidecar():
    class BrokenRelayCli(FakeCli):
        def __call__(self, argv, timeout):
            if argv[5:7] == ["python3", "-c"] and argv[-1] == "3211":
                self.calls.append(argv)
                return (1, b"", b"")
            return super().__call__(argv, timeout)

    fs: dict[str, bytes] = {}
    client = FakePodmanClient(fs=fs)
    cli = BrokenRelayCli(fs)
    cfg = SandboxConfig(
        backend="podman",
        runtime="crun",
        host_service_upstream="https://agent.internal:8443",
    )
    svc = PodmanSandboxService(cfg, client=client, cli_runner=cli)
    with pytest.raises(SandboxUnavailableError, match="relay failed readiness"):
        await svc.create(
            SandboxSpec(host_services=True), owner_id="o", conversation_id="c"
        )
    assert client.created[0].removed
    assert client.networks.created[0].removed
    assert len(client.created) == 1


async def test_inbound_forwarder_launched_on_sidecar_via_cli_one_shell():
    from disco.tools.sandbox._container import PUBLISHED_PORTS

    svc, client, cli = _svc()
    _wire_sandbox_ip(client, "10.89.0.7")
    await svc.create(_filtered_spec(), owner_id="o", conversation_id="c")
    # The forwarder is launched on the SIDECAR via the CLI native remote
    # (`podman --url … exec <sidecar> sh -c …`) — podman-py exec_run is broken over
    # the remote API. The command shape mirrors gVisor's FIX6 base64 ONE-SHELL.
    launched = [c for c in cli.calls if any("inbound_forward.py" in str(a) for a in c)]
    assert launched, "inbound forwarder was not launched on the sidecar"
    argv = launched[0]
    # Targets the SIDECAR (egress net name), via the proven CLI exec path.
    assert argv[:4] == ["podman", "--url", svc._cli_url, "exec"]
    assert argv[4].startswith("disco-egr-")
    joined = " ".join(argv)
    # base64 one-shell: write + run share one process (no cross-exec gofer gap).
    assert "base64 -d > /inbound_forward.py" in joined, joined
    assert "python3 /inbound_forward.py" in joined, joined
    # backgrounded so the `podman exec` returns (the egress-proxy launch pattern).
    assert joined.rstrip().endswith("&"), joined
    # targets the SANDBOX's internal IP + every published port.
    assert "10.89.0.7" in joined, joined
    for port in sorted(PUBLISHED_PORTS):
        assert str(port) in joined, f"port {port} missing: {joined}"


async def test_no_forwarder_without_sandbox_ip():
    # No internal IP (reload returned nothing) → forwarder is NOT launched (it would
    # forward to nowhere); best-effort, never raises.
    svc, client, cli = _svc()  # sandbox attrs stay {} → no IP
    await svc.create(_filtered_spec(), owner_id="o", conversation_id="c")
    assert not any("inbound_forward.py" in str(a) for c in cli.calls for a in c)


async def test_expose_port_inherits_base_impl_no_stub():
    # The podman-only `expose_port` STUB (returned None) is GONE — the class now
    # inherits the shared `_resolve_mapping`-backed impl, same as gVisor.
    from disco.tools.sandbox.podman import PodmanSandboxInstance

    assert "expose_port" not in PodmanSandboxInstance.__dict__


async def test_expose_port_reads_sidecar_binding_for_filtered():
    svc, client, _ = _svc()
    _wire_sandbox_ip(client, "10.89.0.7")
    inst = await svc.create(_filtered_spec(), owner_id="o", conversation_id="c")
    sidecar, sandbox = client.created[0], client.last
    # The SIDECAR holds the real host-published mapping (it published the ports).
    sidecar.attrs = {
        "NetworkSettings": {"Ports": {"8000/tcp": [{"HostIp": "127.0.0.1", "HostPort": "49160"}]}}
    }
    # A decoy binding on the sandbox must be IGNORED for a filtered box.
    sandbox.attrs.setdefault("NetworkSettings", {})["Ports"] = {
        "8000/tcp": [{"HostIp": "127.0.0.1", "HostPort": "1"}]
    }
    url = inst.expose_port(8000)
    assert url == "http://127.0.0.1:49160"  # sidecar's port, not decoy


async def test_sealed_sandbox_has_no_sidecar_or_exposed_mapping():
    svc, client, _ = _svc()
    inst = await svc.create(SandboxSpec(), owner_id="o", conversation_id="c")
    assert inst._egress_sidecar is None
    client.last.attrs = {
        "NetworkSettings": {"Ports": {"8000/tcp": [{"HostIp": "127.0.0.1", "HostPort": "33333"}]}}
    }
    url = inst.expose_port(8000)
    assert url is None


def test_preview_host_derived_from_cli_url():
    from disco.tools.sandbox.podman import _preview_host

    assert _preview_host("ssh://sandbox@100.73.110.47/run/user/1000/podman/podman.sock") == (
        "100.73.110.47"
    )
    assert _preview_host("ssh://user@host.example:22/run/podman.sock") == "host.example"
    assert _preview_host("unix:///run/user/1000/podman/podman.sock") == "localhost"


async def test_instance_preview_host_set_from_cli_url():
    svc, _client, _cli = _svc()
    inst = await svc.create(SandboxSpec(), owner_id="o", conversation_id="c")
    assert inst._preview_host == "100.73.110.47"  # default podman_url tailnet host
