"""BP-16 marathon harness fixtures.

Run order is phase A → B → C (separate pytest invocations; state flows through
test-record/marathon/state.json):

    uv run pytest harness/marathon/test_phase_a.py -s
    uv run pytest harness/marathon/test_phase_b.py -s
    uv run pytest harness/marathon/test_phase_c.py -s

Preconditions (the orchestrator's, not the harness's):
  * agent-server on :8000 with PMX_LOG_JSON=1, log tee'd to /tmp/pmx-marathon.log
  * VM-201 reachable (ssh sandbox@100.81.82.115)
The vite dev server on :5174 is harness-managed (same port the sanctioned
playwright.live.config.ts uses; never :5173 — that one is the user's).
"""

from __future__ import annotations

import subprocess
import time
from pathlib import Path

import httpx
import pytest
from common import API, RECORD_DIR, ROOT, SHOT_DIR, UI


_MARATHON_DIR = Path(__file__).parent


def pytest_collection_modifyitems(items: list[pytest.Item]) -> None:
    """Mark every marathon test `live`.

    PKG-19-CERT-STRUCTURAL finding F6-b: these phases require a reachable
    agent-server (and VM-201, and vite, and Firefox — see the module docstring),
    but carried **no marker**, so a deterministic battery had no contractual way
    to exclude them. `pytest -q harness -m "not live and not sandbox_integration"`
    still collected them, phases A and B ERRORed at fixture setup, and C then
    failed on the phase log the earlier phases never wrote.

    Applied here rather than as a `pytestmark` in each `test_phase_*.py` so a
    future phase file cannot be added without it. It is also why this costs the
    recorded-marker baseline nothing: the inventory scanner counts
    skip/xfail/todo/only *decorators in source text*, and `live` is a selection
    marker applied at collection, not a suppression.

    **The path filter is load bearing, not defensive.** A
    `pytest_collection_modifyitems` hook in ANY conftest is handed the whole
    session's item list, not just the items under its own directory. The first
    version of this hook omitted the filter and marked all **1253** harness
    tests `live`, so `pytest -q harness -m "not live"` collected nothing and
    exited 5 — a silent total loss of the harness battery that looked like a
    passing run to anything checking only for failures. Caught by running the
    complete battery; kept here as the reason the filter exists.
    """
    for item in items:
        try:
            item_path = Path(str(item.fspath))
        except AttributeError:  # pragma: no cover - defensive for exotic items
            continue
        if _MARATHON_DIR in item_path.parents:
            item.add_marker(pytest.mark.live)


def _up(url: str, timeout: float = 3.0) -> bool:
    try:
        return httpx.get(url, timeout=timeout).status_code < 500
    except Exception:  # noqa: BLE001
        return False


@pytest.fixture(scope="session")
def dirs() -> None:
    RECORD_DIR.mkdir(parents=True, exist_ok=True)
    SHOT_DIR.mkdir(parents=True, exist_ok=True)


@pytest.fixture(scope="session")
def agent_server() -> str:
    assert _up(f"{API}/health"), (
        f"agent-server not reachable at {API} — respawn the tmux pane "
        "(PMX_LOG_JSON=1, tee /tmp/pmx-marathon.log) before running the gate"
    )
    return API


@pytest.fixture(scope="session")
def vite() -> str:
    """Serve the frontend on :5174 for the harness run; reuse a live one."""
    if _up(UI):
        yield UI
        return
    proc = subprocess.Popen(
        ["npm", "run", "dev", "--", "--port", "5174", "--strictPort"],
        cwd=ROOT / "frontend",
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    deadline = time.monotonic() + 60
    while time.monotonic() < deadline and not _up(UI):
        time.sleep(1)
    assert _up(UI), "vite never came up on :5174"
    yield UI
    proc.terminate()
    proc.wait(timeout=15)


@pytest.fixture()
def firefox(vite, agent_server, dirs):
    from playwright.sync_api import sync_playwright

    with sync_playwright() as pw:
        browser = pw.firefox.launch(headless=True)
        ctx = browser.new_context(
            base_url=UI,
            viewport={"width": 1280, "height": 2200},  # bp-04 lesson: no autoscroll
        )
        page = ctx.new_page()
        yield page
        ctx.close()
        browser.close()
