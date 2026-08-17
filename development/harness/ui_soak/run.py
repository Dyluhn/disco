#!/usr/bin/env python3
"""Drive the deployed Disco UI through repeatable Research and Build soaks."""

from __future__ import annotations

import argparse
import csv
import importlib
import importlib.util
import os
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit

# Direct execution would let the required local selectors.py shadow Python's
# stdlib module while asyncio imports subprocess. Remove that one path entry.
SCRIPT_DIR = Path(__file__).resolve().parent
if __package__ in {None, ""}:
    sys.path = [entry for entry in sys.path if Path(entry or ".").resolve() != SCRIPT_DIR]

# These imports intentionally happen only after the direct-script path is sanitized:
# development/harness/ui_soak/selectors.py would otherwise shadow the stdlib selectors module.
asyncio = importlib.import_module("asyncio")
playwright_api = importlib.import_module("playwright.async_api")
BrowserContext = playwright_api.BrowserContext
Page = playwright_api.Page
async_playwright = playwright_api.async_playwright
PlaywrightTimeoutError = playwright_api.TimeoutError

if __package__:
    from . import selectors as S
else:  # The documented absolute-path invocation reaches this branch.
    spec = importlib.util.spec_from_file_location(
        "_disco_ui_soak_selectors", SCRIPT_DIR / "selectors.py"
    )
    if spec is None or spec.loader is None:  # pragma: no cover - filesystem corruption
        raise RuntimeError("could not load the UI soak selector map")
    S = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(S)


RESEARCH_PROMPT = "What year did the Apollo 11 mission land?"
BUILD_PROMPT = "Create a single index.html with the heading 'UI Soak OK' and finish."
EXPECTED_PREVIEW_HEADING = "UI Soak OK"
LOAD_TIMEOUT_MS = 30_000
APP_READY_TIMEOUT_S = 45
RESEARCH_TIMEOUT_S = 240
SMOKE_RESEARCH_TIMEOUT_S = 90
BUILD_TIMEOUT_S = 900
PREVIEW_TIMEOUT_S = 30
ACTION_TIMEOUT_MS = 15_000


class PhaseFailure(RuntimeError):
    pass


def compact(value: object, limit: int = 900) -> str:
    text = " ".join(str(value).replace("\t", " ").splitlines()).strip()
    return text if len(text) <= limit else f"{text[: limit - 1]}…"


class Ledger:
    def __init__(self, path: Path) -> None:
        self._file = path.open("w", encoding="utf-8", newline="")
        self._writer = csv.writer(self._file, delimiter="\t", lineterminator="\n")
        self._writer.writerow(("iteration", "phase", "verdict", "detail"))
        self._file.flush()

    def row(self, iteration: int, phase: str, verdict: str, detail: object) -> None:
        self._writer.writerow((iteration, phase, verdict, compact(detail)))
        self._file.flush()

    def close(self) -> None:
        self._file.close()


@dataclass
class BrowserSignals:
    console_errors: list[str] = field(default_factory=list)
    page_errors: list[str] = field(default_factory=list)
    server_errors: list[str] = field(default_factory=list)

    def attach(self, page: Page) -> None:
        def on_console(message: object) -> None:
            if getattr(message, "type", None) == "error":
                self.console_errors.append(compact(getattr(message, "text", message), 300))

        def on_page_error(error: object) -> None:
            self.page_errors.append(compact(error, 300))

        def on_response(response: object) -> None:
            status = int(getattr(response, "status", 0))
            if status >= 500:
                request = getattr(response, "request", None)
                method = getattr(request, "method", "?")
                url = getattr(response, "url", "?")
                parts = urlsplit(url)
                safe_url = parts._replace(query="", fragment="").geturl()
                self.server_errors.append(compact(f"{status} {method} {safe_url}", 400))

        page.on("console", on_console)
        page.on("pageerror", on_page_error)
        page.on("response", on_response)

    def result(self) -> tuple[bool, str]:
        counts = (
            f"console.error={len(self.console_errors)}, "
            f"pageerror={len(self.page_errors)}, 5xx={len(self.server_errors)}"
        )
        blocking = self.page_errors + self.server_errors
        samples = blocking + self.console_errors
        detail = counts if not samples else f"{counts}; samples: {' | '.join(samples[:4])}"
        return not blocking, detail


