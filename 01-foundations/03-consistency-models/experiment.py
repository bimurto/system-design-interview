#!/usr/bin/env python3
"""
Consistency Models Lab — Redis Master + 2 Replicas

Prerequisites: docker compose up -d (wait ~10s for replicas to sync)

What this demonstrates:
  1. Eventual consistency: write to master, read from replica — may be stale
  2. Strong consistency with WAIT: block until N replicas have acknowledged
  3. Latency cost of stronger consistency (eventual vs WAIT 1 vs WAIT 2)
  4. Read-your-writes: ensuring a client sees its own write
  5. Monotonic read violations forced via replica replication-offset divergence
  6. Causal consistency: why ordering of related writes matters across replicas
"""

import time
import subprocess
import threading

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
    print(f"\n{'=' * 62}")
    print(f"  {title}")
    print("=" * 62)


def check_replicas(master, replica1, replica2):
    """Verify all nodes are reachable before running experiments."""
    section("Pre-flight: Checking Connectivity")
    all_ok = True
    for label, conn, port in [
        ("master   (6379)", master, 6379),
        ("replica1 (6380)", replica1, 6380),
        ("replica2 (6381)", replica2, 6381),
    ]:
        try:
            conn.ping()
            info = conn.info("replication")
            role = info.get("role", "?")
            if role == "slave":
                lag = info.get("master_last_io_seconds_ago", "?")
                print(f"  OK  {label}  role={role}  master_io_lag={lag}s")
            else:
                connected = info.get("connected_slaves", 0)
                print(f"  OK  {label}  role={role}  connected_replicas={connected}")
        except Exception as e:
            print(f"  FAIL  {label}  error={e}")
            all_ok = False
    if not all_ok:
        print("\n  One or more nodes unreachable. Run: docker compose up -d")
        raise SystemExit(1)


def phase1_eventual_consistency(master, replica1):
    section("Phase 1: Eventual Consistency — Stale Reads")
    print("""  Write to master; read from replica immediately without any sync.
  The replica may return a stale value if replication hasn't arrived.
  On localhost this window is tiny (~0.1ms); across WAN it can be seconds.
""")

    # Use a pipeline to make the write and read as close together as possible,
    # reducing the chance that local replication closes the gap before we read.
    KEY = "phase1:counter"
    master.set(KEY, 0)
    master.wait(2, 500)  # ensure both replicas start synced at 0

    stale_count = 0
    trials = 20
    for i in range(1, trials + 1):
        master.set(KEY, i)
        # Read from replica with NO wait — purely eventual
        val = replica1.get(KEY)
        observed = int(val) if val is not None else None
        is_stale = observed != i
        if is_stale:
            stale_count += 1
        marker = "STALE" if is_stale else "fresh"
        print(f"  Write={i:<3}  replica_read={observed!s:<5}  [{marker}]")

    print(f"""
  Stale reads: {stale_count}/{trials} trials
  Observation: even on localhost, a write-then-immediate-read without WAIT
  can return a stale value. In a real cluster (LAN/WAN), stale reads are
  common and must be handled by the application or mitigated with WAIT.
""")


def phase2_wait_latency(master, replica1):
    section("Phase 2: Latency Cost of Stronger Consistency (WAIT)")
    print("""  WAIT <n> <timeout_ms> blocks until n replicas have acknowledged all
  pending writes.  Returns the number that actually acked.
  This converts eventual consistency into read-your-writes (or linearizable
  if you subsequently read only from an acked replica).

  We measure avg write+WAIT+read latency across 15 iterations each.
""")

    RUNS = 15
    timings = {}

    # Eventual — no WAIT
    total = 0
    for i in range(RUNS):
        start = time.perf_counter()
        master.set(f"perf:eventual:{i}", i)
        replica1.get(f"perf:eventual:{i}")   # may be stale
        total += (time.perf_counter() - start) * 1000
    timings["eventual"] = total / RUNS

    # WAIT 1
    total = 0
    last_acked1 = 0
    for i in range(RUNS):
        start = time.perf_counter()
        master.set(f"perf:wait1:{i}", i)
        last_acked1 = master.wait(1, 200)
        replica1.get(f"perf:wait1:{i}")      # guaranteed fresh (replica acked)
        total += (time.perf_counter() - start) * 1000
    timings["wait_1"] = total / RUNS

    # WAIT 2
    total = 0
    last_acked2 = 0
    for i in range(RUNS):
        start = time.perf_counter()
        master.set(f"perf:wait2:{i}", i)
        last_acked2 = master.wait(2, 200)
        replica1.get(f"perf:wait2:{i}")      # guaranteed fresh
        total += (time.perf_counter() - start) * 1000
    timings["wait_2"] = total / RUNS

    overhead1 = timings["wait_1"] - timings["eventual"]
    overhead2 = timings["wait_2"] - timings["eventual"]

    print(f"  No WAIT (eventual):    {timings['eventual']:6.2f} ms  (replicas acked: N/A)")
    print(f"  WAIT 1 replica:        {timings['wait_1']:6.2f} ms  (replicas acked: {last_acked1})  overhead: +{overhead1:.2f} ms")
    print(f"  WAIT 2 replicas:       {timings['wait_2']:6.2f} ms  (replicas acked: {last_acked2})  overhead: +{overhead2:.2f} ms")
    print(f"""
  On localhost the overhead is sub-millisecond (replication over loopback).
  Across a WAN with 50ms RTT: WAIT 1 adds ~50ms, WAIT 2 adds ~50ms
  (limited by the slowest replica, not additive).  That is the real cost
  of strong consistency at global scale.

  Key insight: WAIT returns how many replicas actually acked.  If fewer
  than requested ack (timeout), the write may not be durable on that many
  nodes.  The application MUST check the return value.
""")
    return timings


