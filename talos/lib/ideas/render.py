"""A UI screen (screens.py) -> PNG: the page (page.py) with the current stylesheets (styles.py), screenshotted by a
headless browser (browser.py)."""

import tempfile
import threading
from pathlib import Path

from . import browser, page
from .browser import RenderError  # noqa: F401  (callers catch it from here)
from .styles import Stylesheets


class Renderer:
    """Renders an agent's screens. The browser is looked for when the first screen is asked for, so a machine
    without one only matters to ideas that use screens. One render at a time."""

    def __init__(self, browser_bin=None, engineer="", prometheus_url=None, styles=None):
        self.browser_bin, self.engineer = browser_bin, engineer
        self.styles = styles or Stylesheets(prometheus_url)
        self._browser = None
        self._lock = threading.Lock()

    def render(self, screen):
        with self._lock:
            if self._browser is None:
                self._browser = browser.find(self.browser_bin)
            # ignore_cleanup_errors: on Windows a browser helper may still hold the profile for a moment
            with tempfile.TemporaryDirectory(prefix="talos-screen-", ignore_cleanup_errors=True) as tmp:
                tmp = Path(tmp)
                (tmp / "screen.html").write_text(page.build(screen, self.styles.current(), self.engineer),
                                                 encoding="utf-8")
                try:
                    return browser.screenshot(self._browser, tmp / "screen.html", tmp / "screen.png",
                                              screen["width"], screen["height"], tmp)
                except RenderError as e:
                    raise RenderError(f"'{screen['key']}': {e}")
