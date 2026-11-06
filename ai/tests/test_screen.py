"""Unit tests for lib/screen.py (`ai screen render`) with a fake browser. Run: python -m unittest discover -s tests"""

import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from lib import screen  # noqa: E402
from lib.common import AiError  # noqa: E402

BLOCK = '```nestor-screen key="demo" title="Demo"\n<section class="content"><h1>Hi</h1></section>\n```\n'


class FakeRenderer:
    def __init__(self, *a, **k):
        self.styles = mock.Mock(current=lambda: ["font", "file:///x.css"])

    def render(self, s):
        return b"PNG" + s["key"].encode()


class ScreenTest(unittest.TestCase):
    def render(self, text):
        _, render, _, config = screen._talos()
        with tempfile.TemporaryDirectory() as tmp, \
                mock.patch.object(config, "STATE_DIR", Path(tmp)), \
                mock.patch.object(config, "settings", lambda: {}), \
                mock.patch.object(render, "Renderer", FakeRenderer):
            out, source, problems = screen.render_block(text)
            return out, out.read_bytes(), source, problems, Path(tmp)

    def test_renders_to_previews(self):
        out, data, source, problems, tmp = self.render(BLOCK)
        self.assertEqual(out, tmp / "nestor" / "previews" / "demo.png")
        self.assertEqual(data, b"PNGdemo")
        self.assertEqual(source, "the checkout's CSS")
        self.assertEqual(problems, [])

    def test_rerender_overwrites(self):
        out, *_ = self.render(BLOCK)
        self.assertTrue(out.name == "demo.png")

    def test_no_block(self):
        with self.assertRaises(AiError):
            self.render("just text")

    def test_invalid_block(self):
        with self.assertRaises(AiError) as e:
            self.render('```nestor-screen key="Bad Key"\n<p>x</p>\n```')
        self.assertIn("invalid", str(e.exception))

    def test_render_error_is_clean(self):
        _, render, _, config = screen._talos()

        class Failing(FakeRenderer):
            def render(self, s):
                raise render.RenderError("no browser")

        with mock.patch.object(config, "settings", lambda: {}), mock.patch.object(render, "Renderer", Failing):
            with self.assertRaises(AiError) as e:
                screen.render_block(BLOCK)
        self.assertIn("no browser", str(e.exception))

    def test_style_source(self):
        self.assertEqual(screen.style_source(["F", "http://prom:4200/a.css"], "F"), "running Prometheus (prom:4200)")


if __name__ == "__main__":
    unittest.main()
