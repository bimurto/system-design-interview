#!/usr/bin/env python3
"""
Distributed Cache Lab — experiment.py

What this demonstrates:
  1. Pipeline batching: 1000 individual GETs vs pipelined GETs — latency diff
  2. Eviction under memory pressure: fill past maxmemory → LRU eviction
  3. Pub/sub: publisher sends 100 messages to 3 subscribers
  4. Lua script for atomic check-and-set (distributed lock simulation)
  5. Measure RDB snapshot time

Run:
  docker compose up -d
  # Wait ~20s for cluster-init to finish
  # Then either:
  docker compose run --rm -it python:3.11-slim sh -c "pip install redis --quiet && python /experiment.py"
  # Or locally:
  pip install redis
  python experiment.py
"""

import random
import string
import threading
import time
import os

import redis
from redis.cluster import RedisCluster, ClusterNode

# Cluster nodes (exposed on host)
NODES = [
    ("localhost", 7001),
    ("localhost", 7002),
    ("localhost", 7003),
]

random.seed(42)


# ── Helpers ───────────────────────────────────────────────────────────────────

def section(title: str):
    print(f"\n{'=' * 62}")
    print(f"  {title}")
    print("=" * 62)


def connect_cluster() -> RedisCluster:
    startup_nodes = [ClusterNode(host, port) for host, port in NODES]
    return RedisCluster(
        startup_nodes=startup_nodes,
        decode_responses=True,
        skip_full_coverage_check=True,
    )


def connect_single(port: int = 7001) -> redis.Redis:
    """Connect to a single node for non-cluster operations (pubsub, pipeline)."""
    return redis.Redis(host="localhost", port=port, decode_responses=True)


def wait_for_cluster(max_wait: int = 60):
    print("  Waiting for Redis cluster ...")
    for i in range(max_wait):
        try:
            rc = connect_cluster()
            rc.ping()
            info = rc.cluster_info()
            if info.get("cluster_state") == "ok":
                print(f"  Cluster ready after {i + 1}s")
                return
        except Exception:
            pass
        time.sleep(1)
    raise RuntimeError("Redis cluster not ready")


# ── Phase 1: Pipeline batching ────────────────────────────────────────────────

def phase1_pipeline_batching():
    section("Phase 1: Pipeline Batching — Individual vs Pipelined")

    r = connect_single(7001)

    N = 1000
    keys = [f"pipeline:key:{i}" for i in range(N)]
    values = [f"value-{i}" for i in range(N)]

    # Pre-fill keys
    pipe = r.pipeline(transaction=False)
    for k, v in zip(keys, values):
        pipe.set(k, v, ex=60)
    pipe.execute()

    print(f"\n  Reading {N} keys individually (one round-trip per GET):")
    t0 = time.perf_counter()
    results_individual = []
    for k in keys:
        results_individual.append(r.get(k))
    individual_ms = (time.perf_counter() - t0) * 1000

    print(f"  Reading {N} keys with pipeline (one round-trip total):")
    t0 = time.perf_counter()
    pipe = r.pipeline(transaction=False)
    for k in keys:
        pipe.get(k)
    results_pipelined = pipe.execute()
    pipelined_ms = (time.perf_counter() - t0) * 1000

    speedup = individual_ms / pipelined_ms if pipelined_ms > 0 else float("inf")

    print(f"\n  {'Method':<25} {'Time (ms)':>12}  {'Req/s':>10}")
    print(f"  {'-'*25}  {'-'*12}  {'-'*10}")
    print(f"  {'Individual GETs':<25}  {individual_ms:>10.1f}ms  {N*1000/individual_ms:>10,.0f}")
    print(f"  {'Pipelined GETs':<25}  {pipelined_ms:>10.1f}ms  {N*1000/pipelined_ms:>10,.0f}")
    print(f"\n  Speedup: {speedup:.1f}x")
    print(f"  Correct: {results_individual == results_pipelined}")

    print(f"""
  Why pipelining is faster:
  - Individual: N requests × (network RTT + Redis processing)
    ~1ms RTT × 1000 = 1000ms
  - Pipelined: 1 request × (network RTT + N × Redis processing)
    ~1ms + N × 0.01ms = ~11ms

  Network RTT dominates. Pipelining eliminates N-1 round trips.
  Redis processes commands at ~500K ops/second; network is the bottleneck.

  WARNING: Pipeline is NOT transactional (use MULTI/EXEC for that).
  A pipeline failure mid-way may leave partial results.
""")

    r.close()


