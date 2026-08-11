"""W-45 — `verify_web_app` tool: deterministic verdict + stable fingerprint.

The verdict logic is a PURE function (`compute_verdict`) so it is tested directly
without a sandbox. A single `run()` integration test exercises the wiring with a
fake sandbox + a stubbed BrowserTool (no real Playwright daemon).
"""

from types import SimpleNamespace

import pytest
from disco.core import SecurityRisk
from disco.tools.anatomy import ToolContext, ToolOutcome
from disco.tools.builtin import verify_app
from disco.tools.builtin.verify_app import (
    VerifyWebAppArgs,
    VerifyWebAppTool,
    _failure_fingerprint,
    compute_verdict,
)
from disco.tools.verify.web_app_probe import _VISION_REVIEW_CHECKLIST


def _structured(
    *, console=None, network=None, title="My App", text="Welcome to the demo page", elements=None
):
    return {
        "url": "http://127.0.0.1:8000/",
        "title": title,
        "text": text,
        "elements": elements if elements is not None else ["1[:] <a>Home</a>"],
        "console": console or [],
        "network": network or [],
        "screenshot_path": ".pmx/screenshots/0001-navigate.png",
    }


# ---- (a) PASS: clean served page -------------------------------------------


def test_verdict_pass_clean_page():
    v = compute_verdict(
        url="http://127.0.0.1:8000/",
        reachable=True,
        http_status=200,
        structured=_structured(),
        meaningful=True,
    )
    assert v["passed"] is True
    assert v["verdict"] == "pass"
    assert v["http_status"] == 200
    assert v["console_errors"] == [] and v["network_failures"] == []
    # A PASS used to carry `next_action: ""` — the one branch that said nothing,
    # in the one situation where the agent most needs to know its proof is
    # durable. Counted-promotion evidence 2026-07-27 (p4_ff_react_continue seeds
    # 400006/400023): verification passed at action #16 and 33-35 further actions
    # followed, re-proving the same URL. It now states what the verifier knows —
    # and deliberately does NOT say "finish", because this tool cannot see the
    # plan; only the finish gate knows whether steps remain.
    assert v["next_action"], "a passing verdict must still tell the agent where it stands"
    assert "finish" in v["next_action"]
    assert "material change" in v["next_action"]
    assert v["meaningful_content"] is True
    # clean fingerprint is stable + non-empty
    assert v["failure_fingerprint"] == _failure_fingerprint([], [])
    assert v["vision"] == {"used": False, "passed": None, "notes": []}


# ---- (a2) ADVISORY visual self-review — vision ON / OFF ---------------------


def test_verdict_vision_on_seeds_screenshot_and_checklist():
    """H1: with a browser observation carrying screenshot_b64 (vision active), the
    verdict surfaces it AND seeds the advisory checklist — without touching pass/fail."""
    s = _structured()
    s["screenshot_b64"] = "aGVsbG8="  # base64("hello")
    v = compute_verdict(
        url="http://127.0.0.1:8000/",
        reachable=True,
        http_status=200,
        structured=s,
        meaningful=True,
    )
    assert v["screenshot_b64"] == "aGVsbG8="
    assert v["vision"]["used"] is True
    assert v["vision"]["passed"] is None  # advisory only, never a signal
    assert v["vision"]["notes"] == [_VISION_REVIEW_CHECKLIST]
    # the checklist mentions the concrete design dimensions
    assert "padding / alignment / spacing" in _VISION_REVIEW_CHECKLIST
    # the structured finish decision is unaffected by vision
    assert v["passed"] is True and v["verdict"] == "pass"


def test_verdict_vision_off_is_lean_and_carries_structured_claim_evidence():
    """H1: without screenshot_b64 (no-vision model), the verdict carries NO
    screenshot_b64 key, vision.used stays False, and the structured verdict is
    still carries no pixels while retaining bounded DOM/freshness evidence."""
    kwargs = dict(
        url="http://127.0.0.1:8000/",
        reachable=True,
        http_status=200,
        structured=_structured(),  # no screenshot_b64
        meaningful=True,
    )
    v = compute_verdict(**kwargs)  # type: ignore[arg-type]
    assert "screenshot_b64" not in v
    assert v["vision"] == {"used": False, "passed": None, "notes": []}
    # Claim evidence is structured and text-only; screenshot bytes remain absent.
    assert set(v.keys()) == {
        "passed",
        "verdict",
        "url",
        "http_status",
        "title",
        "document_content_type",
        "meaningful_content",
        "visible_text_chars",
        "elements_count",
        "canvas_count",
        "console_errors",
        "console_warnings",
        "network_failures",
        "screenshot_path",
        "rendered_text",
        "visible_dom_text",
        "freshness",
        "vision",
        "failure_fingerprint",
        "summary",
        "next_action",
    }


