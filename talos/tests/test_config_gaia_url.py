import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from lib import config  # noqa: E402


class GaiaUrlFallbackTest(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())
        (self.dir / "talos").mkdir()
        (self.dir / "gaia-mcp").mkdir()

    def local(self, **values):
        (self.dir / "talos" / "config.local.json").write_text(json.dumps(values), encoding="utf-8")

    def env(self, text):
        (self.dir / "gaia-mcp" / "gaia-mcp.env").write_text(text, encoding="utf-8")

    def test_local_config_wins(self):
        self.local(gaiaUrl="https://local")
        self.env("GAIA_URL=https://env\n")
        self.assertEqual("https://local", config.settings(self.dir)["gaiaUrl"])

    def test_env_file_beats_the_default(self):
        self.local(hermesUrl="https://h")
        self.env('# c\nGAIA_EMAIL=a\nGAIA_URL="https://env"\n')
        cfg = config.settings(self.dir)
        self.assertEqual("https://env", cfg["gaiaUrl"])
        self.assertEqual("https://h", cfg["hermesUrl"])

    def test_default_when_nothing_else(self):
        default = json.loads(config.CONFIG_FILE.read_text(encoding="utf-8"))["gaiaUrl"]
        self.assertEqual(default, config.settings(self.dir)["gaiaUrl"])
        self.env("GAIA_EMAIL=a\n")
        self.assertEqual(default, config.settings(self.dir)["gaiaUrl"])


if __name__ == "__main__":
    unittest.main()