# ── Phase 2: Eviction under memory pressure ───────────────────────────────────

def phase2_eviction():
    section("Phase 2: LRU Eviction Under Memory Pressure")

    r = connect_single(7001)

    print("""
  Setting maxmemory=2mb and maxmemory-policy=allkeys-lru on redis-1.
  Then filling with data until keys are evicted.
""")

    r.config_set("maxmemory", "2mb")
    r.config_set("maxmemory-policy", "allkeys-lru")

    # Access some keys to make them "hot" (recently used)
    hot_keys = [f"hot:key:{i}" for i in range(50)]
    cold_keys = [f"cold:key:{i}" for i in range(200)]

    # Write hot keys first, then access them (to mark as recently used)
    for k in hot_keys:
        r.set(k, "hot-value-" * 10, ex=300)
    # Access hot keys to update LRU timestamp
    for k in hot_keys:
        r.get(k)

    # Write cold keys (less recently used than hot keys)
    for k in cold_keys:
        r.set(k, "cold-value-" * 10, ex=300)

    # Fill with a large batch to trigger eviction
    big_values = []
    evicted_estimate = 0
    for i in range(1000):
        key = f"filler:key:{i}"
        r.set(key, "x" * 500, ex=300)
        if i % 200 == 0:
            info = r.info("memory")
            used = info["used_memory"]
            evictions = info.get("evicted_keys", 0)
            evicted_estimate = evictions
            print(f"  After {i+1} filler keys: memory={used:,}B, evictions={evictions}")

    # Check which keys survived
    hot_survived = sum(1 for k in hot_keys if r.exists(k))
    cold_survived = sum(1 for k in cold_keys if r.exists(k))

    print(f"\n  LRU eviction results:")
    print(f"    Hot keys survived:  {hot_survived}/{len(hot_keys)}")
    print(f"    Cold keys survived: {cold_survived}/{len(cold_keys)}")

    print(f"""
  Hot keys (recently accessed) survive LRU eviction.
  Cold keys (less recently accessed) are evicted first.

  Eviction policies:
  ┌────────────────────────┬──────────────────────────────────────────┐
  │ Policy                 │ Behavior                                 │
  ├────────────────────────┼──────────────────────────────────────────┤
  │ noeviction             │ Return error when memory full            │
  │ allkeys-lru            │ Evict least recently used (any key)      │
  │ volatile-lru           │ Evict LRU from keys with TTL only        │
  │ allkeys-lfu            │ Evict least frequently used (any key)    │
  │ volatile-lfu           │ Evict LFU from keys with TTL only        │
  │ allkeys-random         │ Evict random key                         │
  │ volatile-ttl           │ Evict key with shortest remaining TTL    │
  └────────────────────────┴──────────────────────────────────────────┘

  LRU vs LFU:
  - LRU: good for "recency" workloads (recently accessed = important)
  - LFU: good for "frequency" workloads (viral content stays cached)
  - Twitter uses allkeys-lfu for their timeline cache
""")

    # Reset memory config
    r.config_set("maxmemory", "0")  # unlimited
    r.config_set("maxmemory-policy", "noeviction")
    r.close()


# ── Phase 3: Pub/Sub ──────────────────────────────────────────────────────────