# ---- (b) FAIL variants: console error / API 500 / blank render --------------


def test_verdict_fail_console_error_has_fingerprint_and_next_action():
    console = [
        {
            "level": "error",
            "text": "Uncaught ReferenceError: x is not defined",
            "location": {
                "url": "http://127.0.0.1:8000/app.js",
                "lineNumber": 12,
                "columnNumber": 3,
            },
        }
    ]
    v = compute_verdict(
        url="http://127.0.0.1:8000/",
        reachable=True,
        http_status=200,
        structured=_structured(console=console),
        meaningful=True,
    )
    assert v["passed"] is False and v["verdict"] == "fail"
    assert len(v["console_errors"]) == 1
    assert v["console_errors"][0]["source"] == "http://127.0.0.1:8000/app.js:12:3"
    assert v["failure_fingerprint"] and v["failure_fingerprint"] != _failure_fingerprint([], [])
    assert "ReferenceError" in v["next_action"]
    assert "console error" in v["summary"]


def test_verdict_fail_network_500():
    network = [{"method": "GET", "url": "http://127.0.0.1:8000/api/data", "status": 500}]
    v = compute_verdict(
        url="http://127.0.0.1:8000/",
        reachable=True,
        http_status=200,
        structured=_structured(network=network),
        meaningful=True,
    )
    assert v["passed"] is False and v["verdict"] == "fail"
    assert v["network_failures"] == [
        {"method": "GET", "url": "http://127.0.0.1:8000/api/data", "status": 500, "failure": None}
    ]
    assert "/api/data" in v["next_action"]


def test_verdict_blank_render_is_degraded():
    v = compute_verdict(
        url="http://127.0.0.1:8000/",
        reachable=True,
        http_status=200,
        structured=_structured(text="", elements=[]),
        meaningful=False,
    )
    assert v["passed"] is False
    assert v["verdict"] == "degraded"
    assert "blank" in v["summary"].lower() or "no visible" in v["summary"].lower()


@pytest.mark.asyncio
async def test_run_short_rendered_heading_passes_with_semantic_evidence(monkeypatch):
    """H335: a visible short heading is content, not a false blank-page verdict."""

    async def fake_browser_run(self, args, ctx):
        return ToolOutcome(
            success=True,
            content="b",
            structured=_structured(title="", text="Live Server Up", elements=[])
            | {"visible_semantic_elements": 1},
        )

    monkeypatch.setattr(verify_app.BrowserTool, "run", fake_browser_run)
    out = await VerifyWebAppTool().run(
        VerifyWebAppArgs(url="http://127.0.0.1:8000/"), _ctx(FakeSandbox("200"))
    )

    assert out.success is True
    assert out.structured["passed"] is True
    assert out.structured["meaningful_content"] is True
    assert out.structured["visible_text_chars"] == len("Live Server Up")


@pytest.mark.asyncio
async def test_run_short_plain_text_document_passes_with_rendered_text_evidence(monkeypatch):
    """A sparse text/plain response is a real rendered document, not a blank SPA."""

    async def fake_browser_run(self, args, ctx):
        return ToolOutcome(
            success=True,
            content="b",
            structured=_structured(title="", text="Node Paused 400913", elements=[])
            | {"document_content_type": "text/plain"},
        )

    monkeypatch.setattr(verify_app.BrowserTool, "run", fake_browser_run)
    out = await VerifyWebAppTool().run(
        VerifyWebAppArgs(url="http://127.0.0.1:8000/"), _ctx(FakeSandbox("200"))
    )

    assert out.success is True
    assert out.structured["passed"] is True
    assert out.structured["meaningful_content"] is True
    assert out.structured["document_content_type"] == "text/plain"
    assert out.structured["rendered_text"] == "Node Paused 400913"


