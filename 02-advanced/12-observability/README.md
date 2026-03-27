# Observability

**Prerequisites:** `../10-cdn-and-edge/`
**Next:** `../13-security-at-scale/`

---

## Concept

Observability is the ability to understand what a system is doing from the outside by examining its outputs. A system is "observable" when you can answer any question about its internal state from the data it emits, without deploying new instrumentation. This matters because distributed systems fail in complex, unexpected ways — a service may be healthy by all traditional monitors yet silently returning wrong data, or the p99 latency of one endpoint may be degrading while the average looks fine. Good observability lets you find and diagnose these issues in minutes rather than hours.

The three pillars of observability are metrics, traces, and logs. Metrics are numeric aggregates over time — request rates, error counts, latency distributions. They are cheap to store, query, and alert on, making them ideal for dashboards and automated alerting. Traces follow a single request as it flows through multiple services, recording how long each step takes and what data was passed. They are essential for debugging latency problems in microservice architectures where a single slow dependency can cascade. Logs are structured event records with arbitrary key-value context. They are the highest-fidelity signal but the most expensive to store and query at scale.

Prometheus is the de facto standard for metrics in cloud-native systems. It uses a pull-based model: the Prometheus server scrapes an HTTP `/metrics` endpoint on each service every few seconds. Services expose metrics using the `prometheus_client` library, which maintains in-memory counters, gauges, and histograms. PromQL, Prometheus's query language, can compute rates, percentiles, and aggregations across all scraped metrics. Grafana connects to Prometheus as a data source and renders dashboards that update in real time.

Metric types matter for correctness. A Counter is a monotonically increasing integer (total requests, total errors). Never query a counter directly — use `rate(counter[window])` to get the per-second increase. A Gauge is a value that can go up or down (active connections, queue depth, CPU%). A Histogram records observations in predefined buckets, enabling `histogram_quantile()` to compute p50/p95/p99 latency from aggregated data across all instances. Summaries compute quantiles on the client side — they are more accurate than histograms for a single instance but cannot be aggregated across instances, making them less useful in multi-instance deployments.

SLO (Service Level Objective) is a target for a measurable SLI (Service Level Indicator). For example: "99th percentile request latency must be below 200ms, measured over a 30-day rolling window." The SLA (Service Level Agreement) is the contractual commitment — what happens if the SLO is missed (credits, refunds). The error budget is 1 minus the SLO: if your SLO is 99.9% availability, your error budget is 0.1% of requests per month that can fail. Error budgets are a powerful tool: when the budget is healthy, teams ship features faster; when it is burning quickly, deployments are frozen until the service stabilises.

## How It Works

**Prometheus Metrics Pipeline (Instrument → Alert → Dashboard):**
1. Application code instruments key events using the `prometheus_client` library — incrementing counters, recording histogram observations, updating gauges on every request
2. Application exposes a `/metrics` HTTP endpoint returning all current metric values in Prometheus text exposition format
3. Prometheus server scrapes each registered target's `/metrics` endpoint on a configurable interval (default 15 seconds)
4. Scraped time-series samples are stored in Prometheus's local TSDB (time-series database) on disk
5. Alerting rules evaluate continuously against stored data using PromQL (e.g., `rate(http_errors_total[5m]) > 0.01`); when a rule fires, Prometheus sends the alert to Alertmanager
6. Alertmanager deduplicates, groups, and routes the alert to the appropriate receiver (PagerDuty, Slack, email) with configurable silencing and inhibition
7. Grafana queries Prometheus via PromQL to render real-time dashboards; dashboards visualise the same time-series data used for alerting

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

**Multi-window burn rate alerting (Google SRE chapter 5):**

