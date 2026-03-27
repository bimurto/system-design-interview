#!/usr/bin/env python3
"""
Flask app instrumented with Prometheus metrics.

Exposes:
  - http_requests_total      Counter  (method, endpoint, status_code)
  - http_request_duration_seconds  Histogram  (endpoint)
  - http_active_connections  Gauge    (current in-flight requests)
  - http_errors_total        Counter  (endpoint, error_type)
  - dependency_call_duration_seconds  Histogram  (dependency, operation)

Endpoints:
  /api/fast      5–20ms baseline latency
  /api/slow      90% at 50–150ms, 10% at 400–600ms (tail latency demo)
  /api/flaky     5% error rate (SLO violation demo)
  /api/db-call   Simulates a service calling a downstream dependency;
                 instruments the dependency call separately so you can
                 see that the service latency is dominated by the dep.
  /health        Simple health check (not instrumented — avoid noise)
  /metrics       Prometheus scrape endpoint
"""
import contextlib
import time
import random
from flask import Flask, Response, request, jsonify
from prometheus_client import (
    Counter, Histogram, Gauge, generate_latest, CONTENT_TYPE_LATEST
)

app = Flask(__name__)

# ── Metrics ────────────────────────────────────────────────────────────────

REQUEST_COUNT = Counter(
    "http_requests_total",
    "Total HTTP requests",
    ["method", "endpoint", "status_code"]
)

# Buckets aligned to SLO thresholds (200ms) and slow-endpoint behaviour (500ms).
# Rule: place a bucket at every SLO boundary so histogram_quantile() is accurate.
REQUEST_LATENCY = Histogram(
    "http_request_duration_seconds",
    "HTTP request latency in seconds",
    ["endpoint"],
    buckets=[0.005, 0.010, 0.025, 0.050, 0.100, 0.200, 0.300, 0.500, 1.0, 2.0]
)

ACTIVE_CONNECTIONS = Gauge(
    "http_active_connections",
    "Number of in-flight HTTP requests (gauge — goes up and down)"
)

ERROR_COUNT = Counter(
    "http_errors_total",
    "Total HTTP errors",
    ["endpoint", "error_type"]
)

# Demonstrates dependency instrumentation: measure the downstream call separately
# so you can identify whether latency lives in your service or in a dependency.
DEPENDENCY_LATENCY = Histogram(
    "dependency_call_duration_seconds",
    "Latency of calls to downstream dependencies",
    ["dependency", "operation"],
    buckets=[0.005, 0.010, 0.025, 0.050, 0.100, 0.200, 0.500, 1.0]
)


@contextlib.contextmanager
def track_request(endpoint):
    """Context manager for request lifecycle: active connections + latency + count."""
    ACTIVE_CONNECTIONS.inc()
    start = time.time()
    status_code = 200
    try:
        yield lambda code: setattr(  # allow handler to set the status code
            track_request, "_code", code
        )
    finally:
        elapsed = time.time() - start
        REQUEST_LATENCY.labels(endpoint=endpoint).observe(elapsed)
        ACTIVE_CONNECTIONS.dec()
    return status_code


def record_request(endpoint, start, status_code):
    """Record completed request metrics (counter + histogram)."""
    elapsed = time.time() - start
    REQUEST_LATENCY.labels(endpoint=endpoint).observe(elapsed)
    REQUEST_COUNT.labels(
        method=request.method,
        endpoint=endpoint,
        status_code=str(status_code)
    ).inc()
    ACTIVE_CONNECTIONS.dec()


def begin_request():
    """Increment active connections counter; return start timestamp."""
    ACTIVE_CONNECTIONS.inc()
    return time.time()


# ── Routes ─────────────────────────────────────────────────────────────────

@app.route("/health")
def health():
    """Health check — not instrumented to avoid polluting RED metrics."""
    return jsonify({"status": "ok"}), 200


@app.route("/metrics")
def metrics():
    """Prometheus scrape endpoint — returns all metric families."""
    return Response(generate_latest(), content_type=CONTENT_TYPE_LATEST)


@app.route("/api/fast")
def fast():
    """Baseline endpoint: 5–20ms latency, no errors. Represents a cache hit."""
    start = begin_request()
    time.sleep(random.uniform(0.005, 0.020))
    record_request("/api/fast", start, 200)
    return jsonify({"status": "ok", "endpoint": "fast"})


@app.route("/api/slow")
def slow():
    """Tail latency demo: 90% of requests at 50–150ms, 10% at 400–600ms.
    This creates a clearly visible p99 >> p50 gap, showing why averages lie.
    With these numbers, p50 ≈ 100ms but p99 ≈ 500ms — the tail is 5× the median.
    """
    start = begin_request()
    if random.random() < 0.10:
        # Tail: simulate a slow DB query, GC pause, or cold cache miss
        time.sleep(random.uniform(0.4, 0.6))
    else:
        time.sleep(random.uniform(0.05, 0.15))
    record_request("/api/slow", start, 200)
    return jsonify({"status": "ok", "endpoint": "slow"})


@app.route("/api/flaky")
def flaky():
    """Error budget demo: 5% of requests return HTTP 500.
    With a 99% SLO this is burning the error budget at 5× the allowed rate.
    """
    start = begin_request()
    time.sleep(random.uniform(0.010, 0.050))
    if random.random() < 0.05:
        ERROR_COUNT.labels(endpoint="/api/flaky", error_type="internal_error").inc()
        record_request("/api/flaky", start, 500)
        return jsonify({"error": "internal server error"}), 500
    record_request("/api/flaky", start, 200)
    return jsonify({"status": "ok", "endpoint": "flaky"})


@app.route("/api/db-call")
def db_call():
    """Dependency instrumentation demo.

    Total request latency: ~120–180ms
    DB query latency:      ~100–150ms  (measured separately)

    When you see service p99 = 160ms and db_query p99 = 150ms, the diagnosis
    is immediate: the database is the bottleneck, not your service logic.
    Without per-dependency instrumentation you would only know total latency.
    """
    start = begin_request()

    # Simulate non-DB work: input validation, auth check, response serialization
    time.sleep(random.uniform(0.005, 0.015))

    # Instrument the downstream DB call separately
    db_start = time.time()
    time.sleep(random.uniform(0.100, 0.150))  # simulated slow DB query
    db_elapsed = time.time() - db_start
    DEPENDENCY_LATENCY.labels(
        dependency="postgres",
        operation="user_lookup"
    ).observe(db_elapsed)

    # Simulate post-DB work: cache write, response assembly
    time.sleep(random.uniform(0.005, 0.015))

    record_request("/api/db-call", start, 200)
    return jsonify({
        "status": "ok",
        "endpoint": "db-call",
        "db_latency_ms": round(db_elapsed * 1000, 1)
    })


if __name__ == "__main__":
    print("Flask app starting on :8000")
    print("  Endpoints: /api/fast  /api/slow  /api/flaky  /api/db-call")
    print("  Metrics:   http://localhost:8000/metrics")
    app.run(host="0.0.0.0", port=8000, threaded=True)
