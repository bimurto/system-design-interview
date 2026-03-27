# Stream Processing

**Prerequisites:** `../05-message-queues-kafka/`
**Next:** `../08-distributed-caching/`

---

## Concept

Batch processing is the traditional model for data analytics: collect data over a period (an hour, a day), process it all at once, and produce results. This works well for reports and dashboards where some latency is acceptable, but fails for real-time use cases: fraud detection must trigger within seconds, not hours; a recommendation engine should know you just added an item to your cart, not yesterday's cart contents. Stream processing extends the batch model to data in motion — events are processed as they arrive, producing results continuously rather than at the end of a batch.

The key conceptual shift is from thinking of data as a bounded dataset (a table) to thinking of data as an unbounded, continuously arriving sequence of events (a stream). A SQL query `SELECT user_id, COUNT(*) FROM clicks GROUP BY user_id` works on a finite table. The streaming equivalent must run continuously as new clicks arrive, updating counts in real time. But because the stream never ends, we need a different mechanism to bound the computation: **windows** — finite time intervals over which we aggregate.

**Windowing** is the fundamental primitive of stream processing. A **tumbling window** divides time into non-overlapping fixed-size buckets (e.g., minute 0:00–1:00, 1:00–2:00, 2:00–3:00). Each event belongs to exactly one window. This is the streaming equivalent of `GROUP BY hour(timestamp)`. A **sliding window** has a fixed size but slides forward in steps smaller than the window size (e.g., a 60-second window that advances every 10 seconds). Events belong to multiple overlapping windows — each event is counted in `window_size / slide_step` windows. A **session window** has no fixed size — it groups events by activity gaps: if a user has no events for 30 seconds, the session ends and a new one begins on the next event. Session windows are ideal for user behavior analysis. A **global window** collects all events into a single unbounded window; results are only emitted when an explicit trigger fires (e.g., count-based: emit every 1000 events). This is rarely the right choice but appears in interview discussions about trigger-based processing.

The hard problem in stream processing is **time**. Events have an *event time* (when they occurred according to the device clock) and a *processing time* (when they arrived at the stream processor). In a perfect world these are the same, but mobile devices go offline and buffer events, networks delay delivery, and clock skew is ubiquitous. If you group by processing time, a user activity event from an offline phone that syncs an hour late looks like it happened an hour later. If you group by event time (correct), you need to handle the fact that events may arrive at the processor out of order and late.

**Watermarks** solve the late event problem by defining a threshold of allowed lateness. A watermark at time T means "the stream processor has seen all events with event time ≤ T." When the watermark advances past T + window_end, the window is closed and its result emitted. The watermark is typically computed as `max(event_time) - allowed_lateness`, where `allowed_lateness` is a configuration parameter. Setting it too low means late events are dropped (undercounting). Setting it too high means windows stay open longer, consuming more memory and delaying results.

## How It Works

**Tumbling windows:**
```
  Time axis:  0        60       120      180      240
              |────────|────────|────────|────────|
  Window 1:   [     W1     )
  Window 2:            [     W2     )
  Window 3:                     [     W3     )

  Event at t=45:  belongs to W1 only
  Event at t=75:  belongs to W2 only
  Event at t=145: belongs to W3 only

  Implementation: window_start = (ts // 60) * 60
```

**Sliding windows:**
```
  Window size=60s, slide=30s:

  Time:   0        30       60       90       120
          |────────|────────|────────|────────|
  W[0]:   [────────────────)         (size=60s)
  W[30]:           [────────────────)
  W[60]:                    [────────────────)

  Event at t=45: belongs to W[0] AND W[30]
  → O(window_size/slide) windows per event
```

**Session windows:**
```
  Activity: [ev1]...[ev2]...[ev3]  [gap > 30s]  [ev4]...[ev5]
                                        ↓
  Session 1: [ev1────ev2────ev3]   Session 2: [ev4────ev5]

  State per user: just the timestamp of the last event seen.
  Gap detection: if (current_ts - last_ts) > gap_threshold → new session.
  Failure mode: unlimited open sessions if users generate unique IDs.
  Fix: configure max_session_duration (e.g., 30 minutes absolute cap).
```

**Global windows:**
```
  Single window spanning all time:
  [────────────────────────────────────────────────►  (never closes automatically)

  Requires explicit trigger, e.g.:
    Count trigger: emit every 1000 events
    Processing-time trigger: emit every 60 seconds
  Use case: running totals, count-based sampling.
```

