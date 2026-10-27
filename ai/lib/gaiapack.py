"""The Gaia knowledge pack of `ai context`: what the agent needs before touching Gaia, delivered up front.

For the models and endpoints an issue names it adds the `gaia-dev` answers (model summary, ACL rows, existing api
cases), the generated doc sections that match (`ai kb search --kind doc`), the change bundle the issue most
resembles with its exemplar PR, and the `gaia-dev` checks to run before shipping. The whole pack is capped
(`gaiaPackLines` in config.json, default 300 lines); what doesn't fit is listed as `ai kb section` references.
"""

import json
import re
import shutil
import subprocess
from pathlib import Path

from .common import config, repo_dir

DEFAULT_CAP = 300
GRAPH = "docs/generated/graph.json"
GAIA_DEV = "tools/gaia-dev/bin/gaia-dev"
VENDOR_VOLUME = "gaia_vendor"
ALWAYS_CHECKS = ("graph export --check", "docs generate --check")

# Bundle letter -> words the issue text uses for that kind of work (docs/dev/bundles.md).
BUNDLE_WORDS = {
    "A": ("api-tester", "acl case", "acl cases", "client.json", "acl.json", "seed", "fixture", "profile",
          "prepare-test-db", "case"),
    "B": ("new resource", "new model", "metadata", "scaffold resource", "routes.php", "models.php", "migration",
          "new table"),
    "C": ("action", "controller", "rest action", "patch", "override", "custom logic", "domain"),
    "D": ("behaviour", "behavior", "beforesave", "aftersave", "unit test"),
    "E": ("live event", "live events", "hermes", "eventnames", "socket"),
    "U": ("upload", "attachment", "attachable"),
    "F": ("notification", "notifier", "mailer", "email template"),
    "M": ("acl sweep", "every metadata", "all metadata", "all models"),
    "G": ("core", "framework", "restcontroller", "acl.php", "router", "security"),
    "H": ("schema", "migration", "alter table", "database"),
}
BUNDLE_CHECKS = {
    "A": ("check api-cases",),
    "B": ("check resource {model}", "check api-cases"),
    "C": ("check action", "check api-cases"),
    "D": ("check behavior",),
    "E": ("check live-events",),
}
# Bundle letter -> the gaia-dev scaffolder that writes its touchpoints (tools/gaia-dev/README.md); {model} is the
# first model the issue names, else <Name>.
BUNDLE_SCAFFOLDS = {
    "A": "scaffold api-cases <METHOD> /api/v1/<endpoint> --profile=<profile> --catalog=acl|client [--dry-run]",
    "B": "scaffold resource {model} [--parent=Project] --fields=name:type[:length][:required] "
         "[--acl=<profile>:allow|members|deny]",
    "C": "scaffold action {model} <verb> [--domain=<DomainClass>]",
    "D": "scaffold behavior <name> --model={model} --events=beforeSave",
    "E": "scaffold live-events {model} --events=created,updated --fields=<fields>",
}


def pack_cap():
    return int(config().get("gaiaPackLines") or DEFAULT_CAP)


def load_graph(root):
    try:
        return json.loads((Path(root) / GRAPH).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"nodes": []}


def named_models(text, graph, endpoints=()):
    """Model names the text mentions: a whole word, case-insensitive, or an /api/v1/<endpoint> path."""
    keys = {n["key"] for n in graph.get("nodes", []) if n.get("type") == "model"}
    lower = {k.lower(): k for k in keys}
    words = set(re.findall(r"[A-Za-z]+", text))
    found = {lower[w.lower()] for w in words if w.lower() in lower and (w[:1].isupper() or w.lower() in endpoints)}
    found |= {lower[e.lower()] for e in endpoints if e.lower() in lower}
    return sorted(found)


def pick_bundle(text):
    """The bundle letter whose words the text uses most (ties go to the earlier letter), or None."""
    low = text.lower()
    scores = {letter: sum(len(re.findall(r"\b" + re.escape(w) + r"\b", low)) for w in words)
              for letter, words in BUNDLE_WORDS.items()}
    best = max(scores.values(), default=0)
    return next((l for l in BUNDLE_WORDS if scores[l] == best), None) if best else None


def exemplar(root, letter):
    """First exemplar PR link of the bundle's generated guide, or None."""
    page = Path(root) / "docs" / "generated" / "bundles" / f"{letter}.md"
    try:
        text = page.read_text(encoding="utf-8")
    except OSError:
        return None
    m = re.search(r"\[(#\d+)\]\((https?://[^)]+)\)", text.split('<a id="exemplar">', 1)[-1])
    return f"{m.group(1)} {m.group(2)}" if m else None


