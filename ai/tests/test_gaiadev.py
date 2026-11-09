"""ai gaia-dev: the argument allowlist, the throwaway-container mount and the checkout it runs on."""

import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from lib import common, gaiadev, gaiapack  # noqa: E402


def link_dir(target, link):
    """A directory link like the dev-tools -> .dev-tools-runtime one in a .runs/issue-N folder."""
    if os.name == "nt":
        import _winapi
        _winapi.CreateJunction(str(target), str(link))
    else:
        os.symlink(target, link, target_is_directory=True)


class AllowlistTest(unittest.TestCase):
    def test_generators_checks_and_queries_are_allowed(self):
        for args in (["graph", "export"], ["graph", "export", "--check"], ["docs", "generate"],
                     ["docs", "generate", "--check"], ["check", "api-cases"], ["check", "api-cases", "/api/v1/idea"],
                     ["check", "resource", "Report"], ["model", "Idea"], ["route", "POST /api/v1/idea"],
                     ["acl", "idea", "aclNoProject"], ["cases", "idea", "--method=POST", "--all"], ["assertions"],
                     ["profiles"], ["fixtures"]):
            self.assertEqual(gaiadev.validate(args), args)

    def test_database_and_writing_commands_are_refused(self):
        for args in (["migration", "migrate"], ["migration"], ["scaffold", "resource", "Report"],
                     ["check", "behavior"], ["graph"], ["docs", "generate", "--force"]):
            with self.assertRaisesRegex(common.AiError, "not allowed"):
                gaiadev.validate(args)

    def test_arguments_are_checked(self):
        for args in (["check", "resource"], ["check", "resource", "A", "B"], ["graph", "export", "extra"]):
            with self.assertRaisesRegex(common.AiError, "takes"):
                gaiadev.validate(args)
        for args in (["model", "Idea;rm -rf /"], ["cases", "idea", "--profile=$(id)"], ["model", "$HOME"]):
            with self.assertRaisesRegex(common.AiError, "not allowed"):
                gaiadev.validate(args)

    def test_no_command_prints_the_usage(self):
        with self.assertRaisesRegex(common.AiError, "usage: ai gaia-dev <command>.*graph export"):
            gaiadev.validate([])

    def test_value_options_need_a_value_and_flags_take_none(self):
        for args in (["cases", "idea", "--method"], ["cases", "idea", "--all=x"], ["graph", "export", "--check=x"],
                     ["cases", "idea", "--method", "GET"], ["route", "../../etc/passwd"]):
            with self.assertRaises(common.AiError):
                gaiadev.validate(args)

    def test_the_packs_checks_point_at_ai_gaia_dev_only_when_it_runs_them(self):
        self.assertEqual(gaiapack.check_line("check resource Report"), "dev-tools/ai/ai gaia-dev check resource Report")
        self.assertEqual(gaiapack.check_line("check behavior"), f"php {gaiapack.GAIA_DEV} check behavior")


class CommandTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name) / "gaia"
        self.root.mkdir()

    def tearDown(self):
        self.tmp.cleanup()

    def test_container_mounts_the_checkout_with_the_vendor_volume_and_no_network(self):
        cmd = gaiapack.gaia_dev_command(self.root, ["docs", "generate"], which=lambda n: n == "docker")
        self.assertEqual(cmd[:2], ["docker", "run"])
        self.assertIn(f"{self.root.resolve()}:/var/www/html", cmd)
        self.assertIn(f"{gaiapack.VENDOR_VOLUME}:/var/www/html/vendor", cmd)
        self.assertEqual(cmd[cmd.index("--network") + 1], "none")
        self.assertEqual(cmd[-4:], ["php", gaiapack.GAIA_DEV, "docs", "generate"])
        self.assertEqual("--user" in cmd, os.name != "nt")

    def test_local_php_needs_the_checkouts_vendor_folder(self):
        which = lambda n: n in ("php", "docker")  # noqa: E731
        self.assertEqual(gaiapack.gaia_dev_command(self.root, ["profiles"], which=which)[0], "docker")
        (self.root / "vendor").mkdir()
        (self.root / "vendor" / "autoload.php").write_text("<?php\n", encoding="utf-8")
        self.assertEqual(gaiapack.gaia_dev_command(self.root, ["profiles"], which=which),
                         ["php", gaiapack.GAIA_DEV, "profiles"])

    def test_nothing_to_run_it_with(self):
        self.assertIsNone(gaiapack.gaia_dev_command(self.root, ["profiles"], which=lambda n: False))


