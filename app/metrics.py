"""Minimal Prometheus metrics, exposed at /metrics and fed by the access-log
middleware (one observe() per request).

ponytail: hand-rolled text exposition, no prometheus_client dependency. Label
values are server-controlled (HTTP method, status code) so there is nothing to
escape. In-process + single-worker, like the rate limiter; for multi-worker,
scrape each replica separately or switch to prometheus_client with multiprocess
mode. Upgrade path is a drop-in: replace observe()/render() with a Counter +
Histogram from prometheus_client.
"""
from collections import defaultdict
from threading import Lock

# Cumulative histogram upper bounds (seconds), Prometheus-style.
_BUCKETS = (0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0)


class Metrics:
    def __init__(self):
        self._lock = Lock()
        self._count: dict[tuple[str, str], int] = defaultdict(int)  # (method, status) -> n
        self._bucket: dict[float, int] = defaultdict(int)           # le -> cumulative n
        self._sum = 0.0
        self._total = 0

    def observe(self, method: str, status: int, duration_s: float):
        with self._lock:
            self._count[(method, str(status))] += 1
            self._sum += duration_s
            self._total += 1
            for b in _BUCKETS:
                if duration_s <= b:
                    self._bucket[b] += 1   # each le>=duration counts it → cumulative

    def render(self) -> str:
        lines = ["# HELP http_requests_total Total HTTP requests.",
                 "# TYPE http_requests_total counter"]
        with self._lock:
            for (method, status), n in sorted(self._count.items()):
                lines.append(f'http_requests_total{{method="{method}",status="{status}"}} {n}')
            lines += ["# HELP http_request_duration_seconds HTTP request latency.",
                      "# TYPE http_request_duration_seconds histogram"]
            for b in _BUCKETS:
                lines.append(f'http_request_duration_seconds_bucket{{le="{b}"}} {self._bucket[b]}')
            lines.append(f'http_request_duration_seconds_bucket{{le="+Inf"}} {self._total}')
            lines.append(f"http_request_duration_seconds_sum {self._sum}")
            lines.append(f"http_request_duration_seconds_count {self._total}")
        return "\n".join(lines) + "\n"


metrics = Metrics()


if __name__ == "__main__":   # self-check: python -m app.metrics
    metrics.observe("GET", 200, 0.03)
    metrics.observe("GET", 200, 0.4)
    metrics.observe("POST", 500, 7.0)
    out = metrics.render()
    assert 'http_requests_total{method="GET",status="200"} 2' in out
    assert 'http_requests_total{method="POST",status="500"} 1' in out
    # cumulative buckets: le=0.05 covers the 0.03 request only (1);
    # le=0.5 covers 0.03+0.4 (2); +Inf covers all 3; count == 3.
    assert 'http_request_duration_seconds_bucket{le="0.05"} 1' in out
    assert 'http_request_duration_seconds_bucket{le="0.5"} 2' in out
    assert 'http_request_duration_seconds_bucket{le="+Inf"} 3' in out
    assert "http_request_duration_seconds_count 3" in out
    print("metrics self-check OK")