def check_commands(letter, models):
    """gaia-dev arguments to run before shipping: the drift checks plus the bundle's own."""
    out = list(ALWAYS_CHECKS)
    for c in BUNDLE_CHECKS.get(letter, ()):
        if "{model}" in c:
            out += [c.format(model=m) for m in models[:1]]
        else:
            out.append(c)
    return out


def scaffold_command(letter, models):
    """The gaia-dev scaffolder arguments for the bundle, or None when it has no scaffolder."""
    c = BUNDLE_SCAFFOLDS.get(letter)
    return c.format(model=models[0] if models else "<Name>") if c else None


def gaia_dev(root, args, timeout=120):
    """(exit code, output) of `gaia-dev <args>` on this checkout: local php, else the gaia image with it mounted.

    (None, reason) when neither is available. Never the shared gaia-dev container: it may mount another checkout.
    """
    root = Path(root)
    if shutil.which("php"):
        cmd = ["php", GAIA_DEV, *args]
    elif shutil.which("docker"):
        cmd = ["docker", "run", "--rm", "-e", "XDEBUG_MODE=off", "-v", f"{root}:/var/www/html",
               "-v", f"{VENDOR_VOLUME}:/var/www/html/vendor", "-w", "/var/www/html", "gaia-gaia",
               "php", GAIA_DEV, *args]
    else:
        return None, "no php or docker on PATH"
    try:
        r = subprocess.run(cmd, cwd=root, capture_output=True, text=True, encoding="utf-8", errors="replace",
                           timeout=timeout)
    except (OSError, subprocess.TimeoutExpired) as e:
        return None, str(e)
    return r.returncode, (r.stdout + r.stderr).strip()


def kb_sections(text, limit=4):
    """(key, size, title) of the generated doc sections matching the text, from the local kb index."""
    from . import kb
    store = kb._store()
    try:
        return [(h["key"], h["size"], h["title"]) for h in store.search_docs(text, kind="doc", limit=limit)]
    except Exception:
        return []
    finally:
        store.close()


def build(root, text, endpoints, run=gaia_dev, sections=kb_sections, cap=None):
    """The pack as a list of lines, capped; sections that don't fit become references."""
    cap = cap or pack_cap()
    graph = load_graph(root)
    models = named_models(text, graph, endpoints)
    letter = pick_bundle(text)
    blocks = []                                  # (title, lines), in priority order

    for m in models[:3]:
        for title, args in ((f"model {m}", ["model", m]), (f"acl {m}", ["acl", m.lower()]),
                            (f"cases {m}", ["cases", m.lower()])):
            code, out = run(root, args)
            blocks.append((f"gaia-dev {title}", out.splitlines() if code == 0 else
                           [f"(unavailable: {out.splitlines()[0] if out else 'exit ' + str(code)})"]))
    if letter:
        ex = exemplar(root, letter)
        guide = f"docs/generated/bundles/{letter}.md"
        blocks.append((f"bundle {letter} (closest to this issue)",
                       [f"guide: ai kb section {guide}#touchpoints-in-order"] +
                       ([f"exemplar PR: {ex}"] if ex else []) +
                       ([f"generate it, don't hand-write it: php {GAIA_DEV} {sc}"]
                        if (sc := scaffold_command(letter, models)) else [])))
    hits = sections(text)
    if hits:
        blocks.append(("doc sections", [f"{k} ({s} lines) {t}" for k, s, t in hits]))
    checks = check_commands(letter, models)
    blocks.append(("checks to run before shipping (ai ship gates on the drift checks and api-cases only)", [f"php {GAIA_DEV} {c}" for c in checks]))

    out, left, deferred = [], cap, []
    for title, lines in blocks:
        if len(lines) + 1 <= left:
            out += [f"\n-- {title}"] + [f"  {l}" for l in lines]
            left -= len(lines) + 1
        else:
            deferred.append(title)
    if deferred:
        out.append("\n-- over the pack cap, run on demand: " + "; ".join(deferred))
    return out


def run_checks(repo, run=gaia_dev):
    """Run the gaia-dev check set for the checkout; [(args, ok, output)] with ok None when it couldn't run."""
    root = repo_dir(repo)
    results = []
    for c in ALWAYS_CHECKS + ("check api-cases",):
        code, out = run(root, c.split(), timeout=300)
        results.append((c, None if code is None else code == 0, out))
    return results
