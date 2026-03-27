# Stream Processing

**Prerequisites:** `../05-message-queues-kafka/`
**Next:** `../07-distributed-caching/`

---

## Concept

Batch processing is the traditional model for data analytics: collect data over a period (an hour, a day), process it all at once, and produce results. This works well for reports and dashboards where some latency is acceptable, but fails for real-time use cases: fraud detection must trigger within seconds, not hours; a recommendation engine should know you just added an item to your cart, not yesterday's cart contents. Stream processing extends the batch model to data in motion — events are processed as they arrive, producing results continuously rather than at the end of a batch.

The key conceptual shift is from thinking of data as a bounded dataset (a table) to thinking of data as an unbounded, continuously arriving sequence of events (a stream). A SQL query `SELECT user_id, COUNT(*) FROM clicks GROUP BY user_id` works on a finite table. The streaming equivalent must run continuously as new clicks arrive, updating counts in real time. But because the stream never ends, we need a different mechanism to bound the computation: **windows** — finite time intervals over which we aggregate.

**Windowing** is the fundamental primitive of stream processing. A **tumbling window** divides time into non-overlapping fixed-size buckets (e.g., minute 0:00–1:00, 1:00–2:00, 2:00–3:00). Each event belongs to exactly one window. This is the streaming equivalent of `GROUP BY hour(timestamp)`. A **sliding window** has a fixed size but slides forward in steps smaller than the window size (e.g., a 60-second window that advances every 10 seconds). Events belong to multiple overlapping windows. A **session window** has no fixed size — it groups events by activity gaps: if a user has no events for 30 seconds, the session ends and a new one begins on the next event. Session windows are ideal for user behavior analysis.

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
```

**Watermarks in Apache Flink:**
```
  Events arrive at processor (out of order):
  ts=100, ts=95, ts=110, ts=88, ts=115, ts=90 (late!)

  Watermark = max_seen - allowed_lateness = 115 - 10 = 105

  Window [60, 120): still open (watermark 105 < 120)
  After event ts=130: watermark = 130 - 10 = 120
  Window [60, 120): CLOSED, result emitted
  Event ts=90 arrives: ts < watermark → late! Route to side output.
```

**State management:** stream processors must maintain state across events (e.g., accumulating window counts). This state must be checkpointed to durable storage for fault tolerance. Flink uses RocksDB as its state backend — state can be larger than memory, and checkpoints to S3/HDFS allow recovery from failures without reprocessing the entire stream from scratch.

### Trade-offs

| Approach | Latency | Throughput | Complexity | Late data handling | Example |
|---|---|---|---|---|---|
| Batch (Spark) | Minutes-hours | Very high | Low | Easy (all data present) | Daily reports |
| Micro-batch (Spark Streaming) | Seconds | High | Low-medium | Good (per-batch) | Near-real-time metrics |
| True streaming (Flink) | Milliseconds | High | High | Excellent (watermarks) | Fraud detection |
| Kafka Streams | Milliseconds | Medium | Medium | Good | Microservice event processing |
| Lambda (batch + stream) | Seconds for recent, minutes for historical | Highest | Very high | Excellent | High-accuracy dashboards |
| Kappa (stream only) | Milliseconds | High | Medium | Good | Most modern use cases |

### Failure Modes

**State loss on worker failure without checkpointing:** if a stream processing worker crashes and state is only in memory (window counts, session data), all accumulated state is lost. Flink solves this with periodic checkpointing to durable storage; upon recovery, the job resumes from the last checkpoint. Kafka consumer offsets are reset to the checkpoint's corresponding Kafka offset, so events between the checkpoint and the crash are reprocessed.

**Watermark stalls from slow partitions:** in a Kafka-backed stream processor, the watermark advances to the minimum event time across all partitions. If one partition receives no events for a long time (e.g., a partition for a low-traffic user segment), the watermark stalls — windows never close, results never emit, and state memory grows unboundedly. Mitigation: use idle partition detection (declare a partition idle if no events for N seconds) and advance the watermark based on wall clock for idle partitions.

**Window state explosion:** session windows with long gaps accumulate open sessions indefinitely. A DoS attack generating fake user IDs would create millions of open sessions. Always configure maximum session duration, and monitor state size as an operational metric.

## Interview Talking Points

- "Tumbling windows divide time into non-overlapping fixed-size buckets. Sliding windows overlap — each event belongs to multiple windows. Session windows are gap-based, variable-size, and ideal for user behavior analysis."
- "Always use event time, not processing time, for correct results. A mobile device offline for an hour will produce events with event times 1 hour ago. Processing time-based windows would put them in the wrong window."
- "Watermarks define how long to wait for late events before closing a window. Watermark = max(event_time) - allowed_lateness. Too small: drop too many late events. Too large: slow results and high memory usage."
- "Lambda architecture = batch layer (accurate, high latency) + speed layer (approximate, low latency) + merge layer. Kappa architecture = stream only, with historical reprocessing done by replaying Kafka from offset 0."
- "Flink checkpoints state (window accumulators, join tables) to S3/HDFS periodically. On recovery, the job replays Kafka from the checkpoint's Kafka offset, reprocessing the recent events without starting from scratch."
- "Exactly-once in stream processing requires: (1) idempotent output sinks (writing the same record twice has no effect), (2) checkpointing input offsets atomically with output commits. Flink achieves this with two-phase commit to Kafka."

## Hands-on Lab

**Time:** ~25-35 minutes
**Services:** Zookeeper (2181), Kafka (9092), Python worker

### Setup

```bash
cd system-design-interview/02-advanced/06-stream-processing/
docker compose up -d
# Or run directly:
pip install kafka-python-ng
python experiment.py
```

### Experiment

The script produces 1000 click events spread across 5 minutes with random users and URLs, then runs tumbling-window aggregation showing per-minute per-user click counts, sliding-window computation showing top users in the last 60 seconds, demonstrates late-arriving event handling with three strategies (hard cutoff, watermark, side output), and closes with Lambda vs Kappa architecture comparisons.

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