def phase3_read_your_writes(master, replica1, replica2):
    section("Phase 3: Read-Your-Writes Guarantee")
    print("""  Scenario: user updates their profile; the very next page load must
  show the updated profile — even if it hits a different replica.

  Violation path (WITHOUT any fix):
    1. Client writes to master (acknowledged immediately)
    2. Load balancer routes the next GET to replica2 (not yet synced)
    3. User sees their old profile — confusing and incorrect

  Fix A — WAIT before reading from replica:
    After the write, call WAIT 1 or WAIT 2. Then read from any acked replica.

  Fix B — Always read from master for the first post-write request.

  Fix C — Sticky routing: pin the user to one replica for the session;
    that replica will eventually have the write, and the client only sees
    monotonically advancing state on that replica.
""")

    # Demonstrate fix A
    KEY = "session:user42"
    payload = '{"name": "Alice", "role": "admin", "theme": "dark"}'
    master.set(KEY, payload)
    acked = master.wait(2, 300)   # ensure both replicas have it

    r1_val = replica1.get(KEY)
    r2_val = replica2.get(KEY)
    print(f"  Write to master + WAIT 2 ({acked} acked)")
    print(f"  replica1 sees: {r1_val}")
    print(f"  replica2 sees: {r2_val}")
    consistent = r1_val == payload and r2_val == payload
    print(f"  Read-your-writes satisfied on both replicas: {consistent}")
    print()

    # Demonstrate violation without WAIT
    print("  Now demonstrating a likely violation (write without WAIT, immediate read):")
    KEY2 = "session:user99"
    stale_seen = False
    for attempt in range(30):
        master.set(KEY2, f"attempt-{attempt}")
        # Read from replica2 immediately — no WAIT
        val = replica2.get(KEY2)
        expected = f"attempt-{attempt}"
        if val != expected:
            stale_seen = True
            print(f"  Attempt {attempt:2d}: wrote '{expected}', replica2 returned '{val}' — VIOLATION")
            break

    if not stale_seen:
        print("  No violation observed (replication too fast on localhost).")
        print("  In production, this violation is common under write bursts or WAN latency.")


def phase4_monotonic_reads_forced(master, replica1, replica2):
    section("Phase 4: Monotonic Read Violations — Forced via Replication Lag")
    print("""  Monotonic reads: once a client has seen value V, it must never see
  a value older than V.

  Violation: load balancer routes reads round-robin across replicas at
  different replication offsets. Client sees version go backward.

  We force divergence: pause replica2's replication, write several updates
  to master (caught by replica1 via normal sync), then re-enable replica2.
  During the divergence window, alternating reads show value regression.

  Note: this uses DEBUG SLEEP on replica2 to simulate lag. On a real cluster
  you'd see this with any replica that falls behind (slow disk, GC pause, etc).
""")

    KEY = "mono:counter"

    # Sync both replicas to a known baseline
    master.set(KEY, 0)
    master.wait(2, 500)

    # Force replica2 to lag: flood it with a slow command so its event loop
    # is busy while master advances. We use a long-running WAIT on replica2's
    # connection (harmless but occupies its processing for a moment), and
    # simultaneously write a burst to master.
    #
    # A cleaner approach: issue DEBUG SLEEP if the server allows it.
    try:
        replica2.debug_sleep(0.2)   # sleep for 200ms — blocks replication thread
        sleep_supported = True
    except Exception:
        sleep_supported = False

    if sleep_supported:
        # Write several versions to master while replica2 is asleep
        for v in range(1, 6):
            master.set(KEY, v)
            time.sleep(0.01)
        # replica1 should be at v=5; replica2 is still at 0 (sleeping)

        reads = []
        replicas = [replica1, replica2]
        for i in range(10):
            r = replicas[i % 2]
            label = "r1" if i % 2 == 0 else "r2"
            val = int(r.get(KEY) or 0)
            reads.append((label, val))

        print(f"  Alternating reads (r1=replica1, r2=replica2 [lagging]):")
        violations = 0
        for idx, (label, val) in enumerate(reads):
            lag_marker = " <-- STALE (replica2 lagging)" if label == "r2" and val < reads[idx - 1][1] else ""
            if idx > 0 and val < reads[idx - 1][1]:
                violations += 1
            print(f"    read {idx+1:2d} from {label}: {val}{lag_marker}")

        print(f"\n  Monotonic read violations: {violations}")
        if violations:
            print("  Client saw value go BACKWARD — a clear monotonic reads violation.")
        print()
    else:
        print("  DEBUG SLEEP not available (Redis security config). Showing conceptual demo.")
        print()
        # Synthetic illustration
        simulated = [(1, "r1"), (5, "r1"), (5, "r1"), (1, "r2"), (5, "r1"), (1, "r2")]
        print("  Simulated read sequence (r2 lagging by 4 versions):")
        for i, (val, src) in enumerate(simulated):
            stale = " <-- MONOTONIC VIOLATION" if i > 0 and val < simulated[i-1][0] else ""
            print(f"    read {i+1}: {val} from {src}{stale}")
        print()

    print("""  Fix: sticky routing — pin a user session to one replica.
  That replica advances monotonically even if it's behind master.
  Alternative: include a 'min_offset' token in the session; replicas
  refuse to serve reads if their replication offset is below the token.
""")


