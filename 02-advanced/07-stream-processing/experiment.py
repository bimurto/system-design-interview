#!/usr/bin/env python3
"""
Stream Processing Lab — Windowing, Watermarks, and Session Windows with Kafka

Prerequisites: docker compose up -d (wait ~30s for Kafka to be ready)

What this demonstrates:
  1. Produce 1000 click events with realistic, out-of-order timestamps
  2. Tumbling window: count clicks per user per 1-minute window
  3. Sliding window: count clicks per user in last 60 seconds (with window slide)
  4. Session window: group user events by inactivity gaps
  5. Out-of-order events: step through watermark advancement with explicit event stream
  6. Late-arriving event: three handling strategies (hard cutoff, watermark, side output)
  7. Lambda vs Kappa architecture comparison
"""

import json
import time
import subprocess
import uuid
import random
from datetime import datetime, timezone
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


# ── Helpers ────────────────────────────────────────────────────────────────────

def section(title):
    print(f"\n{'=' * 60}")
    print(f"  {title}")
    print("=" * 60)


def format_ts(ts):
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%H:%M:%S")


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


# ── Window logic ───────────────────────────────────────────────────────────────

def tumbling_window_key(event_ts_seconds, window_size_seconds):
    """Map a timestamp to the start of its tumbling window bucket."""
    return (event_ts_seconds // window_size_seconds) * window_size_seconds


def tumbling_aggregate(events, window_size_seconds=WINDOW_SEC):
    """
    Aggregate events into non-overlapping tumbling windows.
    Returns {(user_id, window_start_ts) -> count}.
    """
    counts = defaultdict(int)
    for ev in events:
        ts   = ev["ts"]
        user = ev["user_id"]
        wkey = tumbling_window_key(ts, window_size_seconds)
        counts[(user, wkey)] += 1
    return counts


def sliding_window_counts(events, now_ts, window_size_seconds=WINDOW_SEC):
    """
    Count events per user in the last window_size_seconds ending at now_ts.
    This models a single snapshot of a sliding window — in a real stream
    processor the window advances continuously as new events arrive.
    """
    cutoff = now_ts - window_size_seconds
    counts = defaultdict(int)
    for ev in events:
        if cutoff <= ev["ts"] <= now_ts:
            counts[ev["user_id"]] += 1
    return counts


def session_windows(user_events, gap_seconds=30):
    """
    Group a sorted list of events for one user into sessions.
    A new session begins whenever the gap between consecutive events exceeds
    gap_seconds (the inactivity timeout).

    Returns a list of sessions: each session is a list of events.

    Real framework equivalent:
      Flink: EventTimeSessionWindows.withGap(Time.seconds(gap_seconds))
      Kafka Streams: SessionWindows.ofInactivityGapWithNoGrace(Duration.ofSeconds(gap_seconds))
    """
    if not user_events:
        return []
    sorted_events = sorted(user_events, key=lambda e: e["ts"])
    sessions = [[sorted_events[0]]]
    for ev in sorted_events[1:]:
        if ev["ts"] - sessions[-1][-1]["ts"] > gap_seconds:
            sessions.append([ev])  # gap exceeded — start new session
        else:
            sessions[-1].append(ev)
    return sessions


def compute_watermark(events, allowed_lateness_seconds):
    """
    Watermark = max(event_time_seen) - allowed_lateness.
    The watermark is a heuristic boundary: the processor declares that
    all events with ts < watermark have been seen. Windows whose end
    timestamp <= watermark are eligible to close.
    """
    if not events:
        return None
    max_ts = max(e["ts"] for e in events)
    return max_ts - allowed_lateness_seconds


# ── Phases ─────────────────────────────────────────────────────────────────────

def phase1_produce(now):
    """Produce 1000 click events with realistic out-of-order timestamps."""
    section("Phase 1: Producing 1000 Click Events (with out-of-order noise)")

    random.seed(42)

    events = []
    for _ in range(NUM_EVENTS):
        user_id = f"user-{random.randint(1, NUM_USERS):03d}"
        # Base timestamp spread over the last 5 minutes
        base_ts = now - random.randint(0, 299)
        # Add realistic jitter: ±15 seconds to simulate network delay / clock skew.
        # This produces out-of-order events — critical for watermark discussion.
        jitter = random.randint(-15, 5)
        ts = max(now - 300, base_ts + jitter)  # clamp to our 5-minute window
        url = random.choice(["/home", "/products", "/cart", "/checkout", "/search"])
        events.append({
            "event_id": str(uuid.uuid4()),
            "user_id":  user_id,
            "url":      url,
            "ts":       ts,
        })

    # Produce in arrival order (by processing time = now), NOT sorted by event time.
    # This simulates a real Kafka topic where events arrive out of event-time order.
    producer = get_producer()
    for ev in events:
        producer.send(TOPIC, key=ev["user_id"], value=ev)
    producer.flush()
    producer.close()

    sorted_ts = sorted(e["ts"] for e in events)
    print(f"  Produced {NUM_EVENTS} click events.")
    print(f"  Event-time range: {format_ts(sorted_ts[0])} → {format_ts(sorted_ts[-1])}")
    print(f"  Users: {NUM_USERS} unique users, avg {NUM_EVENTS // NUM_USERS} events/user")
    print(f"  Jitter applied: ±15s to simulate real-world out-of-order delivery")
    print(f"""
  Why out-of-order events matter:
    Kafka guarantees order within a partition but NOT across partitions.
    Even in a single partition, events arrive in the order producers sent them
    (ingestion time), which is NOT necessarily sorted by event time.
    A GPS event from a mobile device buffered for 5 minutes has an event time
    that is 5 minutes earlier than its Kafka offset timestamp.
    Processing time == event time only if you produce events synchronously
    with no buffering — never true in practice.
""")
    return events


def phase2_tumbling(consumed):
    """Demonstrate tumbling window aggregation."""
    section("Phase 2: Tumbling Window — Clicks per User per Minute")

    from collections import Counter
    tumbling = tumbling_aggregate(consumed)
    windows = sorted(set(wkey for _, wkey in tumbling.keys()))
    print(f"\n  Tumbling 1-minute windows (event time):\n")
    for wkey in windows:
        window_data = {u: c for (u, w), c in tumbling.items() if w == wkey}
        top = Counter(window_data).most_common(3)
        total = sum(window_data.values())
        print(f"    [{format_ts(wkey)}─{format_ts(wkey+WINDOW_SEC)}): "
              f"{total:3d} clicks | top: " +
              ", ".join(f"{u}={c}" for u, c in top))

    print(f"""
  Tumbling window properties:
    • Non-overlapping: each event belongs to exactly ONE window
    • Fixed size: every window is exactly {WINDOW_SEC}s
    • Epoch-aligned: windows start at 00:00, 01:00, 02:00 UTC
    • Use case: "orders per hour", "daily active users", billing periods

  Key formula:
    window_start = (event_ts // window_size) * window_size
    Snaps any timestamp to the start of its containing bucket.

  Memory cost:
    One accumulator per (user, window) pair — bounded once the window closes.
    With 20 users and 5 one-minute windows → 100 accumulators max.
""")


def phase3_sliding(consumed, now):
    """Demonstrate sliding window with explicit window advancement."""
    section("Phase 3: Sliding Window — Clicks in Last 60s (slide=30s)")
    from collections import Counter

    ref_time = now
    sliding = sliding_window_counts(consumed, ref_time, WINDOW_SEC)
    top_sliding = Counter(sliding).most_common(5)
    print(f"\n  Top 5 users by clicks in [{format_ts(ref_time - WINDOW_SEC)}─{format_ts(ref_time)}):\n")
    for rank, (user, count) in enumerate(top_sliding, 1):
        bar = "#" * min(count, 40)
        print(f"    #{rank}: {user} — {count:2d} clicks  {bar}")

    # Advance the window by 30 seconds (slide step), add 50 new events
    ref_time_30s = now + 30
    new_events = []
    for _ in range(50):
        user_id = f"user-{random.randint(1, NUM_USERS):03d}"
        ts = now + random.randint(1, 30)   # these events have event time in [now, now+30]
        new_events.append({"user_id": user_id, "ts": ts, "url": "/new",
                            "event_id": str(uuid.uuid4())})
    all_events = consumed + new_events

    sliding_30 = sliding_window_counts(all_events, ref_time_30s, WINDOW_SEC)
    top_30 = Counter(sliding_30).most_common(5)
    print(f"\n  Window slides +30s. 50 new events arrive.")
    print(f"  Top 5 users in [{format_ts(ref_time_30s - WINDOW_SEC)}─{format_ts(ref_time_30s)}):\n")
    for rank, (user, count) in enumerate(top_30, 1):
        bar = "#" * min(count, 40)
        print(f"    #{rank}: {user} — {count:2d} clicks  {bar}")

    # Show how rank changes reveal the "rate-of-change" signal
    old_ranks = {u: r for r, (u, _) in enumerate(top_sliding, 1)}
    print(f"\n  Rank changes (sliding window detects momentum):")
    for rank, (user, count) in enumerate(top_30, 1):
        old_rank = old_ranks.get(user, "new")
        change = f"↑ was #{old_rank}" if isinstance(old_rank, int) and rank < old_rank else \
                 f"↓ was #{old_rank}" if isinstance(old_rank, int) and rank > old_rank else \
                 "(new entry)" if old_rank == "new" else "(unchanged)"
        print(f"    #{rank} {user} {change}")

    print(f"""
  Sliding window vs tumbling:
    Size=60s, Slide=30s → 50% overlap. Each event is counted in 2 windows.
    Size=60s, Slide=1s  → 98% overlap. Each event counted in 60 windows.
    Size=60s, Slide=60s → 0% overlap. Equivalent to tumbling window.

  Cost: O(window_size / slide_step) windows per event.
    Slide=1s on a 60s window → 60x more work than tumbling.

  Efficient implementation: monotone deque (double-ended queue).
    Add new events to the tail. Remove events older than (now - window_size) from head.
    O(1) amortized per event vs O(window_size) for naive re-scan.

  Use cases: moving averages, real-time leaderboards, rate-of-change detection.
""")


def phase4_session(consumed):
    """Demonstrate session window grouping."""
    section("Phase 4: Session Windows — User Sessions by Inactivity Gap")

    SESSION_GAP = 30  # seconds

    # Group events by user
    by_user = defaultdict(list)
    for ev in consumed:
        by_user[ev["user_id"]].append(ev)

    print(f"\n  Session gap: {SESSION_GAP}s (new session if user inactive >{SESSION_GAP}s)\n")

    # Show session analysis for a sample of users
    sample_users = sorted(by_user.keys())[:5]
    session_stats = []
    for user in sample_users:
        sessions = session_windows(by_user[user], gap_seconds=SESSION_GAP)
        session_stats.append((user, sessions))
        print(f"  {user}: {len(by_user[user])} events → {len(sessions)} session(s)")
        for i, sess in enumerate(sessions, 1):
            duration = sess[-1]["ts"] - sess[0]["ts"]
            print(f"    Session {i}: {len(sess)} events, "
                  f"{format_ts(sess[0]['ts'])}─{format_ts(sess[-1]['ts'])} "
                  f"(duration {duration}s)")

    # Show a visual timeline for the user with the most sessions
    most_sessions_user, most_sessions = max(session_stats, key=lambda x: len(x[1]))
    if len(most_sessions) > 1:
        print(f"\n  Timeline for {most_sessions_user} ({len(most_sessions)} sessions):\n")
        all_user_ts = sorted(e["ts"] for e in by_user[most_sessions_user])
        min_ts, max_ts = all_user_ts[0], all_user_ts[-1]
        span = max(max_ts - min_ts, 1)
        width = 50

        for i, sess in enumerate(most_sessions, 1):
            sess_start = sess[0]["ts"]
            sess_end   = sess[-1]["ts"]
            left  = int((sess_start - min_ts) / span * width)
            right = int((sess_end   - min_ts) / span * width)
            bar = " " * left + "[" + "─" * max(right - left - 1, 0) + "]" + " " * (width - right)
            print(f"    S{i}: {bar}  ({len(sess)} events)")
        print(f"         {format_ts(min_ts)}{' ' * (width - 8)}{format_ts(max_ts)}")

    print(f"""
  Session window properties:
    • Variable size: determined by user activity, not a fixed time interval
    • Gap-based: a new session begins after {SESSION_GAP}s of inactivity
    • No upfront size: you don't know when a session will end
    • State: must keep the last event timestamp per user — O(num_active_users)

  Key difference from tumbling/sliding:
    Tumbling/sliding are time-driven (close at fixed boundaries).
    Session windows are event-driven (close when the user goes quiet).

  Failure mode — state explosion:
    A bot generating fake user IDs creates one open session per ID, forever.
    Mitigation: configure maximum session duration (e.g., 30 minutes).
    Monitor state backend size (RocksDB in Flink) as an operational metric.

  Use cases: user journey analysis, funnel attribution, engagement scoring.
""")


def phase5_watermark_stepping():
    """
    Walk through watermark advancement step-by-step with a small out-of-order stream.
    This is the most important concept for FAANG interviews — show the exact mechanics.
    """
    section("Phase 5: Watermark Mechanics — Step-by-Step Out-of-Order Stream")

    # Simulate a small stream arriving at the processor in this order
    # (NOT sorted by event time — that's the whole point)
    BASE = 1000  # abstract base timestamp for readability
    stream = [
        # (arrival_order, event_time, user)
        (1,  BASE + 10,  "A"),
        (2,  BASE + 5,   "B"),  # out of order: event time earlier than previous
        (3,  BASE + 20,  "A"),
        (4,  BASE + 8,   "C"),  # another late arrival
        (5,  BASE + 35,  "B"),
        (6,  BASE + 12,  "A"),  # late: arrives after event at t=35
        (7,  BASE + 50,  "C"),
        (8,  BASE + 62,  "A"),  # this will trigger the [0,60) window to close
        (9,  BASE + 15,  "B"),  # VERY late: arrives after window [0,60) closed
    ]

    ALLOWED_LATENESS = 10
    WINDOW_SIZE = 60
    watermark = -float("inf")
    window_counts = defaultdict(lambda: defaultdict(int))  # {window_start: {user: count}}
    closed_windows = set()
    side_output = []

    print(f"\n  Stream config: window_size={WINDOW_SIZE}s, allowed_lateness={ALLOWED_LATENESS}s")
    print(f"  Watermark formula: max(event_time_seen) - {ALLOWED_LATENESS}\n")
    print(f"  {'Arr':>4}  {'Evt Time':>10}  {'User':>5}  {'Watermark':>12}  Action")
    print(f"  {'-'*4}  {'-'*10}  {'-'*5}  {'-'*12}  {'-'*40}")

    for arrival, evt_time, user in stream:
        relative_evt = evt_time - BASE
        wstart = (evt_time // WINDOW_SIZE) * WINDOW_SIZE
        wend   = wstart + WINDOW_SIZE

        new_watermark = max(watermark, evt_time - ALLOWED_LATENESS)

        # Check if the window for this event is already closed
        action = ""
        if wstart in closed_windows:
            side_output.append((arrival, evt_time, user))
            action = f"→ SIDE OUTPUT (window [{relative_evt // WINDOW_SIZE * WINDOW_SIZE}, {relative_evt // WINDOW_SIZE * WINDOW_SIZE + WINDOW_SIZE}) closed)"
        else:
            window_counts[wstart][user] += 1
            action = f"→ ACCEPTED into window [{wstart - BASE}, {wend - BASE})"

        watermark_rel = new_watermark - BASE if new_watermark != -float("inf") else "—"
        print(f"  {arrival:>4}  t=+{relative_evt:<8}  {user:>5}  wm=+{watermark_rel!s:<10}  {action}")

        # Check if watermark advance closes any windows
        if new_watermark > watermark:
            for ws in sorted(window_counts.keys()):
                we = ws + WINDOW_SIZE
                if we <= new_watermark and ws not in closed_windows:
                    closed_windows.add(ws)
                    total = sum(window_counts[ws].values())
                    print(f"         *** WINDOW [{ws - BASE}, {we - BASE}) CLOSED — "
                          f"emitting result: {total} events {dict(window_counts[ws])}")
            watermark = new_watermark

    print(f"\n  Final state:")
    print(f"    Watermark: t=+{watermark - BASE}")
    print(f"    Closed windows: {sorted(ws - BASE for ws in closed_windows)}")
    if side_output:
        print(f"    Side output ({len(side_output)} events — too late, routed for reconciliation):")
        for arr, evt, usr in side_output:
            print(f"      arrival={arr}, event_time=+{evt - BASE}, user={usr}")

    print(f"""
  Key observations from the trace above:
    1. The watermark advances monotonically (never goes backward).
    2. A window closes only when watermark >= window_end, NOT when wall clock advances.
    3. Events arriving after their window closes → side output (not silently dropped).
    4. Allowed lateness = {ALLOWED_LATENESS}s: event at t=+12 arriving at step 6 (after wm=+25)
       is ACCEPTED because its window [0,60) hasn't closed yet.
    5. Event at t=+15 at step 9 → SIDE OUTPUT because window [0,60) is already closed.

  Watermark tuning:
    Too aggressive (small allowed_lateness):
      → Windows close fast, low memory, but late events dropped → undercounting
    Too conservative (large allowed_lateness):
      → Windows stay open longer, high memory, results delayed
    Best practice: measure p99 event lateness from your actual Kafka consumers,
    set allowed_lateness = p99_lateness + buffer.

  Idle partition problem:
    Watermark = MIN across all Kafka partitions.
    A single idle partition (no events) freezes the watermark for all windows.
    Fix: declare partitions idle after N seconds, advance watermark via wall clock.
""")


def phase6_late_event_strategies(now):
    """Three strategies for handling late events."""
    section("Phase 6: Late Event Handling — Three Strategies")

    late_ts = now - 90   # event timestamped 90 seconds ago
    late_ev = {
        "event_id": str(uuid.uuid4()),
        "user_id":  "user-001",
        "url":      "/checkout",
        "ts":       late_ts,
    }

    print(f"""
  Late event:
    Event time (ts):     {format_ts(late_ts)}  (90s ago)
    Processing time:     {format_ts(now)}
    Lateness:            90 seconds

  Why events arrive late:
    • Mobile device offline — events buffered locally for hours
    • Network delay / TCP retry storm
    • Kafka consumer lag (slow consumer falls behind the write head)
    • Clock skew between service and Kafka broker
""")

    producer = get_producer()
    producer.send(TOPIC, key=late_ev["user_id"], value=late_ev)
    producer.flush()
    producer.close()

    # Strategy 1: Hard cutoff
    cutoff = now - WINDOW_SEC
    print(f"  Strategy 1 — Hard cutoff (reject anything older than {WINDOW_SEC}s):")
    if late_ev["ts"] < cutoff:
        print(f"    Event ts={format_ts(late_ev['ts'])} < cutoff={format_ts(cutoff)}")
        print(f"    → DROPPED. Simple to implement, but causes systematic undercounting.")
        print(f"    Risk: if your p99 lateness exceeds the cutoff, you lose real data.")
    else:
        print(f"    → ACCEPTED (within cutoff)")

    # Strategy 2: Watermark with allowed lateness
    allowed_lateness = 30
    watermark = now - allowed_lateness
    window_start = tumbling_window_key(late_ev["ts"], WINDOW_SEC)
    window_end   = window_start + WINDOW_SEC

    print(f"\n  Strategy 2 — Watermark (allowed_lateness={allowed_lateness}s):")
    print(f"    Current watermark:  {format_ts(watermark)}")
    print(f"    Event window:       [{format_ts(window_start)}─{format_ts(window_end)})")
    if window_end > watermark:
        print(f"    window_end={format_ts(window_end)} > watermark={format_ts(watermark)}")
        print(f"    → ACCEPTED: window is still open, recompute aggregate.")
    else:
        print(f"    window_end={format_ts(window_end)} <= watermark={format_ts(watermark)}")
        print(f"    → DROPPED by watermark (90s lateness > 30s allowed).")
        print(f"    Route to side output topic 'late-events'.")

    # Strategy 3: Side output + reconciliation
    print(f"\n  Strategy 3 — Side output + periodic reconciliation:")
    print(f"    Late events → 'late-events' Kafka topic (not silently dropped)")
    print(f"    Hourly batch job reads late-events, recomputes affected windows")
    print(f"    Corrected results overwrite the streaming output")
    print(f"    → Eventual accuracy with bounded latency impact")

    print(f"""
  Choosing a strategy:
    Hard cutoff:    simplest, lowest memory. Acceptable when p99 lateness << window.
    Watermark:      standard for Flink/Spark Streaming. Tune allowed_lateness to
                    your data's lateness distribution (e.g., p99 + 10s buffer).
    Side output:    best for compliance-critical use cases (billing, fraud) where
                    every event must eventually be counted. Adds operational complexity.

  Exactly-once guarantee (for FAANG interviews):
    Requires two things working together:
    1. Idempotent sinks: writing the same record twice has no effect.
       (Kafka transactions, UPSERT into a DB with a dedup key)
    2. Atomic checkpoint: Kafka consumer offset + output committed together.
       Flink two-phase commit: pre-commit output, commit offset, then finalize.
    Without both, you get at-least-once (duplicates) or at-most-once (data loss).
""")


def phase7_lambda_vs_kappa():
    """Lambda vs Kappa architecture comparison."""
    section("Phase 7: Architecture Patterns — Lambda vs Kappa")

    print("""
  LAMBDA ARCHITECTURE (Nathan Marz, 2011):
  ┌─────────────────────────────────────────────────────────┐
  │  Raw data ─┬─► Batch layer (Hadoop/Spark)               │
  │            │   Reprocesses ALL data daily                │
  │            │   Produces accurate "batch views"           │
  │            │                                             │
  │            └─► Speed layer (Kafka Streams / Flink)       │
  │                Processes recent data, low latency         │
  │                Produces approximate "realtime views"      │
  │                                                          │
  │  Serving layer merges batch view + realtime view         │
  └─────────────────────────────────────────────────────────┘

  Pro:
    + Batch layer corrects errors from speed layer (handles late data)
    + Highest throughput (Spark batch is faster than continuous streaming)
    + Battle-tested pattern for very large data (10TB+ dimension joins)
  Con:
    - Two codebases (batch + streaming) diverge over time — subtle bugs
    - Merge layer is complex (which view wins? how to blend counts?)
    - Operational overhead: two systems, two deployment pipelines

  KAPPA ARCHITECTURE (Jay Kreps, 2014):
  ┌─────────────────────────────────────────────────────────┐
  │  Raw data ──► Kafka (long retention) ──► Stream job     │
  │                        │                                 │
  │                        └─► New consumer group           │
  │                            (replay from offset 0)        │
  │                            for reprocessing              │
  └─────────────────────────────────────────────────────────┘

  Pro:
    + Single codebase (streaming only) — no divergence
    + Reprocessing = new consumer group replaying Kafka from offset 0
    + Simpler operational model
    + Kafka's long retention (168h default; ∞ with log compaction) is the
      "historical record" — no separate data lake needed for recent data
  Con:
    - Reprocessing is slower than Spark batch for very large historical data
    - Requires Kafka to retain data long enough to replay (cost: disk)
    - Joins with large static datasets (10TB lookup tables) are harder

  Modern default: Kappa when:
    • Stream processor is mature (Flink, Kafka Streams)
    • Kafka retention covers the required historical depth
    • Historical reprocessing windows are well-defined (<7 days typical)

  Lambda still wins when:
    • Joining streaming events with 10TB+ static dimension tables
    • Batch SQL is simpler to validate/audit than streaming logic
    • p99 data lateness is extreme (hours) — batch layer absorbs it cleanly

  ──────────────────────────────────────────────────────────

  Framework comparison (for interviews):

  Apache Flink:
    Processing model:  true event-by-event streaming
    Latency:           milliseconds
    State:             RocksDB (spills to disk), checkpoints to S3/HDFS
    Exactly-once:      yes (two-phase commit to Kafka sinks)
    Late data:         watermarks + side outputs + allowed lateness
    Best for:          fraud detection, real-time ML feature stores

  Spark Structured Streaming:
    Processing model:  micro-batch (configurable trigger interval)
    Latency:           seconds (continuous mode: ~1ms, experimental)
    State:             in-memory + checkpoint to HDFS/S3
    Exactly-once:      yes (idempotent writes + WAL)
    Late data:         watermarks (simpler API than Flink)
    Best for:          teams with Spark expertise, SQL-heavy pipelines

  Kafka Streams:
    Processing model:  per-record streaming (library, not a cluster)
    Latency:           milliseconds
    State:             RocksDB local, changelog topic in Kafka for recovery
    Exactly-once:      yes (Kafka transactions)
    Late data:         grace period on windowed stores
    Best for:          microservice event processing, tight Kafka integration

  ksqlDB:
    Processing model:  SQL over Kafka Streams
    Latency:           milliseconds
    Best for:          rapid prototyping, simple transformations in SQL
""")


def main():
    section("STREAM PROCESSING LAB — WINDOWING, WATERMARKS, SESSION WINDOWS")
    print("""
  Stream processing concepts at a glance:

  Batch processing:
    Collect all data → process once → output results
    Latency: minutes to hours (Hadoop MapReduce, Spark batch)

  Stream processing:
    Process data as it arrives, continuously
    Latency: milliseconds (Flink, Kafka Streams) to seconds (Spark Streaming)

  Window types:
  ┌──────────────────────────────────────────────────────────┐
  │                                                          │
  │  TUMBLING (non-overlapping, fixed-size):                 │
  │  [00:00─01:00)[01:00─02:00)[02:00─03:00)                 │
  │  Each event → exactly one window.                        │
  │                                                          │
  │  SLIDING (overlapping, fixed-size, fixed-slide):         │
  │  [00:00─01:00)                                           │
  │       [00:30─01:30)                                      │
  │            [01:00─02:00)                                 │
  │  Each event → O(size/slide) windows.                     │
  │                                                          │
  │  SESSION (variable size, gap-based):                     │
  │  [─activity─][gap≥30s][─new session─]                    │
  │  Each session → one window per activity burst.           │
  │                                                          │
  │  GLOBAL (single unbounded window):                       │
  │  [──────────────────────────────────►                    │
  │  One window for all time — trigger manually.             │
  │                                                          │
  └──────────────────────────────────────────────────────────┘
""")

    # Verify connectivity
    try:
        admin = KafkaAdminClient(bootstrap_servers=BOOTSTRAP)
        admin.list_topics()
        admin.close()
        print("  Connected to Kafka at", BOOTSTRAP)
    except Exception as e:
        print(f"  ERROR: Cannot connect to Kafka: {e}")
        print("  Run: docker compose up -d")
        print("  Then wait ~30s for Kafka to be ready.")
        return

    create_topic(TOPIC)

    now = int(time.time())
    random.seed(42)

    events = phase1_produce(now)

    # Consume all events for in-process analysis
    consumer = KafkaConsumer(
        TOPIC,
        bootstrap_servers=BOOTSTRAP,
        group_id=f"stream-lab-{uuid.uuid4().hex[:8]}",
        auto_offset_reset="earliest",
        consumer_timeout_ms=5000,
        value_deserializer=lambda v: json.loads(v.decode()),
    )
    consumed = [msg.value for msg in consumer]
    consumer.close()

    phase2_tumbling(consumed)
    phase3_sliding(consumed, now)
    phase4_session(consumed)
    phase5_watermark_stepping()
    phase6_late_event_strategies(now)
    phase7_lambda_vs_kappa()

    section("Summary — Key Interview Points")
    print("""
  1. Always use EVENT TIME, not processing time.
     Processing time = when the stream processor sees the event.
     Event time = when the event actually happened (in the data payload).
     A mobile event buffered for 1 hour has an event time 1 hour in the past.
     Using processing time puts it in the wrong window.

  2. Watermark = max(event_time_seen) - allowed_lateness.
     It is the processor's promise: "I've seen all events with ts < watermark."
     A window closes when watermark >= window_end, not when wall clock advances.
     One idle Kafka partition freezes the watermark for ALL windows.

  3. Window type selection:
     Tumbling → aggregations with clear time buckets (hourly, daily)
     Sliding  → moving averages, rate-of-change, real-time leaderboards
     Session  → user journeys, funnel analysis, engagement scoring
     Global   → total aggregations with explicit triggers (rare)

  4. Exactly-once = idempotent sinks + atomic checkpoint of offsets + output.
     Flink: two-phase commit to Kafka sinks.
     Kafka Streams: Kafka transactions (produces + offset commits atomically).

  5. Kappa vs Lambda:
     Kappa: stream-only, single codebase, reprocess by replaying Kafka.
     Lambda: batch + stream, two codebases, batch corrects stream.
     Modern default is Kappa; Lambda for joins with massive static datasets.

  6. State management:
     Flink: RocksDB state backend (disk-spillable, S3 checkpoints).
     Kafka Streams: RocksDB + Kafka changelog topic for recovery.
     Always configure state TTL to prevent unbounded growth.
""")


if __name__ == "__main__":
    main()
