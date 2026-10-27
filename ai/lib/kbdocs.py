"""Gaia knowledge for the knowledge base: doc sections, graph nodes and exemplar PRs, read from a Gaia checkout.

Everything is a record {key, kind, subtype, file, anchor, title, body, size, hash}; `key` is `<file>#<anchor>` and
is what `ai kb section` takes. Sections are cut at markdown headings (anchor: the `<a id>` line above the heading,
else the heading's slug) and long ones are split into parts of at most SECTION_LINES lines (`anchor~2`, ...), so a
caller can fetch exactly one short section. Records are content-hashed, so a sync rewrites only what changed.
"""

import hashlib
import json
import re
import time

SECTION_LINES = 40
KINDS = ("doc", "node", "pr")

DOC_GLOBS = ("docs/generated/**/*.md", "docs/architecture/**/*.md", "docs/dev/bundles.md")
GRAPH_FILE = "docs/generated/graph.json"
BUNDLES_FILE = "docs/dev/bundles.md"
NODE_TYPES = ("model", "route", "controller", "component", "permission", "profile", "assertion")

HEADING = re.compile(r"^(#{1,6})\s+(.+?)\s*#*\s*$")
ANCHOR = re.compile(r'^\s*<a\s+id="([^"]+)"\s*>\s*</a>\s*$')
FENCE = re.compile(r"^\s*(```|~~~)")
VALUE_LIMIT = 200


def _record(kind, subtype, file, anchor, title, body):
    return {"key": f"{file}#{anchor}", "kind": kind, "subtype": subtype, "file": file, "anchor": anchor,
            "title": title, "body": body, "size": len(body.splitlines()),
            "hash": hashlib.sha1(f"{title}\n{body}".encode("utf-8")).hexdigest()}


def _slug(text):
    return re.sub(r"[^\w\s-]", "", text.lower()).strip().replace(" ", "-") or "section"


def split_sections(text):
    """Markdown -> [(anchor, title, lines)] cut at headings; text before the first heading is `top`."""
    sections, current, pending, in_fence = [], None, None, False
    used = {}

    def start(title, anchor):
        n = used[anchor] = used.get(anchor, 0) + 1
        return [anchor if n == 1 else f"{anchor}-{n}", title, []]

    for line in text.splitlines():
        if FENCE.match(line):
            in_fence = not in_fence
        m = None if in_fence else HEADING.match(line)
        a = None if in_fence else ANCHOR.match(line)
        if a:
            pending = a.group(1)
            continue
        if m:
            current = start(m.group(2), pending or _slug(m.group(2)))
            sections.append(current)
            pending = None
        elif current is None:
            if not line.strip():
                continue
            current = start("(top)", "top")
            sections.append(current)
        current[2].append(line)
    return [(a, t, ls) for a, t, ls in sections if "".join(ls).strip()]


def _parts(lines):
    """Lines (trailing blanks dropped) -> chunks of at most SECTION_LINES."""
    while lines and not lines[-1].strip():
        lines = lines[:-1]
    return [lines[i:i + SECTION_LINES] for i in range(0, len(lines), SECTION_LINES)] or [[]]


def doc_records(root, file):
    rel = file.relative_to(root).as_posix()
    out = []
    for anchor, title, lines in split_sections(file.read_text(encoding="utf-8", errors="replace")):
        for n, part in enumerate(_parts(lines), 1):
            out.append(_record("doc", None, rel, anchor if n == 1 else f"{anchor}~{n}",
                               title if n == 1 else f"{title} (part {n})", "\n".join(part)))
    return out


def _flat(value):
    if isinstance(value, (list, tuple)):
        text = ", ".join(_flat(v) for v in value)
    elif isinstance(value, dict):
        text = ", ".join(f"{k}={_flat(v)}" for k, v in value.items())
    else:
        text = str(value)
    return text if len(text) <= VALUE_LIMIT else text[:VALUE_LIMIT] + "…"


def node_records(graph):
    out = []
    for node in graph.get("nodes", []):
        if node.get("type") not in NODE_TYPES:
            continue
        lines = [f"{node['type']} {node['key']}"]
        lines += [f"{k}: {_flat(v)}" for k, v in (node.get("attributes") or {}).items() if v not in (None, "", [])]
        out.append(_record("node", node["type"], GRAPH_FILE, node["id"], node["id"], "\n".join(lines)))
    return out


def exemplar_records(text):
    """bundles.md -> one record per exemplar PR, listing the bundles it is an exemplar for and why."""
    prs = {}
    for anchor, title, lines in split_sections(text):
        collecting = False
        for line in lines:
            if re.match(r"\*\*Exemplars?:?\*\*", line):
                collecting = True
                rest = re.sub(r"^\*\*Exemplars?:?\*\*:?", "", line).strip()
                _collect(prs, rest, title)
                if rest:
                    collecting = False
            elif collecting:
                if line.lstrip().startswith("-"):
                    _collect(prs, line.lstrip("- ").strip(), title)
                elif line.strip():
                    collecting = False
    return [_record("pr", None, BUNDLES_FILE, f"pr-{n}", f"PR #{n}",
                    "\n".join(f"Exemplar for {bundle}: {note}" for bundle, note in uses))
            for n, uses in sorted(prs.items(), key=lambda kv: int(kv[0]))]


def _collect(prs, text, bundle):
    """`#193 (note). #178 (note)` or `#179 (note)` -> prs[number] += (bundle, note)."""
    for m in re.finditer(r"#(\d+)\b\s*(\([^)]*\))?", text):
        prs.setdefault(m.group(1), []).append((bundle, (m.group(2) or "").strip("()") or text.strip()))


def collect(root):
    """Every record found in the Gaia checkout at `root` (empty if it has no docs)."""
    records = []
    files = sorted({f for pattern in DOC_GLOBS for f in root.glob(pattern) if f.is_file()})
    for f in files:
        records += doc_records(root, f)
    graph = root / GRAPH_FILE
    if graph.is_file():
        records += node_records(json.loads(graph.read_text(encoding="utf-8")))
    bundles = root / BUNDLES_FILE
    if bundles.is_file():
        records += exemplar_records(bundles.read_text(encoding="utf-8", errors="replace"))
    return records


def sync(store, root):
    """Brings the store's docs in line with the checkout at `root`: only changed records are rewritten, records
    that are gone are removed. Returns (added, updated, removed, unchanged)."""
    records = {r["key"]: r for r in collect(root)}
    known = store.doc_hashes()
    added = [k for k in records if k not in known]
    updated = [k for k in records if k in known and known[k] != records[k]["hash"]]
    removed = [k for k in known if k not in records]
    with store.transaction():
        for k in added + updated:
            store.upsert_doc(records[k])
        store.delete_docs(removed)
        store.set_meta("docs_sync", time.strftime("%Y-%m-%d %H:%M"))
    return len(added), len(updated), len(removed), len(records) - len(added) - len(updated)
