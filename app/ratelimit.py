"""Per-caller sliding-window rate limiting.

ponytail: in-process counters — correct for a single worker (our current dev
and small-deployment shape). For multiple workers move the window to Redis;
the audit's resource-exhaustion concern is otherwise addressed here plus the
proxy's global httpx connection cap (max_connections=100) and response/request
size caps.

Caller key: the first X-Forwarded-For IP if present (behind nginx/the vite
proxy), else the socket peer. A production nginx should ALSO rate-limit at the
edge — this is defence in depth, not the only layer.
"""
import time
from collections import defaultdict, deque

from fastapi import HTTPException, Request


class SlidingWindow:
    def __init__(self, limit: int, window_s: int = 60):
        self.limit = limit
        self.window = window_s
        self._hits: dict[str, deque] = defaultdict(deque)
        self._last_sweep = time.monotonic()

    def check(self, key: str) -> tuple[bool, int]:
        """Returns (allowed, retry_after_seconds)."""
        now = time.monotonic()
        self._sweep(now)
        dq = self._hits[key]
        cutoff = now - self.window
        while dq and dq[0] <= cutoff:
            dq.popleft()
        if len(dq) >= self.limit:
            return False, int(self.window - (now - dq[0])) + 1
        dq.append(now)
        return True, 0

    def _sweep(self, now: float):
        """Drop callers with no hits left in the window. Bounds memory to
        recently-active callers — without this a rotating X-Forwarded-For (or
        just many one-off clients) would grow `_hits` unboundedly. Runs at most
        once per window, so it's amortized O(1) per check."""
        if now - self._last_sweep < self.window:
            return
        self._last_sweep = now
        cutoff = now - self.window
        for k in list(self._hits):
            dq = self._hits[k]
            while dq and dq[0] <= cutoff:
                dq.popleft()
            if not dq:
                del self._hits[k]


def client_key(request: Request) -> str:
    xff = request.headers.get("x-forwarded-for", "")
    if xff:
        return xff.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


def limiter(per_minute: int):
    """Build a FastAPI dependency enforcing `per_minute` requests per caller."""
    window = SlidingWindow(per_minute, 60)

    def dependency(request: Request):
        allowed, retry = window.check(client_key(request))
        if not allowed:
            raise HTTPException(429, "rate limit exceeded",
                                headers={"Retry-After": str(retry)})

    return dependency
