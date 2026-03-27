#!/usr/bin/env python3
"""
Multi-Region Architecture Lab

Prerequisites: docker compose up -d (wait ~5s)

What this demonstrates:
  1. Active-passive replication with configurable lag
  2. Replication lag — stale reads from secondary region
  3. Read-your-writes session affinity
  4. Active-active conflict — LWW with logical clock vs CRDT set-merge
  5. Regional failover with quorum arbiter — RPO data-loss window measured
  6. Replication lag monitoring — alert when lag exceeds threshold
"""

import time
import json
import threading
import subprocess

try:
    import redis
except ImportError:
    print("Installing dependencies...")
    subprocess.run(["pip", "install", "redis", "-q"], check=True)
    import redis

REGION_A_URL      = "redis://localhost:6380"
REGION_B_URL      = "redis://localhost:6381"
REGION_ARBITER_URL = "redis://localhost:6382"


def section(title):
    print(f"\n{'=' * 60}")
    print(f"  {title}")
    print("=" * 60)


def connect(url, label, timeout=15):
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            r = redis.Redis.from_url(url, decode_responses=True)
            r.ping()
            print(f"  {label} ready.")
            return r
        except Exception:
            time.sleep(1)
    print(f"  ERROR: {label} not ready at {url}. Run: docker compose up -d")
    return None


# ── Simulated async replication ────────────────────────────────

class AsyncReplicator:
    """
    Simulates async replication from a primary to one or more secondaries.
    Each write is queued and applied to secondaries after `lag_s` seconds.
    Also tracks the high-watermark sequence number to support monotonic reads.
    """
    def __init__(self, primary, secondaries, lag_s=0.5):
        self.primary     = primary
        self.secondaries = secondaries if isinstance(secondaries, list) else [secondaries]
        self.lag_s       = lag_s
        self._queue      = []           # [(deliver_at, key, value, seq)]
        self._lock       = threading.Lock()
        self._stop       = threading.Event()
        self._seq        = 0            # write-ahead sequence number
        # Per-secondary: highest seq number delivered
        self._delivered  = {id(s): 0 for s in self.secondaries}
        self._thread     = threading.Thread(target=self._replicate_loop, daemon=True)
        self._thread.start()

    def enqueue(self, key, value):
        """Queue a write for async replication. Returns the sequence number."""
        with self._lock:
            self._seq += 1
            seq = self._seq
            deliver_at = time.time() + self.lag_s
            self._queue.append((deliver_at, key, value, seq))
        return seq

    def secondary_seq(self, secondary):
        """Return the highest replication sequence number delivered to this secondary."""
        with self._lock:
            return self._delivered.get(id(secondary), 0)

    def primary_seq(self):
        """Return the current primary write sequence number."""
        with self._lock:
            return self._seq

    def _replicate_loop(self):
        while not self._stop.is_set():
            now = time.time()
            with self._lock:
                ready  = [(k, v, s) for t, k, v, s in self._queue if t <= now]
                self._queue = [(t, k, v, s) for t, k, v, s in self._queue if t > now]
            for key, value, seq in ready:
                for sec in self.secondaries:
                    try:
                        sec.set(key, value)
                        with self._lock:
                            if seq > self._delivered[id(sec)]:
                                self._delivered[id(sec)] = seq
                    except Exception:
                        pass
            time.sleep(0.05)

    def stop(self):
        self._stop.set()


# ── Phase 1: Active-passive replication ───────────────────────

