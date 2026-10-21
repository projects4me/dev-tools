"""Which stylesheets a rendered UI screen uses.

Prometheus' real, built CSS when a Prometheus is reachable (talos config `prometheusUrl`, else `appUrl` from
`p4m configure`): the stylesheets its index page links, so every component looks as in the app. Otherwise the
checkout's Bootstrap 3, AdminLTE and skin-black-light plus the Projects4Me overlay in screen_kit/. Always Font
Awesome from screen_kit/ and Source Sans Pro (as Prometheus loads it).

The configured Prometheus is trusted: its index decides which stylesheets load. Only http(s) links of the same
origin are used.
"""

import json
import re
import threading
import time
import urllib.parse
import urllib.request
from pathlib import Path

from ..config import DEV_TOOLS, WORKSPACE, log

KIT = Path(__file__).resolve().parent / "screen_kit"
PROMETHEUS_CSS = ("prometheus/vendor/AdminLTE/bootstrap/css/bootstrap.min.css",
                  "prometheus/vendor/AdminLTE/dist/css/AdminLTE.min.css",
                  "prometheus/vendor/AdminLTE/dist/css/skins/skin-black-light.css")
FONT = "https://fonts.googleapis.com/css?family=Source+Sans+Pro:300,400,600,700,300italic,400italic,600italic"
LINK = re.compile(r"<link\b[^>]*>", re.I)
HREF = re.compile(r'\bhref="([^"]+)"', re.I)
STYLESHEET = re.compile(r'\brel="stylesheet"', re.I)
REFRESH_SECONDS = 600          # the built CSS file names change with every Prometheus deploy


def _file(path):
    return Path(path).resolve().as_uri()


def app_url(configured=None):
    """Where a running Prometheus is: config `prometheusUrl`, else `appUrl` in workspace.local.json."""
    if configured:
        return configured
    local = DEV_TOOLS / "workspace.local.json"
    if local.exists():
        try:
            return json.loads(local.read_text(encoding="utf-8")).get("appUrl") or None
        except ValueError:
            return None
    return None


def built_css(url, timeout=8):
    """The same-origin stylesheets Prometheus' index page links (its built vendor and app CSS), or []."""
    if not url:
        return []
    base = url.rstrip("/") + "/"
    try:
        with urllib.request.urlopen(base, timeout=timeout) as r:
            index = r.read(2_000_000).decode("utf-8", "replace")
    except (OSError, ValueError) as e:
        log(f"Prometheus at {url} not reachable for screen styles ({e}); using the checkout's CSS")
        return []
    origin = urllib.parse.urlsplit(base)[:2]
    out = []
    for tag in LINK.findall(index):
        href = HREF.search(tag)
        if not href or not STYLESHEET.search(tag):
            continue
        full = urllib.parse.urljoin(base, href.group(1))
        if urllib.parse.urlsplit(full)[:2] == origin and full.lower().startswith(("http://", "https://")):
            out.append(full)
    return out


def stylesheets(built=(), workspace=WORKSPACE):
    """Built Prometheus CSS when there is some; else the checkout's AdminLTE plus the Projects4Me overlay."""
    css = [FONT]
    if built:
        css += list(built)
    else:
        css += [_file(workspace / rel) for rel in PROMETHEUS_CSS if (workspace / rel).exists()]
    css.append(_file(KIT / "font-awesome" / "css" / "font-awesome.min.css"))
    if not built:
        css.append(_file(KIT / "projects4me.css"))
    return css


class Stylesheets:
    """The current stylesheets, looked up again every REFRESH_SECONDS (a deploy or Prometheus coming back)."""

    def __init__(self, prometheus_url=None, fetch=built_css):
        self.prometheus_url, self.fetch = prometheus_url, fetch
        self._css, self._at = None, 0
        self._lock = threading.Lock()

    def current(self):
        with self._lock:
            if self._css is None or time.monotonic() - self._at > REFRESH_SECONDS:
                self._css = stylesheets(self.fetch(app_url(self.prometheus_url)))
                self._at = time.monotonic()
            return self._css
