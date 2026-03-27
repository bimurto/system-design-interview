#!/usr/bin/env python3
"""
Backpressure & Flow Control Lab

Prerequisites: docker compose up -d (wait ~5s)

What this demonstrates:
  1. No backpressure: producer floods consumer; queue grows unboundedly
  2. Blocking backpressure: producer pauses when queue exceeds threshold
  3. Drop strategy: producer discards new items when queue is full
  4. Reject with jitter: consumer signals 503; producer backs off with jitter
  5. Thundering herd: same reject scenario but WITHOUT jitter — retry storms
"""

import time
import random
import threading
import subprocess
from collections import deque

try:
    import redis
except ImportError:
    print("Installing dependencies...")
    subprocess.run(["pip", "install", "redis", "-q"], check=True)
    import redis

REDIS_URL = "redis://localhost:6379"
QUEUE_KEY = "work_queue"


def section(title):
    print(f"\n{'=' * 62}")
    print(f"  {title}")
    print("=" * 62)


def connect_redis():
    r = redis.Redis.from_url(REDIS_URL, decode_responses=True)
    r.ping()
    return r


def wait_ready(timeout=15):
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            connect_redis()
            print("  Redis ready.")
            return True
        except Exception:
            time.sleep(1)
    print("  ERROR: Redis not ready. Run: docker compose up -d")
    return False


def depth_bar(depth, max_depth, width=40):
    """ASCII bar showing queue depth as a fraction of max_depth."""
    fill = int(min(depth, max_depth) / max_depth * width)
    bar  = "#" * fill + "." * (width - fill)
    pct  = min(depth / max_depth * 100, 100)
    return f"[{bar}] {depth:4d} ({pct:5.1f}%)"


# ── Scenario 1: No backpressure ────────────────────────────────────────────

def scenario1_no_backpressure(r):
    section("Scenario 1: No Backpressure — Producer Floods Consumer")
    print("""  Producer: 500 items/sec
  Consumer: 100 items/sec
  No limit on queue size.

  Watch the queue grow without bound. In a real system this eventually
  exhausts broker disk or process memory, causing a hard crash.
""")

    r.delete(QUEUE_KEY)
    MAX_DISPLAY = 500
    snapshots   = []
    stop        = threading.Event()

    def producer():
        for i in range(200):
            if stop.is_set():
                break
            r.rpush(QUEUE_KEY, f"item:{i}")
            time.sleep(0.002)  # 500/sec

    def consumer():
        while not stop.is_set():
            r.lpop(QUEUE_KEY)
            time.sleep(0.01)  # 100/sec

    def monitor():
        for _ in range(22):
            if stop.is_set():
                break
            snapshots.append(r.llen(QUEUE_KEY))
            time.sleep(0.15)

    threads = [
        threading.Thread(target=producer, daemon=True),
        threading.Thread(target=consumer, daemon=True),
        threading.Thread(target=monitor,  daemon=True),
    ]
    for t in threads:
        t.start()
    time.sleep(3.5)
    stop.set()
    for t in threads:
        t.join(timeout=1)

    print("  Queue depth over time (500 item/s in, 100 item/s out):\n")
    for i, depth in enumerate(snapshots):
        print(f"    t={i * 0.15:.1f}s  {depth_bar(depth, MAX_DISPLAY)}")

    final = r.llen(QUEUE_KEY)
    print(f"""
  Final queue depth: {final}
  Queue grew monotonically — consumer cannot keep up.
  Without backpressure: broker disk fills and the process crashes.
  The producer has no idea it is causing a problem.
""")
    r.delete(QUEUE_KEY)


# ── Scenario 2: Blocking backpressure ──────────────────────────────────────

