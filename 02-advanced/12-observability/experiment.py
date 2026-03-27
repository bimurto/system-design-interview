#!/usr/bin/env python3
"""
Observability Lab — Prometheus metrics, SLO analysis, RED method.

What this demonstrates:
  1. Send 500 requests with varied latencies (10% slow, 5% errors)
  2. Query Prometheus API: request rate, error rate, p50/p95/p99 latency
  3. SLO analysis: is the p99 latency SLO of 200ms being met?
  4. RED method summary (Rate, Errors, Duration)
  5. Grafana dashboard available at http://localhost:3000
"""

import os
import time
import random
import threading
import requests

APP_HOST  = os.environ.get("APP_HOST", "localhost")
PROM_HOST = os.environ.get("PROM_HOST", "localhost")
APP_URL   = f"http://{APP_HOST}:8000"
PROM_URL  = f"http://{PROM_HOST}:9090"


def section(title):
    print(f"\n{'=' * 65}")
    print(f"  {title}")
    print("=" * 65)


def prom_query(expr):
    """Run an instant PromQL query, return float or None."""
    try:
        r = requests.get(
            f"{PROM_URL}/api/v1/query",
            params={"query": expr},
            timeout=5
        )
        data = r.json()
        results = data.get("data", {}).get("result", [])
        if results:
            return float(results[0]["value"][1])
    except Exception:
        pass
    return None