def test_verdict_not_serving():
    v = compute_verdict(
        url="http://127.0.0.1:8000/",
        reachable=False,
        http_status=0,
        structured=None,
        meaningful=False,
    )
    assert v["passed"] is False and v["verdict"] == "fail"
    assert "not serving" in v["summary"].lower()
    # Remediation must steer to the PLATFORM preview (preview_start) and warn AGAINST a
    # manual server — a model-run server duels the PreviewManager (bake-off #9 dueling).
    nxt = v["next_action"].lower()
    assert "preview_start" in nxt
    assert "do not" in nxt or "don't" in nxt  # explicitly discourages a self-run server


# ---- fingerprint stability + allowlist --------------------------------------


def test_fingerprint_stable_across_volatile_line_numbers():
    # Same error, different line/col offsets across reloads → SAME fingerprint
    c1 = [{"level": "error", "text": "TypeError at line 42 offset 0x1f3a"}]
    c2 = [{"level": "error", "text": "TypeError at line 88 offset 0x9c01"}]
    v1 = compute_verdict(
        url="u",
        reachable=True,
        http_status=200,
        structured=_structured(console=c1),
        meaningful=True,
    )
    v2 = compute_verdict(
        url="u",
        reachable=True,
        http_status=200,
        structured=_structured(console=c2),
        meaningful=True,
    )
    assert v1["failure_fingerprint"] == v2["failure_fingerprint"]


def test_fingerprint_differs_for_distinct_errors():
    c1 = [{"level": "error", "text": "ReferenceError boom"}]
    c2 = [{"level": "error", "text": "SyntaxError unexpected token"}]
    v1 = compute_verdict(
        url="u",
        reachable=True,
        http_status=200,
        structured=_structured(console=c1),
        meaningful=True,
    )
    v2 = compute_verdict(
        url="u",
        reachable=True,
        http_status=200,
        structured=_structured(console=c2),
        meaningful=True,
    )
    assert v1["failure_fingerprint"] != v2["failure_fingerprint"]


def test_favicon_and_analytics_network_failures_ignored():
    network = [
        {"method": "GET", "url": "http://127.0.0.1:8000/favicon.ico", "status": 404},
        {
            "method": "GET",
            "url": "https://www.google-analytics.com/g/collect",
            "failure": "blocked",
        },
    ]
    v = compute_verdict(
        url="http://127.0.0.1:8000/",
        reachable=True,
        http_status=200,
        structured=_structured(network=network),
        meaningful=True,
    )
    # favicon + analytics don't break the build → still a pass
    assert v["network_failures"] == []
    assert v["passed"] is True


def test_favicon_resource_console_error_is_ignored_like_its_network_failure():
    console = [
        {
            "level": "error",
            "text": (
                "Failed to load resource: the server responded with a status of 404 (Not Found)"
            ),
            "location": {
                "url": "http://127.0.0.1:8080/favicon.ico",
                "lineNumber": 0,
                "columnNumber": 0,
            },
        }
    ]
    v = compute_verdict(
        url="http://127.0.0.1:8080/",
        reachable=True,
        http_status=200,
        structured=_structured(console=console),
        meaningful=True,
    )
    assert v["console_errors"] == []
    assert v["passed"] is True


def test_console_filter_keeps_real_exceptions_and_load_bearing_resource_failures():
    console = [
        {
            "level": "error",
            "text": "TypeError: cannot read properties of undefined",
            "location": {"url": "http://127.0.0.1:8080/favicon-helper.js"},
        },
        {
            "level": "error",
            "text": (
                "Failed to load resource: the server responded with a status of 404 (Not Found)"
            ),
            "location": {"url": "http://127.0.0.1:8080/assets/main.js"},
        },
    ]
    v = compute_verdict(
        url="http://127.0.0.1:8080/",
        reachable=True,
        http_status=200,
        structured=_structured(console=console),
        meaningful=True,
    )
    assert [entry["text"] for entry in v["console_errors"]] == [
        "TypeError: cannot read properties of undefined",
        "Failed to load resource: the server responded with a status of 404 (Not Found)",
    ]
    assert v["passed"] is False


# ---- run() integration with fakes -------------------------------------------


class _ShellRes:
    def __init__(self, stdout, exit_code=0, stderr=""):
        self.stdout = stdout
        self.exit_code = exit_code
        self.stderr = stderr


class FakeSandbox:
    def __init__(self, status_stdout="200"):
        self._status_stdout = status_stdout

    async def exec_shell(self, cmd, timeout_s=10):
        return _ShellRes(self._status_stdout)


def _ctx(sandbox):
    return ToolContext(
        sandbox=sandbox,
        workspace_path="/workspace",
        timeout_s=30,
        capabilities=None,
        owner_id="o",
        conversation_id="c",
    )


