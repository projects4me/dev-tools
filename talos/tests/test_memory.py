"""The workspace's auto-memory folder, the one every Talos run shares (memory.py)."""

import json
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from lib import memory  # noqa: E402


class MemoryTest(unittest.TestCase):
    def test_slug_matches_claude_codes_project_folder_names(self):
        self.assertEqual(memory.slug(r"D:\Claude\Workspace\Projects\Pr4M"), "D--Claude-Workspace-Projects-Pr4M")
        self.assertEqual(memory.slug("/home/me/work/Pr4M"), "-home-me-work-Pr4M")
        self.assertEqual(memory.slug("/srv/my_ws.v2"), "-srv-my-ws-v2")

    def test_folder_is_the_workspaces_not_the_runs(self):
        with mock.patch.dict(memory.os.environ, {"CLAUDE_CONFIG_DIR": str(Path("cfg"))}):
            root = Path("/srv/Pr4M")
            self.assertEqual(memory.folder(root), Path("cfg") / "projects" / memory.slug(root) / "memory")

    def test_claude_home_defaults_to_dot_claude(self):
        with mock.patch.dict(memory.os.environ, {}, clear=True), \
                mock.patch.object(memory.Path, "home", return_value=Path("me")):
            self.assertEqual(memory.claude_home(), Path("me") / ".claude")

    def test_settings(self):
        self.assertEqual(json.loads(memory.settings(Path("m"))), {"autoMemoryDirectory": str(Path("m"))})


if __name__ == "__main__":
    unittest.main()