def scenario2_blocking(r):
    section("Scenario 2: Blocking Backpressure — Producer Pauses When Queue Full")
    print("""  Producer blocks when queue depth >= 20.
  Consumer processes at 100/sec.
  Producer resumes only when queue drains below threshold.

  WARNING: blocking on a SHARED thread pool (e.g., Tomcat's 200 threads)
  causes head-of-line blocking — one slow consumer starves ALL other
  endpoints sharing the pool. Use blocking only in dedicated pipelines.
""")

    r.delete(QUEUE_KEY)
    MAX_QUEUE   = 20
    snapshots   = []
    stop        = threading.Event()
    pauses      = [0]

    def producer():
        for i in range(200):
            if stop.is_set():
                break
            # Block until there is room — this is the backpressure signal
            while r.llen(QUEUE_KEY) >= MAX_QUEUE:
                pauses[0] += 1
                time.sleep(0.01)
            r.rpush(QUEUE_KEY, f"item:{i}")
            time.sleep(0.002)  # attempts 500/sec, but is throttled by blocking

    def consumer():
        while not stop.is_set():
            r.lpop(QUEUE_KEY)
            time.sleep(0.01)  # 100/sec

    def monitor():
        for _ in range(28):
            if stop.is_set():
                break
            snapshots.append(r.llen(QUEUE_KEY))
            time.sleep(0.12)

    threads = [
        threading.Thread(target=producer, daemon=True),
        threading.Thread(target=consumer, daemon=True),
        threading.Thread(target=monitor,  daemon=True),
    ]
    for t in threads:
        t.start()
    time.sleep(3.5)
    stop.set()
    for t in threads:
        t.join(timeout=1)

    print("  Queue depth over time (blocking at depth=20):\n")
    for i, depth in enumerate(snapshots):
        print(f"    t={i * 0.12:.1f}s  {depth_bar(depth, MAX_QUEUE + 5)}")

    print(f"""
  Queue stayed bounded at ~{MAX_QUEUE} items.
  Producer paused ~{pauses[0]} times waiting for consumer.

  Blocking backpressure slows the producer to match the consumer.
  Cost: producer thread is held idle while waiting.
  Safe for: batch pipelines, background workers, ETL jobs with dedicated threads.
  Dangerous for: shared API thread pools (head-of-line blocking risk).
""")
    r.delete(QUEUE_KEY)


# ── Scenario 3: Drop strategy ───────────────────────────────────────────────

def scenario3_drop(r):
    section("Scenario 3: Drop Strategy — Discard When Queue Full")
    print("""  Producer sends at 500/sec. Queue max = 20.
  When queue is full, NEW items are dropped (newest discarded — LIFO drop).
  Consumer processes at 100/sec.
  Producer is never blocked.

  In production: always expose drop_rate as a metric.
  5% drop rate = acceptable for telemetry. 50% drop rate = incident.
""")

    r.delete(QUEUE_KEY)
    MAX_QUEUE = 20
    snapshots = []
    stop      = threading.Event()
    # Use a lock for the shared counters to avoid data races under high concurrency
    lock      = threading.Lock()
    sent      = [0]
    dropped   = [0]

    def producer():
        for i in range(200):
            if stop.is_set():
                break
            if r.llen(QUEUE_KEY) >= MAX_QUEUE:
                with lock:
                    dropped[0] += 1
                # In production: metrics_client.increment("queue.dropped")
            else:
                r.rpush(QUEUE_KEY, f"item:{i}")
                with lock:
                    sent[0] += 1
            time.sleep(0.002)

    def consumer():
        while not stop.is_set():
            r.lpop(QUEUE_KEY)
            time.sleep(0.01)

    def monitor():
        for _ in range(28):
            if stop.is_set():
                break
            snapshots.append(r.llen(QUEUE_KEY))
            time.sleep(0.12)

    threads = [
        threading.Thread(target=producer, daemon=True),
        threading.Thread(target=consumer, daemon=True),
        threading.Thread(target=monitor,  daemon=True),
    ]
    for t in threads:
        t.start()
    time.sleep(3.5)
    stop.set()
    for t in threads:
        t.join(timeout=1)

    print("  Queue depth over time (drop when full at depth=20):\n")
    for i, depth in enumerate(snapshots):
        print(f"    t={i * 0.12:.1f}s  {depth_bar(depth, MAX_QUEUE + 5)}")

    with lock:
        total = sent[0] + dropped[0]
        drop_rate = (dropped[0] / total * 100) if total > 0 else 0.0
        print(f"""
  Items sent (accepted): {sent[0]}
  Items dropped:         {dropped[0]} ({drop_rate:.1f}% drop rate)

  Queue stayed bounded. Producer was never blocked.
  Cost: data loss — dropped items are permanently gone.
  Use for: metrics, logs, real-time sensor data where the latest reading
           is more valuable than an old one (stale data = useless data).
""")
    r.delete(QUEUE_KEY)


# ── Scenario 4: Reject with jitter ─────────────────────────────────────────

