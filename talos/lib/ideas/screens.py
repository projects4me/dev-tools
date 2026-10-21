"""UI screens Nestor designs, as fenced blocks in a reply:

    ```nestor-screen key="grouped-list" title="Notifications grouped by issue" size="1440x900" shell="app"
    <section class="content-header"><h1>Notifications</h1></section>
    <section class="content"><div class="box box-primary">...</div></section>
    ```

The HTML uses Prometheus' own markup and classes (Bootstrap 3, AdminLTE, the design system). `shell="app"` (the
default) puts it inside the app's header and sidebar, as a page; `shell="none"` renders it alone (a modal, a
component). `body="notifications-sidebar-open"` adds state classes to <body> (panels Prometheus opens with a body
class); `project="A Gaia"` is the project shown in the sidebar's selector. render.py turns it into a PNG. A later
block with the same key is a new version of that screen.
"""

import re

from .sanitize import SCREEN, clean_html

BLOCK = re.compile(r"```nestor-screen([^\n]*)\r?\n(.*?)\r?\n[ \t]*```", re.S)
ATTR = re.compile(r'(\w+)\s*=\s*"([^"]*)"')
KEY = re.compile(r"^[a-z0-9][a-z0-9-]{0,39}$")
SIZE = re.compile(r"^(\d{3,4})x(\d{3,4})$")
CLASSES = re.compile(r"[a-z0-9 _-]{0,120}")
DEFAULT_SIZE = (1440, 900)
MAX_SCREENS = 4                 # per reply
MAX_HTML = 60_000


def extract(reply):
    """(reply text without the screen blocks, valid screens, problems). Each screen: key, title, width, height,
    shell, html (sanitised)."""
    screens, problems, seen = [], [], set()
    for m in BLOCK.finditer(reply or ""):
        attrs = dict(ATTR.findall(m.group(1)))
        key, title = attrs.get("key", ""), " ".join(attrs.get("title", "").split())[:120]
        if not KEY.match(key):
            problems.append(f"a screen without a valid key (lowercase letters, digits, dashes): '{key}'")
            continue
        if key in seen:
            problems.append(f"screen '{key}' twice in one reply; only the first was used")
            continue
        size = SIZE.match(attrs.get("size", "") or f"{DEFAULT_SIZE[0]}x{DEFAULT_SIZE[1]}")
        width, height = (int(size.group(1)), int(size.group(2))) if size else DEFAULT_SIZE
        if not (800 <= width <= 2560 and 400 <= height <= 4000):
            width, height = DEFAULT_SIZE
        markup = clean_html(m.group(2)[:MAX_HTML], SCREEN)
        if not markup:
            problems.append(f"screen '{key}' has no HTML left after removing what screens may not contain")
            continue
        if len(screens) >= MAX_SCREENS:
            problems.append(f"at most {MAX_SCREENS} screens per reply; '{key}' was left out")
            continue
        seen.add(key)
        body = attrs.get("body", "")
        project = " ".join(attrs.get("project", "").split())[:60]
        screens.append({"key": key, "title": title or key, "width": width, "height": height,
                        "shell": "none" if attrs.get("shell") == "none" else "app", "html": markup,
                        "body": body if CLASSES.fullmatch(body) else "",
                        "project": project})                  # escaped where it goes into the page (page.py)
    text = BLOCK.sub("", reply or "").strip()
    return text, screens, problems
