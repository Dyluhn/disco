"""P2 tests: daemon headed/headless selection logic and live_view ensure_live."""
import sys
from unittest.mock import MagicMock, patch


def test_browser_state_headless_by_default():
    """BrowserState.start() with no display arg → headless=True (the default)."""
    mock_browser = MagicMock()
    mock_context = MagicMock()
    mock_page = MagicMock()
    mock_pw = MagicMock()
    mock_pw.chromium.launch.return_value = mock_browser
    mock_browser.new_context.return_value = mock_context
    mock_context.new_page.return_value = mock_page
    mock_sp_instance = MagicMock()
    mock_sp_instance.start.return_value = mock_pw

    import disco.tools.builtin._browser_daemon as daemon_mod

    with patch.object(daemon_mod, "sync_playwright", return_value=mock_sp_instance):
        state = daemon_mod.BrowserState()
        state.start()
        # Must be headless=True
        mock_pw.chromium.launch.assert_called_once()
        call_kwargs = mock_pw.chromium.launch.call_args
        assert call_kwargs.kwargs.get("headless", True) is True


def test_browser_state_headed_when_display_given():
    """BrowserState.start(display=':1') → headless=False."""
    mock_browser = MagicMock()
    mock_context = MagicMock()
    mock_page = MagicMock()
    mock_pw = MagicMock()
    mock_pw.chromium.launch.return_value = mock_browser
    mock_browser.new_context.return_value = mock_context
    mock_context.new_page.return_value = mock_page
    mock_sp_instance = MagicMock()
    mock_sp_instance.start.return_value = mock_pw

    import disco.tools.builtin._browser_daemon as daemon_mod

    with patch.object(daemon_mod, "sync_playwright", return_value=mock_sp_instance):
        state = daemon_mod.BrowserState()
        state.start(display=":1")
        call_kwargs = mock_pw.chromium.launch.call_args
        assert call_kwargs.kwargs.get("headless") is False


def test_live_view_ensure_live_calls_all_three():
    """ensure_live() calls ensure_xvfb, ensure_x11vnc, and ensure_websockify."""
    import disco.tools.builtin.live_view as lv_mod

    with patch.object(lv_mod, "ensure_xvfb", return_value=True) as mock_xvfb, \
         patch.object(lv_mod, "ensure_x11vnc", return_value=True) as mock_vnc, \
         patch.object(lv_mod, "ensure_websockify", return_value=True) as mock_ws:
        result = lv_mod.ensure_live()
        assert result is True
        mock_xvfb.assert_called_once()
        mock_vnc.assert_called_once()
        mock_ws.assert_called_once()