async def is_visible(page: Page, selector: str) -> bool:
    if page.is_closed():
        raise PhaseFailure("browser page closed unexpectedly")
    try:
        return await page.locator(selector).first.is_visible()
    except Exception:
        return False


async def wait_for_any(page: Page, choices: tuple[tuple[str, str], ...], timeout_s: float) -> str:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        for name, selector in choices:
            if await is_visible(page, selector):
                return name
        await asyncio.sleep(min(0.25, max(0.0, deadline - time.monotonic())))
    names = ", ".join(name for name, _ in choices)
    raise PhaseFailure(f"timed out after {timeout_s:.0f}s waiting for one of: {names}")


async def load_and_pair(page: Page, base: str, token: str | None) -> str:
    await page.goto(base, wait_until="domcontentloaded", timeout=LOAD_TIMEOUT_MS)
    choices = (("app ready", S.APP_READY), ("pairing prompt", S.PAIRING_TOKEN))
    state = await wait_for_any(page, choices, APP_READY_TIMEOUT_S)
    if state == "app ready":
        return "app loaded; pairing prompt absent (existing or auto-paired session)"
    if not token:
        raise PhaseFailure(
            "pairing prompt appeared but no --pairing-token or DISCO_PAIRING_TOKEN was supplied"
        )

    await page.locator(S.PAIRING_TOKEN).fill(token, timeout=ACTION_TIMEOUT_MS)
    await page.locator(S.PAIRING_SUBMIT).click(timeout=ACTION_TIMEOUT_MS)
    deadline = time.monotonic() + APP_READY_TIMEOUT_S
    while time.monotonic() < deadline:
        if await is_visible(page, S.APP_READY):
            return "app loaded; pairing token accepted through the UI"
        if await is_visible(page, S.PAIRING_ERROR):
            detail = await page.locator(S.PAIRING_ERROR).first.inner_text()
            raise PhaseFailure(f"pairing rejected: {compact(detail, 300)}")
        await asyncio.sleep(0.25)
    raise PhaseFailure(f"pairing did not reach the app within {APP_READY_TIMEOUT_S}s")


async def start_fresh(page: Page, mode_selector: str, target_selector: str) -> None:
    await page.locator(S.NEW_CONVERSATION).first.click(timeout=ACTION_TIMEOUT_MS)
    await page.locator(mode_selector).first.click(timeout=ACTION_TIMEOUT_MS)
    await page.locator(f'{mode_selector}[data-active="true"]').first.wait_for(
        state="visible", timeout=ACTION_TIMEOUT_MS
    )
    await page.locator(target_selector).first.wait_for(state="visible", timeout=ACTION_TIMEOUT_MS)


async def run_research(page: Page, timeout_s: int, shot: Path) -> str:
    await start_fresh(page, S.MODE_SEARCH, S.RESEARCH_COMPOSER)
    await page.locator(S.RESEARCH_COMPOSER).fill(RESEARCH_PROMPT, timeout=ACTION_TIMEOUT_MS)
    await page.locator(S.RESEARCH_SEND).click(timeout=ACTION_TIMEOUT_MS)

    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        answer = page.locator(S.RESEARCH_ANSWER).first
        if await is_visible(page, S.RESEARCH_ANSWER):
            rendered = (await answer.inner_text(timeout=ACTION_TIMEOUT_MS)).strip()
            if rendered:
                await page.screenshot(path=str(shot), full_page=True, animations="disabled")
                return f"authoritative answer rendered ({len(rendered)} characters)"
        if await is_visible(page, S.RESEARCH_ERROR):
            detail = await page.locator(S.RESEARCH_ERROR).first.inner_text()
            raise PhaseFailure(f"research UI reported an error: {compact(detail, 350)}")
        await asyncio.sleep(0.5)
    raise PhaseFailure(f"research answer did not complete within {timeout_s}s")