@pytest.mark.asyncio
async def test_run_pass_with_stubbed_browser(monkeypatch):
    async def fake_browser_run(self, args, ctx):
        return ToolOutcome(success=True, content="b", structured=_structured(console=[]))

    monkeypatch.setattr(verify_app.BrowserTool, "run", fake_browser_run)
    out = await VerifyWebAppTool().run(
        VerifyWebAppArgs(url="http://127.0.0.1:8000/"), _ctx(FakeSandbox("200"))
    )
    assert out.success is True
    assert out.structured is not None
    assert out.structured["passed"] is True
    assert out.structured["verdict"] == "pass"
    assert "PASS" in out.content


@pytest.mark.asyncio
async def test_run_mobile_medium_uses_mobile_viewport(monkeypatch):
    seen = {}

    async def fake_browser_run(self, args, ctx):
        seen["viewport"] = (args.viewport_width, args.viewport_height)
        return ToolOutcome(success=True, content="b", structured=_structured(console=[]))

    monkeypatch.setattr(verify_app.BrowserTool, "run", fake_browser_run)
    out = await VerifyWebAppTool().run(
        VerifyWebAppArgs(url="http://127.0.0.1:8000/", medium="mobile"),
        _ctx(FakeSandbox("200")),
    )

    assert out.success is True
    assert out.structured["medium"] == "mobile"
    assert seen["viewport"] == (390, 844)


@pytest.mark.asyncio
async def test_run_game_medium_interacts_and_keeps_canvas_meaningful(monkeypatch):
    seen = []

    async def fake_browser_run(self, args, ctx):
        seen.append((args.action, args.selector, args.key))
        return ToolOutcome(
            success=True,
            content="b",
            structured=_structured(
                text="",
                elements=[],
                console=[],
                title="Canvas Game",
            )
            | {
                "canvas_count": 1,
                "screenshot_path": f".pmx/screenshots/{len(seen):04d}-{args.action}.png",
            },
        )

    monkeypatch.setattr(verify_app.BrowserTool, "run", fake_browser_run)
    out = await VerifyWebAppTool().run(
        VerifyWebAppArgs(url="http://127.0.0.1:8000/", medium="game"),
        _ctx(FakeSandbox("200")),
    )

    assert out.success is True
    assert out.structured["passed"] is True
    assert out.structured["medium"] == "game"
    assert out.structured["canvas_count"] == 1
    assert ("click", "canvas", "") in seen
    assert ("press", "", "Space") in seen
    assert ("press", "", "ArrowRight") in seen
    interaction = out.structured["game_interaction"]
    assert interaction["before_screenshot_path"].endswith("navigate.png")
    assert interaction["after_screenshot_path"].endswith("screenshot.png")


@pytest.mark.asyncio
async def test_run_fail_console_error_with_stubbed_browser(monkeypatch):
    console = [{"level": "error", "text": "Uncaught TypeError"}]

    async def fake_browser_run(self, args, ctx):
        return ToolOutcome(success=True, content="b", structured=_structured(console=console))

    monkeypatch.setattr(verify_app.BrowserTool, "run", fake_browser_run)
    out = await VerifyWebAppTool().run(
        VerifyWebAppArgs(url="http://127.0.0.1:8000/"), _ctx(FakeSandbox("200"))
    )
    assert out.success is True
    assert out.structured["passed"] is False
    assert out.structured["failure_fingerprint"]
    assert out.structured["next_action"]
    # content is stable (no screenshot seq) so the no-progress breaker can match
    assert ".pmx/screenshots" not in out.content


@pytest.mark.asyncio
async def test_run_server_unreachable_no_browser(monkeypatch):
    called = {"browser": False}

    async def fake_browser_run(self, args, ctx):
        called["browser"] = True
        return ToolOutcome(success=True, content="b", structured=_structured())

    monkeypatch.setattr(verify_app.BrowserTool, "run", fake_browser_run)
    # status "0" → unreachable; the tool must NOT launch the browser
    out = await VerifyWebAppTool().run(
        VerifyWebAppArgs(url="http://127.0.0.1:8000/"), _ctx(FakeSandbox("0"))
    )
    assert out.structured["passed"] is False
    assert out.structured["verdict"] == "fail"
    assert called["browser"] is False


