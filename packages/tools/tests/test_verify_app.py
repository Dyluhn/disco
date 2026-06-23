"""W-45 — `verify_web_app` tool: deterministic verdict + stable fingerprint.

The verdict logic is a PURE function (`compute_verdict`) so it is tested directly
without a sandbox. A single `run()` integration test exercises the wiring with a
fake sandbox + a stubbed BrowserTool (no real Playwright daemon).
"""

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
    assert v["next_action"] == ""
    assert v["meaningful_content"] is True
    # clean fingerprint is stable + non-empty
    assert v["failure_fingerprint"] == _failure_fingerprint([], [])
    assert v["vision"] == {"used": False, "passed": None, "notes": []}


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
    assert "dev server" in v["next_action"].lower()


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


def test_tool_definition_registered_low_risk():
    d = VerifyWebAppTool.definition
    assert d.name == "verify_web_app"
    assert d.base_risk == SecurityRisk.LOW
    assert d.runs_in == "sandbox"
