# Failure Modes & Reliability Patterns

**Prerequisites:** `../13-proxies-reverse-proxies/`
**Next:** `../../02-advanced/01-consistent-hashing/`

---

## Concept

Distributed systems fail differently from single-process programs. A local function either returns a value or throws an exception — the caller knows immediately. A network call can return an error, return a wrong answer, return too slowly, or never return at all. The process on the other end might be crashed, overloaded, or partitioned from the network. This uncertainty is fundamental, not accidental — it is what makes distributed systems hard.

The **fallacies of distributed computing** (Peter Deutsch, 1994) enumerate eight false assumptions that engineers consistently make: the network is reliable; latency is zero; bandwidth is infinite; the network is secure; topology doesn't change; there is one administrator; transport cost is zero; the network is homogeneous. Every item on this list is a source of production failures. Understanding them is the starting point for designing reliable systems.

### Failure Taxonomy

**Crash failure:** a process stops. No more messages. The cleanest failure type — at least you know the node is gone (once you detect it).

**Omission failure:** a process fails to send or receive messages it should. A queue fills up and starts dropping messages. A firewall silently discards packets. The process is alive but unreachable.

**Timing failure:** a process responds too slowly. It's functioning, but not within acceptable time bounds. Most dangerous in synchronous systems where a slow response blocks the caller indefinitely. This is the most common failure type in well-run production systems.

**Byzantine failure:** a process sends incorrect or conflicting responses to different callers. Hardware bit-flip, corrupted data, or a compromised node. The hardest failure to detect and handle — the system cannot easily distinguish a Byzantine node from a correct one. Rare in non-adversarial environments; typically handled by running multiple independent replicas and taking a majority vote (BFT consensus requires 3f+1 nodes to tolerate f Byzantine failures).

**Partial failure:** some operations succeed while others fail on the same node or across a cluster. For example: a distributed transaction where some participants commit and others abort, leaving the system in an inconsistent state. Partial failures are why distributed systems need explicit consistency protocols rather than relying on all-or-nothing semantics.

### Reliability Patterns

**Timeouts** are the foundation. Every network call must have a deadline. Without a timeout, a slow or dead downstream service causes the caller's thread or connection to block indefinitely. Threads accumulate; the caller's connection pool exhausts; the caller starts failing its own callers. This cascade is how a single slow service takes down an entire call chain.

There are three distinct timeout types to configure per dependency:
- **Connect timeout**: how long to wait for the TCP handshake (typically 100–500ms)
- **Read timeout**: how long to wait for response bytes after the connection is established (set to your SLA budget minus processing overhead)
- **Idle timeout**: how long to keep an idle keep-alive connection before closing it

Rule of thumb: `read_timeout < (caller_SLA) - (caller_processing_time)`. If your API must respond in 500ms and your own processing takes ~50ms, downstream read timeouts should be ≤450ms.

**Retries** recover from transient failures — a brief network hiccup, a pod restart, a momentary overload. The failure is real but not persistent; retrying after a short wait succeeds. The danger is **retry amplification**: if 1,000 callers all retry 3 times when a backend is overloaded, the backend receives 3,000x the normal request rate — making the overload worse. Retries must be gated by a circuit breaker and applied only to idempotent operations. `GET`, `PUT`, and `DELETE` are safe to retry; `POST` (non-idempotent) is not without an idempotency key. Also track **total retry budget** (total retries per time window across all callers), not just per-call retry count.

**Exponential backoff with full jitter** prevents retry storms. After the first failure, compute a cap (e.g., `base * 2^attempt`), then wait a random value uniformly drawn from `[0, cap]`. This is "full jitter" — as opposed to additive jitter which adds a small random value to the deterministic backoff. Full jitter spreads retries across the entire window and is what AWS SDKs implement. Without jitter, even with exponential backoff, clients that all failed simultaneously will all retry at t+1s, t+2s, t+4s — still a synchronized burst.

**Circuit breaker** monitors failure rate to a downstream service and opens (stops sending requests) when the rate exceeds a threshold. When the circuit is open, calls fail immediately with a local error rather than waiting for a timeout. After a configurable cooldown period, the circuit enters half-open state and allows one probe request through. If it succeeds, the circuit closes; if it fails, the circuit stays open.

The circuit breaker is a state machine: `CLOSED → OPEN → HALF-OPEN → CLOSED`.

Benefits: (1) fails fast instead of piling up blocked threads; (2) gives the downstream service time to recover without being hammered; (3) prevents cascading failure. Netflix Hystrix popularized this pattern; Resilience4j and Envoy implement it in production systems.