@pytest.mark.asyncio
async def test_run_browser_unavailable_is_terminal_not_degraded(monkeypatch):
    """ROOT-3 (slides spiral): when the browser daemon can't start, a REACHABLE
    server must yield a terminal 'unverifiable' verdict the agent treats as
    done-with-verification — NOT a misleading blank-render DEGRADED it retries."""
    from disco.tools.builtin.browser import BROWSER_UNAVAILABLE_MSG

    async def fake_browser_run(self, args, ctx):
        # Mirrors BrowserTool's BrowserUnavailableError handling.
        return ToolOutcome(
            success=False,
            content="",
            error=BROWSER_UNAVAILABLE_MSG,
            structured={
                "browser_unavailable": True,
                "startup_diagnostic": "exit=1; chromium launch failed",
            },
        )

    monkeypatch.setattr(verify_app.BrowserTool, "run", fake_browser_run)
    out = await VerifyWebAppTool().run(
        VerifyWebAppArgs(url="http://127.0.0.1:8000/"), _ctx(FakeSandbox("200"))
    )
    assert out.success is True
    assert out.structured["browser_unavailable"] is True
    assert out.structured["verdict"] == "unverifiable"
    assert out.structured["verdict"] != "degraded"
    assert out.structured["passed"] is False
    assert out.structured["failure_fingerprint"] == "browser_unavailable"
    assert out.structured["startup_diagnostic"] == ("exit=1; chromium launch failed")
    assert out.content.startswith("VERIFY_WEB_APP: UNVERIFIABLE")
    assert "Browser rendering remains unverified" in out.content
    assert "report the missing browser proof explicitly" in out.content
    assert "does not require a browser" not in out.content


@pytest.mark.asyncio
async def test_run_browser_failure_is_not_fabricated_as_blank_page(monkeypatch):
    """A failed render probe is infrastructure evidence, not an empty DOM."""

    async def fake_browser_run(self, args, ctx):
        return ToolOutcome(
            success=False,
            content="browser error: screenshot workspace is read-only",
            error="browser error: screenshot workspace is read-only",
        )

    monkeypatch.setattr(verify_app.BrowserTool, "run", fake_browser_run)
    out = await VerifyWebAppTool().run(
        VerifyWebAppArgs(url="http://127.0.0.1:8000/"), _ctx(FakeSandbox("200"))
    )
    assert out.success is False
    assert out.structured["render_probe_failed"] is True
    assert out.structured["verdict"] == "unverifiable"
    assert "render probe failed" in out.content
    assert "blank page" not in out.content


# ---- Bug 7: process-backend auto-detect targets the conversation's served port,
#      never the agent-server's control port (8000) -----------------------------


class _ProcessLikeSandbox:
    """A process-backend-shaped sandbox: `workspace_path` is a host path (Bug 7's
    shared-host signal) and `exec_shell` answers the port-ownership probe with 8000
    owned by a NON-conversation agent-server and 8080 owned by THIS conversation."""

    conversation_id = "conv_zzzz1111"  # cid8 == "conv_zzz"
    workspace_path = "/tmp/sbx-proc"

    def __init__(self):
        self.probed_url: str | None = None

    async def exec_shell(self, cmd, *, timeout_s=10):
        # The ownership probe ships ports as bare argv; answer with the soak shape.
        if "port" in cmd.lower() and ("8080" in cmd or "8000" in cmd):
            return _ShellRes(
                '[{"port": 8000, "pid": 7, "session": "disco-other777-preview"},'
                ' {"port": 8080, "pid": 9, "session": "disco-conv_zzz-preview"},'
                ' {"port": 5173, "pid": null, "session": null},'
                ' {"port": 3000, "pid": null, "session": null},'
                ' {"port": 5000, "pid": null, "session": null},'
                ' {"port": 4321, "pid": null, "session": null}]'
            )
        return _ShellRes("0")  # HTTP probe → unreachable (we only assert the target)


@pytest.mark.asyncio
async def test_process_autodetect_targets_conversation_port_not_8000(monkeypatch):
    """`verify_web_app({})` on the process backend must verify the conversation's
    served port (8080), NEVER the agent-server's control port (8000)."""
    sbx = _ProcessLikeSandbox()

    async def fake_probe(self, ctx, url):
        sbx.probed_url = url
        return (False, 0)  # unreachable; we only care WHICH url was targeted

    monkeypatch.setattr(VerifyWebAppTool, "_probe_http", fake_probe)
    out = await VerifyWebAppTool().run(VerifyWebAppArgs(url=""), _ctx(sbx))
    assert sbx.probed_url == "http://127.0.0.1:8080/", sbx.probed_url
    assert "8000" not in (sbx.probed_url or "")
    # not-serving verdict (we forced unreachable) — honest, not a fabricated pass.
    assert out.structured["passed"] is False


