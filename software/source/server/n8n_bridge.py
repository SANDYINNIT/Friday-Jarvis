"""Opt-in n8n v1 bridge. No IO at import; passive reads never initialize state.

This is a transport/receipt authority, not an automation executor. Remote actions
need a separately verified n8n implementation and exact one-use owner decisions.
"""
from __future__ import annotations
from contextlib import contextmanager
from copy import deepcopy
import hashlib
import hmac
import json
import math
import os
from pathlib import Path
import re
import sqlite3
import tempfile
import threading
import time
from urllib.parse import urlsplit
from uuid import uuid4

VERSION = "friday.n8n.v1"
MAX_BYTES = 64 * 1024
CAPABILITIES = ("chat", "gmail.read", "calendar.read", "docs.read",
                "gmail.send", "calendar.write", "docs.write", "schedule.write", "memory.write")
WRITES = frozenset(CAPABILITIES[4:])
_LOCK = threading.RLock()


class Refused(ValueError):
    """Safe, fixed diagnostic code. Never include input values."""


def encoded(value):
    try:
        return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
                          allow_nan=False).encode("utf-8")
    except (ValueError, TypeError, RecursionError):
        raise Refused("invalid_json") from None


def bounded(value, limit=MAX_BYTES):
    data = encoded(value)
    if len(data) > limit:
        raise Refused("payload_too_large")
    return value


def literal(data):
    def pairs(rows):
        result = dict(rows)
        if len(result) != len(rows):
            raise Refused("duplicate_json_key")
        return result
    try:
        result = json.loads(data, object_pairs_hook=pairs, parse_constant=lambda _: (_ for _ in ()).throw(Refused("invalid_json")))
        bounded(result)
        return result
    except (ValueError, TypeError, UnicodeError, RecursionError):
        raise Refused("invalid_json") from None


def identifier(value):
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,95}", value):
        raise Refused("invalid_identifier")
    return value


def config_path():
    explicit = os.environ.get("FRIDAY_N8N_CONFIG")
    if explicit:
        return Path(explicit)
    # Public distribution shares its protected per-user profile.
    if os.environ.get("FRIDAY_USER_ROUTES"):
        return Path(os.environ["FRIDAY_USER_ROUTES"]).parent / "n8n.json"
    return Path.home() / ".friday" / "n8n.json"


def default():
    return {"version": 2, "revision": 0, "enabled": False, "deployment": "",
            "secret": "", "capabilities": [], "notifications": False,
            "client_id": "", "account_id": "host"}


def validate(raw):
    if not isinstance(raw, dict) or set(raw) != set(default()) or raw.get("version") != 2:
        raise Refused("unsupported_configuration")
    if type(raw["revision"]) is not int or raw["revision"] < 0:
        raise Refused("invalid_revision")
    if any(type(raw[k]) is not bool for k in ("enabled", "notifications")):
        raise Refused("invalid_choice")
    caps = raw["capabilities"]
    if not isinstance(caps, list) or len(caps) != len(set(caps)) or any(c not in CAPABILITIES for c in caps):
        raise Refused("unsupported_capability")
    if raw["account_id"] != "host":
        raise Refused("account_not_supported")
    if raw["client_id"]:
        identifier(raw["client_id"])
    secret = raw["secret"]
    if not isinstance(secret, str) or (secret and not re.fullmatch(r"[A-Za-z0-9_-]{32,256}", secret)):
        raise Refused("invalid_credential")
    url = raw["deployment"]
    if not isinstance(url, str) or len(url) > 2048:
        raise Refused("invalid_deployment")
    if url:
        try:
            p = urlsplit(url)
            valid = (p.scheme == "https" or p.scheme == "http" and p.hostname in ("127.0.0.1", "localhost", "::1"))
            if not valid or not p.hostname or p.username or p.password or p.query or p.fragment or p.path not in ("", "/"):
                raise Refused("invalid_deployment")
            _ = p.port
        except (ValueError, TypeError):
            raise Refused("invalid_deployment") from None
    if raw["enabled"] and not (url and secret and raw["client_id"]):
        raise Refused("incomplete_configuration")
    bounded(raw)
    return deepcopy(raw)


