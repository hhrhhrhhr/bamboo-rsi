"""Transactional review inbox. Approvals bind exact evidence, never deploy a reply."""
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
import sqlite3
import uuid


KINDS = {"unknown_question", "unresolved_reply", "repeat_question", "confusion",
         "human_request", "stop_request", "system_failure", "learning_sample"}
CATEGORIES = {"annotation", "knowledge", "wording", "strategy", "engineering"}


def encoded(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def digest(value):
    return hashlib.sha256(encoded(value).encode()).hexdigest()


def text(value, maximum=2000):
    if not isinstance(value, str) or not value.strip() or len(value) > maximum:
        raise ValueError("A nonempty bounded string is required")
    return value


def identifier(value):
    if not isinstance(value, str) or not re.fullmatch(r"[a-zA-Z0-9_.:-]{1,160}", value):
        raise ValueError("Use opaque identifiers, not account numbers or file paths")
    return value


def instant(value):
    stamp = datetime.fromisoformat(text(value, 40).replace("Z", "+00:00"))
    if stamp.utcoffset() is None:
        raise ValueError("Time zone required")
    return stamp.astimezone(timezone.utc)


def validate_event(event):
    required = {"event_id", "session_id", "customer_group", "round_id", "policy_version", "source_system",
                "origin", "split", "kind", "topic", "user_quote", "assistant_quote", "signals",
                "hypotheses", "rating", "learning_example"}
    if type(event) is not dict or set(event) != required:
        raise ValueError("Unexpected feedback fields")
    for key in ("event_id", "session_id", "customer_group", "round_id", "policy_version", "source_system", "topic"):
        identifier(event[key])
    if event["origin"] not in {"synthetic_fixture", "redacted_business"} or event["split"] not in {"training", "audit_only"}:
        raise ValueError("Declare source and learning eligibility")
    if event["kind"] not in KINDS:
        raise ValueError("Unknown issue kind")
    text(event["user_quote"])
    text(event["assistant_quote"])
    if not isinstance(event["signals"], list) or len(event["signals"]) > 16:
        raise ValueError("Bounded evidence signals required")
    for signal in event["signals"]:
        identifier(signal)
    if not isinstance(event["hypotheses"], list) or len(event["hypotheses"]) > 5:
        raise ValueError("Bounded hypotheses required")
    for h in event["hypotheses"]:
        if (set(h) != {"claim", "basis_quote", "source", "confirmed"}
                or h["source"] != "model_inference" or h["confirmed"] is not False
                or text(h["basis_quote"]) not in event["user_quote"]):
            raise ValueError("A psychological inference must remain unconfirmed and quote its basis")
        text(h["claim"], 500)
    rating = event["rating"]
    if rating is not None:
        if (set(rating) != {"value", "scale", "source", "evidence_ref"} or rating["scale"] != 5
                or type(rating["value"]) is not int or not 1 <= rating["value"] <= 5
                or rating["source"] != "explicit_customer_rating"):
            raise ValueError("Only explicit customer ratings; absence is not a zero score")
        identifier(rating["evidence_ref"])
    row = event["learning_example"]
    if row is not None:
        # Production conversation capture is supported, but this first training adapter is synthetic-only.
        if (event["origin"] != "synthetic_fixture" or event["split"] != "training"
                or type(row) is not dict or row.get("sessionId") != event["session_id"]
                or row.get("userQuery") != event["user_quote"]):
            raise ValueError("Learning attachment must match a synthetic training conversation")
    if len(encoded(event).encode()) > 32000:
        raise ValueError("Feedback record too large")
    return event


@dataclass(frozen=True)
class Principal:
    actor: str
    role: str
    kind: str = "human_operator"

    def require(self, role):
        identifier(self.actor)
        if self.role != role or self.kind not in {"human_operator", "test_fixture", "service"}:
            raise PermissionError("Role not permitted")
        if role == "reviewer" and self.kind == "service":
            raise PermissionError("Service ingestion cannot approve its own evidence")


class Conflict(ValueError):
    pass


class Inbox:
    def __init__(self, path):
        self.path = Path(path).resolve()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connection() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS metadata(key TEXT PRIMARY KEY, value TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS events(id TEXT PRIMARY KEY, issue TEXT NOT NULL, hash TEXT NOT NULL, body TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS reviews(seq INTEGER PRIMARY KEY, id TEXT UNIQUE NOT NULL, issue TEXT NOT NULL, hash TEXT NOT NULL, body TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS revocations(review TEXT PRIMARY KEY, body TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS requests(key TEXT PRIMARY KEY, hash TEXT NOT NULL, response TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS audit(seq INTEGER PRIMARY KEY, previous TEXT, hash TEXT NOT NULL, body TEXT NOT NULL);
            """)
            db.execute("INSERT OR IGNORE INTO metadata VALUES ('identity', ?)", (uuid.uuid4().hex,))
            self.identity = db.execute("SELECT value FROM metadata WHERE key='identity'").fetchone()[0]

    @contextmanager
    def connection(self):
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        try:
            db.execute("PRAGMA foreign_keys=ON")
            db.execute("BEGIN IMMEDIATE")
            yield db
            db.commit()
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()

    def _audit(self, db, value):
        last = db.execute("SELECT hash FROM audit ORDER BY seq DESC LIMIT 1").fetchone()
        previous = last[0] if last else None
        body = {**value, "at": datetime.now(timezone.utc).isoformat()}
        db.execute("INSERT INTO audit(previous,hash,body) VALUES (?,?,?)",
                   (previous, digest([previous, body]), encoded(body)))

    @staticmethod
    def _rows(db):
        result = []
        for row in db.execute("SELECT * FROM events ORDER BY id"):
            value = json.loads(row["body"])
            if digest(value) != row["hash"] or row["id"] != value["event_id"]:
                raise ValueError("Stored evidence changed")
            result.append({"id": row["id"], "issue": row["issue"], "hash": row["hash"], "event": value})
        return result

    @staticmethod
    def _reviews(db):
        result = []
        for row in db.execute("SELECT * FROM reviews ORDER BY seq"):
            value = json.loads(row["body"])
            if digest(value) != row["hash"] or value["id"] != row["id"]:
                raise ValueError("Review evidence changed")
            result.append(value)
        return result

    def ingest(self, event, principal):
        principal.require("ingest")
        validate_event(event)
        if principal.kind == "test_fixture" and event["origin"] != "synthetic_fixture":
            raise ValueError("Test fixtures cannot impersonate business evidence")
        stamp = digest(event)
        # Do not merge different domains, evidence origins or holdout/training partitions.
        issue = digest([event[k] for k in ("source_system", "origin", "split", "kind", "topic")])
        with self.connection() as db:
            prior = db.execute("SELECT hash FROM events WHERE id=?", (event["event_id"],)).fetchone()
            if prior:
                if prior[0] != stamp:
                    raise Conflict("Event ID already has different evidence")
                return {"event_id": event["event_id"], "issue_id": issue, "duplicate": True}
            db.execute("INSERT INTO events VALUES (?,?,?,?)", (event["event_id"], issue, stamp, encoded(event)))
            self._audit(db, {"type": "ingest", "event_id": event["event_id"], "evidence_hash": stamp,
                             "actor": principal.actor, "actor_kind": principal.kind})
        return {"event_id": event["event_id"], "issue_id": issue, "duplicate": False}

    def _issues(self, db):
        groups = {}
        latest = {key: review for review in self._reviews(db) for key in review["events"]}
        revoked = {r[0] for r in db.execute("SELECT review FROM revocations")}
        now = datetime.now(timezone.utc)
        for row in self._rows(db):
            event = row["event"]
            group = groups.setdefault(row["issue"], {"issue_id": row["issue"], "kind": event["kind"],
                "topic": event["topic"], "origin": event["origin"], "split": event["split"], "events": [], "states": {}})
            review = latest.get(row["id"])
            state = review["decision"] if review else "pending"
            if review and review["id"] in revoked:
                state = "revoked"
            elif review and review["decision"] == "approved" and instant(review["valid_until"]) <= now:
                state = "expired"
            group["states"][state] = group["states"].get(state, 0) + 1
            group["events"].append({**row, "state": state, "review_id": review["id"] if review else None})
        for group in groups.values():
            group["revision"] = digest({r["id"]: [r["hash"], r["state"], r["review_id"]] for r in group["events"]})
        return list(groups.values())

    def issues(self):
        with self.connection() as db:
            return self._issues(db)

    def review(self, request, principal, key):
        principal.require("reviewer")
        identifier(key)
        required = {"issue_id", "expected_revision", "event_ids", "decision", "category", "summary", "references",
                    "confirmed_intent", "valid_until"}
        if type(request) is not dict or set(request) != required or request["decision"] not in {"approved", "rejected", "needs_info"}:
            raise ValueError("Invalid review request")
        if request["category"] not in CATEGORIES:
            raise ValueError("Invalid review category")
        text(request["summary"])
        refs = request["references"]
        if not isinstance(refs, list) or len(refs) > 10:
            raise ValueError("Bounded reviewer references required")
        for reference in refs:
            text(reference, 500)
        if request["confirmed_intent"] is not None:
            identifier(request["confirmed_intent"])
        if request["decision"] == "approved":
            if not refs or instant(request["valid_until"]) <= datetime.now(timezone.utc):
                raise ValueError("Approval needs source references and a future expiry")
        elif request["valid_until"] is not None:
            raise ValueError("Only approvals have expiry")
        stamp = digest([request, principal.actor, principal.kind])
        with self.connection() as db:
            prior = db.execute("SELECT hash,response FROM requests WHERE key=?", (key,)).fetchone()
            if prior:
                if prior[0] != stamp:
                    raise Conflict("Idempotency key reused with different review")
                return json.loads(prior[1])
            issue = next((i for i in self._issues(db) if i["issue_id"] == request["issue_id"]), None)
            if issue is None or issue["revision"] != request["expected_revision"]:
                raise Conflict("Issue evidence changed; inspect it before reviewing")
            if principal.kind == "test_fixture" and issue["origin"] != "synthetic_fixture":
                raise ValueError("Fixture reviewers cannot approve business evidence")
            ids = request["event_ids"]
            available = {r["id"]: r["hash"] for r in issue["events"]}
            if (not isinstance(ids, list) or not ids or len(set(ids)) != len(ids)
                    or any(i not in available for i in ids)):
                raise ValueError("Select distinct events from this issue")
            review = {**request, "id": uuid.uuid4().hex, "actor": principal.actor, "actor_kind": principal.kind,
                      "events": {i: available[i] for i in ids},
                      "at": datetime.now(timezone.utc).isoformat(), "production_release": False}
            db.execute("INSERT INTO reviews(id,issue,hash,body) VALUES (?,?,?,?)",
                       (review["id"], issue["issue_id"], digest(review), encoded(review)))
            db.execute("INSERT INTO requests VALUES (?,?,?)", (key, stamp, encoded(review)))
            self._audit(db, {"type": "review", "review_id": review["id"], "review_hash": digest(review),
                             "actor": principal.actor, "actor_kind": principal.kind})
            return review

    def revoke(self, review_id, reason, principal):
        principal.require("reviewer")
        text(reason)
        with self.connection() as db:
            review = next((r for r in self._reviews(db) if r["id"] == review_id), None)
            if review is None:
                raise ValueError("Unknown review")
            if principal.kind == "test_fixture" and review["actor_kind"] != "test_fixture":
                raise PermissionError("Fixture reviewer cannot revoke real reviews")
            prior = db.execute("SELECT body FROM revocations WHERE review=?", (review_id,)).fetchone()
            if prior:
                return json.loads(prior[0])
            value = {"review": review_id, "reason": reason, "actor": principal.actor,
                     "actor_kind": principal.kind, "at": datetime.now(timezone.utc).isoformat()}
            db.execute("INSERT INTO revocations VALUES (?,?)", (review_id, encoded(value)))
            self._audit(db, {"type": "revoke", **value})
            return value

    def approved(self):
        """A snapshot for candidate preparation, not a runtime knowledge-base switch."""
        with self.connection() as db:
            return self._approved(db)

    def _approved(self, db):
        reviews = {r["id"]: r for r in self._reviews(db)}
        latest = {eid: r for r in reviews.values() for eid in r["events"]}
        records = []
        for issue in self._issues(db):
            for row in issue["events"]:
                if row["state"] != "approved" or issue["split"] != "training":
                    continue
                review = latest[row["id"]]
                if review["events"][row["id"]] != row["hash"]:
                    raise ValueError("Approval does not bind current evidence")
                # Hypotheses, account fields and unconfirmed emotion never become training facts.
                records.append({"event_id": row["id"], "evidence_hash": row["hash"], "review": review,
                                "origin": issue["origin"], "learning_example": row["event"]["learning_example"]})
        return {"queue_id": self.identity, "records": records, "production_release": False,
                "psychological_inferences_exported": False}

    def audit(self):
        with self.connection() as db:
            previous, count = None, 0
            for row in db.execute("SELECT * FROM audit ORDER BY seq"):
                body = json.loads(row["body"])
                if row["previous"] != previous or digest([previous, body]) != row["hash"]:
                    raise ValueError("Audit chain changed")
                previous, count = row["hash"], count + 1
            return {"entries": count, "head": previous}

    def summary(self):
        issues = self.issues()
        rows = [row for issue in issues for row in issue["events"]]
        ratings = {(r["event"]["origin"], r["event"]["session_id"], r["event"]["rating"]["evidence_ref"]):
                   r["event"]["rating"]["value"] for r in rows if r["event"]["rating"] is not None}
        return {"queue_id": self.identity, "issues": len(issues), "events": len(rows),
                "states": {s: sum(i["states"].get(s, 0) for i in issues) for s in
                           ("pending", "approved", "rejected", "needs_info", "revoked", "expired")},
                "kinds": {k: sum(r["event"]["kind"] == k for r in rows) for k in sorted(KINDS)},
                "explicit_rating_count": len(ratings),
                "ratings_by_origin": {origin: {"count": len(values), "mean": sum(values) / len(values) if values else None}
                    for origin in ("synthetic_fixture", "redacted_business")
                    for values in [[v for (o, _, _), v in ratings.items() if o == origin]]},
                "customer_comfort_established": False, "production_release": False}
