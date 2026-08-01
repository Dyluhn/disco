"""Renderer-readiness probe, split out of ``_browser_daemon`` to keep the
module under its size budget. Re-exported via a redundant alias so
``daemon_mod.BrowserRendererUnavailable`` / ``daemon_mod._text_renderer_ready``
(imported directly by tests) keep resolving exactly as before.
"""

from __future__ import annotations


class BrowserRendererUnavailable(RuntimeError):
    """Chromium launched, but cannot produce trustworthy painted text evidence."""


def text_renderer_ready(page) -> bool:
    """Calibrate ordinary system-font layout on a disposable browser page."""
    try:
        page.set_content(
            '<!doctype html><span id="disco-render-probe" '
            'style="font:16px sans-serif">Disco renderer probe</span>'
        )
        result = page.evaluate("""
            () => {
                const element = document.getElementById('disco-render-probe');
                const node = element && element.firstChild;
                if (!element || !node || !(element.innerText || '').trim()) return false;
                const range = document.createRange();
                range.selectNodeContents(node);
                return Array.from(range.getClientRects()).some(
                    rect => rect.width > 0 && rect.height > 0
                );
            }
        """)
    except Exception:
        return False
    return result is True
