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
    endpoints = ["/api/fast", "/api/slow", "/api/flaky", "/api/db-call"]
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
    """Compute the p-th percentile using nearest-rank method."""
    if not values:
        return 0
    sorted_vals = sorted(values)
    # nearest-rank: ceil(p/100 * n), 1-indexed → subtract 1 for 0-index
    idx = max(0, int(len(sorted_vals) * p / 100) - 1)
    # for p=99 on 500 values: idx=494 (0-indexed), i.e. the 495th value
    return sorted_vals[idx]


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
    print("  Sending 500 requests across 10 workers to 4 endpoints...")
    print("  Endpoints:")
    print("    /api/fast    5–20ms (cache hit baseline)")
    print("    /api/slow    10% at 400–600ms (tail latency demo, p99 >> p50)")
    print("    /api/flaky   5% HTTP 500 errors (error budget burn demo)")
    print("    /api/db-call 120–180ms total, 100–150ms in DB (dependency instrumentation)")
    print()

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

    # Per-endpoint p99 to show tail latency differences across endpoints
    p99_slow = prom_query(
        'histogram_quantile(0.99, sum(rate(http_request_duration_seconds_bucket{endpoint="/api/slow"}[1m])) by (le))'
    )
    p99_fast = prom_query(
        'histogram_quantile(0.99, sum(rate(http_request_duration_seconds_bucket{endpoint="/api/fast"}[1m])) by (le))'
    )

    # Dependency instrumentation: compare service p99 vs DB call p99
    p99_db_dep = prom_query(
        'histogram_quantile(0.99, sum(rate(dependency_call_duration_seconds_bucket{dependency="postgres"}[1m])) by (le))'
    )
    p99_db_svc = prom_query(
        'histogram_quantile(0.99, sum(rate(http_request_duration_seconds_bucket{endpoint="/api/db-call"}[1m])) by (le))'
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
    print()
    print(f"  Per-endpoint p99 (shows tail latency divergence):")
    print(f"    /api/fast    p99 = {fmt(p99_fast, 'ms', 1000)}")
    print(f"    /api/slow    p99 = {fmt(p99_slow, 'ms', 1000)}  ← tail latency victim")
    print()
    print(f"  Dependency instrumentation (/api/db-call):")
    print(f"    Service p99  = {fmt(p99_db_svc, 'ms', 1000)}")
    print(f"    DB query p99 = {fmt(p99_db_dep, 'ms', 1000)}  ← most of service latency is the DB")
    print(f"    Diagnosis: if DB p99 ≈ service p99, the DB is the bottleneck, not your code.")

    print(f"""
  PromQL explained:
    rate(metric[1m])              → per-second rate over last 1 minute
    histogram_quantile(0.99, …)   → p99 from bucket counts (approximation)
    sum(…) by (le)                → aggregate across instances, keep 'le' label
    {{label="value"}}              → label selector filters to one time series

  Key insight: histograms store bucket counts, not raw values.
  This enables low-overhead percentile computation across all instances.
  Accuracy depends on bucket placement — buckets MUST straddle the SLO threshold.
  Counter precision: exact. Histogram precision: ~1-5% depending on buckets.
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

    # ── Phase 4: Error Budget Burn Rate ───────────────────────────
    section("Phase 4: Error Budget Burn Rate Analysis")
    print("""  Error budget burn rate = (actual error rate) / (allowed error rate)

  SLO: 99.9% success → error budget = 0.1% of requests per 30 days
  Burn rate 1.0 = consuming budget at steady state (exhausted in 30 days)
  Burn rate 14.4 = budget exhausted in ~2 hours → PAGE IMMEDIATELY
  Burn rate 6.0  = budget exhausted in ~5 days → page within 6 hours
  Burn rate 3.0  = budget exhausted in ~10 days → create a ticket
""")

    SLO_SUCCESS_RATE = 0.999   # 99.9% SLO
    BUDGET_FRACTION = 1.0 - SLO_SUCCESS_RATE   # 0.001

    if err_rate is not None:
        actual_error_fraction = err_rate / 100.0
        if BUDGET_FRACTION > 0:
            burn_rate = actual_error_fraction / BUDGET_FRACTION
        else:
            burn_rate = 0.0

        print(f"  Measured error rate:      {err_rate:.2f}%")
        print(f"  SLO error budget:         {BUDGET_FRACTION * 100:.1f}%")
        print(f"  Burn rate:                {burn_rate:.1f}×")

        if burn_rate >= 14.4:
            severity = "CRITICAL — PAGE IMMEDIATELY (budget exhausted in ~2h)"
        elif burn_rate >= 6.0:
            severity = "HIGH — PAGE within 6h window (budget exhausted in ~5d)"
        elif burn_rate >= 3.0:
            severity = "MEDIUM — create ticket (budget exhausted in ~10d)"
        elif burn_rate >= 1.0:
            severity = "LOW — consuming budget, monitor closely"
        else:
            severity = "OK — burn rate under 1× (budget not being consumed)"

        print(f"  Alert severity:           {severity}")
    else:
        print("  Error rate N/A — Prometheus data not yet available")

    print("""
  Multi-window burn rate alert (Google SRE recommended pattern):
    Alert fires only when BOTH windows exceed the burn threshold:
    - Fast window (5m): catches the spike quickly
    - Slow window (1h): confirms it's sustained, not a blip
    This prevents false positives from transient traffic spikes.

  PromQL for 14.4× burn rate (99.9% SLO):
    (
      rate(http_requests_total{status_code=~"5.."}[1h])
      / rate(http_requests_total[1h]) > 14.4 * 0.001
    ) AND (
      rate(http_requests_total{status_code=~"5.."}[5m])
      / rate(http_requests_total[5m]) > 14.4 * 0.001
    )
""")

    # ── Phase 5: RED Method Summary ────────────────────────────────
    section("Phase 5: RED Method Summary")
    rate_str     = fmt(rate, ' req/s')
    err_str      = fmt(err_rate, '%')
    dur_str      = f"p50={fmt(p50_prom, 'ms', 1000)}, p95={fmt(p95_prom, 'ms', 1000)}, p99={fmt(p99_prom, 'ms', 1000)}"

    print(f"""
  RED Method (for every service):
  ┌─────────────────────────────────────────────────────────────┐
  │  R — Rate     {rate_str:<46}│
  │  E — Errors   {err_str:<46}│
  │  D — Duration {dur_str:<46}│
  └─────────────────────────────────────────────────────────────┘

  USE Method (for resources: CPU, disk, network):
    U — Utilization  (% time resource is busy)
    S — Saturation   (queue depth, wait time)
    E — Errors       (hardware errors, drops)

  Metric types:
    Counter   → always increasing (requests_total, errors_total)
                never query directly — use rate() for per-second rate
                irate() for instantaneous rate (more volatile, good for dashboards)
    Gauge     → can go up/down (active_connections, memory_bytes)
                query directly; supports max_over_time(), avg_over_time()
    Histogram → bucket counts for latency distribution
                query with histogram_quantile() for percentiles
                buckets must be defined at instrumentation time
                accuracy depends on bucket alignment with SLO thresholds
    Summary   → pre-computed quantiles on the client side
                more accurate per-instance than histogram
                CANNOT be aggregated across instances — avoid in multi-replica deploys

  Cardinality math (avoid the common trap):
    labels: endpoint(3) × status_code(10) × method(3) = 90 series per metric
    50 metrics × 90 series = 4,500 total series → safe
    Adding user_id(1M users): 50 × 1M × 10 × 3 = 1.5B series → OOM in minutes

  OpenTelemetry note:
    OTel SDK emits metrics, traces, and logs with a single instrumentation.
    Send to OTel Collector → fan out to Prometheus, Jaeger, Loki simultaneously.
    Vendor-neutral: swap Prometheus for Datadog without re-instrumenting code.

  Grafana dashboard: http://localhost:3000 (admin/admin)
  Prometheus UI:     http://localhost:9090

  Next: ../13-security-at-scale/
""")


if __name__ == "__main__":
    main()
