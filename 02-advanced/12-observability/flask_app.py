#!/usr/bin/env python3
"""
Flask app instrumented with Prometheus metrics.
Exposes: request counter, latency histogram, active connections gauge.
"""
import time
import random
import threading
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

REQUEST_LATENCY = Histogram(
    "http_request_duration_seconds",
    "HTTP request latency",
    ["endpoint"],
    buckets=[0.005, 0.01, 0.025, 0.05, 0.1, 0.2, 0.3, 0.5, 1.0, 2.0]
)

ACTIVE_CONNECTIONS = Gauge(
    "http_active_connections",
    "Number of active HTTP connections"
)

ERROR_COUNT = Counter(
    "http_errors_total",
    "Total HTTP errors",
    ["endpoint", "error_type"]
)


def track_request(endpoint):
    """Context manager to track request metrics."""
    ACTIVE_CONNECTIONS.inc()
    start = time.time()
    return start


def finish_request(endpoint, start, status_code):
    ACTIVE_CONNECTIONS.dec()
    elapsed = time.time() - start
    REQUEST_LATENCY.labels(endpoint=endpoint).observe(elapsed)
    REQUEST_COUNT.labels(
        method=request.method,
        endpoint=endpoint,
        status_code=str(status_code)
    ).inc()


@app.route("/metrics")
def metrics():
    return Response(generate_latest(), content_type=CONTENT_TYPE_LATEST)


@app.route("/api/fast")
def fast():
    start = track_request("/api/fast")
    time.sleep(random.uniform(0.005, 0.020))
    finish_request("/api/fast", start, 200)
    return jsonify({"status": "ok", "endpoint": "fast"})


@app.route("/api/slow")
def slow():
    """10% of requests are slow (~500ms)."""
    start = track_request("/api/slow")
    if random.random() < 0.10:
        time.sleep(random.uniform(0.4, 0.6))
    else:
        time.sleep(random.uniform(0.05, 0.15))
    finish_request("/api/slow", start, 200)
    return jsonify({"status": "ok", "endpoint": "slow"})


@app.route("/api/flaky")
def flaky():
    """5% of requests return 500."""
    start = track_request("/api/flaky")
    time.sleep(random.uniform(0.01, 0.05))
    if random.random() < 0.05:
        ERROR_COUNT.labels(endpoint="/api/flaky", error_type="internal_error").inc()
        finish_request("/api/flaky", start, 500)
        return jsonify({"error": "internal server error"}), 500
    finish_request("/api/flaky", start, 200)
    return jsonify({"status": "ok", "endpoint": "flaky"})


if __name__ == "__main__":
    print("Flask app starting on :8000 — metrics at /metrics")
    app.run(host="0.0.0.0", port=8000, threaded=True)