def send_requests(count, workers=10):
    """Send `count` requests split across endpoints."""
    endpoints = ["/api/fast", "/api/slow", "/api/flaky"]
    lock = threading.Lock()
    stats = {"ok": 0, "err": 0, "latencies": []}

    def worker(n):
        for _ in range(n):
            endpoint = random.choice(endpoints)
            start = time.time()
            try:
                resp = requests.get(f"{APP_URL}{endpoint}", timeout=5)
                elapsed = (time.time() - start) * 1000
                with lock:
                    if resp.status_code >= 500:
                        stats["err"] += 1
                    else:
                        stats["ok"] += 1
                    stats["latencies"].append(elapsed)
            except Exception:
                with lock:
                    stats["err"] += 1

    per_worker = count // workers
    threads = [threading.Thread(target=worker, args=(per_worker,)) for _ in range(workers)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    return stats


def percentile(values, p):
    if not values:
        return 0
    sorted_vals = sorted(values)
    idx = int(len(sorted_vals) * p / 100)
    return sorted_vals[min(idx, len(sorted_vals) - 1)]


def main():
    section("OBSERVABILITY LAB")
    print("""
  Three Pillars of Observability:

  Metrics   → numeric aggregates over time (Prometheus)
  Traces    → per-request causal chain (Jaeger, Zipkin)
  Logs      → structured event records (Loki, ELK)

  This lab focuses on Metrics + the RED method for API health.

  RED Method:
    Rate     → requests per second
    Errors   → error rate (%)
    Duration → latency percentiles (p50, p95, p99)
""")

    # ── Phase 1: Send 500 requests ─────────────────────────────────
    section("Phase 1: Sending 500 Requests (10% slow, 5% errors)")
    print("  Sending 500 requests across 10 workers to 3 endpoints...")
    print("  Endpoints: /api/fast (5-20ms), /api/slow (10% at 500ms), /api/flaky (5% errors)\n")

    start_time = time.time()
    stats = send_requests(500, workers=10)
    elapsed_total = time.time() - start_time

    total = stats["ok"] + stats["err"]
    error_pct = stats["err"] / total * 100 if total > 0 else 0
    throughput = total / elapsed_total
    p50 = percentile(stats["latencies"], 50)
    p95 = percentile(stats["latencies"], 95)
    p99 = percentile(stats["latencies"], 99)

    print(f"  Sent {total} requests in {elapsed_total:.1f}s")
    print(f"  Throughput: {throughput:.1f} req/s")
    print(f"  Errors:     {stats['err']} ({error_pct:.1f}%)")
    print(f"  Latency (client-side measured):")
    print(f"    p50 = {p50:.1f}ms")
    print(f"    p95 = {p95:.1f}ms")
    print(f"    p99 = {p99:.1f}ms")

    # Wait for Prometheus to scrape (up to 15s)
    print("\n  Waiting 15s for Prometheus to scrape metrics...")
    time.sleep(15)

    # ── Phase 2: Query Prometheus ──────────────────────────────────
    section("Phase 2: Querying Prometheus API (PromQL)")
    print("  Using Prometheus HTTP API to query aggregated metrics:\n")

    # Request rate
    rate = prom_query("sum(rate(http_requests_total[1m]))")
    # Error rate
    err_rate = prom_query(
        "100 * sum(rate(http_requests_total{status_code=~\"5..\"}[1m])) "
        "/ sum(rate(http_requests_total[1m]))"
    )
    # Latency percentiles from histogram
    p50_prom = prom_query(
        "histogram_quantile(0.50, sum(rate(http_request_duration_seconds_bucket[1m])) by (le))"
    )
    p95_prom = prom_query(
        "histogram_quantile(0.95, sum(rate(http_request_duration_seconds_bucket[1m])) by (le))"
    )
    p99_prom = prom_query(
        "histogram_quantile(0.99, sum(rate(http_request_duration_seconds_bucket[1m])) by (le))"
    )

    def fmt(val, unit="", scale=1):
        if val is None:
            return "N/A (scrape pending)"
        return f"{val * scale:.1f}{unit}"

    print(f"  Request rate:  {fmt(rate, ' req/s')}")
    print(f"  Error rate:    {fmt(err_rate, '%')}")
    print(f"  Latency p50:   {fmt(p50_prom, 'ms', 1000)}")
    print(f"  Latency p95:   {fmt(p95_prom, 'ms', 1000)}")
    print(f"  Latency p99:   {fmt(p99_prom, 'ms', 1000)}")

    print(f"""
  PromQL explained:
    rate(metric[1m])          → per-second rate over last 1 minute
    histogram_quantile(0.99,…) → compute p99 from histogram buckets
    sum(…) by (label)          → aggregate across instances, split by label

  Key insight: histograms store bucket counts, not raw values.
  This is why Prometheus can compute percentiles with low storage overhead.
  Counter precision: exact. Histogram precision: ~1-5% error depending on buckets.
""")

    # ── Phase 3: SLO Analysis ─────────────────────────────────────
    section("Phase 3: SLO Analysis")
    print("""  Service Level Objective definitions:
    SLI (Service Level Indicator) — a metric: "p99 latency in ms"
    SLO (Service Level Objective) — the target: "p99 < 200ms"
    SLA (Service Level Agreement) — contractual consequence if SLO missed
    Error Budget = 1 - SLO = allowed fraction of failures
""")

    SLO_P99_MS   = 200.0   # p99 latency must be < 200ms
    SLO_ERROR_PCT = 1.0    # error rate must be < 1%
    SLO_RATE_RPS  = 5.0    # minimum throughput > 5 req/s

    def check_slo(name, value, threshold, unit, invert=False):
        if value is None:
            print(f"  {'⚠':2} {name:<35} N/A (waiting for data)")
            return
        violated = (value > threshold) if not invert else (value < threshold)
        symbol = "VIOLATED" if violated else "OK      "
        print(f"  {symbol}  {name:<35} {value:8.1f}{unit}  (SLO: {'<' if not invert else '>'}{threshold}{unit})")

    check_slo("p99 latency",   (p99_prom or 0) * 1000, SLO_P99_MS, "ms")
    check_slo("error rate",    err_rate or 0,           SLO_ERROR_PCT, "%")
    check_slo("request rate",  rate or 0,               SLO_RATE_RPS, " req/s", invert=True)

    print(f"""
  Error budget concept:
    SLO: 99.9% of requests succeed (error budget = 0.1%)
    If we're burning budget too fast → freeze deployments
    If budget is healthy → ship features faster (accept more risk)

  Prometheus alert example:
    ALERT HighP99Latency
    IF histogram_quantile(0.99, ...) > 0.200
    FOR 5m
    LABELS {{ severity="warning" }}
    ANNOTATIONS {{ summary="p99 latency SLO violated" }}
""")

    # ── Phase 4: RED Method Summary ────────────────────────────────
    section("Phase 4: RED Method Summary")
    print(f"""
  RED Method (for every service):
  ┌─────────────────────────────────────────────────────────────┐
  │  R — Rate     {fmt(rate, ' req/s'):<46}│
  │  E — Errors   {fmt(err_rate, '%'):<46}│
  │  D — Duration p50={fmt(p50_prom, 'ms', 1000)}, p95={fmt(p95_prom, 'ms', 1000)}, p99={fmt(p99_prom, 'ms', 1000):<20}│
  └─────────────────────────────────────────────────────────────┘

  USE Method (for resources: CPU, disk, network):
    U — Utilization  (% time resource is busy)
    S — Saturation   (queue depth, wait time)
    E — Errors       (hardware errors, drops)

  Metric types:
    Counter   → always increasing (requests_total, errors_total)
                query with rate() to get per-second rate
    Gauge     → can go up/down (active_connections, memory_bytes)
                query directly
    Histogram → bucket counts for latency distribution
                query with histogram_quantile() for percentiles
    Summary   → pre-computed percentiles (less flexible than histogram)

  Grafana dashboard: http://localhost:3000 (admin/admin)
  Prometheus UI:     http://localhost:9090

  Next: ../12-security-at-scale/
""")


if __name__ == "__main__":
    main()