def phase1_active_passive(ra, rb):
    section("Phase 1: Active-Passive Replication")
    print("""  Region A = primary (handles all writes)
  Region B = secondary (read-only replica, async replication)
  Replication lag = 500ms
""")

    ra.flushall()
    rb.flushall()

    replicator = AsyncReplicator(ra, rb, lag_s=0.5)

    def write_to_primary(key, value):
        ra.set(key, value)
        return replicator.enqueue(key, value)

    # Write to primary
    seq1 = write_to_primary("user:1:name", "Alice")
    seq2 = write_to_primary("user:2:name", "Bob")
    print(f"  Wrote user:1 (seq={seq1}) and user:2 (seq={seq2}) to region-A (primary).")

    # Read immediately from secondary (before replication completes)
    time.sleep(0.1)
    name_a = ra.get("user:1:name")
    name_b = rb.get("user:1:name")
    lag_seq = replicator.primary_seq() - replicator.secondary_seq(rb)
    print(f"\n  Immediately after write (lag=500ms):")
    print(f"    Region A reads: user:1 = {name_a}")
    print(f"    Region B reads: user:1 = {name_b or '(not yet replicated)'}  <- STALE")
    print(f"    Replication lag: {lag_seq} write(s) behind")

    # Wait for replication
    time.sleep(0.6)
    name_b_after = rb.get("user:1:name")
    lag_seq_after = replicator.primary_seq() - replicator.secondary_seq(rb)
    print(f"\n  After 600ms (replication complete):")
    print(f"    Region B reads: user:1 = {name_b_after}")
    print(f"    Replication lag: {lag_seq_after} write(s) behind (caught up)")

    print("""
  Replication lag window = 500ms.
  Any read from region-B during this window returns stale data.
  This is acceptable for many use cases (product catalog, user profiles).
  Not acceptable for financial balances, inventory counts, or permission checks.
""")
    replicator.stop()
    return replicator


# ── Phase 2: Read-your-writes session affinity ─────────────────

def phase2_read_your_writes(ra, rb):
    section("Phase 2: Read-Your-Writes Violation and Session Affinity Fix")
    print("""  User writes to region-A, then is geo-routed to region-B.
  Without session affinity: user doesn't see their own write (confusing).
  With session affinity: user is routed back to region-A until replication done.

  Advanced fix: monotonic reads via replication sequence tracking.
    After write returns seq=N, only route reads to secondaries with delivered_seq >= N.
""")

    ra.flushall()
    rb.flushall()

    replicator = AsyncReplicator(ra, rb, lag_s=0.8)

    # Simulate user updating their profile in region-A
    ra.set("user:42:bio", "Software engineer, coffee lover")
    write_seq = replicator.enqueue("user:42:bio", "Software engineer, coffee lover")
    print(f"  User updates profile in region-A (write_seq={write_seq}).")

    # User is then routed to region-B (geo-routing, nearest region)
    time.sleep(0.1)
    bio_from_b = rb.get("user:42:bio")
    rb_seq = replicator.secondary_seq(rb)
    print(f"\n  WITHOUT session affinity (routed to region-B):")
    print(f"    Region B delivered_seq={rb_seq}, write_seq={write_seq} — secondary is behind")
    print(f"    Region B shows bio: {bio_from_b or '(empty — user does not see their update!)'}  <- violation")

    # FIX 1: Session affinity — for 2s after a write, route to region-A
    print(f"\n  FIX 1 — Session affinity (routed back to region-A):")
    bio_from_a = ra.get("user:42:bio")
    print(f"    Region A shows bio: {bio_from_a}  <- correct")

    # FIX 2: Monotonic reads — route to region-B only after it catches up
    print(f"\n  FIX 2 — Monotonic reads (wait for region-B delivered_seq >= {write_seq}):")
    deadline = time.time() + 2.0
    while time.time() < deadline:
        if replicator.secondary_seq(rb) >= write_seq:
            break
        time.sleep(0.05)
    rb_seq_now = replicator.secondary_seq(rb)
    bio_from_b_monotonic = rb.get("user:42:bio")
    print(f"    Region B delivered_seq={rb_seq_now} (>= write_seq={write_seq}) — safe to read")
    print(f"    Region B shows bio: {bio_from_b_monotonic}  <- correct via monotonic read")

    print("""
  Session affinity implementation:
    After a write, set a cookie: region=us-east-1; max-age=5
    Load balancer reads cookie and routes to specified region.
    After 5s (safely past max replication lag), routing returns to normal.

  Monotonic read implementation:
    Write response includes last-write-seq (server-side sequence number).
    Client passes seq in subsequent reads; router checks secondary_seq >= req_seq.
    If no secondary qualifies, route to primary — never return stale data.
    This is how DynamoDB Global Tables' "strongly consistent reads" work.
""")
    replicator.stop()


# ── Phase 3: Active-active conflict ───────────────────────────