class Config:
    def __init__(self, path=None):
        self.path = Path(path) if path is not None else config_path()

    def read(self):
        if not self.path.exists():
            return default()
        try:
            if self.path.is_symlink() or self.path.stat().st_size > MAX_BYTES:
                raise Refused("configuration_unavailable")
            if os.name != "nt" and (
                    self.path.parent.stat().st_uid != os.getuid() or self.path.parent.stat().st_mode & 0o077
                    or self.path.stat().st_uid != os.getuid() or self.path.stat().st_mode & 0o077):
                raise Refused("private_configuration_required")
            raw = literal(self.path.read_text(encoding="utf-8"))
            return validate(raw)
        except Refused:
            raise
        except Exception:
            raise Refused("configuration_unavailable") from None

    def save(self, changes, expected):
        if not isinstance(changes, dict) or set(changes) - {"enabled", "deployment", "secret", "remove_secret", "capabilities", "notifications"}:
            raise Refused("unknown_setting")
        with _LOCK:
            self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            if self.path.parent.is_symlink() or self.path.is_symlink():
                raise Refused("unsafe_configuration_location")
            lock = self.path.with_name(self.path.name + ".write-lock")
            try:
                fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            except OSError:
                raise Refused("configuration_busy") from None
            try:
                os.close(fd)
                if os.name != "nt":
                    if self.path.parent.stat().st_uid != os.getuid() or self.path.parent.stat().st_mode & 0o077:
                        raise Refused("private_directory_required")
                    if self.path.exists() and (self.path.stat().st_uid != os.getuid() or self.path.stat().st_mode & 0o077):
                        raise Refused("private_file_required")
                current = self.read()
                if type(expected) is not int or expected != current["revision"]:
                    raise Refused("stale_configuration")
                next_value = deepcopy(current)
                for key, value in changes.items():
                    if key != "remove_secret":
                        next_value[key] = value
                if changes.get("remove_secret") is True:
                    next_value["secret"] = ""
                    next_value["enabled"] = False
                if "remove_secret" in changes and type(changes["remove_secret"]) is not bool:
                    raise Refused("invalid_choice")
                next_value["deployment"] = next_value["deployment"].rstrip("/")
                next_value["client_id"] = current["client_id"] or uuid4().hex
                next_value["revision"] += 1
                validate(next_value)
                fd, tmp = tempfile.mkstemp(prefix=".n8n-", dir=self.path.parent)
                try:
                    with os.fdopen(fd, "wb") as handle:
                        handle.write(encoded(next_value))
                        handle.flush()
                        os.fsync(handle.fileno())
                    os.replace(tmp, self.path)
                finally:
                    if os.path.exists(tmp):
                        os.unlink(tmp)
                return next_value
            finally:
                lock.unlink()


def public_config(value):
    return {k: deepcopy(value[k]) for k in ("version", "revision", "enabled", "capabilities", "notifications", "client_id", "account_id")} | {
        "deployment_configured": bool(value["deployment"]), "credential_present": bool(value["secret"]),
        "endpoint_scope": "FRIDAY POSTs request/status/pull/ack to n8n. No inbound/public listener.",
        "credential_scope": "Private credential; never returned. Rotation applies to the next operation.",
    }


def envelope(kind, client, session, payload, *, request_id=None, now=None):
    now = time.time() if now is None else now
    return bounded({"version": VERSION, "origin": "n8n" if kind in ("event", "result") else "friday", "kind": kind, "id": request_id or uuid4().hex,
                    "client_id": client, "account_id": "host", "session_id": session,
                    "created_at": now, "expires_at": now + 120, "payload": payload})


def validate_envelope(data, *, client, session=None, kind=None, now=None):
    now = time.time() if now is None else now
    keys = {"version", "origin", "kind", "id", "client_id", "account_id", "session_id", "created_at", "expires_at", "payload"}
    if not isinstance(data, dict) or set(data) != keys or data.get("version") != VERSION:
        raise Refused("unsupported_contract")
    bounded(data)
    if data["origin"] != ("n8n" if data["kind"] in ("event", "result") else "friday"):
        raise Refused("wrong_origin")
    for key in ("id", "client_id", "session_id"):
        identifier(data[key])
    if data["client_id"] != client or data["account_id"] != "host" or (session and session != data["session_id"]):
        raise Refused("wrong_owner")
    if kind and data["kind"] != kind:
        raise Refused("wrong_domain")
    for key in ("created_at", "expires_at"):
        if type(data[key]) not in (float, int) or not math.isfinite(data[key]):
            raise Refused("invalid_timestamp")
    if not (now - 86400 <= data["created_at"] <= now + 30 and
            now < data["expires_at"] <= data["created_at"] + 86400):
        raise Refused("expired_or_invalid_timestamp")
    if not isinstance(data["payload"], dict):
        raise Refused("invalid_payload")
    return data