@pytest.mark.asyncio
async def test_process_autodetect_targets_exact_managed_host_port(monkeypatch):
    """Blank verification follows the manager selection without scanning the range."""

    class _ManagedHostSandbox(_ProcessLikeSandbox):
        def __init__(self):
            super().__init__()
            self._preview_manager = SimpleNamespace(
                canonical_port=lambda: 10_123,
                list=lambda: [
                    SimpleNamespace(
                        port=10_123,
                        status=SimpleNamespace(value="running"),
                    )
                ],
            )

        async def exec_shell(self, cmd, *, timeout_s=10):
            if "10123" in cmd:
                return _ShellRes(
                    '[{"port": 8000, "pid": 7, "session": "disco-other777-preview"},'
                    ' {"port": 10123, "pid": 9, "session": "disco-conv_zzz-preview"}]'
                )
            return _ShellRes("0")

    sbx = _ManagedHostSandbox()

    async def fake_probe(self, ctx, url):
        sbx.probed_url = url
        return (False, 0)

    monkeypatch.setattr(VerifyWebAppTool, "_probe_http", fake_probe)
    out = await VerifyWebAppTool().run(VerifyWebAppArgs(url=""), _ctx(sbx))

    assert sbx.probed_url == "http://127.0.0.1:10123/"
    assert out.structured["passed"] is False


@pytest.mark.asyncio
async def test_process_autodetect_no_conversation_port_is_undetectable(monkeypatch):
    """P1 #1: when NO port is conversation-owned (only the agent-server on 8000 and
    the Vite UI on 5173 are reachable — both FOREIGN), auto-detect is UNDETECTABLE:
    it probes "" (never guesses 5173 or 8000), so the tool reports not-serving
    instead of a FALSE PASS against the UI / agent-server."""

    class _OnlyForeignServers(_ProcessLikeSandbox):
        async def exec_shell(self, cmd, *, timeout_s=10):
            return _ShellRes(
                '[{"port": 8000, "pid": 7, "session": "disco-other777-preview"},'
                ' {"port": 5173, "pid": 8, "session": "disco-other777-ui"},'
                ' {"port": 8080, "pid": null, "session": null},'
                ' {"port": 3000, "pid": null, "session": null},'
                ' {"port": 5000, "pid": null, "session": null},'
                ' {"port": 4321, "pid": null, "session": null}]'
            )

    sbx = _OnlyForeignServers()

    async def fake_probe(self, ctx, url):
        sbx.probed_url = url
        return (False, 0)

    monkeypatch.setattr(VerifyWebAppTool, "_probe_http", fake_probe)
    out = await VerifyWebAppTool().run(VerifyWebAppArgs(url=""), _ctx(sbx))
    assert sbx.probed_url == "", sbx.probed_url  # undetectable — no blind guess
    assert "5173" not in (sbx.probed_url or "")
    assert "8000" not in (sbx.probed_url or "")
    assert out.structured["passed"] is False  # not-serving, never a false pass


# ---- Bug 7 explicit-url bypass: an agent-supplied url is validated on the shared
#      host too (reserved/foreign port → not-this-build's-app, no false PASS) --------


@pytest.mark.asyncio
async def test_explicit_reserved_port_rejected_on_shared_host():
    """An explicit url pointed at a reserved control/UI port (5173 = Vite UI, 8000 =
    agent-server, 8800 = app-server) on the shared host is NOT verified as the build's
    app — not-serving verdict + a clear 'reserved' reason, never a false PASS."""
    for bad in (
        "http://127.0.0.1:5173/",
        "http://127.0.0.1:8000/",
        "http://127.0.0.1:8800/",
    ):
        out = await VerifyWebAppTool().run(VerifyWebAppArgs(url=bad), _ctx(_ProcessLikeSandbox()))
        assert out.structured["passed"] is False, bad
        assert out.structured["verdict"] == "fail", bad
        assert "reserved" in out.structured["summary"].lower(), out.structured["summary"]