def phase3_active_active(ra, rb):
    section("Phase 3: Active-Active Conflict — LWW vs CRDT Set Merge")
    print("""  Both regions accept writes. Concurrent writes to same key create a conflict.
  Demonstration: compare two resolution strategies on a shopping cart.
    Strategy A: Last-Write-Wins with Hybrid Logical Clock (LWW+HLC)
    Strategy B: CRDT OR-Set merge (no data loss)
""")

    ra.flushall()
    rb.flushall()

    # ── Hybrid Logical Clock ──────────────────────────────────
    class HybridLogicalClock:
        """
        Simplified HLC: (wall_ms, counter) pair.
        wall_ms tracks physical time; counter breaks ties within the same ms.
        Guarantee: hlc(e) > hlc(f) whenever e causally follows f.
        Bound: |hlc.wall_ms - wallclock_ms| < NTP_OFFSET (typically < 10ms).
        Used by CockroachDB, YugabyteDB, and Spanner-compatible systems.
        """
        def __init__(self):
            self._wall = 0
            self._ctr  = 0
            self._lock = threading.Lock()

        def now(self):
            with self._lock:
                pt = int(time.time() * 1000)      # physical time in ms
                if pt > self._wall:
                    self._wall = pt
                    self._ctr  = 0
                else:
                    self._ctr += 1
                return (self._wall, self._ctr)

        def update(self, received_wall, received_ctr):
            """Advance clock when receiving a message with HLC timestamp."""
            with self._lock:
                pt = int(time.time() * 1000)
                new_wall = max(pt, self._wall, received_wall)
                if new_wall == self._wall == received_wall:
                    self._ctr = max(self._ctr, received_ctr) + 1
                elif new_wall == self._wall:
                    self._ctr += 1
                elif new_wall == received_wall:
                    self._ctr = received_ctr + 1
                else:
                    self._ctr = 0
                self._wall = new_wall
                return (self._wall, self._ctr)

        def compare(self, a, b):
            """Return 1 if a > b, -1 if a < b, 0 if equal."""
            if a[0] != b[0]:
                return 1 if a[0] > b[0] else -1
            if a[1] != b[1]:
                return 1 if a[1] > b[1] else -1
            return 0

    hlc_a = HybridLogicalClock()
    hlc_b = HybridLogicalClock()

    key = "cart:user:7"

    # ── Strategy A: LWW + HLC ─────────────────────────────────
    print("  Strategy A: Last-Write-Wins with Hybrid Logical Clock (LWW+HLC)")
    print(f"  Concurrent writes to '{key}' in both regions:\n")

    ts_a = hlc_a.now()
    cart_a = ["item_A", "item_B"]
    record_a = json.dumps({"value": cart_a, "hlc_wall": ts_a[0], "hlc_ctr": ts_a[1], "region": "region-A"})
    ra.set(key, record_a)
    print(f"    region-A: cart={cart_a}  [hlc=({ts_a[0]}, {ts_a[1]})]")

    time.sleep(0.002)  # 2ms later — region-B writes independently
    ts_b = hlc_b.now()
    cart_b = ["item_A", "item_C"]
    record_b = json.dumps({"value": cart_b, "hlc_wall": ts_b[0], "hlc_ctr": ts_b[1], "region": "region-B"})
    rb.set(key, record_b)
    print(f"    region-B: cart={cart_b}  [hlc=({ts_b[0]}, {ts_b[1]})]")

    # Both regions propagate to each other (bi-directional replication arrives)
    # Coordinator reads both, applies LWW
    raw_a = json.loads(ra.get(key))
    raw_b = json.loads(rb.get(key))
    hlc_rec_a = (raw_a["hlc_wall"], raw_a["hlc_ctr"])
    hlc_rec_b = (raw_b["hlc_wall"], raw_b["hlc_ctr"])

    if hlc_a.compare(hlc_rec_a, hlc_rec_b) >= 0:
        winner, loser = raw_a, raw_b
    else:
        winner, loser = raw_b, raw_a

    print(f"\n  LWW result: {winner['region']} wins (higher HLC)")
    print(f"  Surviving cart: {winner['value']}")
    print(f"  Lost items: {set(loser['value']) - set(winner['value'])}  <- silent data loss")

    # ── Strategy B: CRDT OR-Set merge ────────────────────────
    print(f"\n  Strategy B: CRDT OR-Set Merge (no data loss)")

    # OR-Set: each element tagged with a unique ID per region.
    # Merge = union of all (element, tag) pairs.
    # Removes are tracked as tombstones — not demonstrated here for brevity.
    or_set_a = {item: f"region-A:{i}" for i, item in enumerate(cart_a)}
    or_set_b = {item: f"region-B:{i}" for i, item in enumerate(cart_b)}

    # Merge: union of both sets' elements (keys of the dict)
    merged_items = sorted(set(or_set_a.keys()) | set(or_set_b.keys()))
    print(f"    region-A OR-Set: {list(or_set_a.keys())}")
    print(f"    region-B OR-Set: {list(or_set_b.keys())}")
    print(f"    Merged cart:     {merged_items}  <- all items preserved")

    print("""
  Summary:
    LWW+HLC: simple to implement; loses one region's concurrent writes.
             HLC is better than Lamport (bounded real-time drift) and wall
             clock (causal ordering). Still wrong for shopping carts.

    CRDT OR-Set: mathematically guaranteed no data loss; correct for
                 additive use cases (carts, counters, presence sets).
                 Cannot represent all business logic — e.g., an inventory
                 decrement requires a PN-Counter or application-level merge.

    Home-region routing: avoid conflicts entirely by sharding writes to
                 the user's home region. Users in EU always write to eu-west-1.
                 A US user visiting EU pays write latency; EU users do not.
""")