def sign(secret, body):
    return hmac.new(secret.encode(), body if isinstance(body, bytes) else encoded(body), hashlib.sha256).hexdigest()


def authenticate(secret, body, signature):
    if not secret or not isinstance(signature, str) or not hmac.compare_digest(sign(secret, body), signature):
        raise Refused("authentication_failed")


class Receipts:
    """Explicit-use SQLite state; construction/passive snapshot never creates it."""
    def __init__(self, path):
        self.path = Path(path)

    @contextmanager
    def write(self):
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        db = sqlite3.connect(self.path, timeout=2)
        try:
            db.execute("PRAGMA busy_timeout=2000")
            db.execute("CREATE TABLE IF NOT EXISTS records (namespace TEXT, id TEXT, digest TEXT, state TEXT, data TEXT, PRIMARY KEY(namespace,id))")
            db.execute("BEGIN IMMEDIATE")
            if db.execute("SELECT count(*) FROM records").fetchone()[0] >= 5000:
                raise Refused("receipt_capacity_exhausted")
            yield db
            db.commit()
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

    def rows(self, namespace):
        if not self.path.exists():
            return []
        uri = self.path.resolve().as_uri() + "?mode=ro"
        db = sqlite3.connect(uri, uri=True, timeout=2)
        try:
            return [dict(zip(("id", "digest", "state", "data"), row))
                    for row in db.execute("SELECT id,digest,state,data FROM records WHERE namespace=? ORDER BY rowid DESC LIMIT 200", (namespace,))]
        finally:
            db.close()