def phase3_pubsub():
    section("Phase 3: Pub/Sub — Publisher and 3 Subscribers")

    print("""
  Redis Pub/Sub: fire-and-forget message distribution.
  Publisher sends to channel; all active subscribers receive instantly.
  Messages are NOT persisted — late subscribers miss past messages.
  For persistence use Redis Streams instead.
""")

    CHANNEL = "lab-events"
    NUM_MESSAGES = 20
    received = {"sub1": [], "sub2": [], "sub3": []}

    def subscriber(name: str, channel: str):
        r = connect_single(7001)
        ps = r.pubsub()
        ps.subscribe(channel)
        # Skip the subscribe confirmation message
        for msg in ps.listen():
            if msg["type"] == "message":
                received[name].append(msg["data"])
                if len(received[name]) >= NUM_MESSAGES:
                    break
        ps.unsubscribe()
        r.close()

    # Start 3 subscriber threads
    threads = []
    for name in ["sub1", "sub2", "sub3"]:
        t = threading.Thread(target=subscriber, args=(name, CHANNEL), daemon=True)
        t.start()
        threads.append(t)

    time.sleep(0.5)  # let subscribers connect

    # Publish messages
    r_pub = connect_single(7001)
    t0 = time.perf_counter()
    for i in range(NUM_MESSAGES):
        r_pub.publish(CHANNEL, f"event-{i:03d}")
    elapsed_ms = (time.perf_counter() - t0) * 1000
    r_pub.close()

    # Wait for subscribers
    for t in threads:
        t.join(timeout=5)

    print(f"  Published {NUM_MESSAGES} messages in {elapsed_ms:.1f}ms")
    print(f"\n  Received per subscriber:")
    for name, msgs in received.items():
        first = msgs[0] if msgs else "(none)"
        last = msgs[-1] if msgs else "(none)"
        print(f"    {name}: {len(msgs)} messages  first={first}  last={last}")

    all_correct = all(len(msgs) == NUM_MESSAGES for msgs in received.values())
    print(f"\n  All subscribers received all messages: {all_correct}")

    print("""
  Pub/Sub use cases:
  - Real-time notifications (chat messages, live scores)
  - Cache invalidation signals (broadcast "key X is dirty")
  - Live dashboards (stream metrics to all connected clients)

  NOT suitable for:
  - Guaranteed delivery (subscriber must be connected at publish time)
  - Message history / replay (use Redis Streams for that)
  - Work queues (use Redis Lists + BLPOP or Kafka)
""")


# ── Phase 4: Lua script atomic check-and-set (distributed lock) ───────────────

def phase4_distributed_lock():
    section("Phase 4: Atomic Check-and-Set — Distributed Lock")

    r = connect_single(7001)

    print("""
  Distributed lock (simplified Redlock single-node):
    SET lock:{resource} {owner} NX EX {ttl}
    → NX: only set if key does not exist
    → EX: auto-expire in case owner crashes

  Lua script for safe release (only owner can unlock):
    if GET(key) == owner then DEL(key)  -- atomic check+delete
""")

    LOCK_ACQUIRE_LUA = """
local key = KEYS[1]
local owner = ARGV[1]
local ttl = tonumber(ARGV[2])
local result = redis.call("SET", key, owner, "NX", "EX", ttl)
if result then
    return 1
else
    return 0
end
"""

    LOCK_RELEASE_LUA = """
local key = KEYS[1]
local owner = ARGV[1]
if redis.call("GET", key) == owner then
    return redis.call("DEL", key)
else
    return 0
end
"""

    acquire_script = r.register_script(LOCK_ACQUIRE_LUA)
    release_script = r.register_script(LOCK_RELEASE_LUA)
    LOCK_KEY = "lock:critical-section"

    print("\n  Demonstrating lock acquisition and release:\n")
    results = []

    def try_lock(owner: str, should_succeed: bool):
        acquired = acquire_script(keys=[LOCK_KEY], args=[owner, 10])
        results.append((owner, bool(acquired), should_succeed))

    # Owner 1 acquires lock
    try_lock("worker-1", True)
    # Owner 2 tries while lock is held → should fail
    try_lock("worker-2", False)
    # Owner 2 tries again → still fails
    try_lock("worker-2", False)

    print(f"  {'Owner':<12} {'Acquired?':>10}  {'Expected':>10}  {'Match':>6}")
    print(f"  {'-'*12}  {'-'*10}  {'-'*10}  {'-'*6}")
    for owner, acquired, expected in results:
        match = acquired == expected
        print(f"  {owner:<12}  {'YES' if acquired else 'NO':>10}  "
              f"{'YES' if expected else 'NO':>10}  {'OK' if match else 'FAIL':>6}")

    # Worker 1 releases
    released = release_script(keys=[LOCK_KEY], args=["worker-1"])
    print(f"\n  worker-1 releases lock: {'OK' if released else 'FAIL'}")

    # Worker 2 tries again after release → should succeed
    acquired_after = acquire_script(keys=[LOCK_KEY], args=["worker-2", 10])
    print(f"  worker-2 acquires after release: {'YES' if acquired_after else 'NO'}")

    # Worker 1 tries to release worker-2's lock → should fail (not owner)
    wrong_release = release_script(keys=[LOCK_KEY], args=["worker-1"])
    print(f"  worker-1 tries to release worker-2's lock: {'RELEASED (wrong!)' if wrong_release else 'REJECTED (correct)'}")

    # Cleanup
    release_script(keys=[LOCK_KEY], args=["worker-2"])
    r.close()

    print("""
  Key properties:
  - NX flag: atomic "set if not exists" — no TOCTOU race
  - EX flag: auto-expire on crash (prevents lock starvation)
  - Lua release: atomic check-then-delete (only owner can release)

  Limitations of single-node Redlock:
  - If Redis restarts before EX expires, lock is gone → two owners
  - Fix: Redlock uses 3-5 Redis nodes; acquire majority (N/2+1)
  - For strong safety guarantees, use ZooKeeper or etcd
""")


