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
    """Context manager for safe request lifecycle instrumentation.

    Guarantees ACTIVE_CONNECTIONS is always decremented — even if the handler
    raises an unhandled exception. Without this guarantee, a crash loop would
    cause the gauge to monotonically increase, producing a false overload signal
    that looks like a traffic surge instead of a bug.

    This is the preferred pattern over bare begin_request() + record_request().

    Usage:
        with track_request("/api/fast") as set_status:
            ...do work...
            set_status(200)
    """
    ACTIVE_CONNECTIONS.inc()
    start = time.time()
    status_code_holder = [200]

    def set_status(code):
        status_code_holder[0] = code

    try:
        yield set_status
    except Exception:
        # Record as 500 so the error is visible in RED metrics even if Flask
        # never sends a response (e.g. unhandled exception before return).
        status_code_holder[0] = 500
        raise
    finally:
        elapsed = time.time() - start
        REQUEST_LATENCY.labels(endpoint=endpoint).observe(elapsed)
        REQUEST_COUNT.labels(
            method=request.method,
            endpoint=endpoint,
            status_code=str(status_code_holder[0])
        ).inc()
        ACTIVE_CONNECTIONS.dec()


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
    with track_request("/api/fast") as set_status:
        time.sleep(random.uniform(0.005, 0.020))
        set_status(200)
        return jsonify({"status": "ok", "endpoint": "fast"})


@app.route("/api/slow")
def slow():
    """Tail latency demo: 90% of requests at 50–150ms, 10% at 400–600ms.
    This creates a clearly visible p99 >> p50 gap, showing why averages lie.
    With these numbers, p50 ≈ 100ms but p99 ≈ 500ms — the tail is 5× the median.
    """
    with track_request("/api/slow") as set_status:
        if random.random() < 0.10:
            # Tail: simulate a slow DB query, GC pause, or cold cache miss
            time.sleep(random.uniform(0.4, 0.6))
        else:
            time.sleep(random.uniform(0.05, 0.15))
        set_status(200)
        return jsonify({"status": "ok", "endpoint": "slow"})


@app.route("/api/flaky")
def flaky():
    """Error budget demo: 5% of requests return HTTP 500.
    With a 99.9% SLO this is burning the error budget at 50× the allowed rate.
    """
    with track_request("/api/flaky") as set_status:
        time.sleep(random.uniform(0.010, 0.050))
        if random.random() < 0.05:
            ERROR_COUNT.labels(endpoint="/api/flaky", error_type="internal_error").inc()
            set_status(500)
            return jsonify({"error": "internal server error"}), 500
        set_status(200)
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
    with track_request("/api/db-call") as set_status:
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

        set_status(200)
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
