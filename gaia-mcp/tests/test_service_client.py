"""A service's enrollment, sign-in and secret rotation (service_client.py) against a fake Gaia.
Run: python -m unittest discover -s tests (from dev-tools/gaia-mcp)"""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import server  # noqa: E402
import service_client  # noqa: E402
from fake_gaia import ENROLLMENT_TOKEN, USER_CODE, FakeGaia  # noqa: E402


def transport(fake):
    g = service_client.service_gaia("https://gaia.test/", "", "")
    g._http = fake.http
    return g


class EnrollmentTest(unittest.TestCase):
    def enrollment(self, fake):
        self.sleeps = []
        return service_client.Enrollment(transport(fake), sleep=self.sleeps.append, clock=lambda: 0)

    def test_a_valid_enrollment_token_delivers_at_once(self):
        fake = FakeGaia()
        enrollment = self.enrollment(fake)
        requested = enrollment.request("Talos", "runtime", ENROLLMENT_TOKEN)
        self.assertEqual("approved", requested["status"])
        self.assertEqual({"clientId": "svc-e-1", "clientSecret": "secret-1", "serviceClientId": "sc-e-1"},
                         enrollment.wait(requested))
        self.assertEqual([], self.sleeps)
        self.assertTrue(all(auth is False for _, _, auth in fake.calls), "enrollment calls are anonymous")

    def test_without_a_token_it_waits_for_an_admin_to_approve_the_code(self):
        fake = FakeGaia(approve_after_polls=2)
        enrollment = self.enrollment(fake)
        requested = enrollment.request("Talos", "runtime")
        self.assertEqual(("pending", USER_CODE), (requested["status"], requested["userCode"]))
        self.assertEqual("secret-1", enrollment.wait(requested, interval=3)["clientSecret"])
        self.assertEqual([3, 3, 3], self.sleeps)

    def test_an_invalid_token_is_refused_with_gaias_message(self):
        with self.assertRaisesRegex(server.GaiaError, "The enrollment token is not valid"):
            self.enrollment(FakeGaia()).request("Talos", "runtime", "nope")

    def test_too_many_requests_says_so(self):
        g = transport(FakeGaia())
        g._http = lambda *a, **k: (429, {})
        with self.assertRaisesRegex(server.GaiaError, "too many requests"):
            service_client.Enrollment(g).request("Talos", "runtime")

    def test_a_denied_request_fails(self):
        enrollment = self.enrollment(FakeGaia(approve_after_polls=0, deny=True))
        with self.assertRaisesRegex(server.GaiaError, "denied"):
            enrollment.wait(enrollment.request("Talos", "runtime"))

    def test_an_expired_request_fails(self):
        fake = FakeGaia()
        enrollment = self.enrollment(fake)
        requested = enrollment.request("Talos", "runtime")
        fake.enrollments["e-1"]["status"] = "expired"
        with self.assertRaisesRegex(server.GaiaError, "expired"):
            enrollment.wait(requested)

    def test_a_second_delivery_has_no_secret(self):
        enrollment = self.enrollment(FakeGaia())
        requested = enrollment.request("Talos", "runtime", ENROLLMENT_TOKEN)
        enrollment.wait(requested)
        with self.assertRaisesRegex(server.GaiaError, "already delivered"):
            enrollment.wait(requested)

    def test_a_few_failed_polls_are_retried(self):
        fake = FakeGaia()
        enrollment = self.enrollment(fake)
        requested = enrollment.request("Talos", "runtime", ENROLLMENT_TOKEN)
        real, blips = fake.http, iter([(502, {}), TimeoutError("read timed out")])

        def flaky(*a, **k):
            blip = next(blips, None)
            if isinstance(blip, Exception):
                raise blip
            return blip or real(*a, **k)
        enrollment.gaia._http = flaky
        self.assertEqual("secret-1", enrollment.wait(requested)["clientSecret"])
        self.assertEqual(2, len(self.sleeps))

    def test_polls_that_keep_failing_give_up(self):
        enrollment = self.enrollment(FakeGaia())
        requested = enrollment.request("Talos", "runtime")
        enrollment.gaia._http = lambda *a, **k: (503, {})
        with self.assertRaisesRegex(server.GaiaError, "503"):
            enrollment.wait(requested)
        self.assertEqual(service_client.POLL_RETRIES - 1, len(self.sleeps))

    def test_an_unknown_request_is_not_retried(self):
        enrollment = self.enrollment(FakeGaia())
        with self.assertRaisesRegex(server.GaiaError, "does not know"):
            enrollment.wait({"id": "e-9", "pollToken": "x"})
        self.assertEqual([], self.sleeps)

    def test_waiting_gives_up_after_max_wait(self):
        clock = iter(range(0, 1000, 10))
        enrollment = service_client.Enrollment(transport(FakeGaia()), sleep=lambda s: None,
                                               clock=lambda: next(clock))
        with self.assertRaisesRegex(server.GaiaError, "Gave up"):
            enrollment.wait(enrollment.request("Talos", "runtime"), max_wait=30)


class SignInAndRotationTest(unittest.TestCase):
    def setUp(self):
        self.fake = FakeGaia()
        self.fake.secrets["svc-e-1"] = ["secret-1"]
        self.g = service_client.service_gaia("https://gaia.test", "svc-e-1", "secret-1")
        self.g._http = self.fake.http

    def test_a_service_signs_in_with_client_credentials_and_no_user(self):
        self.assertEqual("tok-1", self.g.token())
        self.assertEqual(("", ""), (self.g.email, self.g.password))

    def test_an_expired_token_is_replaced_by_a_new_grant(self):
        self.g.token()
        self.g.expires_at = 0
        self.assertEqual("tok-2", self.g.token())

    def test_rotation_returns_the_new_secret_once_and_it_signs_in(self):
        rotated = service_client.rotate_secret(self.g, "sc-e-1")
        self.assertEqual({"clientId": "svc-e-1", "clientSecret": "secret-2",
                          "previousSecretExpiresAt": "2026-11-09 12:10:00"}, rotated)
        g = service_client.service_gaia("https://gaia.test", "svc-e-1", "secret-2")
        g._http = self.fake.http
        self.assertTrue(g.token())


if __name__ == "__main__":
    unittest.main()