class Bridge:
    def __init__(self, config=None, *, http=None, session=None, clock=time.time):
        self.config = config or Config()
        self.receipts = Receipts(self.config.path.with_name("n8n-receipts.db"))
        self.session = session or uuid4().hex
        self.clock = clock
        self.http = http
        self.observation = {"state": "unobserved", "at": None, "error": ""}
        self._operation_lock = threading.Lock()
        self._epoch = 0
        self.runtime_live = lambda: True

    def invalidate(self):
        self._epoch += 1

    def snapshot(self):
        try:
            value = self.config.read()
            observation = dict(self.observation)
            if observation["at"] is not None and self.clock() - observation["at"] > 60:
                observation["state"] = "stale"
            return {"ok": True, "available": True, "config": public_config(value),
                    "observation": observation, "session_id": self.session,
                    "message": "Saved configuration is not a live connection or successful action."}
        except Exception:
            return {"ok": False, "available": False, "error": "configuration_unavailable",
                    "message": "Configuration unavailable. No defaults were saved."}

    def save(self, changes, expected):
        try:
            value = self.config.save(changes, expected)
            self.invalidate()
            self.observation = {"state": "unobserved", "at": None, "error": ""}
            return {"ok": True, "config": public_config(value), "message": "Saved. No connection test or workflow was run."}
        except Refused as error:
            return {"ok": False, "error": str(error), "conflict": str(error) in ("stale_configuration", "configuration_busy"),
                    "message": "Could not save. Your previous configuration is preserved; reload before retrying."}
        except Exception:
            return {"ok": False, "error": "save_failed", "message": "Save not confirmed. Check saved state before retrying."}

    def _exchange(self, path, body, value, *, live=lambda: True):
        epoch, revision = self._epoch, value["revision"]
        current = lambda: epoch == self._epoch and live() and (path == "status" or self.runtime_live()) and self.config.read()["revision"] == revision
        if not current():
            raise Refused("stale_or_stopped")
        if self.http is None:
            import requests
            post = requests.post
        else:
            post = self.http
        headers = {"X-Friday-Token": value["secret"], "X-Friday-Signature": sign(value["secret"], body),
                   "Content-Type": "application/json"}
        # Never automatically retry; redirects cannot receive this credential.
        response = post(value["deployment"] + "/webhook/friday/v1/" + path, data=encoded(body),
                        headers=headers, timeout=(3, 15), allow_redirects=False, stream=True)
        try:
            if not current():
                raise Refused("stale_remote_outcome")
            if response.status_code in (401, 403):
                raise Refused("authentication_failed")
            if response.status_code != 200:
                raise Refused("http_receipt_only")
            chunks = bytearray()
            response_deadline = time.monotonic() + 20
            for part in response.iter_content(4096):
                if not current() or time.monotonic() > response_deadline:
                    raise Refused("stale_remote_outcome")
                chunks.extend(part)
                if len(chunks) > MAX_BYTES:
                    raise Refused("response_too_large")
            authenticate(value["secret"], bytes(chunks), response.headers.get("X-Friday-Signature"))
            try:
                result = literal(bytes(chunks))
            except (ValueError, UnicodeError):
                raise Refused("invalid_response") from None
            validate_envelope(result, client=value["client_id"], session=self.session, kind="result", now=self.clock())
            if result["id"] != body["id"]:
                raise Refused("wrong_request")
            if not current():
                raise Refused("stale_remote_outcome")
            return result["payload"]
        finally:
            response.close()

    def operate(self, path, payload, *, request_id=None, live=lambda: True, spend=lambda: True):
        """Only explicit actions call this; no polling thread/model/kernel at read."""
        if path not in ("status", "request", "pull", "ack"):
            return {"ok": False, "error": "unsupported_operation"}
        if not self._operation_lock.acquire(blocking=False):
            return {"ok": False, "error": "operation_busy"}
        dispatched = False
        request_ref = None
        try:
            value = self.config.read()
            if not value["secret"] or not value["deployment"] or (path != "status" and not value["enabled"]):
                raise Refused("disabled")
            if path == "request":
                cap = payload.get("capability")
                if cap not in value["capabilities"]:
                    raise Refused("capability_not_allowed")
                # Consequential requests only create proposals, never grant writes.
                if set(payload) - {"capability", "message", "turn_id", "generation"}:
                    raise Refused("unsupported_arguments")
                if not isinstance(payload.get("message"), str) or not 1 <= len(payload["message"]) <= 4000:
                    raise Refused("invalid_message")
                identifier(payload.get("turn_id"))
                identifier(payload.get("generation"))
                payload = {**payload, "mode": "propose" if cap in WRITES else "read"}
            if path == "pull" and not value["notifications"]:
                raise Refused("notifications_disabled")
            body = envelope(path, value["client_id"], self.session, payload, request_id=request_id, now=self.clock())
            request_ref = body["id"]
            namespace = value["client_id"] + ":" + self.session + ":outbound"
            digest = hashlib.sha256(encoded(body["payload"])).hexdigest()
            with self.receipts.write() as db:
                prior = db.execute("SELECT digest,state,data FROM records WHERE namespace=? AND id=?", (namespace, body["id"])).fetchone()
                if prior:
                    if prior[0] != digest:
                        raise Refused("id_conflict")
                    return {"ok": False, "error": "already_dispatched", "outcome": prior[1], "request_id": body["id"]}
                if not live() or (path != "status" and not self.runtime_live()) or (path == "request" and not spend()):
                    raise Refused("stale_or_stopped")
                db.execute("INSERT INTO records VALUES(?,?,?,?,?)", (namespace, body["id"], digest, "unknown", "{}"))
            # A failed connection may still have reached remote; do not replay.
            dispatched = True
            result = self._exchange(path, body, value, live=live)
            if not isinstance(result, dict) or result.get("outcome") not in (
                    "ready", "accepted", "completed", "partial", "failed", "pending_decision", "events", "received"):
                raise Refused("invalid_outcome")
            bounded(result)
            # Free-form remote payload stays untrusted; never executes locally.
            with self.receipts.write() as db:
                db.execute("UPDATE records SET state=? WHERE namespace=? AND id=?",
                           (result["outcome"], namespace, body["id"]))
            self.observation = {"state": "observed", "at": self.clock(), "error": "",
                                "request_id": request_ref, "outcome": result["outcome"]}
            return {"ok": True, "request_id": body["id"], "receipt": "authenticated_response",
                    "outcome": result["outcome"], "result": result, "_revision": value["revision"], "_epoch": self._epoch, "workflow_completion":
                    result.get("workflow_completed") is True and result["outcome"] == "completed"}
        except Exception as error:
            code = str(error) if isinstance(error, Refused) else "transport_or_storage_failed"
            self.observation = {"state": "error", "at": self.clock(), "error": code,
                                "request_id": request_ref, "outcome": "unknown" if dispatched else "not_dispatched"}
            return {"ok": False, "error": code, "request_id": request_ref, "outcome": "unknown" if dispatched else "not_dispatched",
                    "message": "No automatic retry. A dispatched request may have executed remotely." if dispatched else "Request refused before dispatch."}
        finally:
            self._operation_lock.release()

    def pull(self):
        result = self.operate("pull", {"limit": 20})
        if not result.get("ok"):
            return result
        try:
            value = self.config.read()
            if (result["_revision"] != value["revision"] or result["_epoch"] != self._epoch
                    or not value["enabled"] or not value["notifications"] or not self.runtime_live()):
                raise Refused("stale_or_stopped")
            events = result["result"].get("events")
            if not isinstance(events, list) or len(events) > 20:
                raise Refused("invalid_events")
            namespace = value["client_id"] + ":host:events"
            with self.receipts.write() as db:
                if (self.config.read()["revision"] != value["revision"] or not self.runtime_live()
                        or result["_epoch"] != self._epoch):
                    raise Refused("stale_or_stopped")
                existing = {row[0]: (row[1], row[2]) for row in db.execute(
                    "SELECT id,digest,state FROM records WHERE namespace=?", (namespace,))}
                top = max([int(row[0]) for row in db.execute("SELECT state FROM records WHERE namespace=? AND state GLOB '[0-9]*'", (namespace + ":cursor",))] or [0])
                previous = top
                for event in events:
                    validate_envelope(event, client=value["client_id"], kind="event", now=self.clock())
                    # Reconnects cannot adopt approvals bound to a prior session.
                    if event["session_id"] != self.session:
                        raise Refused("stale_session")
                    payload = event["payload"]
                    seq = payload.get("sequence")
                    if type(seq) is not int or seq <= 0:
                        raise Refused("invalid_order")
                    event_digest = hashlib.sha256(encoded(event)).hexdigest()
                    if event["id"] in existing:
                        if existing[event["id"]][0] != event_digest:
                            raise Refused("event_id_conflict")
                        continue
                    if seq <= previous:
                        raise Refused("out_of_order")
                    if payload.get("type") not in ("notice", "decision"):
                        raise Refused("unsupported_event")
                    bounded(payload, 16000)
                    if payload["type"] == "decision":
                        self._validate_decision(payload)
                    db.execute("INSERT INTO records VALUES(?,?,?,?,?)", (namespace, event["id"], event_digest, "stored", encoded(event).decode()))
                    previous = seq
                db.execute("INSERT OR REPLACE INTO records VALUES(?,?,?,?,?)", (namespace + ":cursor", "sequence", "", str(previous), "{}"))
            # Receipt acknowledges durable local storage, not reading/approval/completion.
            ack = self.operate("ack", {"event_ids": [event["id"] for event in events], "receipt": "stored"})
            return {"ok": True, "stored": len(events), "receipt": ack, "message": "Events stored locally. No decision or workflow action was executed."}
        except Exception as error:
            return {"ok": False, "error": str(error) if isinstance(error, Refused) else "event_storage_failed",
                    "message": "Events not acknowledged. No actions were taken."}

    def _validate_decision(self, payload):
        if set(payload) != {"sequence", "type", "action", "arguments", "token", "action_digest"}:
            raise Refused("invalid_decision")
        if payload["action"] not in WRITES or not isinstance(payload["arguments"], dict):
            raise Refused("unsupported_decision")
        if not isinstance(payload["token"], str) or not re.fullmatch(r"[A-Za-z0-9_-]{32,256}", payload["token"]):
            raise Refused("invalid_decision_token")
        digest = hashlib.sha256(encoded({"action": payload["action"], "arguments": payload["arguments"]})).hexdigest()
        if not hmac.compare_digest(digest, str(payload["action_digest"])):
            raise Refused("decision_arguments_changed")

    def inbox(self):
        try:
            value = self.config.read()
            namespace = value["client_id"] + ":host:events"
            rows = self.receipts.rows(namespace)
            result = []
            for row in rows:
                event = json.loads(row["data"])
                p = event["payload"]
                # Do not return secrets, endpoints or remote action arguments by default.
                result.append({"id": row["id"], "type": p["type"], "sequence": p["sequence"], "state": row["state"],
                               "action": p.get("action"), "expires_at": event["expires_at"],
                               "current_session": event["session_id"] == self.session,
                               "text": safe_notice(p.get("text", ""), value["secret"]) if p["type"] == "notice" else ""})
            return {"ok": True, "events": result, "bounded_preview": True, "limit": 200, "possibly_truncated": len(rows) == 200}
        except Exception:
            return {"ok": False, "error": "inbox_unavailable", "events": []}

    def decision_preview(self, event_id):
        value = self.config.read()
        if not value["enabled"] or not self.runtime_live():
            raise Refused("disabled_or_stopped")
        rows = self.receipts.rows(value["client_id"] + ":host:events")
        row = next((r for r in rows if r["id"] == event_id), None)
        if row is None or row["state"] != "stored":
            raise Refused("decision_unavailable")
        event = json.loads(row["data"])
        validate_envelope(event, client=value["client_id"], session=self.session, kind="event", now=self.clock())
        self._validate_decision(event["payload"])
        if event["payload"]["action"] not in value["capabilities"]:
            raise Refused("capability_not_allowed")
        review_arguments(event["payload"]["arguments"], value["secret"])
        return {"id": event_id, "action": event["payload"]["action"],
                "arguments": event["payload"]["arguments"], "digest": event["payload"]["action_digest"],
                "expires_at": event["expires_at"]}

    def decide(self, event_id, digest, choice):
        # Only native/local owner UI calls this, never a model or incoming boolean.
        try:
            if choice not in ("approve", "reject"):
                raise Refused("invalid_choice")
            preview = self.decision_preview(event_id)
            if not hmac.compare_digest(str(digest), preview["digest"]):
                raise Refused("decision_arguments_changed")
            value = self.config.read()
            namespace = value["client_id"] + ":host:events"
            with self.receipts.write() as db:
                row = db.execute("SELECT state,data FROM records WHERE namespace=? AND id=?", (namespace, event_id)).fetchone()
                if row is None or row[0] != "stored":
                    raise Refused("decision_already_consumed")
                event = json.loads(row[1])
                db.execute("UPDATE records SET state='decision_unknown' WHERE namespace=? AND id=?", (namespace, event_id))
            decision = {"event_id": event_id, "token": event["payload"]["token"], "action_digest": digest,
                        "choice": choice, "original_session": event["session_id"]}
            result = self.operate("ack", {"decision": decision}, request_id="decision-" + event_id)
            if result.get("ok"):
                # A verified receipt proves delivery, not application/completion.
                state = "decision_received" if result.get("outcome") == "received" else "decision_reported_" + result["outcome"]
                with self.receipts.write() as db:
                    db.execute("UPDATE records SET state=? WHERE namespace=? AND id=? AND state='decision_unknown'",
                               (state, namespace, event_id))
            return result
        except Exception as error:
            return {"ok": False, "error": str(error) if isinstance(error, Refused) else "decision_failed"}