async def wait_for_build(page: Page, timeout_s: int) -> tuple[int, str, str]:
    deadline = time.monotonic() + timeout_s
    approvals = 0
    approval_armed = True
    last_status = "not mounted"
    last_seq = "0"

    while time.monotonic() < deadline:
        approve_visible = await is_visible(page, S.APPROVE_PLAN)
        if approve_visible and approval_armed:
            try:
                await page.locator(S.APPROVE_PLAN).first.click(timeout=5_000)
                approvals += 1
                approval_armed = False
            except PlaywrightTimeoutError:
                pass  # The gate changed between the visibility check and click.
        elif not approve_visible:
            approval_armed = True

        status = page.locator(S.BUILD_STATUS).first
        if await is_visible(page, S.BUILD_STATUS):
            last_status = (await status.get_attribute("data-status", timeout=2_000)) or "unknown"
            last_seq = (await status.get_attribute("data-seq", timeout=2_000)) or "0"
            if last_status == "FINISHED":
                return approvals, last_status, last_seq
            if last_status in {"ERROR", "STUCK"}:
                raise PhaseFailure(
                    f"build reached unsuccessful terminal status {last_status} (seq {last_seq})"
                )
        await asyncio.sleep(0.5)

    raise PhaseFailure(
        f"build did not finish within {timeout_s}s; last status={last_status}, "
        f"seq={last_seq}, plan approvals={approvals}"
    )


async def capture_preview(page: Page, shot: Path) -> str:
    tab = page.get_by_role(S.PREVIEW_TAB_ROLE, name=S.PREVIEW_TAB_NAME, exact=True)
    await tab.first.click(timeout=ACTION_TIMEOUT_MS)
    await page.locator(S.PREVIEW_ACTIVE).first.wait_for(state="visible", timeout=ACTION_TIMEOUT_MS)

    iframe_title: str | None = None
    deadline = time.monotonic() + PREVIEW_TIMEOUT_S
    while time.monotonic() < deadline:
        frames = page.locator(S.PREVIEW_IFRAMES)
        for index in range(await frames.count()):
            candidate = frames.nth(index)
            if await candidate.is_visible():
                try:
                    heading = candidate.content_frame.get_by_role(
                        "heading", name=EXPECTED_PREVIEW_HEADING, exact=True
                    ).first
                    if await heading.is_visible():
                        iframe_title = await candidate.get_attribute("title")
                        break
                except Exception:
                    pass  # The iframe may still be navigating; retry to the deadline.
        if iframe_title:
            break
        await asyncio.sleep(0.5)
    if not iframe_title:
        raise PhaseFailure(
            f"preview did not render heading {EXPECTED_PREVIEW_HEADING!r} "
            f"within {PREVIEW_TIMEOUT_S}s"
        )

    panel = page.locator(S.PREVIEW_PANEL).first
    target = (
        panel if await is_visible(page, S.PREVIEW_PANEL) else page.locator(S.PREVIEW_ACTIVE).first
    )
    await target.screenshot(path=str(shot), animations="disabled")
    return f"preview captured ({iframe_title}; expected heading rendered)"


async def run_build(page: Page, shot: Path) -> str:
    await start_fresh(page, S.MODE_BUILD, S.COMPOSER)
    await page.locator(S.COMPOSER).first.fill(BUILD_PROMPT, timeout=ACTION_TIMEOUT_MS)
    await page.locator(S.PRIMARY_SEND).first.click(timeout=ACTION_TIMEOUT_MS)
    approvals, status, seq = await wait_for_build(page, BUILD_TIMEOUT_S)
    preview = await capture_preview(page, shot)
    return f"terminal={status}, seq={seq}, plan approvals={approvals}; {preview}"


async def failure_screenshot(page: Page, path: Path) -> None:
    if page.is_closed():
        return
    try:
        await page.screenshot(path=str(path), full_page=True, animations="disabled")
    except Exception:
        pass