**Watermarks in Apache Flink (step-by-step):**
```
  Stream arrives at processor in this order (NOT sorted by event time):
  ts=100, ts=95, ts=110, ts=88, ts=115

  allowed_lateness = 10, window_size = 60

  After ts=100: watermark = 100 - 10 = 90.  Window [60,120) still open.
  After ts=95:  watermark = 90 (max_seen unchanged). Window [60,120) open.
  After ts=110: watermark = 100. Window [60,120) still open (100 < 120).
  After ts=88:  watermark = 100 (88 < 110, no change). Still open.
  After ts=115: watermark = 105. Still open (105 < 120).

  Now ts=130 arrives:
    max_seen = 130, watermark = 130 - 10 = 120
    120 >= window_end(120) → Window [60,120): CLOSED, result emitted.

  Now ts=90 arrives (very late):
    Its window [60,120) is already closed → route to side output.
    The event is NOT silently dropped — it goes to a dedicated late-events topic.

  Watermark = max(event_time_seen) - allowed_lateness
            = a monotonically increasing lower bound on event times still arriving.
```

**State management:** stream processors must maintain state across events (e.g., accumulating window counts). This state must be checkpointed to durable storage for fault tolerance. Flink uses RocksDB as its state backend — state can be larger than memory, and checkpoints to S3/HDFS allow recovery from failures without reprocessing the entire stream from scratch.

**Backpressure** is a critical operational concern. When a downstream operator (e.g., a sink writing to a database) processes slower than the upstream source (Kafka), the unbounded internal queue between them grows until the process runs out of memory and crashes. Flink handles backpressure by propagating credit tokens upstream — when the sink's input buffer is full, the upstream operator stops reading from Kafka. This prevents memory blowup at the cost of increased consumer lag. Kafka consumer lag is the primary operational signal for backpressure; spike it past the consumer group's `max.poll.interval.ms` and the consumer will be kicked out of the group, triggering a rebalance.

### Trade-offs

| Approach | Latency | Throughput | State management | Late data handling | When to use |
|---|---|---|---|---|---|
| Batch (Spark) | Minutes–hours | Very high (no streaming overhead) | N/A (data fully present) | Trivial (reprocess everything) | Daily reports, large dimension joins |
| Micro-batch (Spark Streaming) | Seconds | High | Checkpoint to HDFS | Per-batch watermarks | Teams with Spark expertise, SQL-heavy |
| True streaming (Flink) | Milliseconds | High | RocksDB + S3 checkpoints | Watermarks + side output + exactly-once | Fraud detection, real-time ML features |
| Kafka Streams | Milliseconds | Medium (library, scales with app) | RocksDB + Kafka changelog | Grace period on windowed stores | Microservice event enrichment |
| Lambda (batch + stream) | ms for recent, min for historical | Highest (batch layer) | Two separate systems | Excellent (batch corrects stream) | High-accuracy dashboards, billing |
| Kappa (stream only) | Milliseconds | High | Single streaming system | Good (watermarks + replay) | Most modern use cases, event sourcing |

**Throughput nuance:** Flink and Kafka Streams can both sustain millions of events/second per node, but Spark batch achieves higher single-job throughput because it can process data without the per-event overhead of state checkpointing and network shuffle. The throughput gap narrows when Flink is tuned with large buffer timeouts (trading latency for throughput).

### Failure Modes

**State loss on worker failure without checkpointing:** if a stream processing worker crashes and state is only in memory (window counts, session data), all accumulated state is lost. Flink solves this with periodic checkpointing to durable storage; upon recovery, the job resumes from the last checkpoint. Kafka consumer offsets are reset to the checkpoint's corresponding Kafka offset, so events between the checkpoint and the crash are reprocessed. Checkpoint interval is a tuning knob: shorter intervals reduce recovery time but add overhead (each checkpoint pauses processing briefly for barrier alignment).

**Watermark stalls from idle Kafka partitions:** the watermark advances to the minimum event time across all source partitions. If one partition receives no events for a long time (e.g., a partition for a low-traffic user segment), the watermark stalls for the entire job — windows never close, results never emit, and state memory grows unboundedly. Mitigation: use idle partition detection (declare a partition idle if no events arrive within N seconds) and advance the watermark based on wall-clock time for idle partitions. This is a common production incident at companies running Flink on unevenly-partitioned Kafka topics.

