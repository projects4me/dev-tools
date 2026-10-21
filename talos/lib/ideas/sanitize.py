"""Issue descriptions written by Nestor are model output (possibly steered by injected text) that Gaia stores and
Prometheus renders as HTML, so only a small set of formatting tags survives, without attributes except an http(s)
link target. Everything else is dropped; text is escaped."""

import html
from html.parser import HTMLParser

ALLOWED = {"p", "br", "ul", "ol", "li", "strong", "b", "em", "i", "code", "pre", "blockquote", "h3", "h4", "a",
           "table", "thead", "tbody", "tr", "th", "td"}
VOID = {"br"}
DROP_CONTENT = {"script", "style", "iframe", "object", "embed", "template", "noscript", "svg", "math"}


class _Cleaner(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.out, self.open, self.skip = [], [], 0

    def handle_starttag(self, tag, attrs):
        if tag in DROP_CONTENT:
            self.skip += 1
            return
        if self.skip or tag not in ALLOWED:
            return
        if tag == "a":
            href = dict(attrs).get("href") or ""
            if not href.lower().startswith(("http://", "https://")):
                return
            self.out.append(f'<a href="{html.escape(href, quote=True)}">')
        else:
            self.out.append(f"<{tag}>")
        if tag not in VOID:
            self.open.append(tag)

    def handle_startendtag(self, tag, attrs):
        if tag in VOID and not self.skip:
            self.out.append(f"<{tag}>")

    def handle_endtag(self, tag):
        if tag in DROP_CONTENT:
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
        if not self.skip:
            self.out.append(html.escape(data, quote=False))

    def result(self):
        self.close()
        return "".join(self.out) + "".join(f"</{t}>" for t in reversed(self.open))


def clean_html(text):
    cleaner = _Cleaner()
    cleaner.feed(text or "")
    return cleaner.result().strip()