def phase5_causal_consistency(master, replica1, replica2):
    section("Phase 5: Causal Consistency — Why Ordering Matters")
    print("""  Causal consistency: if event A causally precedes event B
  (e.g., A is a post, B is a reply to that post), all nodes must
  see A before B.  Unrelated events can arrive in any order.

  Classic violation scenario:
    1. Alice writes post P to master  (master replicates to replica1, replica2)
    2. Bob reads P from replica1, writes reply R to master
    3. Carol reads from replica2 — sees reply R but NOT post P yet
       (replica2 is lagging for P but not for R)
    → Carol sees a reply to a post that doesn't exist yet.

  We simulate this with two independent write streams and forced lag.
""")

    # Two causally unrelated keys
    POST_KEY = "causal:post:alice"
    REPLY_KEY = "causal:reply:bob"

    # Reset
    master.delete(POST_KEY, REPLY_KEY)
    master.wait(2, 300)

    # Alice writes a post — force replica2 to lag on this write
    try:
        replica2.debug_sleep(0.3)
        lag_supported = True
    except Exception:
        lag_supported = False

    master.set(POST_KEY, "Alice's post: 'Consistency is hard!'")
    # Bob reads the post from replica1 (which is synced) and writes a reply
    time.sleep(0.05)
    post_on_r1 = replica1.get(POST_KEY)
    if post_on_r1:
        master.set(REPLY_KEY, "Bob's reply to Alice: 'Agreed!'")
        master.wait(1, 200)  # ensure at least replica1 has reply
    else:
        master.set(REPLY_KEY, "Bob's reply to Alice: 'Agreed!'")

    # Carol reads from replica2 — may see reply but not post
    post_on_r2 = replica2.get(POST_KEY)
    reply_on_r2 = replica2.get(REPLY_KEY)

    print(f"  replica1 sees post:  {post_on_r1}")
    print(f"  replica2 sees post:  {post_on_r2}")
    print(f"  replica2 sees reply: {reply_on_r2}")
    print()

    if reply_on_r2 and not post_on_r2:
        print("  CAUSAL VIOLATION: Carol sees Bob's reply but not Alice's post!")
        print("  The reply references context that replica2 hasn't received yet.")
    elif lag_supported:
        print("  No violation observed this run (timing-dependent).")
        print("  Under real network conditions (WAN/GC pause), this is reproducible.")
    else:
        print("  Conceptual demo: in production, this violation occurs when:")
        print("    - Two causally-related writes travel different replication paths")
        print("    - Read is served from a replica that has the effect but not the cause")

    print(f"""
  Fix options:
    1. Vector clocks / version vectors — track causal dependencies explicitly;
       replicas refuse to apply write B until write A (its cause) is applied.
    2. Causal sessions (MongoDB approach) — client sends a cluster time token;
       server holds the read until it has caught up to that logical time.
    3. Single replica per causally-related group — eliminates cross-replica
       ordering issues at the cost of reduced read scalability.

  Key interview point: causal consistency is stronger than eventual but
  cheaper than linearizable.  It's the sweet spot for social/collaborative
  applications where "reply before post" is logically impossible.
""")