**Window state explosion:** session windows with long gaps accumulate open sessions indefinitely. A DoS attack generating fake user IDs would create millions of open sessions. Always configure maximum session duration, and monitor state size as an operational metric. Flink's `StateTtlConfig` and Kafka Streams' `Retention` setting both provide state TTL.

**Backpressure cascade:** when a downstream operator (database sink, external API call) is slow, its input buffer fills up. Flink propagates backpressure upstream by withholding network credit, eventually stalling the Kafka source consumer. The consumer stops polling Kafka; if it exceeds `max.poll.interval.ms` (default 5 minutes), the broker kicks it out of the consumer group and triggers a rebalance. During rebalance, processing halts entirely. Mitigation: async I/O for external calls, bulk writes to sinks, and monitoring Kafka consumer lag as an early warning signal.

**Checkpoint barrier skew:** in a job with many parallel operators, checkpoint barriers (markers that trigger a consistent snapshot) must flow through all operators. If one slow operator holds a barrier for minutes, the checkpoint is delayed for the entire job. Long checkpoints indicate a bottleneck — profile the slow operator and consider increasing parallelism or adding async I/O.

**Out-of-order events across Kafka partitions:** Kafka guarantees ordering within a partition but not across partitions. A user's events may be spread across multiple partitions (if keyed by a hash other than user_id). The stream processor sees events for the same user arriving in non-event-time order. This is why watermarks are per-partition and the global watermark is the minimum across all partitions.

## Interview Talking Points

- "Tumbling windows divide time into non-overlapping fixed-size buckets — each event belongs to exactly one window. Sliding windows overlap; each event belongs to `size/slide` windows, so they cost more memory and CPU. Session windows are gap-based and variable-size — ideal for user journey analysis and funnel attribution. Global windows collect everything into one bucket and require an explicit trigger to emit results."
- "Always use event time, not processing time. A mobile device offline for an hour produces events with event times 1 hour ago. Processing time windows would assign them to the current minute, which is wrong. Processing time is easy but systematically incorrect for out-of-order or late data."
- "Watermark = max(event_time_seen) - allowed_lateness. It is a monotonically increasing lower bound: the processor declares 'all events with ts < watermark have arrived.' A window closes when watermark >= window_end. Idle Kafka partitions freeze the watermark for all windows — use idle partition detection to avoid stalls."
- "Lambda architecture = batch layer (accurate, high latency) + speed layer (approximate, low latency) + serving layer that merges both. Kappa = stream only, with reprocessing done by deploying a new consumer group that replays Kafka from offset 0. Kappa is preferred today when Kafka retention covers the required historical depth."
- "Flink checkpoints state (window accumulators, join tables, session timers) to S3/HDFS periodically using barrier-based consistent snapshots. On recovery, the job replays Kafka from the checkpoint's recorded Kafka offset, reprocessing only the events since the last checkpoint — not the entire stream."
- "Exactly-once requires two things: (1) idempotent sinks — writing the same record twice has no effect (e.g., UPSERT with a dedup key, or Kafka transactions); (2) atomic commit of input offsets and output — Flink achieves this with a two-phase commit protocol coordinated via the checkpoint mechanism."
- "Backpressure: a slow sink stalls the upstream Kafka consumer. If the consumer exceeds max.poll.interval.ms (default 5 minutes), the broker removes it from the consumer group and triggers a rebalance — stopping all processing. Monitor Kafka consumer lag continuously; a growing lag is the first warning sign."
- "State TTL is non-negotiable in production. Session windows without a max-duration cap, join tables without expiry, and uncompacted RocksDB state all grow unboundedly. Always configure StateTtlConfig in Flink or Retention in Kafka Streams, and alert on state backend size."

## Hands-on Lab

**Time:** ~30-40 minutes
**Services:** Zookeeper (2181), Kafka (9092)

### Setup

```bash
cd system-design-interview/02-advanced/07-stream-processing/
docker compose up -d
# Wait ~30s for Kafka to be ready, then:
pip install kafka-python-ng
python experiment.py
```

### Experiment

