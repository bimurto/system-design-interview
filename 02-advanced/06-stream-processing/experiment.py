#!/usr/bin/env python3
"""
Stream Processing Lab — Windowing with pure Python + Kafka

Prerequisites: docker compose up -d (wait ~30s for Kafka to be ready)

What this demonstrates:
  1. Produce 1000 click events with realistic timestamps
  2. Tumbling window: count clicks per user per 1-minute window
  3. Sliding window: count clicks per user in last 60 seconds
  4. Late-arriving event: event backdated 90 seconds — handling strategies
  5. Print window results as they compute
"""

import json
import time
import subprocess
import uuid
import random
from datetime import datetime, timezone, timedelta
from collections import defaultdict

try:
    from kafka import KafkaProducer, KafkaConsumer
    from kafka.admin import KafkaAdminClient, NewTopic
    from kafka.errors import TopicAlreadyExistsError
except ImportError:
    print("Installing dependencies...")
    subprocess.run(["pip", "install", "kafka-python-ng", "-q"], check=True)
    from kafka import KafkaProducer, KafkaConsumer
    from kafka.admin import KafkaAdminClient, NewTopic
    from kafka.errors import TopicAlreadyExistsError

BOOTSTRAP   = "localhost:9092"
TOPIC       = "clicks"
NUM_USERS   = 20
NUM_EVENTS  = 1000
WINDOW_SEC  = 60   # 1-minute windows


def section(title):
    print(f"\n{'=' * 60}")
    print(f"  {title}")
    print("=" * 60)


def create_topic(name):
    admin = KafkaAdminClient(bootstrap_servers=BOOTSTRAP)
    try:
        admin.create_topics([NewTopic(name=name, num_partitions=1, replication_factor=1)])
    except TopicAlreadyExistsError:
        pass
    finally:
        admin.close()


def get_producer():
    return KafkaProducer(
        bootstrap_servers=BOOTSTRAP,
        key_serializer=lambda k: k.encode() if k else None,
        value_serializer=lambda v: json.dumps(v).encode(),
        acks="all",
    )


