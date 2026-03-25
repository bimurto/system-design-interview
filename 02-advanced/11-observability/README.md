# Observability

**Prerequisites:** `../10-cdn-and-edge/`
**Next:** `../12-security-at-scale/`

---

## Concept

Observability is the ability to understand what a system is doing from the outside by examining its outputs. A system is "observable" when you can answer any question about its internal state from the data it emits, without deploying new instrumentation. This matters because distributed systems fail in complex, unexpected ways — a service may be healthy by all traditional monitors yet silently returning wrong data, or the p99 latency of one endpoint may be degrading while the average looks fine. Good observability lets you find and diagnose these issues in minutes rather than hours.

The three pillars of observability are metrics, traces, and logs. Metrics are numeric aggregates over time — request rates, error counts, latency distributions. They are cheap to store, query, and alert on, making them ideal for dashboards and automated alerting. Traces follow a single request as it flows through multiple services, recording how long each step takes and what data was passed. They are essential for debugging latency problems in microservice architectures where a single slow dependency can cascade. Logs are structured event records with arbitrary key-value context. They are the highest-fidelity signal but the most expensive to store and query at scale.

Prometheus is the de facto standard for metrics in cloud-native systems. It uses a pull-based model: the Prometheus server scrapes an HTTP `/metrics` endpoint on each service every few seconds. Services expose metrics using the `prometheus_client` library, which maintains in-memory counters, gauges, and histograms. PromQL, Prometheus's query language, can compute rates, percentiles, and aggregations across all scraped metrics. Grafana connects to Prometheus as a data source and renders dashboards that update in real time.

Metric types matter for correctness. A Counter is a monotonically increasing integer (total requests, total errors). Never query a counter directly — use `rate(counter[window])` to get the per-second increase. A Gauge is a value that can go up or down (active connections, queue depth, CPU%). A Histogram records observations in predefined buckets, enabling `histogram_quantile()` to compute p50/p95/p99 latency from aggregated data across all instances. Summaries compute quantiles on the client side — they are more accurate than histograms for a single instance but cannot be aggregated across instances, making them less useful in multi-instance deployments.

SLO (Service Level Objective) is a target for a measurable SLI (Service Level Indicator). For example: "99th percentile request latency must be below 200ms, measured over a 30-day rolling window." The SLA (Service Level Agreement) is the contractual commitment — what happens if the SLO is missed (credits, refunds). The error budget is 1 minus the SLO: if your SLO is 99.9% availability, your error budget is 0.1% of requests per month that can fail. Error budgets are a powerful tool: when the budget is healthy, teams ship features faster; when it is burning quickly, deployments are frozen until the service stabilises.

## How It Works

**Prometheus data model:** Every metric is identified by a name and a set of key-value labels. `http_requests_total{endpoint="/api/users", status_code="200"}` is a distinct time series from `http_requests_total{endpoint="/api/users", status_code="500"}`. This high-cardinality labelling enables powerful aggregations but also risks label explosion — never use unbounded values (user IDs, request IDs) as label values.

**Histogram percentiles:**

```
  Prometheus histogram stores bucket counts, not raw values:

  Bucket    Count (cumulative)
  ≤5ms      120
  ≤10ms     450
  ≤25ms     890
  ≤50ms     980
  ≤100ms    998
  ≤200ms    999
  ≤500ms    1000  ← all requests
  +Inf      1000

  histogram_quantile(0.99, ...) finds the bucket where 99% of
  requests fall. With 1000 total: 990th request lands in ≤100ms bucket.
  → p99 ≈ 100ms

  This is an approximation — actual value is between bucket boundaries.
  Finer buckets = more precision, more memory.
```

**RED method vs USE method:**