async def one_iteration(
    context: BrowserContext,
    iteration: int,
    base: str,
    token: str | None,
    out: Path,
    smoke: bool,
    ledger: Ledger,
) -> bool:
    page = await context.new_page()
    page.set_default_timeout(ACTION_TIMEOUT_MS)
    page.set_default_navigation_timeout(LOAD_TIMEOUT_MS)
    signals = BrowserSignals()
    signals.attach(page)
    phase_results: list[bool] = []

    try:
        detail = await load_and_pair(page, base, token)
        ledger.row(iteration, "app", "PASS", detail)
        app_ok = True
    except Exception as exc:
        app_ok = False
        phase_results.append(False)
        ledger.row(iteration, "app", "FAIL", f"{type(exc).__name__}: {exc}")

    research_shot = out / f"iteration-{iteration:03d}-research.png"
    if app_ok:
        try:
            timeout = SMOKE_RESEARCH_TIMEOUT_S if smoke else RESEARCH_TIMEOUT_S
            detail = await run_research(page, timeout, research_shot)
            ledger.row(iteration, "research", "PASS", detail)
            phase_results.append(True)
        except Exception as exc:
            await failure_screenshot(page, research_shot)
            ledger.row(iteration, "research", "FAIL", f"{type(exc).__name__}: {exc}")
            phase_results.append(False)
    else:
        await failure_screenshot(page, research_shot)
        ledger.row(iteration, "research", "FAIL", "not run because app load/pairing failed")

    if not smoke:
        build_shot = out / f"iteration-{iteration:03d}-build-preview.png"
        if app_ok and not page.is_closed():
            try:
                detail = await run_build(page, build_shot)
                ledger.row(iteration, "build", "PASS", detail)
                phase_results.append(True)
            except Exception as exc:
                await failure_screenshot(page, build_shot)
                ledger.row(iteration, "build", "FAIL", f"{type(exc).__name__}: {exc}")
                phase_results.append(False)
        else:
            await failure_screenshot(page, build_shot)
            ledger.row(iteration, "build", "FAIL", "not run because the app page was unavailable")
            phase_results.append(False)

    if not page.is_closed():
        await page.wait_for_timeout(250)
        await page.close()
    signals_ok, signals_detail = signals.result()
    ledger.row(iteration, "browser-signals", "PASS" if signals_ok else "FAIL", signals_detail)

    passed = app_ok and all(phase_results) and signals_ok
    ledger.row(
        iteration,
        "iteration",
        "PASS" if passed else "FAIL",
        "all criteria met" if passed else "one or more criteria failed",
    )
    return passed


def positive_int(raw: str) -> int:
    value = int(raw)
    if value < 1:
        raise argparse.ArgumentTypeError("must be at least 1")
    return value


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base", required=True, help="frontend URL, e.g. http://frontend")
    parser.add_argument("--iterations", type=positive_int, default=3)
    parser.add_argument("--out", required=True, type=Path, help="ledger and screenshot directory")
    parser.add_argument("--pairing-token", default=os.environ.get("DISCO_PAIRING_TOKEN"))
    parser.add_argument("--headed", action="store_true", help="show Chromium (headless by default)")
    parser.add_argument(
        "--smoke",
        action="store_true",
        help="one Research-only iteration with a 90-second answer bound",
    )
    args = parser.parse_args()
    parsed = urlsplit(args.base)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        parser.error("--base must be an absolute http(s) frontend URL")
    if parsed.query or parsed.fragment:
        parser.error("--base must not contain a query string or fragment")
    args.base = args.base.rstrip("/")
    return args


async def async_main(args: argparse.Namespace, ledger: Ledger) -> int:
    iterations = 1 if args.smoke else args.iterations
    all_passed = True
    try:
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch(headless=not args.headed)
            context = await browser.new_context(viewport={"width": 1440, "height": 1000})
            try:
                for iteration in range(1, iterations + 1):
                    try:
                        passed = await one_iteration(
                            context,
                            iteration,
                            args.base,
                            args.pairing_token,
                            args.out,
                            args.smoke,
                            ledger,
                        )
                    except Exception as exc:
                        passed = False
                        ledger.row(
                            iteration,
                            "iteration",
                            "FAIL",
                            f"unhandled: {type(exc).__name__}: {exc}",
                        )
                    all_passed = all_passed and passed
                    print(f"[{iteration}/{iterations}] {'PASS' if passed else 'FAIL'}")
            finally:
                await context.close()
                await browser.close()
    except Exception as exc:
        ledger.row(0, "harness", "FAIL", f"{type(exc).__name__}: {exc}")
        print(f"harness startup/runtime failure: {exc}", file=sys.stderr)
        return 1

    print(f"ledger: {args.out / 'ledger.tsv'}")
    print(
        "selector sources: PairingGate, NavRail/ModeSlider, QueryInput/ResearchSurface, "
        "PlanPanel/AgentStatusBar, ExecutionCanvas/PreviewPane"
    )
    return 0 if all_passed else 1


def main() -> int:
    args = parse_args()
    args.out.mkdir(parents=True, exist_ok=True)
    ledger = Ledger(args.out / "ledger.tsv")
    try:
        return asyncio.run(async_main(args, ledger))
    finally:
        ledger.close()


if __name__ == "__main__":
    raise SystemExit(main())
