"""W3 C-4 — the `process` (dev) backend jupyter kernel must launch with a
SCRUBBED environment.

Before the fix, `ProcessKernel.start()` called `start_kernel(cwd=...)` with no
`env=`, so jupyter_client defaulted the child env to `os.environ` — and untrusted
model `code_exec` on this backend could read `os.environ['DISCO_SECRET_KEY']` and
the OpenRouter key straight out. This pins that the launcher passes only the
allowlisted PATH/HOME/TMPDIR/DISCO_WORKSPACE (shared with the shell path via
`base.clean_sandbox_env`).
"""

from __future__ import annotations

from pathlib import Path

import jupyter_client.manager as _jm


async def test_process_kernel_launches_with_scrubbed_env(monkeypatch):
    monkeypatch.setenv("DISCO_SECRET_KEY", "LEAK-CANARY-MASTER")
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-LEAK-CANARY")
    captured: dict[str, object] = {}

    class _FakeKC:
        def start_channels(self) -> None:
            pass

        def stop_channels(self) -> None:
            pass

        async def wait_for_ready(self, timeout: int = 60) -> None:
            pass

    class _FakeKM:
        def __init__(self, kernel_name: str | None = None, **kw: object) -> None:
            captured["manager_kernel_name"] = kernel_name
            captured["manager_options"] = kw
            self.extra_arguments: list[str] = []

        async def start_kernel(self, **kw: object) -> None:
            captured.update(kw)

        async def shutdown_kernel(self) -> None:
            pass

        def client(self) -> _FakeKC:
            return _FakeKC()

    monkeypatch.setattr(_jm, "AsyncKernelManager", _FakeKM)

    from disco.tools.sandbox.kernel import ProcessKernel

    pk = ProcessKernel("/tmp/disco-ws-test")

    # The RLIMIT setup cell needs a live kernel protocol; stub it — we only assert
    # the launch env here.
    async def _noop(*a: object, **k: object) -> None:
        return None

    monkeypatch.setattr(pk, "execute", _noop)

    await pk.start()

    manager_options = captured["manager_options"]
    assert isinstance(manager_options, dict)
    assert manager_options["transport"] == "ipc"
    assert Path(str(manager_options["ip"])).is_absolute()

    env = captured.get("env")
    assert isinstance(env, dict), "start_kernel called without env= → inherits os.environ (leak)"
    assert "DISCO_SECRET_KEY" not in env
    assert "OPENROUTER_API_KEY" not in env
    assert "PMX_SECRET_KEY" not in env
    # …but the minimal allowlist IS present so the kernel still runs.
    assert env["PATH"]
    assert env["HOME"] == "/tmp/disco-ws-test"
    assert env["DISCO_WORKSPACE"] == "/tmp/disco-ws-test"
    await pk.shutdown()