The script runs seven phases:
1. **Produce** 1000 click events with ±15s jitter (realistic out-of-order timestamps)
2. **Tumbling window** — per-user clicks aggregated into 1-minute buckets using event time
3. **Sliding window** — top-5 users in a 60s window, then advances the window by 30s and shows rank changes
4. **Session window** — groups each user's events by 30s inactivity gaps; renders a timeline for the user with the most sessions
5. **Watermark mechanics** — steps through a small out-of-order stream event-by-event, showing exactly when windows open and close
6. **Late event strategies** — demonstrates hard cutoff, watermark with allowed lateness, and side output
7. **Lambda vs Kappa** — architecture comparison with framework trade-offs

### Break It

```bash
# Simulate watermark stall: produce events only for very old timestamps
python -c "
from kafka import KafkaProducer
import json, time, uuid, random

producer = KafkaProducer(
    bootstrap_servers='localhost:9092',
    key_serializer=lambda k: k.encode(),
    value_serializer=lambda v: json.dumps(v).encode(),
)

now = int(time.time())

# Produce 100 events with timestamps 1 hour ago
print('Producing 100 events timestamped 1 hour ago...')
for i in range(100):
    ev = {'event_id': str(uuid.uuid4()), 'user_id': 'user-old', 'ts': now - 3600, 'url': '/old'}
    producer.send('clicks', key='user-old', value=ev)
producer.flush()

# Now produce 1 normal event
ev = {'event_id': str(uuid.uuid4()), 'user_id': 'user-new', 'ts': now, 'url': '/new'}
producer.send('clicks', key='user-new', value=ev)
producer.flush()
producer.close()

print('In a watermark-based system:')
print('  Watermark = max_seen(now) - allowed_lateness')
print('  The 100 old events are 1 hour late — all dropped unless allowed_lateness > 3600s')
print('  This is correct: they belong to windows from an hour ago that are already closed')
print('  Side output / reconciliation batch job handles them')
"
```

### Observe

The tumbling window output shows roughly equal click counts per minute (since events are uniformly distributed). The sliding window top-5 changes when the window advances 30 seconds. The late event (90 seconds late) is dropped by the hard cutoff (60s) but accepted by the watermark strategy (30s allowed lateness) since 90s > 30s: it's dropped and routed to the side output.

### Teardown

```bash
docker compose down -v
```

## Real-World Examples

- **Apache Flink at Alibaba:** Alibaba processes 4.72 trillion events per day through Apache Flink during Singles' Day (11/11). Their use case includes real-time inventory updates, fraud detection, and recommendation engine updates. Flink's event-time windowing allows them to correctly attribute orders to the event time, handling mobile network delays. Source: Alibaba Cloud Blog, "Apache Flink at Alibaba's Scale," 2019.
- **Lyft's real-time fraud detection:** Lyft uses Flink to process rider and driver location events, computing sliding windows to detect unusual ride patterns. A ride that suddenly jumps 100 miles between GPS pings triggers a fraud alert. The sliding window of 5 minutes is computed per rider using event time, so GPS buffering on weak networks doesn't cause false negatives. Source: Lyft Engineering Blog, "Stream Processing with Apache Flink," 2019.
- **LinkedIn's Samza → Kafka Streams migration:** LinkedIn originally built Samza (their stream processing framework) and later migrated to Kafka Streams for most pipelines. Their Kappa-style architecture uses Kafka topics with long retention as the historical record; reprocessing is done by deploying a new consumer group that replays from offset 0. Source: LinkedIn Engineering Blog, "Apache Kafka, Samza, and the Unix Philosophy of Distributed Data," 2013.

## Common Mistakes

- **Using processing time instead of event time.** This is the most common stream processing mistake. Processing time is easy (just use `time.time()`), but it produces incorrect results for any event that arrives late or out of order. Always timestamp events at the source and use event time for windowing.
- **Setting allowed lateness too high "to be safe."** A 10-minute allowed lateness on a 1-minute tumbling window means each window stays open for 11 minutes. Memory usage is 11x higher, and results are delayed by up to 10 minutes. Measure actual lateness distribution from your data and set allowed_lateness to the 99th percentile.
- **Not handling state growth.** Window state, join tables, and session state all grow unboundedly if not cleaned up. Always configure state TTL (Flink: `StateTtlConfig`, Kafka Streams: `Retention`) to bound state size. Monitor state backend size as a key operational metric.
- **Ignoring the idle partition problem.** In multi-partition Kafka topics, the watermark is the minimum across all partitions. One idle partition stalls the entire pipeline. Use idle partition detection or per-partition watermarks with periodic wall-clock advancement.
