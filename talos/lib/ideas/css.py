"""A UI screen's <style> block (sanitize.clean_screen hands it here): the stylesheet is rebuilt from only the parts
that pass, so it can't load anything, escape its <style> element or restyle Prometheus' own classes. Class selectors
(rooted in a new class, not a Bootstrap/AdminLTE one), an allowlist of properties and the design system's colours.
"""

import re

# The design system's colours. Keep in step with prometheus/app/styles/variables.scss (the $...Color values), the
# fixed AdminLTE surfaces of the design system (prometheus/docs/ui/design-system.md) and the issue status colours
# in prometheus/app/styles/issue/issue-status.scss. Lowercase, 6 or 8 digits (#fff is expanded before comparing).
PALETTE = frozenset({
    # variables.scss
    "#b3244f", "#b3244f45", "#640c44", "#1b7bba", "#e5e5e5", "#ffffff", "#cccccc", "#333333", "#999999", "#000000",
    "#dd6a45", "#5fca4a", "#61c4b8", "#db8b0b", "#d33724",
    # fixed surfaces
    "#ecf0f5", "#f9fafc", "#f4f4f4", "#dddddd", "#aaaaaa", "#444444",
    # issue-status.scss
    "#5ac594", "#e98a7e", "#f7bf65", "#80b5d3", "#f5f5f5",
})
COLOUR_WORDS = frozenset({"transparent", "inherit"})
MAX_CSS = 8 * 1024
MAX_CSS_PROBLEMS = 12

PROPERTY = re.compile(
    r"(?:display|gap|row-gap|column-gap|order|position|top|right|bottom|left|(?:min-|max-)?(?:width|height)"
    r"|overflow(?:-x|-y)?|z-index|color|background-color|font-size|font-weight|font-style|line-height|text-align"
    r"|text-transform|letter-spacing|white-space|text-overflow|opacity|cursor|box-sizing|place-(?:items|content|self)"
    r"|(?:flex|grid|margin|padding|border)(?:-[a-z]+)*|(?:align|justify)(?:-[a-z]+)+)")
COLOUR_PROPERTY = re.compile(r"color|background-color|border(?:-[a-z]+)*-color")
BORDER_SHORTHAND = re.compile(r"border(?:-(?:top|right|bottom|left|block|inline)(?:-start|-end)?)?")
BORDER_WORDS = frozenset({"none", "hidden", "solid", "dashed", "dotted", "double", "groove", "ridge", "inset",
                          "outset", "thin", "medium", "thick"}) | COLOUR_WORDS
VALUE_CHARS = re.compile(r"[a-z0-9#.%\s,()/+*-]+")
FUNCTION = re.compile(r"([a-z-]*)\(")
FUNCTIONS = frozenset({"repeat", "minmax", "calc"})
HEX = re.compile(r"#[0-9a-z]*")
WORD = re.compile(r"(?<![#\w.-])[a-z][a-z-]*")
FORBIDDEN = ("url(", "expression(", "behavior:", "javascript:", "\\", "</")
_IDENT = r"-?[A-Za-z_][A-Za-z0-9_-]*"
_PSEUDO = r"::?[a-z-]+(?:\([a-z0-9+\s-]*\))?"
_COMPOUND = rf"(?:[a-z][a-z0-9]*(?:\.{_IDENT})*|(?:\.{_IDENT})+)(?:{_PSEUDO})*"
SELECTOR = re.compile(rf"(?:\.{_IDENT})+(?:{_PSEUDO})*(?:\s*[>+~]\s*{_COMPOUND}|\s+{_COMPOUND})*")
# Bootstrap/AdminLTE/Font Awesome classes Prometheus already styles: a selector may not start from one of them.
FRAMEWORK_CLASS = re.compile(
    r"(?:box|btn|modal|form|table|row|col|container|content|wrapper|main|sidebar|navbar|nav|dropdown|label|badge"
    r"|alert|panel|well|fa|skin|text|bg|pull|hidden|visible|input-group|help-block|callout|treeview|list-group"
    r"|progress|tab|pagination|breadcrumb|popover|tooltip|caret|clearfix|sr|thumbnail|jumbotron|active|open|in"
    r"|fade|show|collapse|disabled)(?:-[A-Za-z0-9_-]*)?")
MAX_Z_INDEX = 10


def _short(text, limit=40):
    text = " ".join(text.split())
    return text if len(text) <= limit else text[:limit - 1] + "…"


def _hex(token):
    return "#" + "".join(c * 2 for c in token[1:]) if len(token) in (4, 5) else token


