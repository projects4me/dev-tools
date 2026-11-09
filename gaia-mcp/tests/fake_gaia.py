"""A fake Gaia for the service-client endpoints (enrollment, client_credentials token, secret rotation, delegable
agents, their specs and the agent-delegation grant), answering
gaia-mcp's `Gaia._http(method, path, params, body, form, auth)`. Shared by the gaia-mcp and Talos tests."""

ENROLLMENT_TOKEN = "enroll-token-ok"
USER_CODE = "ABC-DEF"


class FakeGaia:
    def __init__(self, approve_after_polls=None, deny=False, send_serviceclient_id=True):
        self.approve_after_polls = approve_after_polls   # pending polls before an admin approves; None: never
        self.deny = deny
        self.send_serviceclient_id = send_serviceclient_id
        self.secrets = {}           # client id -> secrets Gaia accepts
        self.enrollments = {}
        self.calls = []
        self.grants = 0
        self.rotations = 0
        self.agents = {}            # agent id -> {"name", "userId", "spec", "delegable" (default True)}
        self.delegations = 0

    def approve(self, enrollment_id="e-1"):
        self.enrollments[enrollment_id]["status"] = "approved"

    def http(self, method, path, params=None, body=None, form=None, auth=True):
        self.calls.append((method, path, auth))
        if path == "token":
            return self._token(form)
        if method == "POST" and path == "serviceenrollment":
            return self._request(body["data"]["attributes"])
        if method == "GET" and path.startswith("serviceenrollment/"):
            return self._poll(path.split("/", 1)[1], (params or {}).get("pollToken"))
        if method == "PATCH" and path.startswith("serviceclient/"):
            return self._rotate(path.split("/", 1)[1], body)
        if method == "GET" and path == "agent":
            return 200, {"data": [{"type": "agent", "id": i, "attributes": {"name": a["name"], "userId": a["userId"]}}
                                  for i, a in self.agents.items() if a.get("delegable", True)]}
        if method == "GET" and path.startswith("agentspec/"):
            agent = self.agents.get(path.split("/", 1)[1])
            if not agent or not agent.get("delegable", True):
                return 404, {"errors": [{"detail": "not found"}]}
            return 200, agent["spec"]
        return 404, {"errors": [{"detail": "not found"}]}

    def _token(self, form):
        if form.get("grant_type") == "urn:projects4me:params:oauth:grant-type:agent-delegation":
            return self._delegate(form)
        if form.get("grant_type") != "client_credentials" or \
                form.get("client_secret") not in self.secrets.get(form.get("client_id"), ()):
            return 400, {"error": "invalid_client"}
        self.grants += 1
        return 200, {"access_token": f"tok-{self.grants}", "expires_in": 3600, "token_type": "Bearer"}

    def _delegate(self, form):
        if form.get("client_secret") not in self.secrets.get(form.get("client_id"), ()):
            return 400, {"error": "invalid_client"}
        agent = self.agents.get(form.get("agent_id"))
        if not agent or not agent.get("delegable", True):
            return 401, {"errors": [{"detail": "The agent does not exist, is not active or cannot be delegated."}]}
        self.delegations += 1
        return 200, {"access_token": f"agent-{form['agent_id']}-{self.delegations}", "expires_in": 3600,
                     "token_type": "Bearer"}

    def _request(self, attributes):
        token = attributes.get("enrollmentToken")
        if token is not None and token != ENROLLMENT_TOKEN:
            return 400, {"errors": [{"detail": "The enrollment token is not valid."}]}
        enrollment_id = f"e-{len(self.enrollments) + 1}"
        self.enrollments[enrollment_id] = {"status": "approved" if token else "pending", "polls": 0,
                                           "name": attributes["name"], "kind": attributes["kind"]}
        return 201, {"data": {"type": "serviceenrollment", "id": enrollment_id, "attributes": {
            "name": attributes["name"], "kind": attributes["kind"], "status": self.enrollments[enrollment_id]["status"],
            "userCode": USER_CODE, "expiresAt": "2026-11-09 12:15:00", "pollToken": f"poll-{enrollment_id}"}}}

    def _poll(self, enrollment_id, poll_token):
        enrollment = self.enrollments.get(enrollment_id)
        if not enrollment or poll_token != f"poll-{enrollment_id}":
            return 404, {"errors": [{"detail": "not found"}]}
        attributes = {"status": enrollment["status"], "userCode": USER_CODE}
        if enrollment["status"] == "pending":
            enrollment["polls"] += 1
            if self.approve_after_polls is not None and enrollment["polls"] > self.approve_after_polls:
                enrollment["status"] = "denied" if self.deny else "approved"
        elif enrollment["status"] == "approved":
            client_id = f"svc-{enrollment_id}"
            self.secrets[client_id] = ["secret-1"]
            enrollment["status"] = "delivered"
            attributes = {"status": "delivered", "clientId": client_id, "clientSecret": "secret-1"}
            if self.send_serviceclient_id:
                attributes["serviceClientId"] = f"sc-{enrollment_id}"
        return 200, {"data": {"type": "serviceenrollment", "id": enrollment_id, "attributes": attributes}}

    def _rotate(self, serviceclient_id, body):
        if body["data"]["attributes"] != {"rotateSecret": True} or body["data"]["id"] != serviceclient_id:
            return 400, {"errors": [{"detail": "rotateSecret must be sent alone and set to true."}]}
        client_id = "svc-" + serviceclient_id[len("sc-"):]
        self.rotations += 1
        secret = f"secret-{self.rotations + 1}"
        self.secrets[client_id] = [self.secrets[client_id][-1], secret]     # the previous one in its grace period
        return 200, {"data": {"type": "serviceclient", "id": serviceclient_id, "attributes": {
            "clientId": client_id, "clientSecret": secret, "previousSecretExpiresAt": "2026-11-09 12:10:00"}}}