def tumbling_window_key(event_ts_seconds, window_size_seconds):
    """Map a timestamp to the start of its tumbling window."""
    return (event_ts_seconds // window_size_seconds) * window_size_seconds


def tumbling_aggregate(events, window_size_seconds=WINDOW_SEC):
    """Aggregate events into non-overlapping tumbling windows."""
    # {(user_id, window_start_ts) -> count}
    counts = defaultdict(int)
    for ev in events:
        ts    = ev["ts"]
        user  = ev["user_id"]
        wkey  = tumbling_window_key(ts, window_size_seconds)
        counts[(user, wkey)] += 1
    return counts


def sliding_window_counts(events, now_ts, window_size_seconds=WINDOW_SEC):
    """Count events per user in the last window_size_seconds ending at now_ts."""
    cutoff = now_ts - window_size_seconds
    counts = defaultdict(int)
    for ev in events:
        if ev["ts"] >= cutoff:
            counts[ev["user_id"]] += 1
    return counts


def format_ts(ts):
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%H:%M:%S")


def main():
    section("STREAM PROCESSING LAB — WINDOWING")
    print("""
  Stream processing concepts:

  Batch processing:
    Collect all data → process it once → output results
    Latency: minutes to hours (Hadoop MapReduce)

  Stream processing:
    Process data as it arrives, in real time
    Latency: milliseconds to seconds (Flink, Spark Streaming, Kafka Streams)

  Window types:
  ┌──────────────────────────────────────────────────────────┐
  │                                                          │
  │  TUMBLING (non-overlapping, fixed size):                 │
  │  [00:00─01:00)[01:00─02:00)[02:00─03:00)                 │
  │                                                          │
  │  SLIDING (overlapping, fixed size, fixed slide):         │
  │  [00:00─01:00)                                           │
  │       [00:30─01:30)                                      │
  │            [01:00─02:00)                                 │
  │                                                          │
  │  SESSION (variable size, gap-based):                     │
  │  [─activity─][gap≥30s][─activity─]                       │
  │                                                          │
  └──────────────────────────────────────────────────────────┘
""")

    # Verify connectivity
    try:
        admin = KafkaAdminClient(bootstrap_servers=BOOTSTRAP)
        admin.list_topics()
        admin.close()
        print("  Connected to Kafka.")
    except Exception as e:
        print(f"  ERROR: {e}")
        print("  Run: docker compose up -d && sleep 30")
        return

    create_topic(TOPIC)

    # ── Phase 1: Produce 1000 click events ────────────────────────
    section("Phase 1: Producing 1000 Click Events")

    now = int(time.time())
    random.seed(42)

    # Generate events spread across a 5-minute window
    events = []
    for i in range(NUM_EVENTS):
        user_id = f"user-{random.randint(1, NUM_USERS):03d}"
        # Spread events over last 5 minutes (300 seconds)
        ts = now - random.randint(0, 299)
        url = random.choice(["/home", "/products", "/cart", "/checkout", "/search"])
        events.append({
            "event_id": str(uuid.uuid4()),
            "user_id":  user_id,
            "url":      url,
            "ts":       ts,
        })

    # Sort by timestamp to simulate ordered stream
    events.sort(key=lambda e: e["ts"])

    producer = get_producer()
    for ev in events:
        producer.send(TOPIC, key=ev["user_id"], value=ev)
    producer.flush()
    producer.close()

    print(f"  Produced {NUM_EVENTS} click events.")
    print(f"  Time range: {format_ts(events[0]['ts'])} → {format_ts(events[-1]['ts'])}")
    print(f"  Users: {NUM_USERS} unique users")
    print(f"  Avg events/user: {NUM_EVENTS / NUM_USERS:.0f}")

    # ── Phase 2: Tumbling window aggregation ──────────────────────
    section("Phase 2: Tumbling Window — Clicks per User per Minute")

    # Consume all events
    consumer = KafkaConsumer(
        TOPIC,
        bootstrap_servers=BOOTSTRAP,
        group_id="tumbling-processor",
        auto_offset_reset="earliest",
        consumer_timeout_ms=5000,
        value_deserializer=lambda v: json.loads(v.decode()),
    )
    consumed = [msg.value for msg in consumer]
    consumer.close()
    print(f"\n  Consumed {len(consumed)} events.")

    tumbling = tumbling_aggregate(consumed)

    # Print top users in each window
    from collections import Counter
    windows = sorted(set(wkey for _, wkey in tumbling.keys()))
    print(f"\n  Tumbling 1-minute windows:")
    for wkey in windows:
        window_data = {u: c for (u, w), c in tumbling.items() if w == wkey}
        top = Counter(window_data).most_common(3)
        total = sum(window_data.values())
        print(f"    [{format_ts(wkey)}─{format_ts(wkey+WINDOW_SEC)}): "
              f"{total:3d} clicks | top: " +
              ", ".join(f"{u}={c}" for u, c in top))

    print(f"""
  Tumbling window properties:
    • Non-overlapping: each event belongs to exactly one window
    • Fixed size: every window is exactly {WINDOW_SEC} seconds
    • Fixed start: windows are aligned to epoch (00:00, 01:00, 02:00, ...)
    • Use case: "how many orders per hour?" "daily active users?"

  Key implementation detail:
    window_start = (event_ts // window_size) * window_size
    This snaps any timestamp to the start of its containing window.
""")

    # ── Phase 3: Sliding window ────────────────────────────────────
    section("Phase 3: Sliding Window — Clicks per User in Last 60 Seconds")

    ref_time = now  # "now" for the sliding window
    sliding = sliding_window_counts(consumed, ref_time, WINDOW_SEC)

    top_sliding = Counter(sliding).most_common(5)
    print(f"\n  Top 5 users by clicks in last {WINDOW_SEC}s (as of {format_ts(ref_time)}):\n")
    for rank, (user, count) in enumerate(top_sliding, 1):
        bar = "#" * min(count, 40)
        print(f"    #{rank}: {user} — {count} clicks  {bar}")

    # Advance "now" by 30 seconds — window slides
    ref_time_30s = now + 30
    events_future = [e for e in consumed if e["ts"] <= ref_time_30s]
    # Simulate new events in the +30s window
    new_events = []
    for i in range(50):
        user_id = f"user-{random.randint(1, NUM_USERS):03d}"
        ts = now + random.randint(1, 30)
        new_events.append({"user_id": user_id, "ts": ts, "url": "/new"})
    all_events = consumed + new_events

    sliding_30 = sliding_window_counts(all_events, ref_time_30s, WINDOW_SEC)
    top_30 = Counter(sliding_30).most_common(5)
    print(f"\n  Top 5 users 30 seconds later (window slid +30s, 50 new events):\n")
    for rank, (user, count) in enumerate(top_30, 1):
        bar = "#" * min(count, 40)
        print(f"    #{rank}: {user} — {count} clicks  {bar}")

    print(f"""
  Sliding window vs tumbling:
    • A sliding window of size W, slide S:
      - W=60s, S=1s: a new window every second — high overlap, expensive
      - W=60s, S=60s: no overlap — same as tumbling
    • More useful for "real-time" metrics: rate-of-change, recent activity
    • Implementation cost: O(W/S) windows per event (more expensive than tumbling)

  Efficient implementation: use a circular buffer or monotone deque
  to add/remove events at window boundaries in O(1) time.
""")

    # ── Phase 4: Late-arriving events ─────────────────────────────
    section("Phase 4: Late-Arriving Events — Watermarks")

    late_ts  = now - 90   # event timestamped 90 seconds ago
    late_ev  = {
        "event_id": str(uuid.uuid4()),
        "user_id":  "user-001",
        "url":      "/checkout",
        "ts":       late_ts,
    }

    print(f"""
  Late-arriving event:
    Event timestamp (event time):   {format_ts(late_ts)}
    Current processing time (now):  {format_ts(now)}
    Lateness:                       90 seconds

  Why events arrive late:
    • Mobile device was offline, events buffered locally
    • Network delays or retries
    • Clock skew between producing service and consumer
    • Kafka topic partition lag (consumer processed events out of order)
""")

    producer = get_producer()
    producer.send(TOPIC, key=late_ev["user_id"], value=late_ev)
    producer.flush()
    producer.close()

    # Strategy 1: Hard cutoff — drop events older than 60s
    cutoff = now - WINDOW_SEC
    if late_ev["ts"] < cutoff:
        print(f"  Strategy 1 — Hard cutoff (drop if > {WINDOW_SEC}s late):")
        print(f"    Event ts={format_ts(late_ev['ts'])} < cutoff={format_ts(cutoff)}")
        print(f"    → DROPPED. Simple, but causes undercounting.")
    else:
        print(f"    → ACCEPTED (within cutoff)")

    # Strategy 2: Watermark with allowed lateness
    watermark = now - 30   # allow up to 30s lateness
    window_start = tumbling_window_key(late_ev["ts"], WINDOW_SEC)
    window_end   = window_start + WINDOW_SEC

    print(f"\n  Strategy 2 — Watermark with allowed lateness (30s):")
    print(f"    Watermark (allowed lateness boundary): {format_ts(watermark)}")
    print(f"    Event belongs to window: [{format_ts(window_start)}─{format_ts(window_end)})")
    if window_end > watermark:
        print(f"    → ACCEPTED: window hasn't passed the watermark yet.")
        print(f"    Recompute window [{format_ts(window_start)}, {format_ts(window_end)}) with late event.")
    else:
        print(f"    → DROPPED: window has already passed the watermark.")
        print(f"    Route to side output for later reconciliation.")

    # Strategy 3: Side output / dead-letter for very late events
    print(f"\n  Strategy 3 — Side output:")
    print(f"    Late events beyond allowed lateness → 'late-events' topic")
    print(f"    Batch job reconciles late events against historical windows daily")
    print(f"    → Eventual accuracy at the cost of real-time accuracy")

    print(f"""
  Watermark in Flink/Spark:
    A watermark at time T means: "I guarantee no event with
    timestamp < T will arrive in the future."
    The system uses watermarks to decide when to close a window.

  Watermark = max(event_time) - allowed_lateness

  Too small allowed_lateness: windows close early, late events dropped
  Too large allowed_lateness: windows stay open longer, higher memory usage,
                               slower results, more recomputation
""")

    # ── Phase 5: Lambda vs Kappa architecture ─────────────────────
    section("Phase 5: Architecture Patterns — Lambda vs Kappa")

    print("""
  LAMBDA ARCHITECTURE (Nathan Marz, 2011):
  ┌─────────────────────────────────────────────────────────┐
  │                                                         │
  │   Raw data ──►  Batch layer (Hadoop/Spark)              │
  │              │  Reprocesses ALL historical data         │
  │              │  Produces accurate batch views            │
  │              │                                          │
  │              └► Speed layer (Kafka Streams/Flink)       │
  │                 Only processes recent data               │
  │                 Low latency, approximate results         │
  │                                                         │
  │   Query layer merges batch view + speed view            │
  │                                                         │
  └─────────────────────────────────────────────────────────┘
    + Handles late data (batch layer corrects speed layer)
    - Two codebases to maintain (batch and streaming)
    - Results merge is complex

  KAPPA ARCHITECTURE (Jay Kreps, 2014):
  ┌─────────────────────────────────────────────────────────┐
  │                                                         │
  │   Raw data ──► Kafka (long retention) ──► Stream job   │
  │                                                         │
  │   Reprocessing: deploy new version of stream job        │
  │   with a new consumer group; replay Kafka from offset 0 │
  │                                                         │
  └─────────────────────────────────────────────────────────┘
    + Single codebase (stream processing only)
    + Simpler operational model
    - Reprocessing may be slower than batch for very large data sets
    - Requires long Kafka retention for historical replay

  Modern trend: Kappa is preferred when:
    • Stream processing framework is mature (Flink, Kafka Streams)
    • Event log retention covers the required historical depth
    • Reprocessing windows are well-defined

  Lambda is still used when:
    • Batch jobs need to join with large datasets (e.g., 10TB dimension tables)
    • The batch job is in SQL and extremely simple to maintain
    • Reprocessing speed is critical (Spark batch >> Kafka consumer replay)
""")

    section("Summary")
    print("""
  Stream vs Batch:
    Batch: latency minutes-hours, processes all data at once, simpler
    Micro-batch (Spark Streaming): latency seconds, batches every N seconds
    True streaming (Flink, Kafka Streams): latency ms, per-event processing

  Window types:
    Tumbling: fixed, non-overlapping; use for hourly/daily aggregations
    Sliding:  overlapping; use for moving averages, rate-of-change
    Session:  variable; use for user sessions (gap = inactivity timeout)
    Global:   single window; use for total aggregations

  Time domains:
    Event time:      when the event actually occurred (in the data)
    Processing time: when the event is processed by the stream job
    Ingestion time:  when the event entered Kafka

    Always use event time for correctness.
    Processing time is easier but produces wrong results for late/reordered data.

  Frameworks:
    Apache Flink:    true streaming, event time, exactly-once, stateful
    Spark Streaming: micro-batch, mature ecosystem, SQL support
    Kafka Streams:   library (no cluster), tight Kafka integration
    ksqlDB:          SQL over Kafka streams, easy to get started

  Next: ../07-distributed-caching/
""")


if __name__ == "__main__":
    main()