@pytest.mark.asyncio
async def test_explicit_foreign_sibling_port_rejected_on_shared_host():
    """An explicit url to a NON-reserved port owned by a SIBLING conversation (not
    this one) is rejected — never verify another conversation's app as ours."""

    class _SiblingOwns3000(_ProcessLikeSandbox):
        async def exec_shell(self, cmd, *, timeout_s=10):
            return _ShellRes(
                '[{"port": 3000, "pid": 5, "session": "disco-sibling1-dev"},'
                ' {"port": 8000, "pid": 7, "session": "disco-other777-preview"},'
                ' {"port": 8080, "pid": null, "session": null},'
                ' {"port": 5173, "pid": null, "session": null},'
                ' {"port": 5000, "pid": null, "session": null},'
                ' {"port": 4321, "pid": null, "session": null}]'
            )

    out = await VerifyWebAppTool().run(
        VerifyWebAppArgs(url="http://127.0.0.1:3000/"), _ctx(_SiblingOwns3000())
    )
    assert out.structured["passed"] is False
    assert "not owned by this conversation" in out.structured["summary"].lower()


@pytest.mark.asyncio
async def test_explicit_conversation_owned_port_is_honored_on_shared_host(monkeypatch):
    """An explicit url to THIS conversation's OWN served port (8080, conversation-
    owned) is honored normally and verified."""
    sbx = _ProcessLikeSandbox()

    async def fake_probe(self, ctx, url):
        sbx.probed_url = url
        return (False, 0)

    monkeypatch.setattr(VerifyWebAppTool, "_probe_http", fake_probe)
    await VerifyWebAppTool().run(VerifyWebAppArgs(url="http://127.0.0.1:8080/"), _ctx(sbx))
    assert sbx.probed_url == "http://127.0.0.1:8080/", sbx.probed_url


@pytest.mark.asyncio
async def test_explicit_url_honored_on_isolated_backend(monkeypatch):
    """On an ISOLATED backend (no `workspace_path`) the explicit url is honored as-is
    — 8000 is the box's app; unchanged from before the Bug 7 fix."""
    probed: dict[str, str] = {}

    async def fake_probe(self, ctx, url):
        probed["url"] = url
        return (False, 0)

    monkeypatch.setattr(VerifyWebAppTool, "_probe_http", fake_probe)
    await VerifyWebAppTool().run(
        VerifyWebAppArgs(url="http://127.0.0.1:8000/"), _ctx(FakeSandbox("0"))
    )
    assert probed["url"] == "http://127.0.0.1:8000/"


def test_tool_definition_registered_low_risk():
    d = VerifyWebAppTool.definition
    assert d.name == "verify_web_app"
    assert d.base_risk == SecurityRisk.LOW
    assert d.runs_in == "sandbox"
    assert "not semantic completeness or completion authority" in d.description
    assert "TWO debug calls shared" in d.description


class _FsSandbox:
    """A minimal bytes fs sandbox for the eject-banner path (read/write/exists)."""

    def __init__(self):
        self.fs: dict[str, bytes] = {}

    async def file_exists(self, path):
        return path in self.fs

    async def read_file(self, path):
        return self.fs[path]

    async def write_file(self, path, data):
        self.fs[path] = data


@pytest.mark.asyncio
async def test_eject_banner_reasserted_and_idempotent():
    """The eject banner is (re)written for an ejected component whose GUIDE has
    none, and a second pass never duplicates it. Regression: read_file returns
    BYTES, so the presence check must be a bytes literal (a str-in-bytes check
    would TypeError on the real sandbox)."""
    from disco.core.trusted_components.lockfile import (
        ComponentsLock,
        EjectRecord,
        InstalledComponent,
    )
    from disco.core.trusted_components.verify import install_path

    sb = _FsSandbox()
    guide_path = install_path("auth-kit", "GUIDE.md")
    sb.fs[guide_path] = b"# auth-kit\n"  # pristine — banner was reverted away
    lock = ComponentsLock()
    lock.components["auth-kit"] = InstalledComponent(
        version="1.0.0", installed_at="2026-07-10T00:00:00+00:00", ejected=True
    )
    lock.ejects.append(
        EjectRecord(name="auth-kit", at="2026-07-10T00:00:00+00:00", reason="core-edit-detected")
    )

    tool = VerifyWebAppTool()
    await tool._append_eject_banner(_ctx(sb), lock, "auth-kit")
    assert b"**Ejected" in sb.fs[guide_path]
    after_first = sb.fs[guide_path]

    # Idempotent: a second call must NOT append a duplicate banner.
    await tool._append_eject_banner(_ctx(sb), lock, "auth-kit")
    assert sb.fs[guide_path] == after_first
    assert sb.fs[guide_path].count(b"**Ejected") == 1


