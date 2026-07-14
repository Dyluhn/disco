"""BP-16 marathon — the UI-driving steps shared by Phase A and Phase B.

Everything here goes through the REAL frontend in Firefox (no API shortcuts
for user actions): front-door submit, paperclip upload, plan approval, and the
final deliverable battery.

Front-door reality (documented deviation from the order's step wording): the
UI creates the conversation ON first submit (`useBuild.submit` → POST
/conversations), so "create → upload → submit" is not reachable as three
separate user actions. The equivalent honest sequence is submit (creates the
conversation and plan-gates it) → upload while AWAITING_PLAN_APPROVAL → approve.
The upload announcement lands before ANY execution step runs.
"""

from __future__ import annotations

import json
import re
import time

from common import (
    API,
    CSV_PATH,
    SHOT_DIR,
    TASK_PROMPT,
    api_get,
    csv_known_values,
    exec_status,
    full_events,
)

PLACEHOLDER = re.compile("Describe what you want the agent to build")
APPROVE_RUN = re.compile(r"Approve.*run", re.I)
CONTINUE_ANYWAY = re.compile(r"Continue anyway", re.I)
APPROVE_BUILD = re.compile(r"Approve.*build", re.I)
QUESTION_ANSWER = (
    "Use your best judgment and continue — everything needed is in the "
    "uploaded sensor_readings.csv and the original task prompt."
)


def answer_gates(page, cid: str, *, shot_path=None) -> str | None:
    """Act as the user on ANY mid-run gate, through the real UI. The marathon
    user's policy is always 'keep going': approve soft-confirms, take
    'Continue anyway' on the repeated-failure gate, answer questions with a
    defer-to-agent line, approve mid-run plan re-gates. Returns the gate kind
    handled (or None)."""
    r = api_get(f"/conversations/{cid}/state", timeout=15)
    if r.status_code != 200:
        return None
    s = r.json()
    status = s.get("execution_status")
    try:
        if status == "WAITING_FOR_CONFIRMATION":
            btn = page.get_by_role("button", name=APPROVE_RUN)
            btn.wait_for(state="visible", timeout=20_000)
            if shot_path is not None:
                page.screenshot(path=str(shot_path))
            btn.click()
            print(f"[gate] approved a confirmation gate for {cid}")
            return "confirmation"
        if status == "AWAITING_USER_DECISION":
            if s.get("pending_question_id"):
                box = page.get_by_role("textbox", name="Answer the agent's question")
                box.wait_for(state="visible", timeout=20_000)
                if shot_path is not None:
                    page.screenshot(path=str(shot_path))
                box.fill(QUESTION_ANSWER)
                page.get_by_role("button", name="Send answer").click()
                print(f"[gate] answered an agent question for {cid}")
                return "question"
            gate = page.get_by_role(
                "alertdialog", name="The agent needs a decision after repeated failures"
            )
            target = gate.get_by_role("button", name=CONTINUE_ANYWAY)
            if not target.count():
                target = gate.get_by_role("button").first  # no bypass → first option
            target.wait_for(state="visible", timeout=20_000)
            if shot_path is not None:
                page.screenshot(path=str(shot_path))
            target.click()
            print(f"[gate] picked an alternatives option for {cid}")
            return "alternatives"
        if status == "AWAITING_PLAN_APPROVAL":
            btn = page.get_by_role("button", name=APPROVE_BUILD)
            btn.wait_for(state="visible", timeout=20_000)
            btn.click()
            print(f"[gate] approved a mid-run plan re-gate for {cid}")
            return "plan"
    except Exception as exc:  # noqa: BLE001 — poll loops retry
        print(f"[gate] handling {status} failed (will re-poll): {exc}")
    return None


def wait_finished_ui(
    page, cid: str, *, deadline_s: float, shot_prefix: str
) -> tuple[str, list[dict]]:
    """Wait for FINISHED/ERROR while acting as the user on every gate.
    Returns (status, [{kind, ts}, …]) — interventions go in the witness."""
    handled: list[dict] = []
    deadline = time.monotonic() + deadline_s
    st = exec_status(cid)
    while time.monotonic() < deadline:
        st = exec_status(cid)
        if st in {"FINISHED", "ERROR"}:
            return st, handled
        shot = SHOT_DIR / f"{shot_prefix}-gate-{len(handled)}.png" if len(handled) < 3 else None
        kind = answer_gates(page, cid, shot_path=shot)
        if kind:
            handled.append({"kind": kind, "ts": time.time()})
        time.sleep(10)
    return st, handled


