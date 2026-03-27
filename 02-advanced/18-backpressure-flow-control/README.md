# Backpressure & Flow Control

**Prerequisites:** `../05-message-queues-fundamentals/`, `../10-rate-limiting-algorithms/`
**Next:** `../19-multi-region-architecture/`

---

## Concept

Every distributed system has components that produce work and components that consume it. When producers are faster than consumers, work accumulates. Left unchecked, this accumulation exhausts memory, fills queues, or causes cascading failures. **Backpressure** is the mechanism by which a downstream component signals to its upstream that it is overwhelmed — and the upstream responds by slowing, pausing, or shedding load.

The word comes from fluid dynamics: backpressure is the resistance that slows flow upstream when the downstream pipe is full. In software, it is the same idea: a full queue, a slow consumer, or an overloaded database "pushes back" against producers.

Without backpressure, overload propagates silently. The queue grows until it exceeds broker disk space and crashes. Or the consumer falls further and further behind until it is processing work that is already stale. Or the consumer accepts all connections and runs out of memory. All of these failure modes are preventable with explicit backpressure signals.

### Where Backpressure Lives

**TCP's congestion control** is the original backpressure mechanism. The TCP receive window advertises how many bytes the receiver can accept. When the application is slow to read from the socket buffer, the window shrinks, which throttles the sender at the OS level. This is invisible to application code but fundamental — every HTTP request benefits from it.

**Message queue depth** is the application-level backpressure signal. When Kafka consumer lag grows, it is a signal that consumers cannot keep up with producers. The system can respond by scaling consumers, rate-limiting producers at the API layer, or activating load shedding.

**Reactive Streams** (RxJava, Project Reactor, Akka Streams) formalize backpressure as a protocol: the subscriber signals to the publisher how many items it is ready to receive (`request(N)`). The publisher sends at most N items. When the subscriber is ready for more, it calls `request(N)` again. This demand-driven model prevents the publisher from overwhelming the subscriber at the library level. Java's `java.util.concurrent.Flow` API (JDK 9+) standardizes this interface.

```
Reactive Streams demand-driven protocol:

  Publisher                      Subscriber
     |                                |
     |<-- subscribe(subscriber) ------|
     |--- onSubscribe(sub) ---------->|
     |                                |
     |<-- request(5) -----------------|   subscriber is ready for 5
     |--- onNext(item1) ------------->|
     |--- onNext(item2) ------------->|
     |--- onNext(item3) ------------->|
     |--- onNext(item4) ------------->|
     |--- onNext(item5) ------------->|
     |                (processing...) |
     |<-- request(3) -----------------|   subscriber finished 3, ready for more
     |--- onNext(item6) ------------->|
     ...
```

This is pull-based flow control: the publisher never sends more than the subscriber has requested. Compare this to a naive push model where the publisher emits at its own rate and the subscriber drowns.

### Backpressure Strategies

**Block (slow the producer):** the producer's `send()` call blocks until the downstream has capacity. Simple to implement; the upstream slows to match the downstream's speed automatically. The downside: blocking propagates upstream — a slow database can freeze your entire API tier if blocking is used uncritically.

