"""First-party starter file templates for ``disco.core.kits.starter``.

The templates here are deliberately plain text and dependency-free. Builders replace
only title placeholders, then the registry applies path safety before a tool writes
anything to a workspace.

The per-kit file-content tables live under ``starter_assets_parts`` (one module per
kit, a pure data/content partition) to keep this facade under the module
logical-line budget; every name below is re-imported unchanged.
"""

from __future__ import annotations

import html
import json

from .starter_assets_parts._device_frames import _DEVICE_FRAMES_FILES
from .starter_assets_parts._game_loop_vanilla import _GAME_LOOP_FILES
from .starter_assets_parts._pwa_shell import _PWA_SHELL_FILES
from .starter_assets_parts._ui_kit_dense import _UI_KIT_DENSE_FILES


def _render(files: dict[str, str], title: str) -> dict[str, str]:
    short = title.strip()[:12] or "App"
    initial = (title.strip()[:1] or "A").upper()
    replacements = {
        "__TITLE_HTML__": html.escape(title, quote=True),
        "__TITLE_JSON__": json.dumps(title),
        "__TITLE_TEXT__": title,
        "__SHORT_TITLE_JSON__": json.dumps(short),
        "__ICON_INITIAL_HTML__": html.escape(initial, quote=True),
    }
    rendered: dict[str, str] = {}
    for path, text in files.items():
        for marker, value in replacements.items():
            text = text.replace(marker, value)
        rendered[path] = text
    return rendered


def game_loop_vanilla(title: str) -> dict[str, str]:
    return _render(_GAME_LOOP_FILES, title)


def pwa_shell(title: str) -> dict[str, str]:
    return _render(_PWA_SHELL_FILES, title)


def device_frames(title: str) -> dict[str, str]:
    return _render(_DEVICE_FRAMES_FILES, title)


def ui_kit_dense(title: str) -> dict[str, str]:
    return _render(_UI_KIT_DENSE_FILES, title)
