"""
A service's own access to Gaia (rbac decisions D-062, D-066, D-068, D-070): enroll without a hand-copied secret,
sign in with the delivered client credentials, and rotate that secret.

- Enrollment: an anonymous POST /serviceenrollment (name, kind and, for an unattended deploy, a single-use
  `enrollmentToken`) returns a user code and a poll token; anonymous polls of GET /serviceenrollment/{id}?pollToken=
  return the client id and secret once, after an admin approves the code (or at once with a valid token).
- Sign-in: a `service_gaia()` is a gaia-mcp `Gaia` on the client_credentials grant; it asks for a new token when the
  current one is about to expire (service tokens have no refresh token).
- Agents (D-067, D-069): a runtime lists the agents it may run (GET /agent answers a runtime token with the delegable
  ones only) and gets a token acting as one of them with the agent-delegation grant (one hour, no refresh token).
- Rotation: PATCH /serviceclient/{id} with only `rotateSecret: true`, sent with the service's own token; the new
  secret comes back once and the previous one stays valid for a short grace period.

Never log a secret or a poll token: the errors here carry Gaia's message, never the request.
"""

import time

from server import NOT_JSON, Gaia, GaiaError, page_text

PENDING = ("pending", "approved")       # still waiting: approved is delivered by the next poll
POLL_SECONDS = 5
DELEGATION_GRANT = "urn:projects4me:params:oauth:grant-type:agent-delegation"
MAX_WAIT_SECONDS = 20 * 60              # a request expires after 15 minutes; this only stops a runaway loop
POLL_RETRIES = 5                        # failed polls in a row (network, 5xx) before giving up


class DelegationRefused(GaiaError):
    """Gaia refused a delegated token: the runtime may not delegate, or the agent can't be delegated (any more)."""


def service_gaia(base, client_id, client_secret, ssl_context=None):
    """A Gaia client signed in as the service: client_credentials, no user."""
    g = Gaia()
    g.base = base.rstrip("/")
    g.email, g.password = "", ""
    g.client_id, g.client_secret = client_id, client_secret
    if ssl_context is not None:
        g.ssl = ssl_context
    return g


def _error_text(status, payload):
    if NOT_JSON in payload:
        return f"{status}, no JSON reply: {page_text(payload[NOT_JSON])}"
    errors = payload.get("errors")
    if isinstance(errors, list) and errors and isinstance(errors[0], dict):
        return f"{status}: {errors[0].get('detail') or errors[0].get('title') or errors[0]}"
    return f"{status}: {payload.get('message') or payload.get('error_description') or payload.get('error') or payload}"


def _attributes(payload):
    data = payload.get("data") or {}
    return dict(data.get("attributes") or {}, id=data.get("id"))


