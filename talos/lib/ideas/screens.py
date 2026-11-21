"""UI screens Nestor designs, as fenced blocks in a reply:

    ```nestor-screen key="grouped-list" title="Notifications grouped by issue" size="1440x900" shell="app"
    <section class="content-header"><h1>Notifications</h1></section>
    <section class="content"><div class="box box-primary">...</div></section>
    ```

The HTML uses Prometheus' own markup and classes (Bootstrap 3, AdminLTE, the design system). `shell="app"` (the
default) puts it inside the app's header, sidebar and footer, as a page; `shell="none"` renders it alone (a modal, a
component). `body="notifications-sidebar-open"` adds state classes to <body> (panels Prometheus opens with a body
class); `project="A Gaia" code="agaia"` is the project in the header's switcher and the sidebar's Project group
(default A Prometheus); `project=""` draws the app with no project selected. A top-level
<aside class="main-sidebar"> in the HTML replaces the app's sidebar (a screen that changes the navigation). One
<style> block may style classes a new component needs (css.clean_css); what it may not contain is dropped and
reported. render.py turns it into a PNG. A later block with the same key is a new version of that screen.
"""

import re
from html.parser import HTMLParser

from .sanitize import clean_screen

BLOCK = re.compile(r"```nestor-screen([^\n]*)\r?\n(.*?)\r?\n[ \t]*```", re.S)
ATTR = re.compile(r'(\w+)\s*=\s*"([^"]*)"')
KEY = re.compile(r"^[a-z0-9][a-z0-9-]{0,39}$")
SIZE = re.compile(r"^(\d{3,4})x(\d{3,4})$")
CLASSES = re.compile(r"[a-z0-9 _-]{0,120}")
CODE = re.compile(r"[a-z0-9]{1,12}")
DEFAULT_SIZE = (1440, 900)
DEFAULT_PROJECT = ("A Prometheus", "aprom")
MAX_SCREENS = 4                 # per reply
MAX_HTML = 60_000


class _TopLevelSidebar(HTMLParser):
    """Finds where the first top-level <aside class="main-sidebar"> starts and ends in sanitised markup."""

    VOID = {"br", "hr", "img", "input", "col", "wbr"}

    def __init__(self):
        super().__init__(convert_charrefs=False)
        self.depth, self.start, self.end, self.inside = 0, None, None, None

    def handle_starttag(self, tag, attrs):
        if tag in self.VOID:
            return
        if (self.depth == 0 and self.start is None and tag == "aside"
                and "main-sidebar" in (dict(attrs).get("class") or "").split()):
            self.start, self.inside = self.getpos(), 1
        self.depth += 1

    def handle_endtag(self, tag):
        self.depth -= 1
        if self.inside and self.depth == 0 and self.end is None:
            self.end, self.inside = self.getpos(), None


def _offset(text, pos):
    line, col = pos
    return sum(len(row) for row in text.splitlines(keepends=True)[:line - 1]) + col


def split_sidebar(markup):
    """(the screen's own top-level <aside class="main-sidebar">, or "", the rest of the markup)."""
    finder = _TopLevelSidebar()
    finder.feed(markup)
    finder.close()
    if finder.start is None or finder.end is None:
        return "", markup
    start = _offset(markup, finder.start)
    end = markup.index(">", _offset(markup, finder.end)) + 1
    return markup[start:end], (markup[:start] + markup[end:]).strip()


def extract(reply):
    """(reply text without the screen blocks, valid screens, problems). Each screen: key, title, width, height,
    shell, html (sanitised), sidebar (its own sanitised <aside class="main-sidebar">, or ""), css (cleaned; ""
    without a <style> block), body, project and code."""
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
        markup, css, dropped = clean_screen(m.group(2)[:MAX_HTML])
        if not markup:
            problems.append(f"screen '{key}' has no HTML left after removing what screens may not contain")
            continue
        if len(screens) >= MAX_SCREENS:
            problems.append(f"at most {MAX_SCREENS} screens per reply; '{key}' was left out")
            continue
        seen.add(key)
        problems.extend(f"screen '{key}' CSS: {reason}" for reason in dropped)
        body = attrs.get("body", "")
        project = " ".join(attrs["project"].split())[:60] if "project" in attrs else DEFAULT_PROJECT[0]
        code = attrs.get("code", "" if "project" in attrs else DEFAULT_PROJECT[1])
        code = code if CODE.fullmatch(code) else ""
        shell = "none" if attrs.get("shell") == "none" else "app"
        sidebar, markup = split_sidebar(markup) if shell == "app" else ("", markup)
        screens.append({"key": key, "title": title or key, "width": width, "height": height, "shell": shell,
                        "html": markup, "sidebar": sidebar, "css": css,
                        "body": body if CLASSES.fullmatch(body) else "",
                        "project": project, "code": code})   # escaped where they go into the page (page.py)
    text = BLOCK.sub("", reply or "").strip()
    return text, screens, problems
