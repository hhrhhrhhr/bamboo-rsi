"""Small loopback review API. Per-credential identity; no claimed actor in request bodies."""
import hmac
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from urllib.parse import urlsplit

from .store import Conflict, Principal, encoded


def server(inbox, credentials, address=("127.0.0.1", 0)):
    if address[0] not in {"127.0.0.1", "localhost"}:
        raise ValueError("Bind loopback; use a separately authenticated TLS gateway for remote clients")
    if not credentials or any(len(token) < 32 or not isinstance(actor, Principal) for token, actor in credentials.items()):
        raise ValueError("Distinct long credentials and configured principals required")

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass  # Never log tokens, customer text or arbitrary request paths.

        def setup(self):
            super().setup()
            self.connection.settimeout(5)

        def respond(self, status, value):
            body = encoded(value).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def dispatch(self, method):
            bearer = self.headers.get("Authorization", "")
            actor = next((p for token, p in credentials.items()
                          if hmac.compare_digest(bearer.encode(), ("Bearer " + token).encode())), None)
            if actor is None:
                return self.respond(401, {"error": "authentication_required"})
            route = urlsplit(self.path)
            if route.query or route.fragment:
                return self.respond(400, {"error": "no_query_parameters"})
            path = route.path
            try:
                if method == "GET":
                    if actor.role not in {"read", "reviewer"}:
                        raise PermissionError()
                    if path == "/v1/issues":
                        return self.respond(200, {"issues": inbox.issues()})
                    if path == "/v1/summary":
                        return self.respond(200, inbox.summary())
                    if path == "/v1/approved":
                        return self.respond(200, inbox.approved())
                elif method == "POST":
                    if self.headers.get("Transfer-Encoding") or self.headers.get("Content-Type", "").split(";")[0] != "application/json":
                        raise ValueError()
                    size = int(self.headers.get("Content-Length", "0"))
                    if not 1 <= size <= 100000:
                        raise ValueError()
                    data = self.rfile.read(size)
                    if len(data) != size:
                        raise ValueError()
                    value = json.loads(data)
                    if path == "/v1/events":
                        return self.respond(200, inbox.ingest(value, actor))
                    if path == "/v1/reviews":
                        result = inbox.review(value, actor, self.headers.get("Idempotency-Key", ""))
                        return self.respond(200, result)
                    if path == "/v1/revocations":
                        if type(value) is not dict or set(value) != {"review_id", "reason"}:
                            raise ValueError()
                        return self.respond(200, inbox.revoke(value["review_id"], value["reason"], actor))
                return self.respond(404, {"error": "unknown_route"})
            except PermissionError:
                return self.respond(403, {"error": "role_not_permitted"})
            except Conflict:
                return self.respond(409, {"error": "evidence_revision_or_idempotency_conflict"})
            except (ValueError, KeyError, TypeError, OverflowError):
                return self.respond(400, {"error": "invalid_request"})
            except Exception:
                return self.respond(500, {"error": "review_service_error"})

        def do_GET(self):
            self.dispatch("GET")

        def do_POST(self):
            self.dispatch("POST")

    return ThreadingHTTPServer(address, Handler)
