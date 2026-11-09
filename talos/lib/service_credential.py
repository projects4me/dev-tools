"""Talos' service credential on disk: `<workspace>/dev-tools/talos/state/service-client.json`, git-ignored and
readable only by its owner. It holds the Gaia URL, client id and secret, the Serviceclient id and when the secret
was issued. Never log its content."""

import getpass
import json
import os
import subprocess

from .config import STATE_DIR, TalosError

CREDENTIAL_FILE = STATE_DIR / "service-client.json"


def owner_account():
    """The account icacls grants: DOMAIN\\user when Windows says the domain (also a machine or Microsoft account)."""
    user = os.environ.get("USERNAME") or getpass.getuser()
    domain = os.environ.get("USERDOMAIN")
    return f"{domain}\\{user}" if domain else user


def restrict_to_owner(path):
    """Only the current user may read or write `path`."""
    if os.name == "nt":
        # Drop inherited entries and grant the owner alone full control.
        result = subprocess.run(["icacls", str(path), "/inheritance:r", "/grant:r", f"{owner_account()}:(F)"],
                                stdin=subprocess.DEVNULL, capture_output=True, text=True)
        if result.returncode != 0:
            raise OSError(f"icacls failed: {(result.stderr or result.stdout).strip()}")
    else:
        os.chmod(path, 0o600)


class CredentialStore:
    """The service credential for one Gaia (a credential for another URL counts as none)."""

    def __init__(self, path=CREDENTIAL_FILE, restrict=restrict_to_owner):
        self.path = path
        self.restrict = restrict

    def load(self, gaia_url):
        if not self.path.exists():
            return None
        try:
            credential = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as e:
            raise TalosError(f"cannot read the service credential {self.path}: {e}")
        if credential.get("gaiaUrl") != gaia_url or not credential.get("clientSecret"):
            return None
        return credential

    def save(self, credential):
        """Write the credential atomically. The temp file is restricted before the secret goes into it (its 0o600
        mode only counts on POSIX; on Windows `restrict` sets the ACL, which the rename keeps). Returns None, or why
        the file could not be restricted: the credential is kept anyway, because Gaia delivers a secret only once."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_name(self.path.name + ".tmp")
        try:
            os.close(os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600))
            try:
                self.restrict(tmp)
                problem = None
            except OSError as e:
                problem = str(e)
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(credential, f, indent=2)
            os.replace(tmp, self.path)
        except OSError as e:
            raise TalosError(f"cannot write the service credential {self.path}: {e}")
        return problem
