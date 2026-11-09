"""Delegated tokens for the Gaia agents Talos runs (D-067): Talos signs in as its own service client
(service_identity.py) and gets a token acting as each agent through the agent-delegation grant. Gaia gives a
one-hour token and no refresh token, so the cache gets a new one about 5 minutes before it expires. Each agent's
token is also written to an owner-only file that its runs' child processes (the gaia MCP server, `ai`) read through
GAIA_TOKEN_FILE on every call. Never log a token."""

import os
import sys
import threading
import time

from .config import GAIA_MCP, TalosError
from .service_credential import restrict_to_owner

sys.path.insert(0, str(GAIA_MCP))
import server as gaia_mcp  # noqa: E402  (dev-tools/gaia-mcp/server.py)
import service_client  # noqa: E402  (dev-tools/gaia-mcp/service_client.py)

RENEW_BEFORE_SECONDS = 300
REPLACE_TRIES = 5           # a reader holding the file open makes the rename fail on Windows for a moment


class Refused(TalosError):
    """Gaia refused the agent's token: it is disabled, its delegation was emptied, or Talos may not delegate."""


class TokenCache:
    """Delegated tokens by Gaia agent id. Thread-safe: an agent's workers, listener and the renewal loop share it."""

    def __init__(self, grant, clock=time.time, renew_before=RENEW_BEFORE_SECONDS):
        self.grant = grant              # grant(agent_id) -> {"accessToken", "expiresIn"}, as service_client returns
        self.clock = clock
        self.renew_before = renew_before
        self._tokens = {}               # agent id -> (token, expires at, scope names)
        self._lock = threading.Lock()

    @classmethod
    def for_identity(cls, identity):
        """Tokens granted to Talos' service client (a ServiceIdentity)."""
        return cls(lambda agent_id: service_client.delegated_token(identity.gaia(), agent_id))

    def token(self, agent_id):
        """The agent's token, renewed when it expires within `renew_before` seconds. Raises Refused when Gaia says
        no (the cached token is dropped); any other failure keeps a token that has not expired yet."""
        with self._lock:
            cached = self._tokens.get(agent_id)
            now = self.clock()
            if cached and now < cached[1] - self.renew_before:
                return cached[0]
            try:
                granted = self.grant(agent_id)
            except service_client.DelegationRefused as e:
                self._tokens.pop(agent_id, None)
                raise Refused(str(e))
            except (gaia_mcp.GaiaError, TalosError, OSError) as e:
                if cached and now < cached[1]:
                    return cached[0]        # Gaia unreachable for a moment: the next call tries again
                raise TalosError(f"no delegated token for agent {agent_id}: {e}")
            self._tokens[agent_id] = (granted["accessToken"], now + granted["expiresIn"],
                                      list(granted.get("scopes") or []))
            return granted["accessToken"]

    def fresh(self, agent_id):
        """A newly granted token, whatever the cache holds: the agent's delegation scopes may have changed since the
        cached token was issued (Gaia fixes a token's scopes when it issues it). Returns the scope names it carries."""
        self.forget(agent_id)
        self.token(agent_id)
        return self.scopes(agent_id)

    def scopes(self, agent_id):
        """The scope names the agent's cached token carries (names only)."""
        with self._lock:
            cached = self._tokens.get(agent_id)
            return list(cached[2]) if cached else []

    def forget(self, agent_id):
        with self._lock:
            self._tokens.pop(agent_id, None)


class AgentToken:
    """One agent's token as a gaia-mcp `token_source`: the cached token, written to `path` whenever it changes."""

    def __init__(self, cache, agent_id, path, restrict=restrict_to_owner):
        self.cache = cache
        self.agent_id = agent_id
        self.path = path
        self.restrict = restrict
        self._written = None
        self._closed = False
        self._lock = threading.Lock()

    def __call__(self):
        if self._closed:                # a run still in progress after its agent stopped gets no new token
            raise Refused(f"agent {self.agent_id} has stopped")
        token = self.cache.token(self.agent_id)
        with self._lock:
            if token != self._written or not self.path.exists():
                write_token(self.path, token, self.restrict)
                self._written = token
        return token

    def fresh(self):
        """Replace the token with a newly granted one (written to the file); returns the scope names it carries."""
        scopes = self.cache.fresh(self.agent_id)
        self()
        return scopes

    def drop(self):
        """Forget the cached token: the next call gets a new grant (the agent's delegation scopes changed)."""
        self.cache.forget(self.agent_id)

    def close(self):
        """The agent stopped: drop its cached token and grant no new one. The file keeps the last token, so a run in
        progress can go on until that token expires (at most an hour); nothing renews it after that."""
        self._closed = True
        self.cache.forget(self.agent_id)


def write_token(path, token, restrict=restrict_to_owner):
    """Write the token atomically, owner-only before it holds the token (as service_credential.CredentialStore).
    The file sits alone in its folder, which a container run mounts: a rename shows there, a file mount would not."""
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    try:
        os.close(os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600))
        restrict(tmp)
        with open(tmp, "w", encoding="utf-8") as f:
            f.write(token)
        for attempt in range(REPLACE_TRIES):
            try:
                os.replace(tmp, path)
                return
            except PermissionError:
                if attempt == REPLACE_TRIES - 1:
                    raise
                time.sleep(0.1)
    except OSError as e:
        raise TalosError(f"cannot write the delegated token file {path}: {e}")
    finally:
        if tmp.exists():                # a failed write never leaves a copy of the token behind
            try:
                tmp.unlink()
            except OSError:
                pass