class CheckoutTest(unittest.TestCase):
    """From a .runs/issue-N folder (dev-tools there links to the runtime copy) it runs on that folder's gaia."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        base = Path(self.tmp.name)
        runtime = base / common.RUNTIME_NAME
        runtime.mkdir()
        self.run_dir = base / ".runs" / "issue-7"
        self.gaia = self.run_dir / "gaia"
        (self.gaia / gaiapack.GAIA_DEV).parent.mkdir(parents=True)
        (self.gaia / gaiapack.GAIA_DEV).write_text("#!/usr/bin/env php\n", encoding="utf-8")
        link_dir(runtime, self.run_dir / "dev-tools")
        self.calls = []

    def tearDown(self):
        os.rmdir(self.run_dir / "dev-tools")       # the link only, not the runtime folder behind it
        self.tmp.cleanup()

    def fake_run(self, root, args, timeout=0):
        self.calls.append((Path(root), args))
        return 0, "ok"

    def run_cmd(self, args, env, cwd):
        with mock.patch.dict(os.environ, env), mock.patch.object(Path, "cwd", return_value=cwd), \
                mock.patch("builtins.print"):
            if "P4M_WORKSPACE" not in env:
                os.environ.pop("P4M_WORKSPACE", None)
            return gaiadev.cmd_gaia_dev(mock.Mock(args=args), run=self.fake_run)

    def test_runs_on_the_run_folders_gaia_not_the_workspaces(self):
        code = self.run_cmd(["docs", "generate"], {"P4M_WORKSPACE": str(self.run_dir)},
                            self.run_dir)
        self.assertEqual(code, 0)
        self.assertEqual(self.calls, [(self.gaia.resolve(), ["docs", "generate"])])
        self.assertNotEqual(self.calls[0][0], (common.WORKSPACE / "gaia").resolve())

    def test_refused_in_a_run_folder_without_p4m_workspace(self):
        with self.assertRaisesRegex(common.AiError, "P4M_WORKSPACE"):
            self.run_cmd(["docs", "generate"], {}, self.run_dir)
        self.assertEqual(self.calls, [])

    def test_a_disallowed_command_never_runs(self):
        with self.assertRaisesRegex(common.AiError, "not allowed"):
            self.run_cmd(["migration", "migrate"], {"P4M_WORKSPACE": str(self.run_dir)}, self.run_dir)
        self.assertEqual(self.calls, [])

    def test_a_run_folder_without_gaia_is_refused(self):
        (self.gaia / gaiapack.GAIA_DEV).unlink()
        with self.assertRaisesRegex(common.AiError, "no gaia-dev"):
            self.run_cmd(["docs", "generate"], {"P4M_WORKSPACE": str(self.run_dir)}, self.run_dir)

    def test_unavailable_runner_is_a_clear_error(self):
        with mock.patch.dict(os.environ, {"P4M_WORKSPACE": str(self.run_dir)}), \
                mock.patch.object(Path, "cwd", return_value=self.run_dir), mock.patch("builtins.print"):
            with self.assertRaisesRegex(common.AiError, "could not run: no docker"):
                gaiadev.cmd_gaia_dev(mock.Mock(args=["profiles"]), run=lambda r, a, timeout=0: (None, "no docker"))


if __name__ == "__main__":
    unittest.main()
