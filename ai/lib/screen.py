"""`ai screen render`: render one nestor-screen block (stdin) to a PNG during Nestor's turn, so Nestor can look at
it and fix it before replying. Same parsing, cleaning and rendering as Talos (talos/lib/ideas)."""

import importlib.util
from importlib import import_module
import sys
from pathlib import Path

from .common import AiError

TALOS_LIB = Path(__file__).resolve().parents[2] / "talos" / "lib"
PREVIEWS = "previews"


def _talos():
    """Talos' lib package under another name: `lib` here is ai's own package."""
    name = "talos_lib"
    if name not in sys.modules:
        spec = importlib.util.spec_from_file_location(name, TALOS_LIB / "__init__.py",
                                                      submodule_search_locations=[str(TALOS_LIB)])
        module = importlib.util.module_from_spec(spec)
        sys.modules[name] = module
        spec.loader.exec_module(module)
    return (import_module(f"{name}.ideas.screens"), import_module(f"{name}.ideas.render"),
            import_module(f"{name}.ideas.styles"), import_module(f"{name}.config"))


def style_source(sheets, fonts):
    """Where the stylesheets came from: a running Prometheus (http links other than the font) or the checkout."""
    built = [s for s in sheets if s.startswith(("http://", "https://")) and s != fonts]
    return f"running Prometheus ({built[0].split('/', 3)[2]})" if built else "the checkout's CSS"


def render_block(text):
    """(png path, stylesheet source, problems) for the one nestor-screen block in `text`."""
    screens, render, styles, config = _talos()
    if "```nestor-screen" not in (text or ""):
        raise AiError("no nestor-screen block on stdin (pipe a fenced ```nestor-screen ...``` block in)")
    _, found, problems = screens.extract(text)
    if not found:
        raise AiError("invalid nestor-screen block: " + ("; ".join(problems) or "nothing usable in it"))
    screen = found[0]
    if len(found) > 1:
        problems.append(f"{len(found)} blocks given; only '{screen['key']}' was rendered")
    try:
        cfg = config.settings()
    except (OSError, ValueError):
        cfg = {}
    renderer = render.Renderer(cfg.get("browserBin"), "", cfg.get("prometheusUrl"))
    try:
        png = renderer.render(screen)
    except (render.RenderError, config.TalosError) as e:
        raise AiError(str(e))
    out_dir = config.STATE_DIR / "nestor" / PREVIEWS
    out_dir.mkdir(parents=True, exist_ok=True)
    out = out_dir / f"{screen['key']}.png"
    out.write_bytes(png)
    return out, style_source(renderer.styles.current(), styles.FONT), problems


def cmd_screen(args):
    if args.action != "render":
        raise AiError(f"unknown screen action '{args.action}'")
    if sys.stdin.isatty():
        raise AiError("no nestor-screen block on stdin (pipe it in with a heredoc)")
    out, source, problems = render_block(sys.stdin.read())
    print(f"png: {out}")
    print(f"styles: {source}")
    for p in problems:
        print(f"problem: {p}")
    return 0
