from __future__ import annotations

import io
import zipfile
from types import SimpleNamespace
from xml.etree import ElementTree as ET

import httpx
from disco.agent_server.routes.preview import make_preview_router
from disco.core.llm.config import default_config
from disco.core.store.sqlite import SqliteEventStore
from disco.tools.builtin._deck_schema import AuthoredDeck, AuthoredSlide, lower_deck
from disco.tools.builtin._pptx_render import render_pptx
from disco.tools.sandbox import PodmanSandboxInstance, PodmanSandboxService, SandboxConfig
from disco.tools.sandbox._container import PREVIEW_PORT, PUBLISHED_PORTS
from disco.tools.sandbox.base import SandboxSpec
from fastapi import FastAPI


class _FakeContainer:
    def __init__(self, name: str, *, ports: dict | None = None, ip: str = "") -> None:
        self.id = f"id-{name}"
        self.name = name
        self.status = "running"
        self.labels = {}
        self.attrs = {
            "HostConfig": {},
            "NetworkSettings": {
                "Networks": {},
                "Ports": ports or {},
            }
        }
        self.ip = ip
        self.started = False
        self.archives: list[tuple[str, bytes]] = []

    def start(self) -> None:
        self.started = True

    def reload(self) -> None:
        self.status = "running"

    def put_archive(self, path: str, data: bytes) -> bool:
        self.archives.append((path, data))
        return True

    def stop(self, timeout: int = 2) -> None:
        self.status = "exited"

    def remove(self, force: bool = True) -> None:
        self.status = "removed"


class _FakeNetwork:
    def __init__(self, name: str) -> None:
        self.name = name
        self.id = f"id-{name}"
        self.connected: list[str] = []

    def connect(self, container: _FakeContainer) -> None:
        self.connected.append(container.name)
        if not container.ip:
            container.ip = "10.89.0.2"
        container.attrs["NetworkSettings"]["Networks"][self.name] = {"IPAddress": container.ip}

    def remove(self) -> None:
        pass


class _FakeImages:
    def exists(self, _image: str) -> bool:
        return True


class _FakeVolume:
    def __init__(self, name: str) -> None:
        self.name = name
        self.removed = False

    def remove(self, force: bool = True) -> None:
        self.removed = force


class _FakeVolumes:
    def __init__(self) -> None:
        self.created: list[dict] = []

    def create(self, *, name: str, **kwargs) -> _FakeVolume:
        self.created.append({"name": name, **kwargs})
        return _FakeVolume(name)


class _FakeLoopbackTunnel:
    def forward(self, remote_port: int) -> tuple[str, int]:
        return "203.0.113.47", remote_port


class _FakeNetworks:
    def __init__(self) -> None:
        self.created: list[dict] = []

    def create(self, name: str, **kwargs) -> _FakeNetwork:
        self.created.append({"name": name, **kwargs})
        return _FakeNetwork(name)

    def list(self, *args, **kwargs) -> list:
        return []


class _FakeContainers:
    def __init__(self) -> None:
        self.created: list[dict] = []
        self.sidecar: _FakeContainer | None = None
        self.sandbox: _FakeContainer | None = None

    def create(self, **kwargs) -> _FakeContainer:
        self.created.append(kwargs)
        name = kwargs["name"]
        if kwargs.get("ports"):
            ports = {
                f"{p}/tcp": [{"HostIp": "127.0.0.1", "HostPort": str(20_000 + p)}]
                for p in sorted(PUBLISHED_PORTS)
            }
            self.sidecar = _FakeContainer(name, ports=ports, ip="10.89.0.2")
            return self.sidecar
        self.sandbox = _FakeContainer(name, ip="10.89.0.3")
        memory = str(kwargs.get("mem_limit", "0")).lower()
        memory_bytes = int(memory.removesuffix("m")) * 1024 * 1024 if memory.endswith("m") else int(memory)
        self.sandbox.attrs["HostConfig"] = {
            "Memory": memory_bytes,
            "CpuQuota": kwargs.get("cpu_quota"),
            "PidsLimit": kwargs.get("pids_limit"),
        }
        for net_name in kwargs.get("networks") or {}:
            self.sandbox.attrs["NetworkSettings"]["Networks"][net_name] = {"IPAddress": "10.89.0.3"}
        return self.sandbox

    def list(self, *args, **kwargs) -> list:
        return []