# ── Phase 4: Regional failover with quorum arbiter ─────────────

def phase4_failover_with_quorum(ra, rb, arbiter):
    section("Phase 4: Regional Failover — Quorum-Based Promotion")
    print("""  Simulate region-A (primary) going offline.
  Quorum arbiter casts deciding vote: region-B is promoted (2-of-3 agree).
  Without the arbiter, region-B cannot safely self-promote — split-brain risk.

  Arbiter pattern:
    3 nodes: primary, secondary, arbiter (lightweight witness, holds no data).
    Promotion requires majority (2 of 3). Arbiter votes only — no data writes.
    This prevents split-brain when primary and secondary cannot reach each other.
""")

    ra.flushall()
    rb.flushall()
    arbiter.flushall()

    # Replicate existing state to region-B
    state = {"user:1": "alice", "user:2": "bob", "user:3": "carol"}
    for k, v in state.items():
        ra.set(k, v)
        rb.set(k, v)  # already replicated

    # Record primary epoch in arbiter (monotonically increasing fencing token)
    epoch = 1
    arbiter.set("primary:epoch", epoch)
    arbiter.set("primary:region", "region-A")
    print(f"  Pre-failover: {len(state)} records replicated to region-B.")
    print(f"  Arbiter epoch={epoch}, primary=region-A")

    # Simulate a write that hasn't replicated yet (RPO window)
    unprotected_write_start = time.time()
    ra.set("user:4", "dave")   # this write will be lost in failover
    unprotected_write_end = time.time()
    print(f"\n  Last write to region-A (not yet replicated): user:4=dave")

    # Simulate region-A failure
    time.sleep(0.05)
    failure_detected_at = time.time()
    rpo_window_ms = (failure_detected_at - unprotected_write_start) * 1000
    print(f"\n  [FAILURE] Region-A is unreachable.")
    print(f"  RPO window: ~{rpo_window_ms:.0f}ms of writes at risk (time since last unacknowledged write)")

    # Quorum vote: region-B and arbiter agree to promote region-B
    # (region-A is unreachable — it cannot vote against promotion)
    new_epoch = epoch + 1
    arbiter.set("primary:epoch", new_epoch)     # monotonic fencing token
    arbiter.set("primary:region", "region-B")
    print(f"\n  Quorum vote: region-B + arbiter agree (2-of-3).")
    print(f"  Arbiter updates epoch={new_epoch}, primary=region-B.")
    print(f"  Fencing token={new_epoch}: any writes from region-A with epoch<={epoch} will be rejected.")

    # Failover reads
    ra_is_down = True

    def read_with_failover(key):
        if ra_is_down:
            return rb.get(key), "region-B (promoted primary)"
        return ra.get(key), "region-A (primary)"

    print("\n  Reads after failover:")
    for key in ["user:1", "user:2", "user:4"]:
        val, source = read_with_failover(key)
        status = f"= {val!r}" if val else "= (not found — RPO data loss)"
        print(f"    {key}: {status}  [from {source}]")

    print(f"""
  user:4 is gone — it was written to region-A but hadn't replicated before failure.
  RPO window was ~{rpo_window_ms:.0f}ms (time between last unacknowledged write and failure detection).

  RPO options:
    RPO = 0     -> synchronous replication: write ACK only after secondary confirms.
                   Cost: +100ms per write across regions.
    RPO = 1s    -> async replication with lag < 1s. Fast writes, tiny loss window.
    RPO = 1 min -> async replication, less critical data (logs, analytics).

  RTO breakdown (active-passive):
    Detection:       30s  (health check interval + consecutive failures)
    Quorum vote:      1s  (arbiter + secondary agree)
    DNS propagation: 60s  (with TTL=60s)
    Total RTO:      ~91s  (reduce with Anycast routing — DNS propagation drops to ~0)

  Active-active RTO: ~0 — region-B was already serving traffic.
""")