# ---- render/export mismatch: the rendered text must faithfully reflect the
#      structured verdict state (the allowlisted _render surface owns this) ----


def test_render_fail_verdict_does_not_render_as_pass():
    """A FAIL verdict must render as FAIL, never as PASS — the rendered text is
    the agent-facing export of the structured verdict, and a mismatch would let a
    failing build present as passing."""
    from disco.tools.builtin.verify_app_parts._render import _render as render

    v = compute_verdict(
        url="http://127.0.0.1:8000/",
        reachable=False,
        http_status=0,
        structured=None,
        meaningful=False,
    )
    assert v["passed"] is False
    text = render(v)
    assert "FAIL" in text
    assert "PASS" not in text


def test_render_pass_verdict_renders_as_pass():
    """Positive control: a PASS verdict renders as PASS — the rendered export
    matches the structured state in both directions."""
    from disco.tools.builtin.verify_app_parts._render import _render as render

    v = compute_verdict(
        url="http://127.0.0.1:8000/",
        reachable=True,
        http_status=200,
        structured=_structured(),
        meaningful=True,
    )
    assert v["passed"] is True
    text = render(v)
    assert "PASS" in text
    assert "FAIL" not in text


def test_render_degraded_verdict_never_claims_pass():
    """A DEGRADED verdict is not a pass — the rendered text must not claim PASS."""
    from disco.tools.builtin.verify_app_parts._render import _render as render

    v = compute_verdict(
        url="http://127.0.0.1:8000/",
        reachable=True,
        http_status=200,
        structured=_structured(text="", elements=[]),
        meaningful=False,
    )
    assert v["verdict"] == "degraded"
    text = render(v)
    assert "PASS" not in text


# ---- evidence-not-verdict contract: target probes produce immutable evidence,
#      never a typed HostVerificationResult ----


def test_web_app_probe_verdict_is_plain_evidence_not_typed_receipt():
    """Tools are evidence producers, not verdict authorities: compute_verdict
    returns a plain dict, never a typed HostVerificationResult. A target probe
    must not manufacture or upgrade a host verification receipt."""
    from disco.core.verification import HostVerificationResult

    v = compute_verdict(
        url="http://127.0.0.1:8000/",
        reachable=True,
        http_status=200,
        structured=_structured(),
        meaningful=True,
    )
    assert isinstance(v, dict)
    assert not isinstance(v, HostVerificationResult)


def test_collect_web_app_probe_returns_plain_evidence_dict():
    """The probe collector returns a plain evidence dict, not a typed receipt."""
    from disco.core.verification import HostVerificationResult
    from disco.tools.verify.web_app_probe import collect_web_app_probe

    probe = collect_web_app_probe(_structured())
    assert isinstance(probe, dict)
    assert not isinstance(probe, HostVerificationResult)
    # the evidence carries the deterministic diagnostics the host gate consumes
    assert "failure_fingerprint" in probe
    assert "console_errors" in probe
    assert "network_failures" in probe


# ---- valid target-specific positive controls: the web-app probe classifies
#      real web target evidence correctly ----


def test_web_app_probe_positive_control_clean_web_page():
    """Positive control: a clean served web page with content, no console errors,
    and no critical network failures classifies as PASS with a clean fingerprint."""
    v = compute_verdict(
        url="http://127.0.0.1:8000/",
        reachable=True,
        http_status=200,
        structured=_structured(),
        meaningful=True,
    )
    assert v["passed"] is True
    assert v["verdict"] == "pass"
    assert v["console_errors"] == []
    assert v["network_failures"] == []
    # the clean fingerprint is stable and non-empty
    assert v["failure_fingerprint"] == _failure_fingerprint([], [])
    assert v["failure_fingerprint"] != ""


def test_web_app_probe_positive_control_console_error_classified():
    """Positive control: a web page with a console error is classified as FAIL
    with the error captured in the evidence."""
    console = [{"level": "error", "text": "Uncaught TypeError: x is not a function"}]
    v = compute_verdict(
        url="http://127.0.0.1:8000/",
        reachable=True,
        http_status=200,
        structured=_structured(console=console),
        meaningful=True,
    )
    assert v["passed"] is False
    assert v["verdict"] == "fail"
    assert len(v["console_errors"]) == 1
    assert "TypeError" in v["console_errors"][0]["text"]
