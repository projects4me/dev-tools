"""Talos as a registered runtime in Gaia: enroll once (no hand-copied secret), keep the service credential
(service_credential.py), sign in with it, and rotate its secret weekly. All Gaia calls go through gaia-mcp's
service_client. Never log or print the secret or the poll token."""

import os
import socket
import sys
import threading
import time

from .config import DEV_TOOLS, GAIA_MCP, TalosError, log
from .service_credential import CredentialStore

sys.path.insert(0, str(GAIA_MCP))
sys.path.insert(0, str(DEV_TOOLS / "installer"))
import filelock  # noqa: E402  (dev-tools/installer/filelock.py)
import server as gaia_mcp  # noqa: E402  (dev-tools/gaia-mcp/server.py)
import service_client  # noqa: E402  (dev-tools/gaia-mcp/service_client.py)

KIND = "runtime"
ENROLLMENT_TOKEN_ENV = "TALOS_ENROLLMENT_TOKEN"
ROTATE_AFTER_SECONDS = 7 * 24 * 3600
CHECK_SECONDS = 3600


def default_name():
    return f"Talos on {socket.gethostname()}"


class ServiceIdentity:
    """Talos' service client in Gaia: enrollment, a signed-in client, weekly secret rotation."""

    def __init__(self, gaia_url, store=None, name=None, enrollment_token=None, announce=print, clock=time.time,
                 ssl_context=None):
        self.gaia_url = gaia_url.rstrip("/")
        self.store = store or CredentialStore()
        self.name = name or default_name()
        self.enrollment_token = enrollment_token
        self.announce = announce
        self.clock = clock
        self.ssl_context = ssl_context
        self._gaia = None
        self._unsaved = None            # a rotated credential the store refused; saved again on the next check
        self._warned = False
        self._lock = threading.RLock()

    @classmethod
    def from_settings(cls, cfg, announce=print):
        """Talos' identity for the Gaia in its settings (`gaiaUrl`), with TALOS_ENROLLMENT_TOKEN when set."""
        url = cfg.get("gaiaUrl")
        if not url:
            raise TalosError("gaiaUrl is not set in dev-tools/talos/config.json")
        return cls(url, enrollment_token=os.environ.get(ENROLLMENT_TOKEN_ENV) or None, announce=announce)

    def credential(self):
        return self._unsaved or self.store.load(self.gaia_url)

    def ensure(self):
        """The stored credential, or a new one from enrollment (waits for an admin unless a token is set). One
        process enrolls at a time (a lock file next to the credential), so two never spend two approvals."""
        credential = self.credential()
        if credential:
            return credential
        lock = self.store.path.with_name(self.store.path.name + ".lock")
        self.store.path.parent.mkdir(parents=True, exist_ok=True)
        try:
            with filelock.held(lock, wait_seconds=0):
                return self.credential() or self.enroll()
        except filelock.LockTimeout:
            raise TalosError(f"another Talos process is enrolling ({filelock.describe_holder(lock)})")

    def enroll(self):
        enrollment = service_client.Enrollment(service_client.service_gaia(self.gaia_url, "", "", self.ssl_context))
        try:
            requested = enrollment.request(self.name, KIND, self.enrollment_token)
            if self.enrollment_token:
                self.announce(f"enrolling '{self.name}' in {self.gaia_url} with {ENROLLMENT_TOKEN_ENV}")
            else:
                self.announce(f"enrollment requested: ask a Gaia admin to approve code {requested.get('userCode')} "
                              f"for '{self.name}' in Prometheus before {requested.get('expiresAt')}; waiting...")
            delivered = enrollment.wait(requested)
        except gaia_mcp.GaiaError as e:
            raise TalosError(f"enrollment failed: {e}")
        credential = {"gaiaUrl": self.gaia_url, "name": self.name, "kind": KIND,
                      "clientId": delivered["clientId"], "clientSecret": delivered["clientSecret"],
                      "serviceClientId": delivered.get("serviceClientId"), "issuedAt": self.clock()}
        with self._lock:
            self._gaia = None
            self._save(credential)
        self.announce(f"enrolled as service client {credential['clientId']}")
        return credential

    def _save(self, credential):
        """Store the credential; when the store fails, keep it in memory (the secret can't be fetched again)."""
        try:
            problem = self.store.save(credential)
        except TalosError:
            self._unsaved = credential
            raise
        self._unsaved = None
        if problem:
            message = (f"service credential: {self.store.path} could not be restricted to its owner ({problem}); "
                       f"restrict it by hand")
            log(message)
            if self.announce is not log:
                self.announce(message)

    def gaia(self):
        """A gaia-mcp client signed in as Talos' service client; it gets a new token when the current one expires."""
        with self._lock:
            if self._gaia is None:
                credential = self.credential()
                if not credential:
                    raise TalosError("Talos is not enrolled in Gaia: run `talos enroll`")
                self._gaia = service_client.service_gaia(self.gaia_url, credential["clientId"],
                                                         credential["clientSecret"], self.ssl_context)
            return self._gaia

    def token(self):
        try:
            return self.gaia().token()
        except gaia_mcp.GaiaError as e:
            raise TalosError(f"service sign-in failed: {e}")

    def rotate_if_due(self):
        """Rotate the secret when it is a week old. Returns True when it rotated."""
        with self._lock:
            if self._unsaved:
                self._save(self._unsaved)
            credential = self.credential()
            if not credential or self.clock() - credential.get("issuedAt", 0) < ROTATE_AFTER_SECONDS:
                return False
            if not credential.get("serviceClientId"):
                if not self._warned:
                    self._warned = True
                    log("service credential: due for rotation, but Gaia did not say which service client it is; "
                        "enroll again to rotate")
                return False
            try:
                rotated = service_client.rotate_secret(self.gaia(), credential["serviceClientId"])
            except gaia_mcp.GaiaError as e:
                raise TalosError(f"secret rotation failed: {e}")
            self.gaia().client_secret = rotated["clientSecret"]
            # Store at once: the previous secret only lasts a short grace period.
            self._save(dict(credential, clientSecret=rotated["clientSecret"], issuedAt=self.clock()))
            log("service credential: secret rotated")
            return True


def keep(identity, stop, interval=CHECK_SECONDS):
    """For `talos run`: enroll when there is no credential, then check for rotation every `interval` seconds until
    `stop` is set. Failures are logged and retried on the next check; they never stop Talos or this loop."""
    while not stop.is_set():
        try:
            identity.ensure()
            identity.rotate_if_due()
        except Exception as e:          # noqa: BLE001  (a network or disk error must not end weekly rotation)
            log(f"service identity: {e}")
        stop.wait(interval)


def start_keeper(identity):
    stop = threading.Event()
    threading.Thread(target=keep, args=(identity, stop), name="service-identity", daemon=True).start()
    return stop