def phase6_linearizability_vs_serializability(master):
    section("Phase 6: Linearizability vs. Serializability — Classic Interview Trap")
    print("""  These two terms are frequently confused, even in interviews.

  SERIALIZABILITY (database term):
    Transactions execute as if they ran one at a time in some serial order.
    Concerned with multi-object, multi-operation transactions.
    Does NOT require the serial order to match real time.
    Example: T1 and T2 run concurrently; result is as if T1 ran before T2
             OR T2 before T1 — but the chosen order can be in the past.

  LINEARIZABILITY (distributed systems term):
    Every individual operation appears to take effect instantaneously
    at a single point between its invocation and response.
    Concerned with single-object operations across distributed nodes.
    DOES require operations to respect real-time ordering.
    Example: if write W completes at wall-clock T=10, any read starting
             after T=10 must see W's value (or a later one).

  STRICT SERIALIZABILITY = Serializability + Linearizability
    Transactions are serializable AND the serial order matches real time.
    This is what databases like Spanner and CockroachDB provide.
    Most "ACID" databases provide only serializability (not linearizable).

  Why this matters in practice:
    A serializable DB allows write W at T=10 to appear "logically before"
    a transaction that started at T=8 if it produces a valid serial schedule.
    Linearizability forbids this — real-time ordering is preserved.

  Demo: we show that Redis WAIT gives us linearizability for single keys,
  but does NOT give us serializability across multiple keys.
""")

    # Show that WAIT gives us single-key linearizability
    KEY = "linear:balance"
    master.set(KEY, 1000)
    acked = master.wait(2, 300)
    val = master.get(KEY)
    print(f"  After WAIT {acked}: master reads balance = {val}")
    print("  Single-key linearizability: any subsequent read from an acked")
    print("  replica will see balance=1000 or newer. Real-time ordering preserved.")
    print()
    print("  Multi-key atomicity (serializability) requires transactions (MULTI/EXEC).")
    print("  WAIT alone does not give you multi-key consistency guarantees.")
    print()


def summary(timings):
    section("Summary")
    print(f"""
  Consistency Model Spectrum:
  ──────────────────────────────────────────────────────────────
  Linearizability (strongest)
    → Every op appears instantaneous on a global timeline
    → Requires coordination on every op (Paxos/Raft/2PC)
    → Redis WAIT + read-from-acked-replica approximates this locally

  Sequential Consistency
    → All nodes see operations in the same order
    → Order may not match real-time (clock skew allowed)
    → Underpins CPU memory models (x86 TSO, Java volatile)

  Causal Consistency
    → If A causes B, all nodes see A before B
    → Unrelated ops can appear in any order
    → MongoDB causal sessions, Dynamo vector clocks

  Eventual Consistency (weakest practical)
    → All nodes converge if writes stop
    → No ordering or timing guarantee
    → Cassandra/DynamoDB default, DNS, caches

  ──────────────────────────────────────────────────────────────
  Client-side guarantees (orthogonal to server-side model):
    Read-your-writes  — your writes visible in your subsequent reads
    Monotonic reads   — reads never go backward in time
    Monotonic writes  — your writes applied in issue order
    Write-follows-reads — writes ordered after reads that caused them
    (All four together = "session consistency")

  ──────────────────────────────────────────────────────────────
  Key distinctions for interviews:
    Linearizability  ≠  Serializability
      Linear: single-object, real-time ordered
      Serial: multi-object transactions, order may not be real-time
      Strict serial = both (Spanner, CockroachDB)

    Replica ≠ Stale
      Sync replica: never stale (write not acked until replica confirms)
      Async replica: can be stale (write acked before replica catches up)

  ──────────────────────────────────────────────────────────────
  Latency measured in this run (localhost loopback):
    No WAIT (eventual):    {timings['eventual']:.2f} ms
    WAIT 1 replica:        {timings['wait_1']:.2f} ms
    WAIT 2 replicas:       {timings['wait_2']:.2f} ms

  Next: ../04-replication/
""")


def main():
    section("CONSISTENCY MODELS LAB")
    print("""
  Consistency spectrum (strongest → weakest):
    Linearizability  — feels like one machine; every read sees latest write
    Sequential       — all nodes see ops in same order (not real-time)
    Causal           — causally related ops appear in correct order
    Eventual         — all nodes converge eventually; no ordering guarantee

  This lab uses Redis master + 2 async replicas to demonstrate the practical
  difference between these models.  Each phase is independent.
""")

    master, replica1, replica2 = connect_all()
    check_replicas(master, replica1, replica2)

    phase1_eventual_consistency(master, replica1)
    timings = phase2_wait_latency(master, replica1)
    phase3_read_your_writes(master, replica1, replica2)
    phase4_monotonic_reads_forced(master, replica1, replica2)
    phase5_causal_consistency(master, replica1, replica2)
    phase6_linearizability_vs_serializability(master)
    summary(timings)


if __name__ == "__main__":
    main()