def review_arguments(value, secret):
    """Never approve concealed arguments. Sensitive/unsupported previews refuse."""
    bounded(value, 8000)
    def visit(item, depth=0):
        if depth > 8:
            raise Refused("unsupported_sensitive_arguments")
        if isinstance(item, dict):
            for key, part in item.items():
                if re.search(r"(?i)(secret|token|password|credential|api.?key|auth|endpoint|headers|path)", str(key)):
                    raise Refused("unsupported_sensitive_arguments")
                visit(part, depth+1)
        elif isinstance(item, list):
            for part in item:
                visit(part, depth+1)
        elif isinstance(item, str):
            if ((secret and secret in item) or re.search(
                    r"(?i)(?:[A-Z]:\\|/(?:home|Users|root)/|https?://|\b(?:gsk_|sk-or-v1-|wk-|ws-))", item)):
                raise Refused("unsupported_sensitive_arguments")
    visit(value)
    return deepcopy(value)


def public_operation(result):
    """Only typed outcomes cross the bridge; no raw remote result/payload."""
    return {key: result[key] for key in ("ok", "error", "message", "outcome", "receipt",
            "request_id", "stored", "workflow_completion") if key in result and key != "receipt"} | (
        {"receipt": public_operation(result["receipt"]) if isinstance(result.get("receipt"), dict) else result["receipt"]}
        if "receipt" in result else {})


def safe_notice(text, secret=""):
    from .redaction import redact
    if not isinstance(text, str):
        return ""
    if secret:
        text = text.replace(secret, "[credential]")
    text = re.sub(r"https?://[^\s<>]+", "[private link]", str(redact(text, force=True)))
    marker = " [truncated]"
    return text if len(text) <= 1000 else text[:1000-len(marker)] + marker


def for_runtime(interpreter):
    """Attach lazily; constructor has no file/network/thread side effects."""
    bridge = getattr(interpreter, "n8n_bridge", None)
    if bridge is None:
        bridge = Bridge()
        interpreter.n8n_bridge = bridge
    return bridge
