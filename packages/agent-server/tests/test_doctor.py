"""`python -m disco.agent_server.doctor` — every check says PASS/WARN/FAIL/SKIP with the reason."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import httpx
from disco.agent_server import doctor as d

AGENT = "http://127.0.0.1:8000"
APP = "http://app-server:8800"


def _transport(routes: dict[str, httpx.Response | Exception]) -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        key = f"{request.method} {request.url.path}"
        hit = routes.get(key)
        if isinstance(hit, Exception):
            raise hit
        return hit or httpx.Response(404, json={"detail": "not found"})

    return httpx.MockTransport(handler)


HEALTHY = {
    "GET /health": httpx.Response(200, json={"status": "ok", "version": "v0.2.0 (abc1234)", "checks": {}}),
    "GET /api/health": httpx.Response(200, json={"status": "ok", "service": "app-server", "version": "v0.2.0 (abc1234)"}),
    "POST /api/auth/mint": httpx.Response(200, json={"ok": True, "csrf_token": "csrf"}, headers={"set-cookie": "disco_session=abc; Path=/"}),
    "GET /api/sandbox/health": httpx.Response(200, json={"reachable": True, "backend": "podman", "detail": "ok"}),
    "GET /conversations": httpx.Response(200, json={"conversation_ids": ["conv_1"]}),
    "GET /conversations/conv_1/state": httpx.Response(200, json={"execution_status": "FINISHED", "iteration": 6}),
    "GET /conversations/conv_1/events": httpx.Response(200, json={"events": [
        {"seq": 1, "kind": "message", "message": {"content": "SECRET PROMPT"}},
        {"seq": 2, "kind": "action", "tool_call": {"tool_name": "shell", "arguments": {"command": "cat .env"}}},
        {"seq": 3, "kind": "observation", "tool_result": {"tool_name": "shell", "success": True, "content": "api_key=sk-live"}},
        {"seq": 4, "kind": "agent_error", "error": "command exited 1"},
    ]}),
}


def _doctor(routes, tmp_path: Path, **env) -> d.Doctor:
    db = tmp_path / "disco.db"
    conn = sqlite3.connect(db)
    conn.execute("CREATE TABLE IF NOT EXISTS events (id TEXT)")
    conn.execute("DELETE FROM events")
    conn.execute("INSERT INTO events VALUES ('e1')")
    conn.commit(); conn.close()
    base_env = {"DISCO_DB": str(db), "DISCO_DATA_DIR": str(tmp_path), "DISCO_SECRET_KEY": "k", "DISCO_BUILD_COMMIT": "abc1234", "DISCO_BUILD_TAG": "v0.2.0"}
    base_env.update(env)
    return d.Doctor(agent_url=AGENT, app_url=APP, transport=_transport(routes), env=base_env, run_model_check=False)


def test_healthy_stack_passes_every_check(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("DISCO_BUILD_COMMIT", "abc1234"); monkeypatch.setenv("DISCO_BUILD_TAG", "v0.2.0")
    checks = _doctor(HEALTHY, tmp_path).run()
    by_name = {c.name: c for c in checks}
    assert by_name["config"].status == "PASS" and "v0.2.0 (abc1234)" in by_name["config"].detail
    assert by_name["agent-server"].status == "PASS"
    assert by_name["app-server"].status == "PASS"
    assert by_name["sandbox"] == d.Check("sandbox", "PASS", "podman reachable")
    assert by_name["data disk"].status in ("PASS", "WARN")
    assert by_name["database"].status == "PASS" and "1 events" in by_name["database"].detail
    assert by_name["secret key"] == d.Check("secret key", "PASS", "DISCO_SECRET_KEY set")
    assert by_name["driver model"] == d.Check("driver model", "SKIP", "--no-model")
    assert not any(c.status == "FAIL" for c in checks)


def test_unreachable_agent_server_and_sandbox_fail_with_the_reason(tmp_path) -> None:
    routes = dict(HEALTHY)
    routes["GET /health"] = httpx.ConnectError("connection refused")
    routes["GET /api/sandbox/health"] = httpx.Response(200, json={"reachable": False, "backend": "podman", "detail": "podman sandbox host unix:///var/run/docker.sock unreachable: No such file"})
    by_name = {c.name: c for c in _doctor(routes, tmp_path).run()}
    assert by_name["agent-server"].status == "FAIL" and "connection refused" in by_name["agent-server"].detail
    assert by_name["sandbox"].status == "FAIL" and "unix:///var/run/docker.sock" in by_name["sandbox"].detail


def test_degraded_health_names_the_failing_check(tmp_path) -> None:
    routes = dict(HEALTHY)
    routes["GET /health"] = httpx.Response(503, json={"status": "degraded", "version": "x", "checks": {"store": "error: disk I/O", "runtime": "ok"}})
    by_name = {c.name: c for c in _doctor(routes, tmp_path).run()}
    assert by_name["agent-server"].status == "FAIL"
    assert "store" in by_name["agent-server"].detail and "runtime" not in by_name["agent-server"].detail


def test_app_server_unreachable_is_a_warning_with_the_flag_hint(tmp_path) -> None:
    routes = dict(HEALTHY)
    routes["GET /api/health"] = httpx.ConnectError("name resolution failed")
    by_name = {c.name: c for c in _doctor(routes, tmp_path).run()}
    assert by_name["app-server"].status == "WARN" and "--app-url" in by_name["app-server"].detail


def test_pairing_required_skips_the_sandbox_check(tmp_path) -> None:
    routes = dict(HEALTHY)
    routes["POST /api/auth/mint"] = httpx.Response(401, json={"detail": {"reason": "pairing_required"}})
    by_name = {c.name: c for c in _doctor(routes, tmp_path).run()}
    assert by_name["sandbox"].status == "SKIP"


def test_missing_db_secret_and_legacy_shadow_are_warnings(tmp_path) -> None:
    doctor = _doctor(HEALTHY, tmp_path, DISCO_DB=str(tmp_path / "missing.db"), DISCO_SECRET_KEY="", PMX_LOG_LEVEL="DEBUG")
    doctor.env.pop("DISCO_SECRET_KEY")
    by_name = {c.name: c for c in doctor.run()}
    assert by_name["database"].status == "WARN"
    assert by_name["secret key"].status == "WARN"
    assert by_name["config"].status == "WARN" and "PMX_LOG_LEVEL" in by_name["config"].detail


def test_disk_thresholds(tmp_path, monkeypatch) -> None:
    import shutil

    doctor = _doctor(HEALTHY, tmp_path)
    monkeypatch.setattr(shutil, "disk_usage", lambda _p: shutil._ntuple_diskusage(100, 99, 300 * 1024**2))
    assert doctor.check_disk().status == "FAIL"
    monkeypatch.setattr(shutil, "disk_usage", lambda _p: shutil._ntuple_diskusage(100, 99, 1 * 1024**3))
    assert doctor.check_disk().status == "WARN"
    monkeypatch.setattr(shutil, "disk_usage", lambda _p: shutil._ntuple_diskusage(100, 99, 50 * 1024**3))
    assert doctor.check_disk().status == "PASS"


def test_bundle_is_redacted_and_carries_shapes_not_content(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("DISCO_BUILD_COMMIT", "abc1234"); monkeypatch.setenv("DISCO_BUILD_TAG", "v0.2.0")
    doctor = _doctor(HEALTHY, tmp_path, DISCO_OPENROUTER_API_KEY="sk-live-123")
    checks = doctor.run()
    bundle = doctor.bundle(checks)
    text = json.dumps(bundle)
    assert "sk-live-123" not in text and "SECRET PROMPT" not in text and "api_key=sk-live" not in text
    assert bundle["env"]["DISCO_OPENROUTER_API_KEY"] == "***REDACTED***"
    assert bundle["build"]["commit"] == "abc1234"
    [conversation] = bundle["conversations"]
    assert conversation["conversation_id"] == "conv_1" and conversation["status"] == "FINISHED"
    assert conversation["events"][1] == {"seq": 2, "kind": "action", "tool": "shell"}
    assert conversation["events"][2] == {"seq": 3, "kind": "observation", "success": True}
    assert conversation["events"][3] == {"seq": 4, "kind": "agent_error", "error": "command exited 1"}


def test_render_and_exit_code(tmp_path, monkeypatch, capsys) -> None:
    routes = dict(HEALTHY)
    routes["GET /health"] = httpx.ConnectError("refused")
    instance = _doctor(routes, tmp_path)
    monkeypatch.setattr(d, "Doctor", lambda **kw: instance)
    code = d.main(["--no-model", "--bundle", str(tmp_path / "bundle.json")])
    out = capsys.readouterr().out
    assert code == 1
    assert "FAIL agent-server" in out and "1 check(s) FAILED" in out
    assert "support bundle written" in out
    assert json.loads((tmp_path / "bundle.json").read_text())["checks"]