class _FakePodmanClient:
    def __init__(self) -> None:
        self.images = _FakeImages()
        self.volumes = _FakeVolumes()
        self.networks = _FakeNetworks()
        self.containers = _FakeContainers()


def test_pb_remote_podman_filtered_session_publishes_and_forwards_preview_port() -> None:
    client = _FakePodmanClient()
    calls: list[list[str]] = []

    def cli_runner(argv: list[str], timeout: float) -> tuple[int, bytes, bytes]:
        calls.append(argv)
        return 0, b"", b""

    cfg = SandboxConfig(
        backend="podman",
        runtime="crun",
        image="disco-sandbox:base",
        podman_url="http+ssh://sandbox@203.0.113.47/run/user/1000/podman/podman.sock",
    )
    service = PodmanSandboxService(cfg, client=client, cli_runner=cli_runner)

    spec = SandboxSpec(egress_allow=frozenset({"example.test"}))
    container, name, _network, sidecar, volume, relay_url = service._start_container(
        spec, "sbx_unbiased", "conv_podman"
    )
    instance = PodmanSandboxInstance(
        id="sbx_unbiased",
        owner_id="local",
        conversation_id="conv_podman",
        spec=spec,
        container=container,
        container_workspace=cfg.container_workspace,
        stop_timeout_s=cfg.stop_timeout_s,
        cli_url=service._cli_url,
        container_name=name,
        cli_runner=cli_runner,
        preview_host="203.0.113.47",
        workspace_volume=volume,
        loopback_tunnel=_FakeLoopbackTunnel(),  # type: ignore[arg-type]
    )
    instance._egress_sidecar = sidecar

    volume_create = client.volumes.created[0]
    sidecar_create = client.containers.created[0]
    sandbox_create = client.containers.created[1]
    assert volume_create == {
        "name": "disco-ws-sbx_unbiased",
        "labels": {"disco.conversation_id": "conv_podman"},
        "driver": "local",
        "driver_opts": {
            "type": "tmpfs",
            "device": "tmpfs",
            "o": "size=4096m,uid=1000,gid=1000,mode=0750,nosuid,nodev",
        },
    }
    assert relay_url is None
    assert set(sidecar_create["ports"]) == {f"{p}/tcp" for p in PUBLISHED_PORTS}
    assert sandbox_create.get("ports") is None
    assert sandbox_create["networks"], "filtered sandbox must attach to the internal net"

    joined_commands = [" ".join(call) for call in calls]
    forwarder_commands = [cmd for cmd in joined_commands if "/inbound_forward.py" in cmd]
    assert forwarder_commands, "filtered podman preview needs an inbound forwarder"
    assert "10.89.0.3" in forwarder_commands[-1]
    assert str(PREVIEW_PORT) in forwarder_commands[-1]

    assert instance.expose_port(PREVIEW_PORT) == f"http://203.0.113.47:{20_000 + PREVIEW_PORT}"


def test_w23_long_bullets_render_as_one_mult_paragraph_autofit_text_frame() -> None:
    bullets = [
        (
            "Alpha marker: this long bullet describes a failure mode with enough "
            "detail to wrap under substituted presentation fonts in office suites."
        ),
        (
            "Beta marker: this second long bullet also wraps, and must flow as a "
            "paragraph instead of spilling from a separate absolute-positioned box."
        ),
        (
            "Gamma marker: this final long bullet keeps the renderer honest by "
            "requiring multiple paragraphs in one body frame with autofit enabled."
        ),
    ]
    authored = AuthoredDeck(
        title="Bullet Fit",
        theme="disco-light",
        slides=[
            AuthoredSlide(
                type="bullets",
                title="Long Bullet Reliability",
                body=bullets,
            )
        ],
    )

    pptx = render_pptx(lower_deck(authored))
    with zipfile.ZipFile(io.BytesIO(pptx)) as zf:
        slide_xml = zf.read("ppt/slides/slide1.xml")

    ns = {
        "p": "http://schemas.openxmlformats.org/presentationml/2006/main",
        "a": "http://schemas.openxmlformats.org/drawingml/2006/main",
    }
    root = ET.fromstring(slide_xml)
    marker_text = ["Alpha marker", "Beta marker", "Gamma marker"]
    bullet_shapes = []
    for shape in root.findall(".//p:sp", ns):
        text = "\n".join(t.text or "" for t in shape.findall(".//a:t", ns))
        if any(marker in text for marker in marker_text):
            bullet_shapes.append((shape, text))

    assert len(bullet_shapes) == 1, (
        "long bullets should be one flowing text frame, not separate absolute boxes"
    )
    bullet_shape, text = bullet_shapes[0]
    assert all(marker in text for marker in marker_text)
    assert len(bullet_shape.findall(".//a:p", ns)) >= len(marker_text)
    assert bullet_shape.find(".//a:normAutofit", ns) is not None


