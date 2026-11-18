"""No-network n8n contract peer. Test-only, never a deployed workflow."""
import hashlib
from copy import deepcopy
from source.server import n8n_bridge as n

class Response:
    def __init__(self, body, secret, status=200):
        self.status_code = status
        self.body = n.encoded(body)
        self.headers = {"X-Friday-Signature": n.sign(secret, body)}
        self.closed = False
    def iter_content(self, size):
        for i in range(0, len(self.body), size):
            yield self.body[i:i+size]
    def close(self):
        self.closed = True

class OfflineN8N:
    """Explicit fixture; no code imports it from production runtime."""
    def __init__(self, secret):
        self.secret = secret
        self.client = None
        self.calls = []
        self.events = []
        self.receipts = set()
        self.requests = {}
        self.consumed = set()
        self.actions = []
        self.outcome = "accepted"
        self.transform = lambda value: value
        self.status = 200
        self.before = lambda: None
        self.fail = False

    def __call__(self, url, *, data, headers, **kwargs):
        assert kwargs["allow_redirects"] is False and kwargs["stream"] is True
        self.calls.append(url.rsplit("/", 1)[-1])
        n.authenticate(self.secret, data, headers["X-Friday-Signature"])
        json = n.literal(data)
        if headers["X-Friday-Token"] != self.secret:
            raise n.Refused("authentication_failed")
        n.validate_envelope(json, client=self.client, kind=self.calls[-1])
        self.before()
        if self.fail:
            raise TimeoutError("SECRET_PRIVATE_URL")
        owner = (json["client_id"], json["account_id"], json["session_id"])
        op = self.calls[-1]
        payload = {"outcome": "ready"}
        if op == "request":
            key = (*owner, json["id"])
            previous = self.requests.get(key)
            if previous is not None:
                if previous != json["payload"]:
                    raise n.Refused("id_conflict")
            self.requests[key] = deepcopy(json["payload"])
            payload = {"outcome": self.outcome, "workflow_completed": self.outcome == "completed", "text": "Synthetic workflow reply <script>inert</script>"}
        elif op == "pull":
            payload = {"outcome": "events", "events": self.events}
        elif op == "ack":
            if "decision" in json["payload"]:
                d = json["payload"]["decision"]
                event = next((e for e in self.events if e["id"] == d["event_id"]), None)
                if event is None:
                    raise n.Refused("unknown_decision")
                n.validate_envelope(event, client=owner[0], session=owner[2], kind="event")
                p = event["payload"]
                if (d["token"] != p["token"] or d["action_digest"] != p["action_digest"]
                        or d["original_session"] != owner[2] or d["choice"] not in ("approve", "reject")):
                    raise n.Refused("wrong_decision")
                key = (*owner, event["id"])
                if key in self.consumed:
                    raise n.Refused("decision_replayed")
                self.consumed.add(key)
                if d["choice"] == "approve":
                    self.actions.append((owner, p["action"], deepcopy(p["arguments"])))
            else:
                self.receipts.update(json["payload"]["event_ids"])
            payload = {"outcome": "received"}
        reply = n.envelope("result", owner[0], owner[2], payload, request_id=json["id"])
        return Response(self.transform(reply), self.secret, self.status)
