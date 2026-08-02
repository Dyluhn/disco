"""Browser-engine selection by MEASURED renderer readiness (HARN-1b, B2).

The daemon used to launch Chromium by name. Whether an engine can actually lay
out ordinary system-font text is a property of the ENVIRONMENT the daemon runs
in, not of the engine's name. Measured on this workstation with one variable
changed at a time: Chromium's ``text_renderer_ready`` calibration returns False
under the developer's own ``HOME`` (a stale ``~/.cache/fontconfig``) and True
under the sandbox's jailed ``HOME``, while Firefox returns True under both. A
daemon that picks an engine by name is therefore right or wrong by luck, and the
luck runs the other way depending on who is running it.

So the engine is chosen by running the daemon's OWN calibration — the same
``text_renderer_ready`` that produced the fatal error before — against each
installed engine in turn, keeping the first that passes. Nothing here decides
what "ready" means or performs the calibration; this module only decides which
engines get asked, and with which options. ``ENGINE_ORDER`` is an ATTEMPT order,
not a preference: an engine wins by measuring ready, never by its position.

Executables are threaded in explicitly because the sandbox sets ``HOME`` to its
jailed workspace, so Playwright's default browsers path
(``$HOME/.cache/ms-playwright``) resolves to an empty directory inside the
sandbox and no engine can be launched by name alone. Shipping only Chromium's
path is what made a Chromium failure terminal rather than recoverable.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any

# Attempt order only — see the module docstring. Selection is by measurement.
ENGINE_ORDER: tuple[str, ...] = ("chromium", "firefox", "webkit")

# The isolation boundary is the sandbox, hence --no-sandbox is acceptable HERE
# only. W-46: --disable-blink-features=AutomationControlled removes the Blink
# flag that otherwise sets navigator.webdriver=true and trips WAF bot
# heuristics. Both are Chromium command-line flags — Firefox and WebKit reject
# unknown arguments, so they are engine-scoped rather than passed to every
# launch.
_CHROMIUM_ARGS: tuple[str, ...] = (
    "--no-sandbox",
    "--disable-blink-features=AutomationControlled",
)

# Per-engine executables, JSON-encoded ``{engine: path}``. The single-engine
# ``DISCO_BROWSER_EXECUTABLE`` predates it and still names Chromium, so it is
# honored as a fallback for anyone launching the daemon by hand.
EXECUTABLES_ENV = "DISCO_BROWSER_EXECUTABLES"
CHROMIUM_EXECUTABLE_ENV = "DISCO_BROWSER_EXECUTABLE"

_PROXY_BYPASS = "localhost,127.0.0.1,[::1]"


def executables_from_env(environ: Mapping[str, str]) -> dict[str, str]:
    """Per-engine executable overrides the host shipped into the sandbox.

    Degrades to an empty mapping on any unusable payload rather than raising:
    an unreadable override means "launch by name", which the engine loop then
    measures like any other candidate instead of treating as fatal.
    """

    resolved: dict[str, str] = {}
    raw = environ.get(EXECUTABLES_ENV) or ""
    if raw:
        try:
            payload = json.loads(raw)
        except ValueError:
            payload = None
        if isinstance(payload, dict):
            resolved = {
                str(name): path
                for name, path in payload.items()
                if isinstance(path, str) and path
            }
    legacy = environ.get(CHROMIUM_EXECUTABLE_ENV)
    if legacy and "chromium" not in resolved:
        resolved["chromium"] = legacy
    return resolved


def proxy_settings(environ: Mapping[str, str]) -> dict[str, str] | None:
    """S-W5: agent/browser sandboxes reach the public web through the
    private/non-global-denying sidecar, not a raw bridge. Playwright does not
    reliably inherit HTTP_PROXY into the engine, so it is wired explicitly."""

    proxy_url = environ.get("HTTPS_PROXY") or environ.get("https_proxy")
    if not proxy_url:
        return None
    return {"server": proxy_url, "bypass": _PROXY_BYPASS}


def launch_arguments(engine: str, display: str | None) -> list[str]:
    """Command-line arguments for one engine.

    Only Chromium takes them. Firefox and WebKit read the X display from the
    ``DISPLAY`` environment variable the caller already sets, and abort on
    unrecognised command-line flags.
    """

    if engine != "chromium":
        return []
    args = list(_CHROMIUM_ARGS)
    if display:
        args.append(f"--display={display}")
    return args


def launch_options(
    engine: str,
    *,
    display: str | None,
    executables: Mapping[str, str],
    proxy: Mapping[str, str] | None,
) -> dict[str, Any]:
    """Keyword arguments for ``playwright.<engine>.launch``."""

    options: dict[str, Any] = {
        "headless": display is None,
        "args": launch_arguments(engine, display),
    }
    executable = executables.get(engine)
    if executable:
        options["executable_path"] = executable
    if proxy is not None:
        options["proxy"] = dict(proxy)
    return options


def installed_engines(playwright: Any) -> list[tuple[str, Any]]:
    """The engines this Playwright build actually exposes, in attempt order.

    ``getattr`` with a default is deliberate and is not a typing shim: the
    daemon's unit tests drive ``start()`` with fake Playwright objects that
    expose only the engines the test cares about, and a real build can be
    packaged without one.
    """

    found: list[tuple[str, Any]] = []
    for name in ENGINE_ORDER:
        engine = getattr(playwright, name, None)
        if engine is not None:
            found.append((name, engine))
    return found


def renderer_unavailable_message(attempts: list[dict[str, Any]]) -> str:
    """The terminal diagnostic when no installed engine calibrated ready.

    Keeps the historical wording ("renderer unavailable", "system-font text")
    that callers and tests key on, and adds what was actually tried — the old
    message named Chromium unconditionally even though the engine was never in
    question, which is how the real cause stayed hidden.
    """

    if not attempts:
        tried = "no browser engine is installed"
    else:
        tried = "tried " + ", ".join(
            f"{attempt.get('engine')}: {attempt.get('outcome')}" for attempt in attempts
        )
    return (
        "browser renderer unavailable: no installed engine could lay out ordinary "
        f"system-font text ({tried}); painted browser evidence is unavailable"
    )