def create_submit_upload_approve(page, *, shot_prefix: str) -> str:
    """Front door → submit the frozen prompt → upload the CSV during the plan
    gate → approve. Returns the conversation id."""
    page.goto("/", timeout=30_000)

    # Build mode (the front door defaults to search).
    page.get_by_role("radio", name="build").click(timeout=30_000)

    composer = page.get_by_placeholder(PLACEHOLDER)
    composer.fill(TASK_PROMPT, timeout=30_000)

    with page.expect_response(
        lambda r: r.url.rstrip("/").endswith("/conversations") and r.request.method == "POST",
        timeout=60_000,
    ) as resp_info:
        composer.press("Enter")
    cid = resp_info.value.json()["conversation_id"]

    # Upload while the plan gate holds (cid exists → the paperclip renders;
    # nothing executes until approval, so the agent sees the file first).
    file_input = page.locator('[data-testid="upload-composer"] input[type="file"]')
    file_input.wait_for(state="attached", timeout=120_000)
    file_input.set_input_files(str(CSV_PATH))

    # The bp-11 announce event is the upload's receipt — wait for it on the wire.
    deadline = time.monotonic() + 120
    announced = False
    while time.monotonic() < deadline:
        if any(
            "sensor_readings.csv" in json.dumps(e)
            for e in full_events(cid)
            if e.get("kind") == "message"
        ):
            announced = True
            break
        time.sleep(3)
    assert announced, "upload announce event for sensor_readings.csv never appeared"

    # Plan approval (27B plan generation is slow — budget 10 min).
    approve = page.get_by_role("button", name=re.compile(r"Approve.*build", re.I))
    approve.wait_for(state="visible", timeout=600_000)
    page.screenshot(path=str(SHOT_DIR / f"{shot_prefix}-plan-approval.png"))
    approve.click()
    return cid


def wait_ports_live(page, cid: str, *, deadline_s: float = 1200) -> None:
    """Block until BOTH app ports serve through the BP-10 proxy (recovery
    complete). The deliverable battery must run NOW — the sandbox reaps at
    FINISHED (task-as-resource), so live surfaces are only checkable while
    the run is alive."""
    deadline = time.monotonic() + deadline_s
    while time.monotonic() < deadline:
        _readings = api_get(f"/conversations/{cid}/port/3000/api/readings", timeout=20)
        ok_api = _readings.status_code == 200
        ok_ui = api_get(f"/conversations/{cid}/port/8000/", timeout=20).status_code == 200
        if ok_api and ok_ui:
            return
        if exec_status(cid) in {"FINISHED", "ERROR"}:
            raise AssertionError(
                "run ended before both ports served through the proxy post-recovery"
            )
        answer_gates(page, cid)
        time.sleep(5)
    raise AssertionError("ports never served through the proxy within the deadline")


def _text_has_number(text: str, val: str, tol: float = 0.051) -> bool:
    """Attempt-7 lesson: the app renders 32.75 as '32.8 °C' — display
    formatting is the APP's prerogative, so cell assertions match numerically
    (tol covers 1-decimal rounding) instead of demanding verbatim CSV strings."""
    target = float(val)
    return any(abs(float(m.group()) - target) <= tol for m in re.finditer(r"-?\d+(?:\.\d+)?", text))