# ── Phase 5: RDB snapshot ─────────────────────────────────────────────────────

def phase5_rdb_snapshot():
    section("Phase 5: RDB Snapshot vs AOF Persistence")

    r = connect_single(7001)

    print("""
  RDB (Redis Database file): point-in-time snapshot.
    - Fork a child process: O(1) using copy-on-write
    - Child writes all data to a .rdb file
    - Parent continues serving requests (using copy-on-write memory)
    - Risk: data written after snapshot is lost on crash

  AOF (Append-Only File): log every write command.
    - Every WRITE command appended to aof file
    - On restart: replay AOF log to reconstruct state
    - Risk: large AOF file, slower restart

  RDB vs AOF trade-offs:
""")

    print(f"  {'Aspect':<30} {'RDB':>15}  {'AOF':>15}")
    print(f"  {'-'*30}  {'-'*15}  {'-'*15}")
    comparisons = [
        ("Restart speed",        "Fast (binary)",    "Slow (replay log)"),
        ("Data loss on crash",   "Minutes (config)", "< 1 second"),
        ("File size",            "Compact",          "Larger (grows)"),
        ("Write performance",    "No overhead",      "fsync overhead"),
        ("Backup simplicity",    "Single file",      "Complex"),
        ("Production default",   "Common",           "Recommended"),
    ]
    for aspect, rdb, aof in comparisons:
        print(f"  {aspect:<30}  {rdb:>15}  {aof:>15}")

    # Trigger a BGSAVE and measure time
    print(f"\n  Triggering BGSAVE (background RDB snapshot) ...")
    t0 = time.perf_counter()

    # Pre-fill some data for a meaningful snapshot
    pipe = r.pipeline(transaction=False)
    for i in range(5000):
        pipe.set(f"snapshot:key:{i}", f"value-{i}" * 5, ex=300)
    pipe.execute()

    r.bgsave()

    # Wait for save to complete
    max_wait = 30
    for i in range(max_wait):
        info = r.info("persistence")
        if info.get("rdb_bgsave_in_progress") == 0:
            elapsed_ms = (time.perf_counter() - t0) * 1000
            print(f"  BGSAVE complete in {elapsed_ms:.1f}ms")
            print(f"  Last RDB save:     {info.get('rdb_last_save_time')}")
            print(f"  Changes since save:{info.get('rdb_changes_since_last_save', 0)}")
            break
        time.sleep(0.1)

    info = r.info("persistence")
    print(f"\n  Persistence info:")
    print(f"    aof_enabled:           {info.get('aof_enabled', 'N/A')}")
    print(f"    rdb_last_bgsave_status:{info.get('rdb_last_bgsave_status', 'N/A')}")
    print(f"    rdb_current_bgsave_time_sec: {info.get('rdb_current_bgsave_time_sec', 0)}")

    r.close()


# ── Phase 6: Cluster info and hash slots ──────────────────────────────────────