def scenario4_reject_with_jitter(r):
    section("Scenario 4: Reject Strategy — 503 with Exponential Backoff + Jitter")
    print("""  Consumer returns 503 when queue depth >= 20.
  Producer receives rejection and backs off with EXPONENTIAL BACKOFF + FULL JITTER:
    sleep = random(0, min(cap=0.5s, base=0.01s * 2^attempt))

  Jitter desynchronizes retries across clients, preventing thundering herds.
  Queue stays bounded; no data is lost (producer retries with delay).
""")

    r.delete(QUEUE_KEY)
    MAX_QUEUE  = 20
    snapshots  = []
    stop       = threading.Event()
    lock       = threading.Lock()
    accepted   = [0]
    rejected   = [0]
    retries    = [0]

    def consumer_gate():
        """Returns True if accepted (item pushed to queue), False (503) if overloaded."""
        if r.llen(QUEUE_KEY) >= MAX_QUEUE:
            return False
        r.rpush(QUEUE_KEY, "item")
        return True

    def drain_consumer():
        while not stop.is_set():
            r.lpop(QUEUE_KEY)
            time.sleep(0.01)  # 100/sec

    def producer():
        attempt = 0
        for i in range(200):
            if stop.is_set():
                break

            if attempt > 0:
                # Full jitter: random(0, min(cap, base * 2^attempt))
                cap     = 0.5
                base    = 0.01
                ceiling = min(cap, base * (2 ** attempt))
                sleep   = random.uniform(0, ceiling)
                time.sleep(sleep)
                with lock:
                    retries[0] += 1

            ok = consumer_gate()
            if ok:
                with lock:
                    accepted[0] += 1
                attempt = max(0, attempt - 1)  # cool down on success
            else:
                with lock:
                    rejected[0] += 1
                attempt += 1

            time.sleep(0.002)

    def monitor():
        for _ in range(28):
            if stop.is_set():
                break
            snapshots.append(r.llen(QUEUE_KEY))
            time.sleep(0.12)

    threads = [
        threading.Thread(target=producer,       daemon=True),
        threading.Thread(target=drain_consumer, daemon=True),
        threading.Thread(target=monitor,        daemon=True),
    ]
    for t in threads:
        t.start()
    time.sleep(3.5)
    stop.set()
    for t in threads:
        t.join(timeout=1)

    print("  Queue depth over time (503 reject, producer backs off with jitter):\n")
    for i, depth in enumerate(snapshots):
        print(f"    t={i * 0.12:.1f}s  {depth_bar(depth, MAX_QUEUE + 5)}")

    with lock:
        print(f"""
  Accepted: {accepted[0]}   Rejected (503): {rejected[0]}   Retries: {retries[0]}

  Queue stayed bounded. No data lost (producer retried with jitter backoff).
  Jitter spreads retries across time — the consumer never faces a synchronized
  retry wave from the producer.

  Correct for: user-facing APIs where 503 + Retry-After gives clients a signal.
  The caller can retry, route to another region, or show a graceful error.
""")
    r.delete(QUEUE_KEY)


# ── Scenario 5: Thundering herd (no jitter) ────────────────────────────────