# ── Phase 5: Replication lag monitoring ───────────────────────

def phase5_lag_monitoring(ra, rb):
    section("Phase 5: Replication Lag Monitoring and Alerting")
    print("""  Production multi-region systems must monitor replication lag continuously.
  Alert threshold: if lag > max_lag_s, stop routing reads to the secondary
  (return an error or redirect to primary — stale reads are worse than errors
  for permission checks, inventory counts, and financial balances).

  This phase simulates a lag spike (network congestion during compaction)
  and shows the monitor detecting it and taking the secondary out of rotation.
""")

    ra.flushall()
    rb.flushall()

    MAX_LAG_S  = 0.3   # 300ms threshold — secondary removed from rotation above this
    SPIKE_LAG  = 1.0   # simulated lag spike (compaction, congestion)
    NORMAL_LAG = 0.1   # normal operating lag

    replicator = AsyncReplicator(ra, rb, lag_s=NORMAL_LAG)
    secondary_in_rotation = True

    def write_stream(n=8):
        """Write n records with 100ms spacing."""
        for i in range(n):
            key   = f"event:{i}"
            value = f"payload-{i}"
            ra.set(key, value)
            replicator.enqueue(key, value)
            time.sleep(0.1)

    def lag_monitor():
        """
        Background monitor: measure lag as (primary_seq - secondary_seq).
        In production, this would compare replication sequence numbers or
        a heartbeat timestamp written by the primary and read by the secondary.
        """
        nonlocal secondary_in_rotation
        while True:
            # Approximate lag: if secondary is N sequences behind, lag >= N * avg_write_interval
            seq_diff = replicator.primary_seq() - replicator.secondary_seq(rb)
            approx_lag_s = seq_diff * replicator.lag_s
            in_rotation_before = secondary_in_rotation
            secondary_in_rotation = approx_lag_s <= MAX_LAG_S

            if in_rotation_before and not secondary_in_rotation:
                print(f"  [ALERT] Replication lag spike: ~{approx_lag_s:.2f}s > threshold {MAX_LAG_S}s")
                print(f"          Region-B removed from read rotation (avoid stale reads)")
            elif not in_rotation_before and secondary_in_rotation:
                print(f"  [RECOVERY] Lag normalized: ~{approx_lag_s:.2f}s <= threshold {MAX_LAG_S}s")
                print(f"             Region-B restored to read rotation")
            time.sleep(0.05)

    monitor_thread = threading.Thread(target=lag_monitor, daemon=True)
    monitor_thread.start()

    print(f"  Normal operation: lag={NORMAL_LAG*1000:.0f}ms, threshold={MAX_LAG_S*1000:.0f}ms\n")

    # Phase A: normal lag — secondary in rotation
    print("  [t=0s] Writing 4 events at normal lag (100ms)...")
    for i in range(4):
        ra.set(f"event:{i}", f"payload-{i}")
        replicator.enqueue(f"event:{i}", f"payload-{i}")
        time.sleep(0.1)
    time.sleep(0.3)
    print(f"  Secondary in rotation: {secondary_in_rotation}")

    # Phase B: simulate lag spike (increase lag dynamically)
    print(f"\n  [t=0.7s] Lag spike begins (simulating compaction / congestion): lag={SPIKE_LAG*1000:.0f}ms")
    replicator.lag_s = SPIKE_LAG
    for i in range(4, 8):
        ra.set(f"event:{i}", f"payload-{i}")
        replicator.enqueue(f"event:{i}", f"payload-{i}")
        time.sleep(0.1)
    time.sleep(0.5)

    # Phase C: lag returns to normal
    print(f"\n  [t=1.5s] Lag spike resolved: lag returns to {NORMAL_LAG*1000:.0f}ms")
    replicator.lag_s = NORMAL_LAG
    time.sleep(0.5)

    replicator.stop()

    print(f"""
  Key monitoring metrics for multi-region systems:
    replication_lag_p50    — typical lag; must be < your SLA's consistency window
    replication_lag_p99    — tail lag; spikes during compaction / maintenance
    replication_lag_max    — worst case; determines max RPO window
    secondary_in_rotation  — binary health signal; drives DNS/load balancer decisions

  In production: emit these metrics to Prometheus/CloudWatch; alert on P99 lag;
  set a hard circuit breaker that removes a secondary from rotation when lag > threshold.
  P99 lag, not P50, determines your actual user-visible staleness.
""")