Critical gotcha: **head-of-line blocking on shared thread pools.** If your API server uses a fixed-size thread pool (e.g., Tomcat's 200-thread default), and each thread blocks waiting for a slow downstream, you exhaust threads for all requests — not just the ones waiting on the slow dependency. One overloaded queue can starve unrelated endpoints. Apply blocking backpressure only in dedicated async/background pipelines where threads are not shared with user-facing paths.

**Buffer (absorb bursts):** accept the data into an in-memory or disk-backed buffer and process at the consumer's rate. Handles short bursts without impacting the producer. The buffer must be bounded — an unbounded buffer is just memory exhaustion in slow motion. When the bounded buffer fills, transition to another strategy (block, drop, or reject).

**Drop (shed load):** when a bounded buffer is full, discard new items. Which items to drop matters: LIFO (drop newest) is common for real-time systems where the latest data is most valuable — dropping old requests before new ones keeps the system processing fresh data. FIFO (drop oldest) is common when processing order matters. Sampling-based dropping (drop every Nth item) is used for metrics and logs at extreme throughput.

**Reject (return pressure to caller):** instead of silently dropping, return an error (`503 Service Unavailable` or `429 Too Many Requests`) to the caller. This propagates the backpressure signal explicitly — the caller can retry later, use a circuit breaker, or route to another region. This is the correct strategy for interactive (user-facing) traffic where silent drops would be invisible and confusing.

**Scale consumers:** add more consumer instances to increase processing throughput. This is the correct response when backpressure is sustained (not just a burst) and the workload is horizontally scalable. Kubernetes HPA (Horizontal Pod Autoscaler) can scale consumer deployments based on queue depth (via KEDA — Kubernetes Event-Driven Autoscaling).

### Queue Depth as a Control Signal

Queue depth (the number of pending items in a queue) is the primary backpressure signal in most systems. Monitoring and alerting on queue depth allows proactive response:

| Queue Depth | Interpretation | Response |
|-------------|----------------|----------|
| Normal range | Consumers keeping up | No action |
| Growing slowly | Consumers slightly behind | Alert; prepare to scale |
| Growing fast | Consumers significantly behind | Scale consumers now |
| At capacity | Consumers overwhelmed | Activate drop/reject; page on-call |
| Shrinking | Recovery in progress | Monitor until stable |

A queue that grows during a traffic spike and then drains is expected behavior. A queue that grows monotonically day after day is a capacity problem.

### Load Shedding

Load shedding is the deliberate rejection of requests when a system is above capacity, to protect the system's ability to serve the remaining requests well. It is preferable to accepting all requests and serving all of them slowly or incorrectly.

Google's SRE book describes "graceful degradation" and "load shedding" as core reliability tools. Rather than letting a fully loaded system degrade to near-zero throughput for all clients, shed load deterministically: reject the cheapest requests to protect the most important ones, or reject requests from lower-priority clients to protect higher-priority ones.

Practical implementations:
- Return `503` with a `Retry-After` header when above a CPU or queue-depth threshold
- Prioritize requests by type (health checks > user-facing reads > background jobs)
- Use token buckets per client tier (premium clients get more tokens)
- Implement a "degraded mode" that disables non-critical features (e.g., turn off recommendations, return cached results) to reduce DB load
- Use a priority queue at the consumer: when overloaded, dequeue high-priority items first and discard low-priority ones from the tail

### Adaptive Throttling

Adaptive throttling is a more sophisticated alternative to fixed-threshold load shedding. Rather than using a static limit, the system adjusts its acceptance rate continuously based on observed rejection rate. Google's internal RPC framework (Stubby/gRPC) uses this approach.

The algorithm tracks, per client, two counters over a rolling window:
- `requests`: total requests sent
- `accepts`: requests the backend actually accepted

When the backend starts rejecting (signaled via response metadata), the client computes a local throttle probability:

```
throttle_probability = max(0, (requests - K * accepts) / (requests + 1))
```

Where K is typically 2. As the backend rejects more, `accepts` falls, `throttle_probability` rises, and the client proactively drops a fraction of its own requests before even sending them. This self-regulating loop converges to a stable rate matching backend capacity — without any central coordinator. It is the software equivalent of TCP congestion control.

Key property: adaptive throttling responds gradually. Unlike hard limits that flip from 0% to 100% rejection instantly, adaptive throttling smoothly backs off, reducing the "thundering herd" problem.

### The Thundering Herd Problem

The reject strategy has a dangerous failure mode: **synchronized retry storms.** When a backend returns 503 to many clients simultaneously, all clients back off for roughly the same duration and then retry at the same time. The retry wave hits the backend before it has recovered, causing another round of rejections — and the cycle repeats.

```
t=0:  Backend overloaded → rejects all clients
t=1:  All clients back off for 1 second
t=2:  All clients retry simultaneously → backend overloaded again → rejects all
t=3:  All clients back off for 2 seconds
t=5:  All clients retry simultaneously → backend overloaded again...
```

The fix is **jitter**: randomize the backoff interval so clients retry at different times. AWS's "Exponential Backoff and Jitter" blog post (2015) showed that full jitter (`sleep = random(0, min(cap, base * 2^attempt))`) dramatically reduces retried request collisions and allows backends to recover faster than any deterministic backoff.

### Flow Control in Streaming Systems

In stream processing (Kafka Streams, Flink, Spark Streaming), flow control is built into the framework:

**Kafka consumer fetch size** (`max.poll.records`, `fetch.max.bytes`) controls how many records a consumer fetches per poll. Reducing these limits slows the consumer's ingestion rate, allowing processing to catch up.

**Flink's credit-based flow control:** network buffers between operators use a credit system. A downstream operator grants credits to its upstream; the upstream sends one record per credit. When the downstream is slow, credits dry up, and the upstream slows automatically. This is Reactive Streams semantics at the framework level.

**Watermarks and late data:** stream processors use watermarks to track event time progress. A slow partition that produces events far behind the watermark is a form of backpressure on time — it causes the processor to wait for late events before advancing windows, increasing end-to-end latency.

### Trade-offs

| Strategy | Producer Impact | Latency Impact | Data Loss | Complexity |
|----------|----------------|----------------|-----------|------------|
| Block | Slows to consumer pace | High (producer stalls) | None | Low |
| Buffer | No immediate impact | Low (bursts absorbed) | None (until full) | Medium |
| Drop | No impact | None | Yes | Low |
| Reject (503) | Must retry/route | Medium (retry delay) | No | Medium |
| Adaptive throttle | Self-regulates | Low (gradual) | No | High |
| Scale consumers | No change | None | None | High (autoscaling) |

## Interview Talking Points

- "Backpressure is how a downstream component signals overload upstream — without it, the upstream drowns the downstream, causing queue buildup, memory exhaustion, or cascading failure"
- "Queue depth is the primary backpressure signal — a monotonically growing queue means consumers can't keep up. Either scale consumers or rate-limit producers"
- "Load shedding (returning 503) is preferable to accepting all requests and serving all of them badly — a system that serves 80% of requests well beats one that serves 100% at 10% normal speed"
- "Bounded buffers are essential — an unbounded buffer is memory exhaustion in slow motion. When the buffer is full, switch to drop or reject"
- "Blocking backpressure on a shared thread pool causes head-of-line blocking — one slow downstream starves all other request types sharing the pool. Use blocking only in dedicated pipelines"
- "The reject strategy creates a thundering herd if all clients use identical backoff intervals. Add full jitter: `sleep = random(0, min(cap, base * 2^attempt))`. Without jitter, retry storms re-overwhelm the recovering backend"
- "Adaptive throttling (Google's approach) is superior to hard limits — clients self-regulate based on observed rejection rate, smoothly converging to the backend's capacity without synchronized storms"
- "Reactive Streams formalize backpressure into a protocol: the subscriber requests N items; the publisher sends at most N. This is demand-driven flow control at the library level"
- "TCP already does backpressure via the receive window — when the application is too slow, the OS shrinks the window and throttles the sender. This is the model everything else is built on"

## Hands-on Lab

**Time:** ~25 minutes
**Services:** Redis (queue depth tracking), Python processes (producer, consumer)

### Setup

```bash
cd system-design-interview/02-advanced/18-backpressure-flow-control/
docker compose up -d
# Wait ~5 seconds for Redis to be ready
```

### Experiment

```bash
python experiment.py
```

The script runs five scenarios:
1. **No backpressure** — producer floods consumer, queue depth grows unboundedly
2. **Blocking backpressure** — producer pauses when queue exceeds threshold; demonstrates head-of-line blocking risk
3. **Drop strategy** — producer discards new items when queue is full; queue stays bounded with measured data loss
4. **Reject with jitter** — producer receives explicit 503 signals and backs off with exponential backoff + full jitter; demonstrates stable convergence
5. **Thundering herd** — same reject scenario but with deterministic (no jitter) backoff; demonstrates synchronized retry storms

Queue depth is tracked in Redis and displayed as a live ASCII histogram.

### Teardown

```bash
docker compose down -v
```

## Real-World Examples

- **Kafka consumer lag at Netflix:** Netflix monitors Kafka consumer lag across hundreds of consumer groups. When lag grows beyond a threshold, their autoscaler (based on Kafka consumer group metrics) increases the number of consumer instances. They set a max lag threshold per topic; exceeding it triggers both scaling and an alert — source: Netflix Tech Blog, "Kafka Consumer Lag Monitoring" (2016).
- **TCP backpressure at Cloudflare:** Cloudflare documented cases where a slow upstream (origin server) caused TCP receive buffers to fill, which caused their nginx workers to block on writes, exhausting worker processes. Their solution was to set aggressive send timeouts and buffer sizes so slow clients or origins don't monopolize workers — source: Cloudflare Blog, "How we scaled nginx and saved the world 54 years every day" (2018).
- **Google adaptive throttling:** Google's internal RPC framework (Stubby/gRPC) implements adaptive throttling at the client level. When a backend signals overload (via response metadata), clients automatically reduce request rate using a proportional-integral controller — similar to TCP congestion control but for RPC. The algorithm requires no central coordinator; each client independently converges to the correct rate — source: Beyer et al., "Site Reliability Engineering," Chapter 21.
- **AWS jitter recommendation:** AWS published analysis showing that exponential backoff without jitter causes retry storms that can keep a backend in a failure state indefinitely. Full jitter (`sleep = random(0, min(cap, base * 2^attempt))`) allows backends to recover in a fraction of the time — source: AWS Architecture Blog, "Exponential Backoff and Jitter" (2015).

## Common Mistakes

- **Unbounded buffers.** An in-memory queue with no size limit will grow until the process runs out of heap and crashes. Always set a max size and decide what to do when it's full.
- **Not monitoring queue depth.** Queue depth is the earliest warning signal of consumer overload. Without an alert on queue depth, the first symptom is a broker crash or massively delayed processing — by then the incident is already severe.
- **Backpressure that blocks shared thread pools.** Blocking the API request handler thread while waiting for a slow downstream causes head-of-line blocking that latency for all users sharing that pool. Apply blocking backpressure only in async/background pipelines with dedicated threads.
- **Dropping without metrics.** If you drop items silently, you have no visibility into the drop rate. Always count drops and expose them as a metric — a 5% drop rate is fine; a 50% drop rate is an incident.
- **Reject without jitter.** Returning 503 to all clients simultaneously causes synchronized retry storms. All backed-off clients resume at the same moment, re-overwhelming the just-recovering backend. Always add full jitter to retry backoff intervals.
- **Scaling consumers indefinitely.** Consumer scaling has limits: database connection pool size, downstream API rate limits, hardware capacity. Build in a maximum consumer count and activate load shedding before hitting these hard limits.
- **Static thresholds that don't adapt.** A fixed `503 when queue > 100` threshold may be too aggressive during normal variance and too lenient during sustained overload. Adaptive throttling or sliding-window-based limits respond more gracefully than hard cutoffs.
