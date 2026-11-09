"""Talos as a runtime service client (service_identity.py) against a fake Gaia: both enrollment paths, a restart
with the stored credential, token refresh, weekly rotation and the credential file."""

import json
import os
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from lib import service_credential, service_identity  # noqa: E402
from lib.config import GAIA_MCP, TalosError  # noqa: E402

sys.path.insert(0, str(GAIA_MCP / "tests"))
from fake_gaia import ENROLLMENT_TOKEN, USER_CODE, FakeGaia  # noqa: E402

URL = "https://gaia.test"
WEEK = service_identity.ROTATE_AFTER_SECONDS


class Clock:
    def __init__(self, now=1_000_000.0):
        self.now = now

    def __call__(self):
        return self.now


class ServiceIdentityTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.restricted = []
        self.store = service_credential.CredentialStore(Path(tmp.name) / "service-client.json",
                                                      restrict=self.restricted.append)
        self.fake = FakeGaia()
        self.clock = Clock()
        self.said = []
        # Every gaia-mcp client (the anonymous transport and the signed-in service) talks to the fake.
        patch = mock.patch.object(service_identity.gaia_mcp.Gaia, "_http",
                                  new=lambda g, *a, **k: self.fake.http(*a, **k))
        patch.start()
        self.addCleanup(patch.stop)
        sleeps = mock.patch.object(service_identity.service_client.time, "sleep", new=lambda s: None)
        sleeps.start()
        self.addCleanup(sleeps.stop)
        logs = mock.patch.object(service_identity, "log")
        self.log = logs.start()
        self.addCleanup(logs.stop)

    def identity(self, token=None):
        return service_identity.ServiceIdentity(URL, store=self.store, name="Talos on test", enrollment_token=token,
                                                announce=self.said.append, clock=self.clock)

    def stored(self):
        return json.loads(self.store.path.read_text(encoding="utf-8"))

    def test_enrollment_token_path_stores_the_delivered_credential(self):
        credential = self.identity(ENROLLMENT_TOKEN).ensure()
        self.assertEqual({"gaiaUrl": URL, "name": "Talos on test", "kind": "runtime", "clientId": "svc-e-1",
                          "clientSecret": "secret-1", "serviceClientId": "sc-e-1", "issuedAt": self.clock.now},
                         self.stored())
        self.assertEqual(credential, self.stored())
        self.assertEqual("runtime", self.fake.enrollments["e-1"]["kind"])

    def test_user_code_path_prints_the_code_and_waits_for_approval(self):
        self.fake.approve_after_polls = 1
        self.identity().ensure()
        self.assertIn(USER_CODE, self.said[0])
        self.assertEqual("secret-1", self.stored()["clientSecret"])

    def test_messages_never_carry_the_secret_or_poll_token(self):
        self.fake.approve_after_polls = 1
        self.identity().ensure()
        self.identity().token()
        printed = " ".join(self.said) + " ".join(str(c) for c in self.log.call_args_list)
        self.assertNotIn("secret-1", printed)
        self.assertNotIn("poll-e-1", printed)

    def test_a_restart_uses_the_stored_credential_without_enrolling(self):
        self.identity(ENROLLMENT_TOKEN).ensure()
        self.fake.calls.clear()
        restarted = self.identity()
        self.assertEqual("svc-e-1", restarted.ensure()["clientId"])
        self.assertEqual("tok-1", restarted.token())
        self.assertEqual([("POST", "token", False)], self.fake.calls)

    def test_a_credential_for_another_gaia_counts_as_none(self):
        self.identity(ENROLLMENT_TOKEN).ensure()
        self.assertIsNone(self.store.load("https://other.test"))

    def test_a_refused_enrollment_is_a_clear_error_and_stores_nothing(self):
        with self.assertRaisesRegex(TalosError, "enrollment failed: .*not valid"):
            self.identity("wrong").ensure()
        self.assertFalse(self.store.path.exists())

    def test_not_enrolled_says_how_to_enroll(self):
        with self.assertRaisesRegex(TalosError, "talos enroll"):
            self.identity().token()

    def test_an_expired_token_is_refreshed_with_a_new_grant(self):
        identity = self.identity(ENROLLMENT_TOKEN)
        identity.ensure()
        self.assertEqual("tok-1", identity.token())
        identity.gaia().expires_at = 0
        self.assertEqual("tok-2", identity.token())

    def test_rotation_waits_a_week(self):
        identity = self.identity(ENROLLMENT_TOKEN)
        identity.ensure()
        self.clock.now += WEEK - 1
        self.assertFalse(identity.rotate_if_due())
        self.assertEqual(0, self.fake.rotations)

    def test_a_week_old_secret_is_rotated_stored_and_used(self):
        identity = self.identity(ENROLLMENT_TOKEN)
        identity.ensure()
        identity.token()
        self.clock.now += WEEK
        self.assertTrue(identity.rotate_if_due())
        self.assertEqual(("secret-2", self.clock.now), (self.stored()["clientSecret"], self.stored()["issuedAt"]))
        self.fake.secrets["svc-e-1"] = ["secret-2"]        # the grace period is over
        identity.gaia().expires_at = 0
        self.assertTrue(identity.token())
        self.assertEqual("secret-2", self.identity().gaia().client_secret)

    def test_rotation_without_a_serviceclient_id_warns_once(self):
        self.fake.send_serviceclient_id = False
        identity = self.identity(ENROLLMENT_TOKEN)
        identity.ensure()
        self.clock.now += WEEK
        self.assertFalse(identity.rotate_if_due())
        self.assertFalse(identity.rotate_if_due())
        self.assertEqual(1, self.log.call_count)
        self.assertEqual(0, self.fake.rotations)

    def test_the_credential_file_is_restricted_before_the_secret_is_written(self):
        self.identity(ENROLLMENT_TOKEN).ensure()
        self.assertEqual([self.store.path.with_name("service-client.json.tmp")], self.restricted)
        self.assertFalse(self.restricted[0].exists())
        if os.name != "nt":
            self.assertEqual(0o600, self.store.path.stat().st_mode & 0o777)

    def test_a_failed_restriction_keeps_the_credential_and_says_so(self):
        def refuse(path):
            raise OSError("no")
        self.store.restrict = refuse
        self.identity(ENROLLMENT_TOKEN).ensure()
        self.assertEqual("secret-1", self.stored()["clientSecret"])
        self.log.assert_called_once()
        self.assertTrue(any("could not be restricted" in said for said in self.said))

    def test_a_rotated_secret_the_store_refused_is_kept_and_saved_on_the_next_check(self):
        identity = self.identity(ENROLLMENT_TOKEN)
        identity.ensure()
        self.clock.now += WEEK
        save = self.store.save
        with mock.patch.object(self.store, "save", side_effect=TalosError("disk full")):
            with self.assertRaisesRegex(TalosError, "disk full"):
                identity.rotate_if_due()
        self.assertEqual("secret-1", self.stored()["clientSecret"])
        self.assertEqual("secret-2", identity.credential()["clientSecret"])
        self.store.save = save
        self.assertFalse(identity.rotate_if_due())
        self.assertEqual("secret-2", self.stored()["clientSecret"])
        self.assertEqual(1, self.fake.rotations)

    def test_only_one_process_enrolls_at_a_time(self):
        lock = self.store.path.with_name("service-client.json.lock")
        with service_identity.filelock.held(lock, wait_seconds=0):
            with self.assertRaisesRegex(TalosError, "another Talos process is enrolling"):
                self.identity(ENROLLMENT_TOKEN).ensure()
        self.assertEqual({}, self.fake.enrollments)

    def test_the_keeper_logs_any_failure_and_stops(self):
        stop = threading.Event()
        identity = self.identity()

        def down():
            stop.set()
            raise TimeoutError("down")
        identity.ensure = down
        service_identity.keep(identity, stop, interval=0)
        self.log.assert_called_once_with("service identity: down")

    def test_settings_need_a_gaia_url_and_read_the_enrollment_token(self):
        with self.assertRaisesRegex(TalosError, "gaiaUrl"):
            service_identity.ServiceIdentity.from_settings({})
        with mock.patch.dict(os.environ, {service_identity.ENROLLMENT_TOKEN_ENV: "tok"}):
            identity = service_identity.ServiceIdentity.from_settings({"gaiaUrl": URL + "/"})
        self.assertEqual((URL, "tok"), (identity.gaia_url, identity.enrollment_token))