def _blocks(css):
    """(prelude, body, closed) per top-level rule; body is None for a statement ending in ';' (an at-rule, junk)."""
    i, n = 0, len(css)
    while i < n:
        brace, semi = css.find("{", i), css.find(";", i)
        if brace < 0 or 0 <= semi < brace:
            end = semi if semi >= 0 else n
            if css[i:end].strip():
                yield css[i:end].strip(), None, True
            i = end + 1
            continue
        depth, j = 0, brace
        while j < n:
            depth += {"{": 1, "}": -1}.get(css[j], 0)
            if depth == 0:
                break
            j += 1
        yield css[i:brace].strip(), css[brace + 1:j], j < n
        i = j + 1


def _declaration(prop, value):
    """Why a declaration (lowercased) can't stay, or None."""
    if "!" in value:
        return "!important is not allowed (style your own classes, not existing ones)"
    if not PROPERTY.fullmatch(prop) or prop.startswith("border-image"):
        return f"property '{prop}' is not allowed"
    if not VALUE_CHARS.fullmatch(value):
        return "the value has characters screens may not use"
    for name in FUNCTION.findall(value):
        if name not in FUNCTIONS:
            return f"{name}() is not allowed"
    if prop == "position" and value not in ("relative", "absolute"):
        return "position may only be relative or absolute"
    if prop == "z-index" and not (value.isdigit() and int(value) <= MAX_Z_INDEX):
        return f"z-index may only be 0 to {MAX_Z_INDEX}"
    if prop.endswith("radius"):
        return None if value == "50%" else "no rounded corners: border-radius only as 50% (square edges)"
    for token in HEX.findall(value):
        if _hex(token) not in PALETTE:
            return f"colour {token} is not in the design system palette"
    if COLOUR_PROPERTY.fullmatch(prop):
        allowed = COLOUR_WORDS
    elif BORDER_SHORTHAND.fullmatch(prop):
        allowed = BORDER_WORDS
    else:
        return None
    bad = sorted(set(WORD.findall(value)) - allowed - FUNCTIONS)
    return f"colour {bad[0]} is not in the design system palette" if bad else None


def clean_css(text):
    """(css, problems): a screen's stylesheet rebuilt from only the selectors and declarations that pass (so
    nothing the checks didn't see gets through), and a short reason for each rule or declaration dropped."""
    rules, problems = [], []
    css = re.sub(r"/\*.*?(?:\*/|$)", " ", text or "", flags=re.S)
    for prelude, body, closed in _blocks(css):
        where = _short(prelude) or "(no selector)"
        bad = next((f for f in FORBIDDEN if f in f"{prelude}{{{body or ''}".lower()), None)
        if prelude.startswith("@"):
            problems.append(f"'{_short(prelude.split()[0], 30)}': at-rules are not allowed")
        elif bad:
            problems.append(f"'{where}': '{bad}' is not allowed; rule dropped")
        elif body is None:
            problems.append(f"'{where}': not a rule; dropped")
        elif not closed:
            problems.append(f"'{where}': no closing brace; rule dropped")
        elif "{" in body:
            problems.append(f"'{where}': nested rules are not allowed; rule dropped")
        else:
            rule = _rule(prelude, body, where, problems)
            if rule:
                rules.append(rule)
    out, size = [], 0
    for index, rule in enumerate(rules):
        size += len(rule) + 1
        if size > MAX_CSS:
            problems.append(f"the stylesheet is over {MAX_CSS // 1024} KB; the last {len(rules) - index} rule(s) "
                            f"were dropped")
            break
        out.append(rule)
    if len(problems) > MAX_CSS_PROBLEMS:
        problems = problems[:MAX_CSS_PROBLEMS] + [f"and {len(problems) - MAX_CSS_PROBLEMS} more CSS problems"]
    return "\n".join(out), problems


def _rule(prelude, body, where, problems):
    """One rule with the selectors and declarations that pass, or None; reasons go to problems."""
    selectors = []
    for selector in (" ".join(s.split()) for s in prelude.split(",")):
        root = re.match(r"\.(-?[A-Za-z_][A-Za-z0-9_-]*)", selector)
        if root and FRAMEWORK_CLASS.fullmatch(root.group(1)):
            problems.append(f"selector '{_short(selector)}': '.{root.group(1)}' is a Prometheus/Bootstrap class; "
                            f"start from a new class of your component")
        elif SELECTOR.fullmatch(selector):
            selectors.append(selector)
        else:
            problems.append(f"selector '{_short(selector)}': only class selectors (starting with '.') are allowed")
    if not selectors:
        return None
    kept = []
    for part in filter(str.strip, body.split(";")):
        prop, colon, value = part.partition(":")
        prop, value = prop.strip().lower(), " ".join(value.split()).lower()
        reason = _declaration(prop, value) if colon and prop and value else "not a declaration"
        if reason:
            problems.append(f"'{where}' {_short(part)}: {reason}")
        else:
            kept.append(f"{prop}: {value}")
    return f"{', '.join(selectors)} {{ {'; '.join(kept)} }}" if kept else None