class Enrollment:
    """One service's enrollment through anonymous calls on a gaia-mcp `Gaia`."""

    def __init__(self, gaia, sleep=None, clock=None):
        self.gaia = gaia
        self.sleep = sleep or time.sleep
        self.clock = clock or time.monotonic

    def request(self, name, kind, enrollment_token=None):
        """Make the request. Returns its id, userCode, expiresAt, status and pollToken (shown once)."""
        attributes = {"name": name, "kind": kind}
        if enrollment_token:
            attributes["enrollmentToken"] = enrollment_token
        status, payload = self.gaia.anonymous("POST", "serviceenrollment",
                                              body={"data": {"type": "serviceenrollment", "attributes": attributes}})
        if status == 429:
            raise GaiaError("Gaia refused the enrollment request: too many requests from this address; "
                            "try again in a few minutes.", status)
        if status != 201:
            raise GaiaError(f"Gaia refused the enrollment request ({_error_text(status, payload)})", status)
        requested = _attributes(payload)
        if not requested.get("id") or not requested.get("pollToken"):
            raise GaiaError("Gaia's enrollment reply has no request id or poll token.")
        return requested

    def poll(self, enrollment_id, poll_token):
        """One poll: the request's attributes, with clientId and clientSecret the one time they are delivered."""
        status, payload = self.gaia.anonymous("GET", f"serviceenrollment/{enrollment_id}",
                                              params={"pollToken": poll_token})
        if status == 404:
            raise GaiaError("Gaia does not know this enrollment request (wrong id or poll token).", status)
        if status != 200:
            raise GaiaError(f"Gaia enrollment poll failed ({_error_text(status, payload)})", status)
        return _attributes(payload)

    def wait(self, requested, interval=POLL_SECONDS, max_wait=MAX_WAIT_SECONDS):
        """Poll until the secret is delivered. Returns {clientId, clientSecret, serviceClientId (None when Gaia
        doesn't send it)}; raises GaiaError when the request is denied, expires or was delivered before."""
        deadline = self.clock() + max_wait
        failures = 0
        while True:
            try:
                polled = self.poll(requested["id"], requested["pollToken"])
                failures = 0
            except (GaiaError, OSError) as e:     # OSError: a read timeout gaia-mcp's transport lets through
                # A network blip or a crashed request must not waste an approval or a single-use token.
                failures += 1
                if getattr(e, "status", None) == 404 or failures >= POLL_RETRIES or self.clock() >= deadline:
                    raise
                self.sleep(interval)
                continue
            state = polled.get("status")
            if state == "delivered" and polled.get("clientSecret"):
                return {"clientId": polled.get("clientId"), "clientSecret": polled["clientSecret"],
                        "serviceClientId": polled.get("serviceClientId")}
            if state == "delivered":
                raise GaiaError("This enrollment was already delivered and its secret can't be read again; "
                                "enroll again.")
            if state == "denied":
                raise GaiaError("An admin denied the enrollment request.")
            if state not in PENDING:
                raise GaiaError(f"The enrollment request is {state or 'in an unknown state'}; enroll again.")
            if self.clock() >= deadline:
                raise GaiaError("Gave up waiting for the enrollment request to be approved; enroll again.")
            self.sleep(interval)


def rotate_secret(gaia, service_client_id):
    """Rotate the signed-in service's own secret. Returns {clientId, clientSecret, previousSecretExpiresAt}."""
    payload = gaia.request("PATCH", f"serviceclient/{service_client_id}", body={"data": {
        "type": "serviceclient", "id": service_client_id, "attributes": {"rotateSecret": True}}})
    rotated = _attributes(payload)
    if not rotated.get("clientSecret"):
        raise GaiaError("Gaia's rotation reply has no new secret.")
    return {"clientId": rotated.get("clientId"), "clientSecret": rotated["clientSecret"],
            "previousSecretExpiresAt": rotated.get("previousSecretExpiresAt")}


def delegable_agents(gaia):
    """The agents the signed-in runtime may run (id, name, userId, ...): Gaia filters the list for a runtime token."""
    return gaia.find("agent", limit=-1)


def delegated_token(gaia, agent_id):
    """A token acting as the agent, got with the runtime's own client credentials (the agent-delegation grant).
    Returns {accessToken, expiresIn, scopes}; raises DelegationRefused when Gaia says no (400, 401, 403)."""
    status, payload = gaia.anonymous("POST", "token", form={
        "grant_type": DELEGATION_GRANT, "agent_id": agent_id,
        "client_id": gaia.client_id, "client_secret": gaia.client_secret})
    if status in (400, 401, 403):
        raise DelegationRefused(f"Gaia refused a token for agent {agent_id} ({_error_text(status, payload)})", status)
    if status != 200 or not payload.get("access_token"):
        raise GaiaError(f"Gaia's delegation grant failed ({_error_text(status, payload)})", status)
    return {"accessToken": payload["access_token"], "expiresIn": int(payload.get("expires_in") or 3600),
            "scopes": str(payload.get("scope") or "").split()}