class RestrictToOwnerTest(unittest.TestCase):
    def test_windows_strips_inheritance_and_grants_the_owner(self):
        path = Path("f.json")
        with mock.patch.object(service_credential.os, "name", "nt"), \
                mock.patch.dict(service_credential.os.environ, {"USERNAME": "me", "USERDOMAIN": "BOX"}), \
                mock.patch.object(service_credential.subprocess, "run") as run:
            run.return_value.returncode = 0
            service_credential.restrict_to_owner(path)
        self.assertEqual(["icacls", str(path), "/inheritance:r", "/grant:r", "BOX\\me:(F)"], run.call_args.args[0])

    def test_windows_failure_is_an_os_error(self):
        with mock.patch.object(service_credential.os, "name", "nt"), \
                mock.patch.object(service_credential.subprocess, "run") as run:
            run.return_value.returncode, run.return_value.stderr = 1, "denied"
            with self.assertRaisesRegex(OSError, "denied"):
                service_credential.restrict_to_owner(Path("f.json"))

    def test_posix_is_owner_read_write_only(self):
        path = Path("f.json")
        with mock.patch.object(service_credential.os, "name", "posix"), \
                mock.patch.object(service_credential.os, "chmod") as chmod:
            service_credential.restrict_to_owner(path)
        chmod.assert_called_once_with(path, 0o600)


if __name__ == "__main__":
    unittest.main()
