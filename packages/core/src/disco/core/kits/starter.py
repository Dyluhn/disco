"""Starter kits (P7) — host-owned scaffolds the contracts/packs name.

A contract declares ``artifact.starter_kit`` and the prompt packs tell the model to
"scaffold from the <name> starter" — but the names were bare strings with nothing behind
them (a false affordance; the model hand-drew frames). A StarterKit resolves a name to a
host-owned file scaffold (path → text), parameterized by the build's title, so the model
EDITS a real frame instead of hand-drawing common chrome.

Pure: AppSpec/render reuse from disco.core.appkit; no runtime/tool imports. The lead_form
starter IS the AppKit default (single source — app_create scaffolds from it).
"""

from __future__ import annotations

import html
from collections.abc import Callable
from pathlib import PurePosixPath

from ..appkit import AppSection, AppSpec, render_html
from .starter_assets import device_frames, game_loop_vanilla, pwa_shell, ui_kit_dense


def _safe_rel(path: str) -> str:
    """A starter writes only NORMALIZED, workspace-relative paths — never absolute and
    never escaping the workspace (no ``..``). Rejects traversal at scaffold time."""
    p = PurePosixPath(path)
    if p.is_absolute() or ".." in p.parts or not p.parts:
        raise ValueError(f"unsafe starter path: {path!r}")
    return str(p)


def lead_form_appspec(title: str) -> AppSpec:
    """The default AppKit lead-gen app (a hero + a lead form). The SINGLE source for both
    the lead_form starter AND app_create's default — so they can't drift."""
    return AppSpec(
        title=title,
        sections=(
            AppSection(id="hero", kind="hero", fields={"headline": title, "subhead": "", "cta_text": "Get started"}),
            AppSection(id="lead", kind="lead_form", fields={"title": "Contact us", "submit_text": "Send"}),
        ),
    )


def _appspec_files(spec: AppSpec) -> dict[str, str]:
    # byte-identical to AppSpecStore.write (.disco/appspec.json + rendered index.html).
    return {
        ".disco/appspec.json": spec.model_dump_json(indent=2) + "\n",
        "index.html": render_html(spec),
    }


_APP_SHELL_HTML = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{title}</title>
<style>
:root{{--fg:#1a1a1a;--bg:#fcfcfa;--accent:#4077a3}}
*{{box-sizing:border-box}}
body{{margin:0;font-family:system-ui,-apple-system,Segoe UI,Roboto,sans-serif;color:var(--fg);background:var(--bg)}}
main{{max-width:720px;margin:0 auto;padding:4rem 1.5rem}}
h1{{font-size:2.25rem;margin:0 0 .5rem}}
p{{font-size:1.1rem;line-height:1.6;color:#444}}
</style>
</head>
<body>
<main>
<h1>{title}</h1>
<p>Start building. Replace this shell with your content.</p>
</main>
</body>
</html>
"""


def _app_shell(title: str) -> dict[str, str]:
    return {"index.html": _APP_SHELL_HTML.format(title=html.escape(title, quote=True))}


def _lead_form(title: str) -> dict[str, str]:
    return _appspec_files(lead_form_appspec(title))


class StarterKit:
    """A named host-owned scaffold: ``scaffold(title)`` → {rel_path: text} (path-safe)."""

    def __init__(self, kit_id: str, builder: Callable[[str], dict[str, str]]) -> None:
        self.id = kit_id
        self._builder = builder

    def scaffold(self, title: str) -> dict[str, str]:
        return {_safe_rel(k): v for k, v in self._builder(title).items()}


class StarterKitRegistry:
    """Resolves a contract's ``starter_kit`` name to a host-owned StarterKit."""

    _BUILTINS: dict[str, Callable[[str], dict[str, str]]] = {
        "app_shell": _app_shell,  # static.site / interactive.prototype
        "lead_form": _lead_form,  # appkit.leadgen — the AppKit default
        "game_loop_vanilla": game_loop_vanilla,  # zero-dep Canvas2D game loop
        "pwa_shell": pwa_shell,  # installable mobile-first PWA shell
        "device_frames": device_frames,  # clean-room phone/window preview frames
        "ui_kit_dense": ui_kit_dense,  # dense dashboard shell primitives
    }

    def get(self, kit_id: str) -> StarterKit | None:
        builder = self._BUILTINS.get(kit_id)
        return StarterKit(kit_id, builder) if builder is not None else None

    def ids(self) -> frozenset[str]:
        return frozenset(self._BUILTINS)

    @classmethod
    def default(cls) -> StarterKitRegistry:
        return cls()
