"""Docker state: parsing docker info/stats output, a missing CLI, an unreachable daemon, the verdict and its hysteresis."""

import json
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from lib import docker_state as ds  # noqa: E402

GIB = 1024 ** 3
INFO = json.dumps({"ServerVersion": "27.0.1", "MemTotal": 8 * GIB, "ServerErrors": None})
STATS = "\n".join([
    json.dumps({"Name": "gaia-test-1", "MemUsage": "1GiB / 8GiB", "CPUPerc": "12.5%"}),
    json.dumps({"Name": "hermes", "MemUsage": "512MiB / 8GiB", "CPUPerc": "0.00%"}),
    "not json",
])


def state(used_gib, total_gib=8, cli=True, daemon=True):
    return {"cli": cli, "daemon": daemon, "memUsed": used_gib * GIB, "memTotal": total_gib * GIB}


class ParseTest(unittest.TestCase):
    def test_sizes(self):
        self.assertEqual(ds.parse_size("1.5GiB"), int(1.5 * GIB))
        self.assertEqual(ds.parse_size("512MB"), 512 * 1000 ** 2)
        self.assertEqual(ds.parse_size("0B"), 0)
        self.assertIsNone(ds.parse_size("--"))

    def test_info(self):
        self.assertEqual(ds.parse_info(INFO), {"serverVersion": "27.0.1", "memTotal": 8 * GIB, "errors": []})
        self.assertEqual(ds.parse_info("garbage"), {})

    def test_stats_skips_unreadable_lines(self):
        rows = ds.parse_stats(STATS)
        self.assertEqual([r["name"] for r in rows], ["gaia-test-1", "hermes"])
        self.assertEqual(rows[0]["memory"], GIB)
        self.assertEqual(rows[0]["cpu"], 12.5)


class ReadTest(unittest.TestCase):
    def read(self, info=(0, INFO, ""), stats=(0, STATS, ""), ps=(0, "a\nb\n", "")):
        with mock.patch.object(ds, "run_info", return_value=info), \
                mock.patch.object(ds, "run_stats", return_value=stats), \
                mock.patch.object(ds, "run_test_containers", return_value=ps):
            return ds.read()

    def test_running(self):
        s = self.read()
        self.assertTrue(s["cli"] and s["daemon"])
        self.assertEqual(s["memTotal"], 8 * GIB)
        self.assertEqual(s["memUsed"], GIB + 512 * 1024 ** 2)
        self.assertEqual(s["p4mTestContainers"], 2)
        self.assertEqual(ds.verdict(s), ds.OK)

    def test_missing_cli(self):
        with mock.patch.object(ds, "run_info", side_effect=ds.DockerMissing("no docker")):
            s = ds.read()
        self.assertFalse(s["cli"])
        self.assertEqual(ds.verdict(s), ds.DOWN)
        self.assertIn("CLI not found", ds.status_line(s, ds.DOWN))

    def test_unreachable_daemon(self):
        s = self.read(info=(1, "", "Cannot connect to the Docker daemon"))
        self.assertTrue(s["cli"])
        self.assertFalse(s["daemon"])
        self.assertIn("Cannot connect", s["error"])
        self.assertEqual(ds.verdict(s), ds.DOWN)

    def test_server_errors_mean_unreachable(self):
        info = json.dumps({"ServerVersion": "", "MemTotal": 0, "ServerErrors": ["context deadline exceeded"]})
        s = self.read(info=(0, info, ""))
        self.assertFalse(s["daemon"])
        self.assertIn("deadline", s["error"])


class VerdictTest(unittest.TestCase):
    def test_thresholds(self):
        self.assertEqual(ds.verdict(state(6.7), 85, 70), ds.OK)          # 83.75%
        self.assertEqual(ds.verdict(state(6.8), 85, 70), ds.PRESSURED)   # 85% exactly
        self.assertEqual(ds.verdict(state(8), 85, 70), ds.PRESSURED)

    def test_hysteresis(self):
        between = state(6)      # 75%: above ok (70), below high (85)
        self.assertEqual(ds.verdict(between, 85, 70, was=ds.OK), ds.OK)
        self.assertEqual(ds.verdict(between, 85, 70, was=ds.PRESSURED), ds.PRESSURED)
        self.assertEqual(ds.verdict(state(5), 85, 70, was=ds.PRESSURED), ds.OK)

    def test_default_ok_is_forty_percent_free(self):
        self.assertEqual(ds.DEFAULT_OK, 60)

    def test_container_tally_holds_dispatch(self):
        st = dict(state(1), p4mRunning=["a", "b"])
        self.assertEqual(ds.verdict(st, max_containers=3), ds.OK)
        self.assertEqual(ds.verdict(st, max_containers=2), ds.PRESSURED)
        self.assertEqual(ds.verdict(st, max_containers=0), ds.OK)

    def test_zero_total_is_not_pressured(self):
        self.assertEqual(ds.verdict(state(0, total_gib=0)), ds.OK)

    def test_status_line(self):
        s = {**state(2), "p4mTestContainers": 3}
        self.assertEqual(ds.status_line(s, ds.OK), "DOCKER  ok  memory 2.0GiB / 8.0GiB (25%)  test containers 3")


if __name__ == "__main__":
    unittest.main()