def deliverable_battery(page, cid: str, *, shot_prefix: str) -> dict:
    """Order step A4, reused by Phase B: the harness acts as the user and
    checks every shipped surface. Every check is recorded pass/fail in the
    witness (the order's evidence package wants the TABLE, not an abort at the
    first finding) — a failed check is a system finding for DEFECTS.md, and
    the gate keeps measuring the remaining surfaces. Screenshots are taken
    unconditionally: a blank preview IS evidence."""
    known = csv_known_values()
    witness: dict = {"known": known, "checks": {}}

    def check(name: str, fn) -> bool:
        try:
            info = fn()
            witness["checks"][name] = {"pass": True} | ({"info": info} if info is not None else {})
            print(f"[battery] {name}: PASS")
            return True
        except Exception as exc:  # noqa: BLE001 — finding, not abort
            witness["checks"][name] = {
                "pass": False,
                "error": str(exc)[:300],
                "exec_status_at_failure": exec_status(cid),
            }
            print(f"[battery] {name}: FAIL — {str(exc)[:160]}")
            return False

    # Attempt-6 lesson: the pills + live iframe live INSIDE the PreviewPane,
    # which defaults to the srcdoc "rendered" view whenever a renderable file
    # exists. The user's path is: Preview tab → "Live server" toggle → pills.
    def live_view():
        page.get_by_role("tab", name="Preview").click(timeout=30_000)
        live_iframe = page.locator('iframe[title="Live preview"]')
        toggle = page.get_by_role("button", name=re.compile(r"Live server", re.I))
        deadline = time.monotonic() + 90
        while time.monotonic() < deadline and not live_iframe.count():
            try:
                if toggle.count():
                    toggle.click(timeout=5_000)
            except Exception:  # noqa: BLE001 — pane may re-render mid-click
                pass
            time.sleep(2)
        assert live_iframe.count(), "PreviewPane never reached the live-server view"

    check("live_view", live_view)

    # Port pills (BP-10): 8000 + 3000 both bound (live mode, >1 user port).
    def port_pills():
        pills = page.get_by_role("tablist", name="Bound ports")
        for port in ("8000", "3000"):
            pills.get_by_role("tab", name=re.compile(rf":{port}")).wait_for(
                state="visible", timeout=90_000
            )
        return {"ports": ["8000", "3000"]}

    check("port_pills", port_pills)

    # API through the BP-10 proxy: real JSON, all 200 real rows.
    def api_json():
        r = api_get(f"/conversations/{cid}/port/3000/api/readings", timeout=60)
        assert r.status_code == 200, f"/port/3000/api/readings → {r.status_code}"
        data = r.json()
        rows = data if isinstance(data, list) else data.get("readings") or data.get("data")
        assert isinstance(rows, list), f"unexpected JSON shape: {str(data)[:200]}"
        assert len(rows) == known["count"], f"API rows {len(rows)} != csv {known['count']}"
        return {"rows": len(rows)}

    check("api_json", api_json)

    # Preview table shows the REAL data (≥3 known cells + count/min/max),
    # matched numerically. Expected to FAIL while DEFECT-3 stands: the
    # path-prefixed proxy 404s the app's absolute-path assets + /api fetch.
    def preview_cells():
        frame = page.frame_locator('iframe[title="Live preview"]')
        wanted = list(known["cells"]) + [known["min"], known["max"]]
        deadline = time.monotonic() + 60
        text = ""
        while time.monotonic() < deadline:
            try:
                text = frame.locator("body").inner_text(timeout=10_000)
            except Exception:  # noqa: BLE001 — iframe mid-load
                text = ""
            if str(known["count"]) in text and all(_text_has_number(text, v) for v in wanted):
                return {"matched": wanted, "count": known["count"]}
            time.sleep(3)
        raise AssertionError(f"preview never showed the data (last text: {text[:200]!r})")

    check("preview_cells", preview_cells)
    page.screenshot(path=str(SHOT_DIR / f"{shot_prefix}-final-preview-table.png"))

    # Terminal tab (BP-14): live session output present.
    def terminal():
        page.get_by_role("tab", name="Terminal").click(timeout=30_000)
        pane = page.get_by_role("tabpanel").locator("pre")
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline:
            content = pane.inner_text(timeout=10_000) if pane.count() else ""
            if content.strip():
                return {"chars": len(content)}
            time.sleep(2)
        raise AssertionError("Terminal tab shows no live session output")

    check("terminal", terminal)

    # Feed thumbnail (BP-15): at least one real screenshot in the feed.
    def feed_thumbnail():
        thumbs = page.locator('img[src*="/workspace/.pmx/screenshots/"]')
        assert thumbs.count() >= 1, "no screenshot thumbnail in the feed"
        return {"thumbnails": thumbs.count()}

    check("feed_thumbnail", feed_thumbnail)

    # Port-3000 JSON, screenshotted as the user would see it.
    def json_shot():
        json_page = page.context.new_page()
        try:
            json_page.goto(f"{API}/conversations/{cid}/port/3000/api/readings", timeout=60_000)
            json_page.screenshot(path=str(SHOT_DIR / f"{shot_prefix}-port-3000-json.png"))
        finally:
            json_page.close()

    check("json_screenshot", json_shot)

    # Late pass: the agent's verification screenshot (feed thumbnail) and a
    # late-populating preview can land right up until FINISHED reaps the
    # sandbox — keep re-trying failed checks while the run is alive.
    retryable = {"preview_cells": preview_cells, "feed_thumbnail": feed_thumbnail}
    late_deadline = time.monotonic() + 600
    while time.monotonic() < late_deadline:
        pending = [n for n, f in retryable.items() if not witness["checks"][n]["pass"]]
        if not pending or exec_status(cid) in {"FINISHED", "ERROR"}:
            break
        answer_gates(page, cid)
        for name in pending:
            if check(name, retryable[name]) and name == "preview_cells":
                page.screenshot(path=str(SHOT_DIR / f"{shot_prefix}-final-preview-table.png"))
        time.sleep(10)

    witness["deliverables_pass"] = all(c["pass"] for c in witness["checks"].values())
    return witness
