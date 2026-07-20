"""Structured JSON access logging.

One JSON line per HTTP request on stderr: request id, method, path, status,
latency, claimed actor, client. 4xx/5xx are level WARNING so auth failures
(401/403) and rate limits (429) stand out. Correlate with a client via the
`X-Request-ID` response header (echoed if the client sends one).

We write JSON straight to stderr rather than going through `logging` — uvicorn
reconfigures the logging tree at startup (disable_existing_loggers), which kept
silencing a named logger no matter when we configured it. A direct writer can't
be disabled and needs no setup.

Pure-ASGI middleware (not BaseHTTPMiddleware) so it never buffers the proxy's
streamed responses. The actor is the *claimed* bearer-token subject, parsed
WITHOUT verification — observability only; the response status reflects the real
auth result, and app/db.AuditLog is the authoritative actor trail.
"""
import base64
import json
import sys
from datetime import datetime, timezone

from .metrics import metrics


def _claimed_actor(auth_header: str) -> str | None:
    """Best-effort subject from a bearer token WITHOUT verifying it."""
    if not auth_header.startswith("Bearer ") or auth_header.count(".") < 2:
        return None
    try:
        payload = auth_header.split(".")[1]
        payload += "=" * (-len(payload) % 4)
        data = json.loads(base64.urlsafe_b64decode(payload))
        return data.get("preferred_username") or data.get("sub")
    except Exception:
        return None


def format_line(level: str, msg: str, fields: dict) -> str:
    rec = {"ts": datetime.now(timezone.utc).isoformat(), "level": level,
           "logger": "mp.access", "msg": msg}
    rec.update(fields)
    return json.dumps(rec, default=str)


def _emit(level: str, msg: str, fields: dict):
    try:
        sys.stderr.write(format_line(level, msg, fields) + "\n")
        sys.stderr.flush()
    except Exception:
        pass


class AccessLogMiddleware:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        import time
        import uuid
        headers = {k.decode("latin-1").lower(): v.decode("latin-1")
                   for k, v in scope.get("headers", [])}
        rid = headers.get("x-request-id") or uuid.uuid4().hex[:16]
        start = time.perf_counter()
        status = {"code": 0}

        async def send_wrapper(message):
            if message["type"] == "http.response.start":
                status["code"] = message["status"]
                message.setdefault("headers", []).append(
                    (b"x-request-id", rid.encode("latin-1")))
            await send(message)

        try:
            await self.app(scope, receive, send_wrapper)
        finally:
            code = status["code"]
            duration = time.perf_counter() - start
            path = scope.get("path")
            if path != "/metrics":   # don't let scrapes inflate their own numbers
                metrics.observe(scope.get("method", "-"), code, duration)
            _emit("WARNING" if code >= 400 else "INFO", "http_request", {
                "request_id": rid,
                "method": scope.get("method"),
                "path": scope.get("path"),
                "status": code,
                "duration_ms": round(duration * 1000, 1),
                "actor": _claimed_actor(headers.get("authorization", "")) or "-",
                "client": (scope.get("client") or ["-"])[0],
                "xff": headers.get("x-forwarded-for", ""),
            })