```
  Problem: a single-window alert (rate(errors[1h]) > threshold) either
  fires too slowly (misses short spikes) or fires too often (false positives).

  Solution: burn rate = actual error rate / SLO error rate
  If SLO is 99.9% (error budget = 0.1%):
    burn rate 1 = consuming budget at exactly 1x speed (use it in 30 days)
    burn rate 14.4 = budget exhausted in 2 hours → page immediately

  Multi-window: alert only when both a fast window AND slow window fire.
  Fast window catches the spike; slow window confirms it's not a blip.

  Google's recommended thresholds (for 99.9% SLO, 30-day window):
  ┌──────────────────┬────────────┬────────────┬───────────────────┐
  │ Severity         │ Burn Rate  │ Fast Window│ Slow Window       │
  ├──────────────────┼────────────┼────────────┼───────────────────┤
  │ Page (critical)  │ 14.4×      │ 1h         │ 5m                │
  │ Page (high)      │ 6×         │ 6h         │ 30m               │
  │ Ticket (medium)  │ 3×         │ 3d         │ 6h                │
  └──────────────────┴────────────┴────────────┴───────────────────┘

  PromQL for 14.4× burn rate alert:
    (
      rate(http_requests_total{status!~"5.."}[1h]) /
      rate(http_requests_total[1h]) < 0.999 - 14.4 * (1 - 0.999)
    ) AND (
      rate(http_requests_total{status!~"5.."}[5m]) /
      rate(http_requests_total[5m]) < 0.999 - 14.4 * (1 - 0.999)
    )
```

**OpenTelemetry — the unified instrumentation standard:**

