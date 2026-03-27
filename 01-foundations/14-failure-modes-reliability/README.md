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

**Timing failure:** a process responds too slowly. It's functioning, but not within acceptable time bounds. Most dangerous in synchronous systems where a slow response blocks the caller indefinitely.

**Byzantine failure:** a process sends incorrect or conflicting responses to different callers. Hardware bit-flip, corrupted data, or a compromised node. The hardest failure to detect and handle — the system cannot easily distinguish a Byzantine node from a correct one.

In practice, most failures in well-run production systems are crash failures and timing failures. Byzantine failures are rare in non-adversarial environments and are typically handled by running multiple independent replicas and taking a majority vote.

### Reliability Patterns

**Timeouts** are the foundation. Every network call must have a deadline. Without a timeout, a slow or dead downstream service causes the caller's thread/connection to block indefinitely. Threads accumulate; the caller's connection pool exhausts; the caller starts failing its own callers. This cascade is how a single slow service takes down an entire call chain. Set timeouts at every layer: TCP connect timeout, read timeout, idle connection timeout. A rule of thumb: timeout < caller's SLA. If your API must respond in 500ms, downstream calls must timeout in ~200ms to leave budget for your own processing.

**Retries** recover from transient failures — a brief network hiccup, a pod restart, a momentary overload. The failure is real but not persistent; retrying after a short wait succeeds. The danger is **retry amplification**: if 10 callers all retry 3 times when a backend is overloaded, the backend receives 30x the normal traffic — making the overload worse. Retries should be gated by the circuit breaker pattern and should only be applied to idempotent operations. `GET`, `PUT`, and `DELETE` are safe to retry; `POST` (non-idempotent) is not, unless the endpoint supports idempotency keys.

**Exponential backoff with jitter** prevents retry storms. After the first failure, wait 1s before retrying. After the second failure, wait 2s. Then 4s, 8s, up to a maximum. Add random jitter (±30%) so that many clients that all failed simultaneously don't all retry at the same moment. AWS, Google, and every major SDK use this pattern. Without jitter, exponential backoff still causes synchronized retry bursts.

**Circuit breaker** monitors failure rate to a downstream service and opens (stops sending requests) when the rate exceeds a threshold. When the circuit is open, calls fail immediately with a local error rather than waiting for a timeout. After a configurable period, the circuit enters half-open state and allows one probe request through. If it succeeds, the circuit closes; if it fails, the circuit stays open. This pattern: (1) fails fast instead of piling up blocked threads; (2) gives the downstream service time to recover without being hammered by retries; (3) prevents cascading failure. Netflix Hystrix popularized this pattern; Resilience4j and Envoy implement it.

**Bulkhead** isolates different call types into separate resource pools so that one slow downstream doesn't exhaust resources needed for other calls. Named after ship bulkheads that contain flooding to one compartment. If your service calls both a payment API and a recommendations API, put them in separate thread pools. When the recommendations API is slow and its pool fills, payment calls are unaffected — they have their own pool. Without bulkheads, a slow recommendations call occupies a thread from the shared pool, and at scale all threads are occupied waiting for recommendations, blocking payment calls too.

**Fallback** provides a degraded response when the primary path fails. If the recommendation engine is down, return popular items. If the user profile service is slow, return a cached version. If the inventory check fails, show "check availability" instead of crashing. Fallbacks transform availability failures into quality degradations, which is almost always preferable. The decision of what constitutes an acceptable fallback is a product decision, not just an engineering one.

**Idempotency keys** make non-idempotent operations safe to retry. A client generates a unique key for each logical operation (e.g., a UUID) and includes it in the request. The server stores the key and result. On retry, the server recognizes the key and returns the stored result without re-executing the operation. Stripe and PayPal use this pattern for payment APIs — retrying a payment with the same idempotency key is safe; the charge will only be applied once.

**Health checks** and **liveness/readiness probes** allow load balancers and orchestrators to detect and remove unhealthy instances from rotation. A liveness probe checks if the process is alive (should it be restarted?). A readiness probe checks if it can serve traffic (should it receive requests?). A startup probe gives a slow-starting application time before the other probes activate. Kubernetes uses all three. An instance that is alive but not ready (e.g., still warming up its cache) should be removed from the load balancer pool without being killed.

### Cascading Failure

The most dangerous failure mode in distributed systems is cascading failure: one component's failure causes increased load or latency on other components, which then fail, causing more load elsewhere, until the entire system is down. The pattern is:

1. Service A slows down (e.g., database gets hot)
2. Service B's calls to A start timing out; B retries, amplifying load on A
3. B's thread pool fills with pending calls to A; B starts timing out on its own callers
4. Service C (which calls B) experiences timeouts; C retries, amplifying load on B
5. The entire stack fails within minutes

Prevention: timeouts at every layer; circuit breakers; retry budgets (limit total retries per time window); load shedding (explicitly reject requests when over capacity rather than queuing them).

### Trade-offs

| Pattern | Benefit | Cost |
|---------|---------|------|
| Timeout | Bounds failure duration | Must tune per dependency; wrong value = premature failures or long hangs |
| Retry | Recovers transient failures | Amplifies load; must be idempotent; needs jitter |
| Circuit breaker | Fast-fail; prevents cascade | Adds state; wrong thresholds cause false opens |
| Bulkhead | Isolates failure domains | Resource overhead; more pools to tune |
| Fallback | Maintains availability | Degraded quality; fallback may itself fail |

## Interview Talking Points

- "Timeouts are non-negotiable in distributed systems. Every network call needs a deadline — without it, one slow service cascades to take down the entire call chain"
- "Retries recover transient failures but amplify load. Gate retries with a circuit breaker and use exponential backoff with jitter to avoid synchronized retry storms"
- "A circuit breaker is a state machine: closed → open (on failure threshold) → half-open (after cooldown). It fails fast to protect the downstream and lets it recover"
- "Bulkheads isolate resource pools per downstream. A slow recommendation service can't starve threads needed for payment calls"
- "Cascading failure is the killer — one slow service causes retries, which cause overload, which causes more failures. Timeouts + circuit breakers + load shedding break the cascade"
- "Idempotency keys make POST-style operations safe to retry — the server de-duplicates by key, so the operation executes exactly once regardless of how many retries the client sends"

## Hands-on Lab

**Time:** ~25 minutes
**Services:** two Flask services (upstream, downstream), Redis (for circuit breaker state)

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

The script demonstrates: (1) timeout behavior — a slow downstream causes the caller to hang without a timeout, then shows bounded latency with a timeout set; (2) retry with jitter — transient failures recovered automatically, with backoff delays printed; (3) circuit breaker state transitions — closed → open under sustained failures → half-open after cooldown → closed on recovery; (4) cascading failure simulation — a slow downstream propagates latency up the call chain without timeouts, then shows containment with proper timeout budgets.

### Teardown

```bash
docker compose down -v
```

## Real-World Examples

- **Amazon's retry strategy:** AWS SDKs implement exponential backoff with full jitter for all API calls. The AWS architecture blog documented that removing jitter from retries caused synchronized retry bursts that DDoS'd their own services during incidents — source: Marc Brooker, "Exponential Backoff And Jitter," AWS Architecture Blog (2015).
- **Netflix Hystrix:** Netflix built Hystrix to add circuit breakers and bulkheads across their 500+ microservices. Before Hystrix, a single slow downstream (e.g., the bookmark service) would exhaust the shared thread pool and take down the entire streaming service. With Hystrix, the bookmark service's failures were isolated to its bulkhead — source: Netflix Tech Blog, "Fault Tolerance in a High Volume, Distributed System" (2012).
- **Google SRE's approach to timeouts:** Google's SRE book documents their "99th percentile timeout" practice: set timeouts at the 99th percentile latency of the downstream service under normal load, not an arbitrary value. This means the timeout fires only in genuine failure scenarios, not during normal tail latency — source: Beyer et al., "Site Reliability Engineering," Chapter 22.

## Common Mistakes

- **No timeouts, or timeouts that are too long.** A 30-second timeout doesn't protect you — threads block for 30 seconds, and at scale the pool exhausts in seconds. Set timeouts close to your SLA budget.
- **Retrying non-idempotent operations.** Retrying a payment or a database insert without an idempotency key results in duplicate charges or duplicate rows. Distinguish safe-to-retry from unsafe, and add idempotency keys to the latter.
- **Circuit breaker thresholds that are too sensitive.** A circuit that opens on the first failure in a low-traffic window causes healthy services to be cut off. Base thresholds on error rate over a minimum request count (e.g., >50% errors over at least 20 requests in the last 10s).
- **Fallback that also fails.** The fallback path is often less tested and less monitored. In an incident, both the primary and fallback fail simultaneously. Test fallback paths regularly and monitor them in production.
- **Ignoring the "success in failure" problem.** A circuit breaker that opens too aggressively starves a service of traffic, which may prevent it from warming up its cache, making it appear slower when the circuit closes — causing it to open again. This oscillation pattern is common; use a slow half-open ramp rather than full traffic on recovery.