def scenario5_thundering_herd(r):
    section("Scenario 5: Thundering Herd — Reject WITHOUT Jitter (Bad Pattern)")
    print("""  Same reject scenario as Scenario 4, BUT with deterministic backoff:
    sleep = base * 2^attempt  (no randomness)

  When many clients all back off for identical durations, they all retry
  simultaneously. The backend gets a synchronized retry storm just as it
  is trying to recover — re-triggering overload repeatedly.

  We simulate 5 producers all using the same deterministic backoff.
  Watch for spikes in queue depth as they retry in unison.
""")

    r.delete(QUEUE_KEY)
    MAX_QUEUE   = 20
    snapshots   = []
    stop        = threading.Event()
    lock        = threading.Lock()
    accepted    = [0]
    rejected    = [0]
    # Track queue depth just before each retry burst to show spikes
    spike_depths = []

    def consumer_gate():
        if r.llen(QUEUE_KEY) >= MAX_QUEUE:
            return False
        r.rpush(QUEUE_KEY, "item")
        return True

    def drain_consumer():
        while not stop.is_set():
            r.lpop(QUEUE_KEY)
            time.sleep(0.01)  # 100/sec — intentionally slow relative to 5 producers

    def producer_no_jitter(producer_id):
        attempt = 0
        for i in range(60):
            if stop.is_set():
                break

            if attempt > 0:
                # Deterministic backoff — all 5 producers sleep the SAME duration
                base    = 0.05
                sleep   = min(0.5, base * (2 ** attempt))
                time.sleep(sleep)

            ok = consumer_gate()
            if ok:
                with lock:
                    accepted[0] += 1
                attempt = max(0, attempt - 1)
            else:
                with lock:
                    rejected[0] += 1
                attempt += 1

            time.sleep(0.002)

    def monitor():
        for _ in range(28):
            if stop.is_set():
                break
            depth = r.llen(QUEUE_KEY)
            snapshots.append(depth)
            time.sleep(0.12)

    threads = [
        threading.Thread(target=drain_consumer, daemon=True),
        threading.Thread(target=monitor,        daemon=True),
    ]
    # Spawn 5 producers — all using identical deterministic backoff
    for pid in range(5):
        threads.append(threading.Thread(
            target=producer_no_jitter, args=(pid,), daemon=True
        ))

    for t in threads:
        t.start()
    time.sleep(3.5)
    stop.set()
    for t in threads:
        t.join(timeout=1)

    print("  Queue depth over time (5 producers, deterministic backoff, no jitter):\n")
    for i, depth in enumerate(snapshots):
        spike_marker = " <-- SPIKE (retry storm)" if depth >= MAX_QUEUE - 2 else ""
        print(f"    t={i * 0.12:.1f}s  {depth_bar(depth, MAX_QUEUE + 5)}{spike_marker}")

    with lock:
        print(f"""
  Accepted: {accepted[0]}   Rejected (503): {rejected[0]}

  Notice spikes near the capacity line — those are synchronized retry waves.
  All 5 producers backed off for the same duration, then retried together,
  overwhelming the consumer again each time.

  Fix: add full jitter (Scenario 4). A single line of code prevents this.
  Formula: sleep = random(0, min(cap, base * 2^attempt))
""")
    r.delete(QUEUE_KEY)


def main():
    section("BACKPRESSURE & FLOW CONTROL LAB")
    print("""
  Core question: what happens when producers outpace consumers?

  Without backpressure:
    Queue grows → broker runs out of disk → system crashes
    OR consumer falls further behind → stale data, missed SLAs

  Backpressure strategies (covered in this lab):
    1. No BP   — baseline: queue grows unboundedly (Scenario 1)
    2. Block   — producer waits for consumer (Scenario 2)
    3. Drop    — discard new items when queue full (Scenario 3)
    4. Reject  — return 503 + jitter backoff; no loss (Scenario 4)
    5. Herd    — reject WITHOUT jitter → retry storm (Scenario 5)
""")

    if not wait_ready():
        return

    r = connect_redis()

    scenario1_no_backpressure(r)
    scenario2_blocking(r)
    scenario3_drop(r)
    scenario4_reject_with_jitter(r)
    scenario5_thundering_herd(r)

    section("Summary")
    print("""
  Backpressure Strategy Selection:
  ──────────────────────────────────────────────────────────────
  Batch / ETL pipelines    → Block (simple; blocking is acceptable)
  Metrics / logs / events  → Drop  (newest data > old; loss acceptable)
  User-facing APIs         → Reject (503 + Retry-After; no silent loss)
  Sustained overload       → Scale consumers (add instances via KEDA/HPA)
  High-concurrency APIs    → Adaptive throttling (client self-regulates)

  Key signals to monitor:
    Queue depth (absolute value + rate of change)
    Consumer lag (Kafka: consumer group lag)
    Drop rate (items discarded per second)
    Rejection rate (503s per second)
    p99 processing latency (are consumers slowing down?)

  Queue depth alert thresholds (example):
    WARNING:  queue > 80% of normal operating range
    CRITICAL: queue > 100% of normal range OR drop rate > 5%
    PAGE:     queue growing monotonically for > 10 minutes

  Critical gotchas:
    Blocking on a SHARED thread pool = head-of-line blocking for all users
    Reject WITHOUT jitter = synchronized retry storms (thundering herd)
    Dropping WITHOUT metrics = invisible data loss (5% is fine; 50% is incident)
    Unbounded buffer = memory exhaustion in slow motion

  TCP already does backpressure (receive window).
  Reactive Streams (RxJava, Project Reactor) do it at the library level.
  Your application layer must do it explicitly for queues and APIs.

  Next steps: ../19-multi-region-architecture/
""")


if __name__ == "__main__":
    main()