```
  RED (for services — user-facing behaviour):
    Rate     = sum(rate(http_requests_total[5m]))
    Errors   = rate(http_requests_total{status_code=~"5.."}[5m])
    Duration = histogram_quantile(0.99, rate(http_request_duration_seconds_bucket[5m]))

  USE (for resources — infrastructure health):
    Utilization = CPU%, disk I/O%, network saturation
    Saturation  = queue length, run queue depth
    Errors      = disk errors, packet drops, hardware faults
```

**Distributed tracing (span model):**

```
  Trace = [Request abc123]
  ├─ Span: api-gateway (5ms)
  ├─ Span: auth-service (12ms)
  │    └─ Span: redis-lookup (2ms)
  └─ Span: user-service (87ms)
       ├─ Span: db-query (75ms)   ← the bottleneck!
       └─ Span: cache-write (8ms)

  Each span records: service, operation, start time, duration, tags, logs
  Trace context propagated via HTTP headers: traceparent (W3C standard)
  Tools: Jaeger, Zipkin, AWS X-Ray, Honeycomb
```

### Trade-offs

| Signal | Cost | Freshness | Aggregatable | Best For |
|---|---|---|---|---|
| Metrics | Low (counters/histograms) | Seconds | Yes | Alerts, dashboards, SLOs |
| Traces | Medium (per-request) | Real-time | No | Latency debugging, dependency mapping |
| Logs | High (full text) | Real-time | Limited | Error context, audit trails |
| Profiling | High (continuous) | Minutes | No | CPU/memory bottleneck analysis |

### Failure Modes

**High-cardinality label explosion:** Adding a label like `user_id` to a counter creates one time series per user. With 10M users, Prometheus stores 10M time series — exhausting memory within minutes. Always bound label cardinality: use endpoint names, status codes, and service names, never user IDs, request IDs, or IP addresses.

**Alert fatigue from poor SLO calibration:** Setting SLOs too tight (p99 < 50ms for a database-backed endpoint) causes constant alerts that on-call engineers learn to ignore. SLOs should reflect the real user experience threshold — what latency actually bothers users — not the best-case performance.

**Scrape lag hiding incidents:** Prometheus scrapes every 5-15 seconds. A spike that lasts 3 seconds (e.g., a cache stampede) may never appear in metrics. For sub-second incidents, distributed tracing or real-time logging is needed. Rate() over a 1-minute window smooths out sub-minute spikes.

**Histogram bucket misalignment:** If your latency histogram buckets are `[0.1, 0.5, 1.0, 5.0]` seconds but your SLO is `p99 < 200ms`, Prometheus cannot accurately compute whether the SLO is met — it can only say "between 100ms and 500ms." Align histogram buckets to your SLO thresholds.

## Interview Talking Points

- "The three pillars are metrics (aggregates, cheap, alertable), traces (per-request causality, expensive), and logs (full context, most expensive). Use metrics for dashboards and alerts, traces for latency debugging, logs for error context."
- "RED method for services: Rate, Errors, Duration. These three metrics answer 'is my service healthy?' for any user-facing endpoint."
- "Prometheus histograms store bucket counts, not raw values. `histogram_quantile()` interpolates percentiles from buckets. This is why you must define buckets around your SLO thresholds."
- "SLI is what you measure, SLO is your target, SLA is the contract. Error budget = 1 - SLO. Use error budgets to balance reliability vs feature velocity."
- "Never use high-cardinality values as Prometheus label values — user IDs, request IDs, URLs with path parameters. Each unique label combination is a separate time series in memory."
- "Distributed tracing propagates a trace ID across service boundaries via headers (`traceparent` in W3C Trace Context). Each service creates a span, reports to a central collector (Jaeger, Zipkin). The trace shows the full latency breakdown across all services."

## Hands-on Lab

**Time:** ~3 minutes
**Services:** Flask app (metrics) + Prometheus + Grafana

### Setup

```bash
cd system-design-interview/02-advanced/11-observability/
docker compose up
```

### Experiment

The script runs four phases automatically:

1. Sends 500 requests across three endpoints — `/api/fast` (5-20ms), `/api/slow` (10% at 500ms), `/api/flaky` (5% errors). Prints client-side latency percentiles.
2. Waits 15 seconds for Prometheus to scrape, then queries the Prometheus HTTP API using PromQL to retrieve request rate, error rate, and p50/p95/p99 latency from histogram buckets.
3. Evaluates three SLOs against the measured values: p99 < 200ms, error rate < 1%, throughput > 5 req/s. Prints VIOLATED or OK for each.
4. Prints a full RED method summary and explains metric types (Counter, Gauge, Histogram).

### Break It

Add a high-cardinality label to trigger Prometheus memory pressure:

```python
# In flask_app.py, add user_id as a label:
REQUEST_COUNT.labels(method="GET", endpoint="/api/fast", status_code="200", user_id="user_12345")
# With 1M users this creates 1M time series — watch Prometheus RSS grow
```

Or deliberately violate the SLO:

```python
# Change /api/slow to always sleep 300ms (will push p99 > 200ms):
time.sleep(0.3)
```

### Observe

With the `/api/slow` endpoint having 10% at 500ms, the p99 should be around 450-500ms — well above the 200ms SLO threshold. The experiment will print "VIOLATED" for the p99 SLO. Open Grafana at `http://localhost:3000` to see the pre-provisioned RED method dashboard with live-updating panels.

### Teardown

```bash
docker compose down
```

## Real-World Examples

- **Google SRE and Error Budgets:** Google pioneered the SRE (Site Reliability Engineering) discipline and the error budget concept. Google's SRE book (2016) describes how product teams and SRE teams negotiate SLOs, and how error budget depletion triggers a freeze on new feature launches until reliability is restored. This model has been widely adopted by companies including Spotify, Netflix, and Atlassian. Source: Beyer et al., "Site Reliability Engineering," O'Reilly, 2016.
- **Prometheus at SoundCloud (origin):** Prometheus was created at SoundCloud in 2012 by Matt Proud and Julius Volz, inspired by Google's internal Borgmon monitoring system. SoundCloud needed a monitoring system that could handle the high-cardinality, multi-dimensional metrics of a microservices architecture. They open-sourced it in 2015, and it became a CNCF graduated project in 2018. Source: Julius Volz, "Prometheus: Monitoring at SoundCloud," 2013.
- **Honeycomb and observability-driven development:** Honeycomb popularised the concept of "observability" as distinct from monitoring, arguing that metrics and dashboards are insufficient for debugging novel failures in distributed systems. Their tool stores raw events (not pre-aggregated metrics) and enables arbitrary queries on high-cardinality data. They introduced the concept of "observability-driven development" — instrumentation as a first-class part of the development process. Source: Charity Majors et al., "Observability Engineering," O'Reilly, 2022.

## Common Mistakes

- **Alerting on symptoms instead of causes.** Alerting on "CPU > 80%" (a cause) misses user impact entirely — CPU could be high with no user-visible problem, or users could be affected with CPU at 30%. Alert on SLO violations (p99 latency, error rate) that directly measure user experience; use CPU/memory as diagnostic information, not alerting triggers.
- **Using averages for latency.** Average latency hides the tail. If 95% of requests take 10ms and 5% take 2000ms, the average is 110ms — which looks healthy. p99 reveals the 2000ms tail. Always alert on percentiles for latency SLOs.
- **Scraping too frequently or with too-short windows.** `rate(counter[30s])` with a 5-second scrape interval has only 6 data points — statistical noise overwhelms the signal. Use `rate(counter[5m])` for stable alerting; reserve short windows for dashboards where you're watching actively.
- **Not instrumenting dependencies.** Measuring your own service's latency is necessary but not sufficient. If your service calls a database, instrument the database call duration separately. A p99 of 300ms in your service with a database p99 of 280ms immediately tells you the database is the bottleneck — without per-call instrumentation, you are guessing.