**Critical threshold design:** a circuit breaker that opens on the first error in a low-traffic window causes false positives. Use a **minimum request guard**: only consider opening after N requests in the window (e.g., error rate > 50% over at least 20 requests in the last 10 seconds). Count-based circuits (like Hystrix's original model) are simpler but can miss bursty traffic; sliding time-window circuits (like Resilience4j's) are more accurate.

**Bulkhead** isolates different call types into separate resource pools so that one slow downstream cannot exhaust resources needed for other calls. Named after ship bulkheads that contain flooding to one compartment. If your service calls both a payment API and a recommendations API, put them in separate thread pools (or semaphore-bounded pools). When the recommendations API is slow and its pool fills, payment calls are unaffected — they use their own pool.

Sizing a bulkhead pool: target concurrency = throughput × p99_latency (Little's Law). A downstream handling 100 req/s with p99=50ms needs at least 5 concurrent slots. Add 2x headroom for latency spikes.

Two bulkhead modes:
- **Thread pool isolation** (Hystrix default): each downstream gets dedicated threads; provides complete CPU isolation but more memory/thread overhead.
- **Semaphore isolation**: no dedicated threads; caller's thread is used but semaphore limits concurrency; lighter weight, preferred in async frameworks (asyncio, Tokio, Vert.x).

**Fallback** provides a degraded response when the primary path fails. If the recommendation engine is down, return popular items. If the user profile service is slow, return a cached version. If the inventory check fails, show "check availability" instead of crashing. Fallbacks transform availability failures into quality degradations, which is almost always preferable. The decision of what constitutes an acceptable fallback is a product decision, not just an engineering one. A critical gotcha: **the fallback path is often less tested and less monitored** — in an incident, both the primary and fallback can fail simultaneously. Test fallback paths with the same rigor as primary paths.

**Idempotency keys** make non-idempotent operations safe to retry. A client generates a unique key for each logical operation (a UUID) and includes it in the request header (e.g., `Idempotency-Key: <uuid>`). The server stores the key and result. On retry, the server recognizes the key and returns the stored result without re-executing. Stripe and PayPal use this pattern for payment APIs — retrying a charge with the same idempotency key is safe; the charge applies exactly once.

Implementation subtleties:
- **Key scope**: scoped per-user and per-operation-type; a key should not be reusable across different endpoints
- **TTL**: keys expire (Stripe uses 24h) — after TTL, the same key is treated as a new operation
- **In-flight deduplication**: if two concurrent retries arrive simultaneously with the same key, one must wait for the other to finish (use `SET NX` in Redis for distributed locking)
- **Response fidelity**: return the exact same response body and status code, not just a "duplicate detected" flag — the client may not know it was a duplicate

**Load shedding** explicitly rejects requests when a service is over capacity, returning 503 immediately rather than queuing them. Without load shedding, an overloaded service accumulates a growing queue. The queue adds latency for all requests; old items in the queue expire before being processed; the service wastes work on requests the caller has already timed out on. Explicit shedding gives callers a clear signal to stop retrying or route elsewhere. Implementation: track a concurrency counter or queue depth; once over threshold, return 503 with a `Retry-After` header.

**Deadline propagation** (also: context cancellation) passes the caller's remaining SLA budget downstream. If your API caller gave you 300ms total and you've spent 50ms on processing, pass a 250ms deadline to your downstream call — not an independent 1s timeout. gRPC propagates deadlines natively. HTTP services should use a request header (e.g., `X-Request-Deadline` or `X-Request-Timeout`). Without this, a downstream service may spend 500ms on a response that the upstream will discard because the caller already timed out and moved on — wasted work that compounds load.

**Hedged requests** reduce tail latency by sending a duplicate request to a second replica after a short delay (e.g., 95th-percentile latency). Cancel the hedge when either response arrives. The first response wins. This is used at Google (described in the Dapper paper and the SRE book) and Uber. It's a latency optimization, not a failure-recovery pattern — it works because tail latency is often caused by transient resource contention (GC, lock contention) that doesn't affect all replicas simultaneously. Cost: up to 2x backend load for the affected requests; use only for latency-sensitive read paths.

**Health checks, liveness probes, and readiness probes** allow load balancers and orchestrators to detect and remove unhealthy instances from rotation.
- **Liveness probe**: should this process be restarted? (e.g., HTTP 200 or a heartbeat)
- **Readiness probe**: should this instance receive traffic? (can fail without triggering restart — e.g., still warming up cache)
- **Startup probe**: gives a slow-starting application time before liveness/readiness probes activate

A common mistake: making the health check endpoint call the database. If the database is slow, all your pods fail their health checks and are removed from rotation simultaneously — causing a full outage of a service that could have served cached or degraded responses.

**Dead letter queues (DLQ)** and **poison pill handling** apply to async message-driven systems. If a consumer consistently fails to process a message (e.g., due to a malformed payload or a bug), the message is a poison pill — retrying it endlessly blocks the queue and stops all downstream processing. A DLQ is a separate queue where messages go after N failed attempts. Engineers can inspect, fix, and replay DLQ messages manually. Without a DLQ, a single bad message can halt an entire processing pipeline indefinitely.

### Cascading Failure

The most dangerous failure mode in distributed systems is cascading failure: one component's failure causes increased load or latency on other components, which then fail, causing more load elsewhere, until the entire system is down. The pattern is:

1. Service A slows down (e.g., database gets hot due to a slow query)
2. Service B's calls to A start timing out; B retries, amplifying load on A by 3x
3. B's thread pool fills with pending calls to A; B starts timing out on its own callers
4. Service C (which calls B) experiences timeouts; C retries, amplifying load on B
5. The entire stack fails within minutes

Prevention: timeouts at every layer; circuit breakers per downstream; retry budgets (limit total retries per time window across all callers); load shedding (explicitly reject requests when over capacity rather than queuing them); deadline propagation (pass remaining SLA budget rather than setting independent timeouts at each layer).

### Trade-offs

| Pattern | Benefit | Cost | Key Tuning Knob |
|---------|---------|------|-----------------|
| Timeout | Bounds failure duration | Wrong value = premature failures or long hangs | connect + read + idle; set per dependency, not globally |
| Retry | Recovers transient failures | Amplifies load; must be idempotent; needs jitter | max attempts + max elapsed time (budget); full jitter |
| Circuit breaker | Fast-fail; prevents cascade; recovery breathing room | Adds state; oscillation risk; per-instance vs shared | error rate threshold + min request guard + cooldown |
| Bulkhead | Isolates failure domains | Resource overhead; more pools to tune | pool size = throughput × p99_latency × headroom |
| Fallback | Maintains availability | Degraded quality; fallback may fail; less tested | define acceptable degradation with product; monitor fallback |
| Idempotency key | Safe retry for non-idempotent ops | Key storage, TTL, in-flight dedup complexity | Redis SET NX for distributed locking; key scoping |
| Load shedding | Controlled degradation; no wasted work | Requires capacity model; shed too early = worse than queue | concurrency counter or queue depth threshold |
| Deadline propagation | Prevents wasted downstream work | Framework support required | pass `remaining_budget - processing_overhead` |

## Interview Talking Points

- "Timeouts are non-negotiable in distributed systems. Every network call needs a deadline — without it, one slow service cascades to take down the entire call chain. Configure three types: connect, read, and idle. Set read timeout to your SLA budget minus processing overhead."
- "Retries recover transient failures but amplify load. Use full jitter (not additive) so 1,000 concurrent clients don't all retry at the same millisecond. Gate retries with a circuit breaker, only retry idempotent ops, and set a budget on total retries per time window — not just per call."
- "A circuit breaker is a state machine: CLOSED → OPEN (on failure threshold) → HALF-OPEN (after cooldown). It fails fast to protect the downstream and gives it breathing room to recover. Gotcha: without a minimum request guard, it opens on the first error during low traffic — false positive."
- "Bulkheads isolate resource pools per downstream. A slow recommendations service can't starve threads needed for payment calls. Size pools with Little's Law: concurrency = throughput × p99 latency."
- "Cascading failure is the killer — one slow service causes retries, which cause overload, which causes more failures. Each reliability pattern addresses one amplification step: timeouts bound blocking, circuit breakers stop retries into broken services, bulkheads prevent pool starvation, load shedding caps queue growth."
- "Idempotency keys make POST-style operations safe to retry. The server stores (key → response) and de-duplicates on subsequent requests — the charge executes exactly once regardless of how many retries the client sends. Stripe scopes keys per user + endpoint + 24h TTL."
- "Deadline propagation is underrated. If a caller has 300ms left and my own processing takes 50ms, I should set a 250ms timeout on my downstream call — not an independent 1s timeout. Without this, downstream services waste work on responses the caller has already abandoned."
- "At 10x scale, in-process circuit breakers become inconsistent — each of 500 pods makes independent open/close decisions. A service mesh (Envoy, Linkerd) applies circuit breaking at the proxy layer with more consistent behavior but adds infrastructure complexity."

## Hands-on Lab

**Time:** ~30 minutes
**Services:** one controllable HTTP downstream service (healthy / slow / failing modes)

### Setup

```bash
cd system-design-interview/01-foundations/14-failure-modes-reliability/
docker compose up -d
# Wait ~5 seconds for services to be ready
```

### Experiment

```bash
python experiment.py
```

The script demonstrates six patterns sequentially:
1. **Timeout** — a 5s downstream response hangs the caller with a 6s timeout; a 1s timeout frees the caller immediately
2. **Retry with full jitter** — a transient failure window is recovered automatically with exponential backoff; delays are printed
3. **Circuit breaker** — CLOSED → OPEN under sustained failures → HALF-OPEN after cooldown → CLOSED on probe success; fast-fail latency shown
4. **Bulkhead** — 12 concurrent slow recommendation calls exhaust the 3-thread recommendations pool; 12 concurrent payment calls to a fast endpoint succeed unaffected
5. **Idempotency keys** — three "retries" of the same charge operation execute the charge exactly once; duplicate responses are returned from the store
6. **Cascading failure** — slowness propagates through a 3-tier chain without timeouts; each layer's timeout + fallback contains it

### Teardown

```bash
docker compose down -v
```

## Real-World Examples

- **Amazon's retry strategy:** AWS SDKs implement exponential backoff with full jitter for all API calls. The AWS architecture blog documented that removing jitter from retries caused synchronized retry bursts that effectively DDoS'd their own services during incidents — source: Marc Brooker, "Exponential Backoff And Jitter," AWS Architecture Blog (2015).
- **Netflix Hystrix:** Netflix built Hystrix to add circuit breakers and bulkheads across their 500+ microservices. Before Hystrix, a single slow downstream (e.g., the bookmark service) would exhaust the shared thread pool and take down the entire streaming service. With Hystrix, the bookmark service's failures were isolated to its bulkhead — source: Netflix Tech Blog, "Fault Tolerance in a High Volume, Distributed System" (2012).
- **Google SRE's timeout practice:** Google's SRE book documents setting timeouts at the 99th-percentile latency of the downstream service under normal load (not an arbitrary round number). This means the timeout fires only in genuine failure scenarios, not during normal tail latency — source: Beyer et al., "Site Reliability Engineering," Chapter 22.
- **Google's hedged requests:** The Google Dapper paper and the SRE book describe sending duplicate requests to a second replica after p95 latency and canceling the slower one. This cut tail latency significantly with minimal extra load — effective because tail latency is often due to transient per-replica contention, not universal slowness.
- **Stripe's idempotency keys:** Stripe's API requires a client-generated `Idempotency-Key` header for all payment mutations. Keys are stored per customer + endpoint for 24 hours. This lets clients safely retry on network errors without risk of double charges — source: Stripe API documentation.

## Common Mistakes

- **No timeouts, or timeouts that are too long.** A 30-second timeout doesn't protect you — threads block for 30 seconds, and at 100 req/s with a 5s downstream response, you need 500 blocked threads before the first one is freed. Set timeouts close to your SLA budget. Configure connect, read, and idle separately.
- **Retrying non-idempotent operations.** Retrying a payment or a database insert without an idempotency key results in duplicate charges or duplicate rows. Distinguish safe-to-retry (GET, PUT, DELETE, POST with idempotency key) from unsafe (POST without key), and enforce at the framework level.
- **Additive jitter instead of full jitter.** Adding ±30% to an exponential delay still produces synchronized clusters. Full jitter (uniform in `[0, cap]`) disperses retries across the entire window. This matters at high concurrency.
- **Circuit breaker thresholds that are too sensitive.** A circuit that opens on the first error in a low-traffic window causes healthy services to be cut off (false positive). Require a minimum request count (e.g., > 50% error rate over at least 20 requests in the last 10s) before opening.
- **Circuit oscillation (thundering herd on close).** A circuit that opens aggressively starves the downstream of traffic, which may prevent cache warmup, making it appear even slower when probed. The probe fails, circuit re-opens. Repeat. Use a slow half-open ramp (send 5%, 25%, 100% of traffic progressively) rather than a binary open/closed. Envoy's outlier detection uses this approach.
- **Fallback that also fails.** The fallback path is often less tested and less monitored. In an incident, both primary and fallback fail simultaneously, and the error surfaces as a different, confusing failure. Test fallback paths under load regularly and monitor their error rate in production dashboards.
- **Health check calls the database.** If your liveness probe hits the database, a slow database takes all your pods out of rotation simultaneously — converting a database performance issue into a full service outage. Health checks should be shallow (process alive? can accept connections?) unless specifically testing deep health with a separate readiness probe.
- **Ignoring deadline propagation.** Each tier sets its own independent timeout (e.g., 1s each in a 3-tier chain). If all tiers are slow simultaneously, the outer caller waits up to 3s — but should have failed at 500ms per the original SLA. Pass the remaining deadline budget downstream; gRPC does this automatically.
- **No dead letter queue in async pipelines.** A single malformed message (poison pill) that consistently fails processing will block the consumer and halt the entire queue unless you implement DLQ routing after N failures.