```
  OpenTelemetry (OTel) is the CNCF standard for emitting metrics, traces,
  and logs from any language/framework, sending to any backend.

  Architecture:
    App code → OTel SDK → OTel Collector → Prometheus / Jaeger / Loki

  Key advantage: vendor-neutral instrumentation. Change backend (Datadog →
  Grafana Cloud) without re-instrumenting every service.

  Auto-instrumentation: OTel agents instrument HTTP, DB, gRPC calls
  automatically — no manual span creation needed for standard libraries.

  Trace context propagation:
    W3C Trace Context header: traceparent: 00-{traceId}-{spanId}-{flags}
    Baggage header:           tracestate: vendor-specific key=value pairs
  Both headers flow through every HTTP call, linking spans across services.
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

**Cardinality budget exhaustion:** A healthy Prometheus installation supports roughly 1–10M active time series before memory pressure causes OOM kills. Compute cardinality before labelling: a metric with labels `{endpoint, status_code, region, version}` with 50 endpoints × 10 status codes × 5 regions × 20 versions = 50,000 series per metric. A fleet of 50 such metrics = 2.5M series — already near the limit. Audit cardinality with `count by (__name__)({__name__=~".+"})`.

**Missing the "long tail" in tracing sampling:** Distributed tracing commonly uses head-based sampling (e.g., keep 1% of traces) to reduce storage costs. This discards 99% of traces, including many slow or erroring ones. Tail-based sampling (decide whether to keep a trace *after* it completes, based on its outcome) captures the interesting traces. Tools like Grafana Tempo and AWS X-Ray support tail-based sampling. For FAANG interviews: mention that "always sample errors and slow traces" is a common policy.

**Metrics without context:** Knowing your p99 is 500ms tells you something is wrong; knowing *which* endpoint, *which* downstream service, and *which* deployment caused it requires correlated signals. Exemplars bridge this gap — a Prometheus exemplar attaches a trace ID to a histogram observation, so you can jump from a slow bucket in Grafana directly to the Jaeger trace. Native histograms (Prometheus 2.40+) support exemplars natively.

## Interview Talking Points

- "The three pillars are metrics (aggregates, cheap, alertable), traces (per-request causality, expensive), and logs (full context, most expensive). Use metrics for dashboards and alerts, traces for latency debugging, logs for error context. A fourth signal — continuous profiling — is increasingly common at scale (Google's pprof, Pyroscope)."
- "RED method for services: Rate, Errors, Duration. These three metrics answer 'is my service healthy?' for any user-facing endpoint. USE method for infrastructure: Utilization, Saturation, Errors. Together they cover both user experience and resource health."
- "Prometheus histograms store bucket counts, not raw values. `histogram_quantile()` interpolates percentiles from buckets — it's an approximation, not exact. Accuracy depends on bucket placement: put buckets at your SLO thresholds (e.g., 0.100, 0.200, 0.500 seconds for a 200ms SLO)."
- "SLI is what you measure, SLO is your target, SLA is the contract. Error budget = 1 - SLO. Use error budgets to balance reliability vs feature velocity — healthy budget means ship faster, depleted budget means freeze deployments."
- "Never use high-cardinality values as Prometheus label values — user IDs, request IDs, URLs with path parameters. Each unique label combination is a separate time series in memory. Audit cardinality with `count by (__name__)({__name__=~\".+\"})` before labelling a metric."
- "Distributed tracing propagates a trace ID across service boundaries via headers (`traceparent` in W3C Trace Context). Each service creates a span, reports to a central collector (Jaeger, Zipkin). The trace shows the full latency breakdown across all services. OpenTelemetry is the vendor-neutral SDK standard — instrument once, switch backends without code changes."
- "Multi-window burn rate alerting solves the alert latency vs noise trade-off. A 14.4× burn rate with both a 1h and 5m window fires within 5 minutes of a critical outage while ignoring brief transient spikes. This is the Google SRE recommended pattern for 30-day SLO windows."
- "Tail-based sampling in distributed tracing is superior to head-based for debugging: decide whether to keep a trace *after* it completes so you always sample errors and slow traces. Head-based sampling (flip a coin at trace start) discards most of the interesting data."

## Hands-on Lab

**Time:** ~3 minutes
**Services:** Flask app (metrics) + Prometheus + Grafana

### Setup

```bash
cd system-design-interview/02-advanced/12-observability/
docker compose up -d flask-app prometheus grafana
```

### Experiment

```bash
python experiment.py
```

The script runs five phases automatically:

1. Sends 500 requests across four endpoints — `/api/fast` (5–20ms), `/api/slow` (10% at 500ms tail), `/api/flaky` (5% errors), `/api/db-call` (DB dependency instrumentation). Prints client-side latency percentiles.
2. Waits 15 seconds for Prometheus to scrape, then queries the Prometheus HTTP API using PromQL to retrieve request rate, error rate, and p50/p95/p99 latency from histogram buckets. Shows per-endpoint p99 breakdown and dependency vs service latency comparison.
3. Evaluates three SLOs against the measured values: p99 < 200ms, error rate < 1%, throughput > 5 req/s. Prints VIOLATED or OK for each.
4. Computes error budget burn rate and maps it to alert severity (critical ≥ 14.4×, high ≥ 6×, medium ≥ 3×). Shows the multi-window burn rate PromQL pattern.
5. Prints a full RED + USE method summary, metric type guide, cardinality math, and OpenTelemetry context.

### Break It

Add a high-cardinality label to trigger Prometheus memory pressure:

```python
# In flask_app.py, add user_id as a label to REQUEST_COUNT:
REQUEST_COUNT = Counter("http_requests_total", "...", ["method", "endpoint", "status_code", "user_id"])
# Then pass a unique user_id per request — with 1M users this creates 1M time series.
# Watch Prometheus RSS in `docker stats` as series accumulate.
```

Or deliberately violate the p99 SLO:

```python
# In flask_app.py, change /api/slow to always sleep 300ms:
time.sleep(0.3)   # p99 will be ~300ms, well above the 200ms SLO
```

Or trigger a high burn rate:

```python
# Change flaky error rate from 5% to 50%:
if random.random() < 0.50:   # 50% error rate = 500× burn rate on a 99.9% SLO
```

### Observe

With the `/api/slow` endpoint having 10% of requests at 400–600ms, the aggregate p99 across all endpoints will be around 200–500ms depending on traffic distribution. The experiment will print "VIOLATED" or "OK" for each SLO. Open Grafana at `http://localhost:3000` to see the pre-provisioned RED method dashboard with live-updating panels.

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
