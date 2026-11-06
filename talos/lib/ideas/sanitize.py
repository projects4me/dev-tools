"""HTML from the agent is model output (possibly steered by injected text), so only an allowlist survives.

Two policies:
- DESCRIPTION: issue descriptions, which Gaia stores and Prometheus renders. Plain formatting, no attributes except
  an http(s) link target.
- SCREEN: UI screens (screens.py), which a headless browser renders to PNG. Layout, forms and tables with `class`
  (Prometheus' Bootstrap/AdminLTE classes), but no scripts, style attributes, event handlers or external URLs:
  links are "#..." and images only inline data: PNG/JPEG/GIF, so a screen can't fetch or run anything. One
  <style> block is kept apart from the markup (clean_screen) and goes through clean_css: class selectors, an
  allowlist of properties and the design system's colours only, for components Prometheus has no classes for yet.
Everything else is dropped; text is escaped.
"""

import html
import re
from dataclasses import dataclass, field
from html.parser import HTMLParser

from .css import clean_css

DROP_CONTENT = {"script", "style", "iframe", "object", "embed", "template", "noscript", "svg", "math", "link", "meta",
                "base", "frame", "frameset", "head", "title"}
VOID = {"br", "hr", "img", "input", "col", "wbr"}
DATA_IMAGE = re.compile(r"data:image/(png|jpeg|gif);base64,[A-Za-z0-9+/=\s]{1,200000}")
ATTR_NAME = re.compile(r"[a-z][a-z0-9-]*")
DROP_VOID = {"link", "meta", "base", "frame"}      # dropped, and nothing to skip after them


@dataclass(frozen=True)
class Policy:
    tags: frozenset
    attrs: frozenset = frozenset()                 # allowed on every allowed tag
    href: str = "http"                             # "http": http(s) only; "anchor": "#..." only
    images: bool = False                           # <img src="data:image/...">
    prefixed: tuple = field(default=())            # attribute prefixes, e.g. ("aria-", "data-")
    style: bool = False                            # keep the text of <style> blocks apart (clean_screen)


DESCRIPTION = Policy(tags=frozenset({"p", "br", "ul", "ol", "li", "strong", "b", "em", "i", "code", "pre",
                                     "blockquote", "h3", "h4", "a", "table", "thead", "tbody", "tr", "th", "td"}))

SCREEN = Policy(
    tags=frozenset({"div", "span", "p", "br", "hr", "h1", "h2", "h3", "h4", "h5", "h6", "ul", "ol", "li", "dl", "dt",
                    "dd", "strong", "b", "em", "i", "small", "code", "pre", "blockquote", "a", "img", "table",
                    "thead", "tbody", "tfoot", "tr", "th", "td", "caption", "col", "colgroup", "section", "header",
                    "footer", "nav", "aside", "main", "article", "figure", "figcaption", "form", "fieldset",
                    "legend", "label", "input", "select", "option", "optgroup", "textarea", "button", "sup", "sub",
                    "mark", "wbr"}),
    attrs=frozenset({"class", "id", "colspan", "rowspan", "type", "placeholder", "value", "checked", "selected",
                     "disabled", "readonly", "title", "alt", "rows", "cols", "name", "for", "role", "width",
                     "height", "multiple", "maxlength", "href", "src"}),
    href="anchor", images=True, prefixed=("aria-", "data-"), style=True)


class _Cleaner(HTMLParser):
    def __init__(self, policy):
        super().__init__(convert_charrefs=True)
        self.policy = policy
        self.out, self.open, self.skip = [], [], 0
        self.styles, self.in_style = [], False             # text of top-level <style> blocks (policy.style)

    def _attrs(self, tag, attrs):
        kept = []
        for name, value in attrs:
            name = (name or "").lower()
            value = value if value is not None else ""
            if not ATTR_NAME.fullmatch(name) or name.startswith("on") or name == "style":
                continue
            if name == "href":
                ok = (value.startswith("#") if self.policy.href == "anchor"
                      else value.lower().startswith(("http://", "https://")))
                if not ok or tag != "a":
                    continue
            elif name == "src":
                if not (self.policy.images and tag == "img" and DATA_IMAGE.fullmatch(value)):
                    continue
            elif name not in self.policy.attrs and not name.startswith(self.policy.prefixed or ("\0",)):
                continue
            kept.append(f' {name}="{html.escape(value, quote=True)}"' if value else f" {name}")
        return "".join(kept)

    def handle_starttag(self, tag, attrs):
        if tag in DROP_CONTENT:
            if tag == "style" and self.policy.style and not self.skip:
                self.styles.append("")
                self.in_style = True
            if tag not in DROP_VOID:
                self.skip += 1
            return
        if self.skip or tag not in self.policy.tags:
            return
        if tag == "a" and self.policy.href == "http" and not any(
                n == "href" and (v or "").lower().startswith(("http://", "https://")) for n, v in attrs):
            return                                             # a description link without a safe target: text only
        self.out.append(f"<{tag}{self._attrs(tag, attrs)}>")
        if tag not in VOID:
            self.open.append(tag)

    def handle_startendtag(self, tag, attrs):
        if tag in VOID and not self.skip and tag in self.policy.tags:
            self.out.append(f"<{tag}{self._attrs(tag, attrs)}>")

    def handle_endtag(self, tag):
        if tag == "style":
            self.in_style = False
        if tag in DROP_CONTENT:
            if tag not in DROP_VOID:
                self.skip = max(0, self.skip - 1)
            return
        if self.skip or tag not in self.open:
            return
        while self.open:                      # close anything left open inside it, too
            top = self.open.pop()
            self.out.append(f"</{top}>")
            if top == tag:
                break

    def handle_data(self, data):
        if self.in_style:
            self.styles[-1] += data
        if not self.skip:
            self.out.append(html.escape(data, quote=False))

    def result(self):
        self.close()
        return "".join(self.out) + "".join(f"</{t}>" for t in reversed(self.open))


def _clean(text, policy):
    cleaner = _Cleaner(policy)
    cleaner.feed(text or "")
    return cleaner


def clean_html(text, policy=DESCRIPTION):
    return _clean(text, policy).result().strip()


def clean_screen(text, policy=SCREEN):
    """(markup, css, problems) of a screen: the sanitised markup, and its first <style> block through clean_css."""
    cleaner = _clean(text, policy)
    markup = cleaner.result().strip()
    if not cleaner.styles:
        return markup, "", []
    css, problems = clean_css(cleaner.styles[0])
    if len(cleaner.styles) > 1:
        problems.append(f"only one <style> block per screen; {len(cleaner.styles) - 1} more dropped")
    return markup, css, problems