def test_w46_browser_launch_uses_anti_fingerprint_context(monkeypatch) -> None:
    captured: dict[str, object] = {}

    class FakePage:
        def set_content(self, _html: str) -> None:
            return None

        def evaluate(self, _script: str) -> bool:
            return True

        def close(self) -> None:
            captured["closed_pages"] = int(captured.get("closed_pages", 0)) + 1

        def on(self, *_args, **_kwargs) -> None:
            return None

    class FakeContext:
        def add_init_script(self, script: str) -> None:
            captured["script"] = script

        def new_page(self) -> FakePage:
            return FakePage()

    class FakeBrowser:
        def new_context(self, **kwargs) -> FakeContext:
            captured["context"] = kwargs
            return FakeContext()

    class FakeChromium:
        def launch(self, **kwargs) -> FakeBrowser:
            captured["launch"] = kwargs
            return FakeBrowser()

    class FakePlaywright:
        chromium = FakeChromium()

    class FakeSync:
        def start(self) -> FakePlaywright:
            return FakePlaywright()

    import disco.tools.builtin._browser_daemon as browser_daemon

    monkeypatch.setattr(browser_daemon, "sync_playwright", lambda: FakeSync())

    state = browser_daemon.BrowserState()
    state.start()

    launch = captured["launch"]  # type: ignore[assignment]
    context = captured["context"]  # type: ignore[assignment]
    script = str(captured["script"])

    assert launch["headless"] is True  # type: ignore[index]
    assert "--disable-blink-features=AutomationControlled" in launch["args"]  # type: ignore[index]
    assert "HeadlessChrome" not in context["user_agent"]  # type: ignore[index]
    assert "Chrome/" in context["user_agent"]  # type: ignore[index]
    assert context["locale"] == "en-US"  # type: ignore[index]
    assert context["extra_http_headers"]["Accept-Language"].startswith("en-US")  # type: ignore[index]
    assert "navigator" in script
    assert "webdriver" in script
    assert "undefined" in script
    assert captured["closed_pages"] == 1


class _LiveReadySession:
    supports_live_view = True

    def __init__(self) -> None:
        self.commands: list[str] = []

    async def exec_shell(self, cmd: str, *, timeout_s: int):
        self.commands.append(cmd)
        assert timeout_s == 3
        return SimpleNamespace(exit_code=0, stdout="OK", stderr="")


class _LiveReadyRuntime:
    def __init__(self, session: _LiveReadySession) -> None:
        cfg = default_config().model_copy(update={"live_browser": SimpleNamespace(enabled=True)})
        self._config_store = SimpleNamespace(load=lambda: cfg)
        self.session = session
        self.wake_calls = 0
        # Epic 13-B3 promoted live-session lookup onto its own collaborator, so
        # production now routes through `runtime.live_sessions.live_session(...)`
        # (current/packages/agent-server/.../routes/preview_browser.py:301). This stub is
        # a test double for that interface, so it owes the same shape; the
        # one-level `live_session` below is kept because the double is also
        # constructed directly elsewhere in this module. Interface migrated; the
        # assertions in the test are untouched.
        self.live_sessions = self

    def live_session(self, _conversation_id: str) -> _LiveReadySession:
        return self.session

    async def wake_for_preview(self, *_args, **_kwargs):
        self.wake_calls += 1
        return "http://should-not-be-called"


async def test_w47_live_ready_polls_health_without_live_start_or_port_wake() -> None:
    store = SqliteEventStore(":memory:")
    session = _LiveReadySession()
    runtime = _LiveReadyRuntime(session)
    app = FastAPI()
    app.include_router(make_preview_router(store, runtime=runtime))  # type: ignore[arg-type]

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.get("/conversations/conv_live/browser/live-ready")

    assert response.status_code == 200
    assert response.json() == {"ready": True, "reason": "ready"}
    assert session.commands == ["curl -sf http://127.0.0.1:8901/health"]
    assert all("live_start" not in cmd for cmd in session.commands)
    assert runtime.wake_calls == 0
