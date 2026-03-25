#!/usr/bin/env python3
"""
Consistency Models Lab — Redis Master + 2 Replicas

Prerequisites: docker compose up -d (wait ~10s for replicas to sync)

What this demonstrates:
  1. Eventual consistency: write to master, read from replica — may be stale
  2. Strong consistency with WAIT: block until N replicas have acknowledged
  3. Latency cost of stronger consistency
  4. Monotonic reads: once you see a value, you don't see an older one
"""

import time
import subprocess

try:
    import redis
except ImportError:
    print("Installing redis-py...")
    subprocess.run(["pip", "install", "redis", "-q"], check=True)
    import redis


def connect_all():
    master = redis.Redis(host="localhost", port=6379, decode_responses=True)
    replica1 = redis.Redis(host="localhost", port=6380, decode_responses=True)
    replica2 = redis.Redis(host="localhost", port=6381, decode_responses=True)
    return master, replica1, replica2


def section(title):
    print(f"\n{'=' * 60}")
    print(f"  {title}")
    print("=" * 60)


def time_op(label, fn):
    start = time.perf_counter()
    result = fn()
    elapsed = (time.perf_counter() - start) * 1000
    print(f"  {label:<45} {elapsed:6.2f} ms  → {result}")
    return result, elapsed


def main():
    section("CONSISTENCY MODELS LAB")
    print("""
  Consistency spectrum (strongest → weakest):
    Linearizability  — feels like one machine; every read sees latest write
    Sequential       — all nodes see ops in same order (not necessarily real-time)
    Causal           — causally related ops appear in order
    Eventual         — all nodes converge eventually; no ordering guarantee

  This lab uses Redis replication to show the practical difference.
""")

    master, replica1, replica2 = connect_all()

    try:
        master.ping()
    except Exception as e:
        print(f"  ERROR: Cannot connect to Redis master: {e}")
        print("  Run: docker compose up -d")
        return

    # ── Phase 1: Eventual Consistency ─────────────────────────────
    section("Phase 1: Eventual Consistency (no WAIT)")

    print("  Writing 'counter=1' to master, then reading from replica1 immediately...")
    master.set("counter", 1)
    time.sleep(0.001)  # 1ms — replication may not have completed

    master_val = master.get("counter")
    replica_val = replica1.get("counter")

    print(f"  Master  sees: counter = {master_val}")
    print(f"  Replica sees: counter = {replica_val}")

    if replica_val != master_val:
        print("  STALE READ observed — replica hasn't caught up yet (eventual consistency)")
    else:
        print("  Replica already caught up (replication is fast on localhost)")

    time.sleep(0.05)  # 50ms — should be enough for local replication
    replica_val_after = replica1.get("counter")
    print(f"\n  After 50ms, replica sees: counter = {replica_val_after}")
    print("  Eventual consistency: both nodes CONVERGE to the same value")

    # ── Phase 2: Strong Consistency with WAIT ─────────────────────
    section("Phase 2: Strong Consistency using WAIT")

    print("""  Redis WAIT command: blocks until N replicas have acknowledged
  all pending writes. This gives read-your-writes guarantee.

  Syntax: WAIT <num_replicas> <timeout_ms>
    Returns the number of replicas that acknowledged.
    timeout_ms=0 means wait forever.
""")

    timings = {}

    print("  --- Eventual consistency (no WAIT) ---")
    runs = 10
    total = 0
    for i in range(runs):
        start = time.perf_counter()
        master.set(f"key_eventual_{i}", i)
        # read immediately, no wait
        _ = master.get(f"key_eventual_{i}")
        total += (time.perf_counter() - start) * 1000
    timings["eventual"] = total / runs
    print(f"  Average write+read latency (no WAIT): {timings['eventual']:.2f} ms")

    print("\n  --- Strong consistency (WAIT 1 replica) ---")
    total = 0
    for i in range(runs):
        start = time.perf_counter()
        master.set(f"key_strong1_{i}", i)
        acked = master.wait(1, 100)  # wait for 1 replica, 100ms timeout
        _ = replica1.get(f"key_strong1_{i}")
        total += (time.perf_counter() - start) * 1000
    timings["wait_1"] = total / runs
    print(f"  Average write+read latency (WAIT 1):  {timings['wait_1']:.2f} ms")
    print(f"  Replicas that acked: {acked}")

    print("\n  --- Strong consistency (WAIT 2 replicas) ---")
    total = 0
    for i in range(runs):
        start = time.perf_counter()
        master.set(f"key_strong2_{i}", i)
        acked = master.wait(2, 100)  # wait for both replicas
        _ = replica1.get(f"key_strong2_{i}")
        total += (time.perf_counter() - start) * 1000
    timings["wait_2"] = total / runs
    print(f"  Average write+read latency (WAIT 2):  {timings['wait_2']:.2f} ms")
    print(f"  Replicas that acked: {acked}")

    print(f"""
  Latency comparison:
    No WAIT (eventual):  {timings['eventual']:.2f} ms
    WAIT 1 replica:      {timings['wait_1']:.2f} ms
    WAIT 2 replicas:     {timings['wait_2']:.2f} ms

  The overhead is the replication round-trip.
  On a LAN it's tiny; across data centers it can be 50–200ms.
""")

    # ── Phase 3: Read-your-writes ─────────────────────────────────
    section("Phase 3: Read-Your-Writes Guarantee")

    print("""  Read-your-writes: after a client writes a value, subsequent
  reads by the SAME client always reflect that write.

  Violation scenario: client writes to master, then reads from
  a replica that hasn't received the write yet.

  Solutions:
    1. Always read from master after a write (simple, adds load)
    2. Use WAIT to ensure replica has the write before reading from it
    3. Route reads for a user to the same replica (sticky routing)
    4. Include a version/timestamp; refuse reads from stale replicas
""")

    master.set("session:user42", '{"name": "Alice", "role": "admin"}')
    master.wait(1, 100)  # ensure replica1 has it

    val = replica1.get("session:user42")
    print(f"  Write to master + WAIT 1 → read from replica1: {val}")
    print("  Read-your-writes satisfied because WAIT confirmed replica has the data.")

    # ── Phase 4: Monotonic Reads ──────────────────────────────────
    section("Phase 4: Monotonic Reads")

    print("""  Monotonic reads: if you read version N, you never read
  a version older than N afterward.

  Violation: load balancer alternates reads between two replicas
  at different replication offsets — you see value go "backward".

  Solution: pin a client to one replica (sticky routing), or
  use WAIT to ensure a minimum replication offset before reading.
""")

    for v in range(1, 6):
        master.set("version_counter", v)
        master.wait(2, 50)

    reads = []
    for _ in range(10):
        # Alternate between replica1 and replica2
        r = replica1 if len(reads) % 2 == 0 else replica2
        reads.append(int(r.get("version_counter") or 0))

    print(f"  Version reads (alternating replicas): {reads}")
    violations = sum(1 for i in range(1, len(reads)) if reads[i] < reads[i-1])
    if violations:
        print(f"  Monotonic read VIOLATIONS detected: {violations} (saw value go backward)")
    else:
        print("  No violations — both replicas caught up (fast local network)")
        print("  In production across WAN, violations are common without sticky routing.")

    # ── Summary ───────────────────────────────────────────────────
    section("Summary")
    print("""
  Consistency Model Spectrum:
  ────────────────────────────────────────────────────────────
  Linearizability (strongest)
    → Every op appears instantaneous on a global timeline
    → Requires coordination on every op (expensive)
    → Redis WAIT + read-from-acked-replica achieves this locally

  Sequential Consistency
    → All nodes see operations in the same order
    → Order may not match real-time (clock skew allowed)
    → Used in some multi-processor memory models

  Causal Consistency
    → If A causes B, everyone sees A before B
    → Unrelated ops can appear in any order
    → MongoDB's causal sessions implement this

  Eventual Consistency (weakest practical)
    → All nodes converge eventually if no new writes
    → No ordering or timing guarantee
    → Cassandra, DynamoDB default mode

  Client-side guarantees (orthogonal to above):
    Read-your-writes  — your writes are visible to your reads
    Monotonic reads   — reads never go backward in time
    Monotonic writes  — your writes appear in order

  Next steps: ../04-replication/
""")


if __name__ == "__main__":
    main()