def main():
    section("MULTI-REGION ARCHITECTURE LAB")
    print("""
  Architecture:
    Region A (us-east-1) <-> async replication <-> Region B (eu-west-1)
                                  |
                         Arbiter (us-west-2) — quorum witness, no data

  Three Redis instances simulate two regional databases + a quorum arbiter.

  Topics:
    1. Active-passive: primary writes -> async replication -> secondary reads
    2. Read-your-writes: stale reads + session affinity + monotonic reads
    3. Active-active: concurrent writes -> LWW+HLC vs CRDT OR-Set merge
    4. Failover with quorum: primary down -> arbiter vote -> promotion -> RPO
    5. Lag monitoring: detect lag spike, remove secondary, restore on recovery
""")

    ra      = connect(REGION_A_URL,       "Region A (us-east-1, primary)")
    rb      = connect(REGION_B_URL,       "Region B (eu-west-1, secondary)")
    arbiter = connect(REGION_ARBITER_URL, "Arbiter  (us-west-2, witness)")
    if not ra or not rb or not arbiter:
        return

    phase1_active_passive(ra, rb)
    phase2_read_your_writes(ra, rb)
    phase3_active_active(ra, rb)
    phase4_failover_with_quorum(ra, rb, arbiter)
    phase5_lag_monitoring(ra, rb)

    section("Summary")
    print("""
  Multi-Region Architecture Decision Tree:
  ─────────────────────────────────────────────────────────────
  Need global low-latency writes?       -> Active-active
  Need simplicity + strong consistency? -> Active-passive (sync replication)
  Need RPO=0?  -> Synchronous replication (pay +100ms per write)
  Need RPO>0?  -> Async replication (fast writes, small data loss window)
  Data residency requirements?          -> Region-pinned data, global metadata only
  Prevent split-brain on failover?      -> Quorum arbiter (2-of-3 promotion vote)

  Conflict Resolution (active-active):
    LWW + Hybrid Logical Clock  — simple; loses concurrent writes; needs HLC not wall clock
    CRDT (OR-Set, PN-Counter)   — no conflicts; limited to specific data types
    Application merge           — flexible; complex; business rules required
    Home-region routing         — avoid conflicts entirely; adds write latency for remote users

  Read-Your-Writes Solutions:
    Session cookie with home region   -> route reads to write region for N seconds
    Monotonic reads (seq tracking)    -> route to secondaries with delivered_seq >= write_seq
    Read from primary                 -> always read from primary after a write (adds latency)

  Key Metrics:
    Replication lag (p50, p99, max)   — how stale is the secondary?
    RPO (data loss on failover)       — how much data is at risk?
    RTO (recovery time)               — how fast can you failover?
    Cross-region write latency        — how much does replication cost?
    secondary_in_rotation             — binary health signal for DNS/LB decisions

  CAP Theorem Position:
    Active-passive (sync)  -> CP (consistency + partition tolerance; availability suffers)
    Active-passive (async) -> AP under partition (eventual consistency)
    Active-active          -> AP (availability + partition tolerance; consistency is eventual)

  Next steps: ../../03-case-studies/
""")


if __name__ == "__main__":
    main()