def phase6_cluster_info():
    section("Phase 6: Redis Cluster — Hash Slots and Key Distribution")

    print("""
  Redis Cluster uses consistent hashing with 16,384 hash slots.
  Each node owns a range of slots. Keys are assigned by:
    slot = CRC16(key) % 16384

  With 3 nodes, each owns ~5,461 slots.
  Client reads cluster topology and routes directly to the right node.
""")

    rc = connect_cluster()

    # Show slot distribution
    try:
        cluster_nodes = rc.cluster_nodes()
        print(f"  Cluster nodes:")
        for node_id, info in list(cluster_nodes.items())[:6]:
            role = "master" if "master" in info.get("flags", "") else "replica"
            slots = info.get("slots", [])
            slot_count = sum(e - s + 1 for s, e in slots) if slots else 0
            host = info.get("host", "?")
            port = info.get("port", "?")
            print(f"    {host}:{port}  {role:<8}  slots: {slot_count}")
    except Exception as e:
        print(f"  (cluster_nodes not available in this client version: {e})")

    # Write 100 keys and show which slot/node each goes to
    sample_keys = [f"user:{i}" for i in range(1, 11)]
    print(f"\n  Hash slot assignment for sample keys:")
    print(f"  {'Key':<20} {'Slot':>8}  {'Node port':>10}")
    print(f"  {'-'*20}  {'-'*8}  {'-'*10}")

    import hashlib

    def crc16(s: str) -> int:
        """CRC16-CCITT used by Redis."""
        crc = 0
        for b in s.encode():
            crc ^= b << 8
            for _ in range(8):
                if crc & 0x8000:
                    crc = (crc << 1) ^ 0x1021
                else:
                    crc <<= 1
            crc &= 0xFFFF
        return crc

    for key in sample_keys:
        slot = crc16(key) % 16384
        # Determine owning node (slot ranges: 0-5460 → 7001, 5461-10922 → 7002, 10923-16383 → 7003)
        if slot <= 5460:
            node_port = 7001
        elif slot <= 10922:
            node_port = 7002
        else:
            node_port = 7003
        rc.set(key, f"data-for-{key}", ex=60)
        print(f"  {key:<20}  {slot:>8}  {node_port:>10}")

    # Hash tags: {} forces a key to a specific slot
    print(f"""
  Hash tags: use {{tag}} to force keys to the same slot.
    Key "{{user:1}}.profile"  → slot of "user:1"
    Key "{{user:1}}.settings" → slot of "user:1"  (same node!)

  This is required for multi-key operations (MGET, transactions) in cluster mode.
  Keys on different slots cannot be used in the same pipeline transaction.
""")

    rc.close()


# ── Main ─────────────────────────────────────────────────────────────────────

def main():
    section("DISTRIBUTED CACHE LAB")
    print("""
  Architecture:
    3-node Redis Cluster (16,384 hash slots across 3 masters)
    Each master owns ~5,461 slots.

  Experiments:
    1. Pipeline batching  — eliminate network RTT overhead
    2. LRU eviction       — memory pressure behavior
    3. Pub/sub            — fire-and-forget messaging
    4. Distributed lock   — atomic Lua check-and-set
    5. RDB snapshot       — persistence and recovery
    6. Cluster hash slots — key routing in cluster mode
""")

    wait_for_cluster()

    phase1_pipeline_batching()
    phase2_eviction()
    phase3_pubsub()
    phase4_distributed_lock()
    phase5_rdb_snapshot()
    phase6_cluster_info()

    section("Lab Complete")
    print("""
  Summary:
  - Pipelining eliminates N-1 network round trips: 10-100x throughput gain
  - allkeys-lru evicts cold keys first; LFU better for frequency workloads
  - Pub/sub is fire-and-forget; use Streams for guaranteed delivery
  - Lua scripts provide atomicity without transactions (single-threaded exec)
  - RDB for fast restarts; AOF for minimal data loss — often used together
  - 16,384 hash slots distributed across nodes; hash tags for co-location

  Next: 12-payment-system/ — exactly-once semantics with double-entry ledger
""")


if __name__ == "__main__":
    main()
