#!/usr/bin/env python3
"""
Distributed Caching Lab — Redis Cluster

Prerequisites: docker compose up -d (wait ~15s for cluster to initialize)

What this demonstrates:
  1. Set 1000 keys, observe distribution across 3 nodes via CLUSTER KEYSLOT
  2. MOVED redirection — client transparently routes to correct node
  3. Hot key problem — all requests for one key hit one node
  4. Key splitting strategy to distribute hot key load
  5. Redis data structures for different caching patterns
  6. Cache stampede — mutex lock prevents thundering herd on single key
"""

import time
import threading
import subprocess
import json
import random
import string
import math
from collections import defaultdict, Counter

try:
    import redis
    from redis.cluster import RedisCluster, ClusterNode
except ImportError:
    print("Installing dependencies...")
    subprocess.run(["pip", "install", "redis", "-q"], check=True)
    import redis
    from redis.cluster import RedisCluster, ClusterNode


NODES = [
    ClusterNode("localhost", 7001),
    ClusterNode("localhost", 7002),
    ClusterNode("localhost", 7003),
]


def section(title):
    print(f"\n{'=' * 60}")
    print(f"  {title}")
    print("=" * 60)


def get_cluster():
    return RedisCluster(
        startup_nodes=NODES,
        decode_responses=True,
        skip_full_coverage_check=True,
    )


def get_raw(port):
    """Direct connection to a single Redis node (for cluster info)."""
    return redis.Redis(host="localhost", port=port, decode_responses=True)


def hash_slot(key):
    """Compute Redis hash slot for a key (CRC16 % 16384)."""
    import binascii
    # Redis uses XMODEM CRC16
    crc = 0
    for byte in key.encode():
        crc ^= byte << 8
        for _ in range(8):
            if crc & 0x8000:
                crc = (crc << 1) ^ 0x1021
            else:
                crc <<= 1
            crc &= 0xFFFF
    return crc % 16384


def get_cluster_slot_assignment(rc):
    """
    Get slot ranges for each node.

    Uses CLUSTER SHARDS (Redis 7+) which supersedes the deprecated CLUSTER SLOTS.
    CLUSTER SHARDS returns a list of dicts, one per shard, each containing:
      'slots'   — flat list of [start, end, start, end, ...]
      'nodes'   — list of node dicts with 'port', 'role', etc.
    Falls back to CLUSTER SLOTS for older Redis versions.
    """
    r1 = get_raw(7001)
    assignments = {}
    try:
        shards = r1.execute_command("CLUSTER", "SHARDS")
        # shards is a list of [slots_array, nodes_array] pairs (RESP2 format)
        for shard in shards:
            # In RESP2: shard = [b'slots', [...], b'nodes', [...]]
            # Parse as key-value pairs
            shard_dict = {}
            for i in range(0, len(shard), 2):
                shard_dict[shard[i]] = shard[i + 1]
            slots_flat = shard_dict.get(b"slots", shard_dict.get("slots", []))
            nodes_list = shard_dict.get(b"nodes", shard_dict.get("nodes", []))
            # slots_flat is [start1, end1, start2, end2, ...]
            slot_ranges = []
            for j in range(0, len(slots_flat), 2):
                slot_ranges.append((int(slots_flat[j]), int(slots_flat[j + 1])))
            # Find the master node's port
            for node_entry in nodes_list:
                node_dict = {}
                for k in range(0, len(node_entry), 2):
                    node_dict[node_entry[k]] = node_entry[k + 1]
                role = node_dict.get(b"role", node_dict.get("role", b""))
                if isinstance(role, bytes):
                    role = role.decode()
                if role == "master":
                    port = int(node_dict.get(b"port", node_dict.get("port", 0)))
                    assignments[port] = assignments.get(port, []) + slot_ranges
                    break
    except Exception:
        # Fallback: CLUSTER SLOTS (deprecated but still functional pre-Redis 7)
        info = r1.execute_command("CLUSTER", "SLOTS")
        for slot_range in info:
            start, end = slot_range[0], slot_range[1]
            master_info = slot_range[2]
            port = master_info[1]
            assignments[port] = assignments.get(port, []) + [(start, end)]
    return assignments


def slot_to_node(slot, assignments):
    for port, ranges in assignments.items():
        for start, end in ranges:
            if start <= slot <= end:
                return port
    return None


def main():
    section("DISTRIBUTED CACHING LAB — REDIS CLUSTER")
    print("""
  Redis Cluster: 16,384 hash slots assigned across N master nodes

  Slot assignment (3 nodes):
    Node 1 (port 7001): slots 0     – 5460   (~33%)
    Node 2 (port 7002): slots 5461  – 10922  (~33%)
    Node 3 (port 7003): slots 10923 – 16383  (~33%)

  Key routing:
    slot = CRC16(key) % 16384
    → if slot is on node 2, client connects to node 2
    → if client connected to wrong node, gets MOVED error with redirect

  Hash tags: {user}:profile and {user}:settings share the same slot
    slot = CRC16("user") % 16384
    Both keys end up on the same node → multi-key operations work
""")

    # Verify connectivity
    try:
        rc = get_cluster()
        rc.ping()
        print("  Connected to Redis Cluster.")
    except Exception as e:
        print(f"  ERROR: {e}")
        print("  Run: docker compose up -d && sleep 15")
        return

    rc = get_cluster()

    # ── Phase 1: Distribute 1000 keys ─────────────────────────────
    section("Phase 1: Key Distribution Across Nodes")

    print(f"\n  Setting 1000 keys and observing slot distribution...\n")

    slot_counts = Counter()
    node_counts = Counter()

    # Get current slot assignment
    try:
        assignments = get_cluster_slot_assignment(rc)
    except Exception:
        # Fallback: approximate based on standard distribution
        assignments = {7001: [(0, 5460)], 7002: [(5461, 10922)], 7003: [(10923, 16383)]}

    for i in range(1000):
        key = f"key:{i}"
        rc.set(key, f"value:{i}", ex=300)
        slot = hash_slot(key)
        slot_counts[slot // 5462] += 1  # approximate bucket
        port = slot_to_node(slot, assignments) or "unknown"
        node_counts[port] += 1

    print(f"  Keys per node (by hash slot):")
    total = sum(node_counts.values())
    for port in sorted(node_counts):
        count = node_counts[port]
        pct   = count / total * 100 if total else 0
        bar   = "#" * int(pct / 2)
        print(f"    Node :{port}: {count:4d} keys ({pct:5.1f}%)  {bar}")

    print(f"\n  Sample slot assignments:")
    for key in ["user:1", "user:2", "product:abc", "session:xyz", "config:db"]:
        slot = hash_slot(key)
        port = slot_to_node(slot, assignments) or "?"
        print(f"    '{key}' → slot {slot:5d} → node :{port}")

    print(f"""
  Hash tags: use {{tag}} to co-locate related keys on the same node:
    {{order:123}}:items  → slot = CRC16("order:123") % 16384
    {{order:123}}:status → slot = CRC16("order:123") % 16384
    Both go to same node → MGET/pipeline works without cross-slot error
""")

    # ── Phase 2: MOVED redirection ─────────────────────────────────
    section("Phase 2: MOVED Redirection — Transparent Client Routing")

    print(f"""
  In Redis Cluster, if you connect to the wrong node for a key,
  the node returns a MOVED error:
    MOVED <slot> <ip>:<port>

  The redis-py cluster client handles this automatically:
    1. Computes CRC16(key) % 16384 from its cached slot map
    2. Connects directly to the node owning that slot
    3. If it gets a MOVED (slot map is stale), updates map and retries

  This is different from consistent hashing — the slot map is:
    • Fixed at 16384 slots (not based on node hashes)
    • Explicitly assigned by the cluster operator
    • Cached by the client to avoid redirects
""")

    test_key = "redirect-test:hello"
    slot = hash_slot(test_key)
    port = slot_to_node(slot, assignments) or "unknown"

    rc.set(test_key, "world")
    val = rc.get(test_key)
    print(f"  Key: '{test_key}'")
    print(f"  Slot: {slot} → Node :{port}")
    print(f"  Value: '{val}' (client transparently routed)")

    print(f"\n  ASK redirect (during slot migration):")
    print(f"  During CLUSTER MIGRATE, keys move from old to new node.")
    print(f"  Old node sends ASK redirect (not MOVED) — client follows it")
    print(f"  but does NOT update its slot cache (migration may be partial).")

    # ── Phase 3: Hot key problem ───────────────────────────────────
    section("Phase 3: Hot Key Problem — All Requests Hit One Node")

    hot_key   = "leaderboard:global"
    NUM_WRITES = 5000
    write_counts = Counter()
    write_lock   = threading.Lock()

    def hammer_key(thread_id, count=250):
        r = get_cluster()
        for i in range(count):
            r.incr(hot_key)
            with write_lock:
                write_counts["total"] += 1

    print(f"\n  Writing to hot key '{hot_key}' {NUM_WRITES} times from 20 threads...")
    start = time.perf_counter()
    threads = [threading.Thread(target=hammer_key, args=(i,)) for i in range(20)]
    for t in threads: t.start()
    for t in threads: t.join()
    elapsed = time.perf_counter() - start

    val = rc.get(hot_key)
    slot = hash_slot(hot_key)
    port = slot_to_node(slot, assignments) or "unknown"

    print(f"\n  Hot key results:")
    print(f"    Key: '{hot_key}'")
    print(f"    Final value: {val}")
    print(f"    All {NUM_WRITES} writes hit node :{port} (slot {slot})")
    print(f"    Throughput: {NUM_WRITES / elapsed:.0f} writes/sec")

    print(f"""
  Hot key problem:
    Redis Cluster distributes keys across nodes, but consistent
    hashing doesn't help for a SINGLE hot key — it always lands
    on the same node, regardless of how many cluster nodes you have.

  Hot key symptoms:
    • One Redis node at 100% CPU while others idle
    • High latency for operations on the hot key
    • Other keys on the same node are also slowed down

  Use cases where hot keys arise:
    • Global counters (total page views, active users)
    • Viral content (tweet goes viral, millions of likes)
    • Game leaderboards (everyone queries the same key)
    • Feature flags (every request checks the same flag)
""")

    # ── Phase 4: Hot key solutions ─────────────────────────────────
    section("Phase 4: Hot Key Solutions — Key Splitting")

    NUM_SHARDS = 10

    def get_shard_key(base_key, shard_id):
        return f"{base_key}:shard:{shard_id}"

    def incr_sharded(base_key, num_shards=NUM_SHARDS):
        shard_id = random.randint(0, num_shards - 1)
        shard_key = get_shard_key(base_key, shard_id)
        rc.incr(shard_key)
        return shard_id

    def get_sharded_total(base_key, num_shards=NUM_SHARDS):
        keys = [get_shard_key(base_key, i) for i in range(num_shards)]
        total = 0
        for key in keys:
            val = rc.get(key)
            if val:
                total += int(val)
        return total

    sharded_key = "leaderboard:sharded"
    shard_dist  = Counter()

    print(f"\n  Sharded counter: '{sharded_key}' split into {NUM_SHARDS} shards")
    print(f"  Each write goes to a random shard (different hash slots)...\n")

    for i in range(NUM_WRITES):
        shard_id = incr_sharded(sharded_key)
        shard_dist[shard_id] += 1

    total = get_sharded_total(sharded_key)
    print(f"  Sharded total: {total} (should be {NUM_WRITES})")
    print(f"\n  Writes distributed across all {NUM_SHARDS} shards:")
    for shard_id in sorted(shard_dist):
        count = shard_dist[shard_id]
        slot  = hash_slot(get_shard_key(sharded_key, shard_id))
        port  = slot_to_node(slot, assignments) or "?"
        bar   = "#" * (count // 20)
        print(f"    shard:{shard_id} → node:{port} slot:{slot:5d}  {count} writes  {bar}")

    print(f"""
  Key splitting trade-offs:
    + Distributes hot key load across multiple nodes
    + Scales with number of shards
    - Read requires aggregating all shards (more network round-trips)
    - Eventual consistency: total may be slightly off between reads
    - More complex code

  Other hot key solutions:
    1. Local in-process cache: cache hot key in app server memory
       - Risk: stale reads, but cache hit rate ~100% for same process
       - Good for read-heavy hot keys (feature flags, config)
    2. Read replicas: add replica nodes for hot read keys
       - Redis Cluster: replicas of the hot node handle reads
       - Trade: slight staleness, but reduces hot node load
    3. CDN/edge caching: cache at the CDN for public read-only data
       - Zero load on Redis for cached content
""")

    # ── Phase 5: Redis data structures ────────────────────────────
    section("Phase 5: Redis Data Structures for Caching Patterns")

    print(f"\n  Demonstrating key Redis data structures:\n")

    # String — simple cache
    rc.set("user:profile:42", json.dumps({"name": "Alice", "tier": "gold"}), ex=300)
    print(f"  STRING — user profile cache:")
    print(f"    SET user:profile:42 '{{...}}' EX 300  → simple key-value with TTL")

    # Hash — field-level caching (avoid serialization overhead)
    rc.hset("user:settings:42", mapping={"theme": "dark", "lang": "en", "tz": "UTC"})
    rc.expire("user:settings:42", 300)
    lang = rc.hget("user:settings:42", "lang")
    print(f"\n  HASH — user settings (field-level access):")
    print(f"    HSET user:settings:42 theme dark lang en  → {lang=}")
    print(f"    HGET user:settings:42 lang  → fetch single field, no deserialize")

    # Sorted Set — leaderboard
    rc.zadd("leaderboard:weekly", {"alice": 9500, "bob": 8200, "carol": 9100, "dave": 7800})
    top3 = rc.zrevrange("leaderboard:weekly", 0, 2, withscores=True)
    print(f"\n  SORTED SET — weekly leaderboard:")
    print(f"    ZADD leaderboard:weekly alice 9500 bob 8200 ...")
    for rank, (name, score) in enumerate(top3, 1):
        print(f"    #{rank}: {name} — {int(score)} pts")

    # Set — online users (membership check)
    for uid in [101, 102, 103, 201, 202]:
        rc.sadd("online:users", uid)
    rc.expire("online:users", 60)
    online = rc.sismember("online:users", 101)
    count  = rc.scard("online:users")
    print(f"\n  SET — online user tracking:")
    print(f"    SADD online:users 101 102 ...  → {count} online users")
    print(f"    SISMEMBER online:users 101  → {online}")

    # List — activity feed / LRU
    for item in ["action:view:p1", "action:buy:p2", "action:view:p3"]:
        rc.lpush("user:activity:42", item)
    rc.ltrim("user:activity:42", 0, 9)  # keep only last 10
    feed = rc.lrange("user:activity:42", 0, 2)
    print(f"\n  LIST — recent activity (capped at 10):")
    print(f"    LPUSH + LTRIM  → {feed}")

    print(f"""
  When to use each structure:
    STRING: simple key-value, serialized objects, counters (INCR)
    HASH:   structured objects with field-level access (avoids full
            deserialization for partial reads/updates)
    SET:    membership tests, unique visitors, tags
    SORTED SET: leaderboards, rate limiting (sliding window), range queries
    LIST:   activity feeds, job queues (LPUSH/RPOP), recent items
    STREAM: event log with consumer groups (Kafka-lite)
""")

    # ── Phase 6: Cache stampede — mutex lock ──────────────────────
    section("Phase 6: Cache Stampede — Mutex Lock vs Thundering Herd")

    stampede_key = "expensive:computation"
    lock_key     = f"lock:{stampede_key}"
    DB_LATENCY   = 0.05   # simulated DB query takes 50ms
    NUM_THREADS  = 30

    print(f"""
  Cache stampede: a popular key expires under high concurrency.
  All {NUM_THREADS} threads miss simultaneously and hammer the DB at once.

  Scenario A — no mutex:
    All threads see a cache miss → all query the DB → {NUM_THREADS}x DB load
  Scenario B — mutex (SET NX EX):
    Thread that wins the lock queries DB; others wait or return stale.
""")

    # ── Scenario A: stampede (no mutex) ───────────────────────────
    db_hit_counts = Counter()
    db_lock = threading.Lock()  # local lock to safely count DB hits

    def fetch_without_mutex(thread_id):
        r = get_cluster()
        val = r.get(stampede_key)
        if val is None:  # cache miss
            # Simulate DB query (no coordination)
            time.sleep(DB_LATENCY)
            with db_lock:
                db_hit_counts["no_mutex"] += 1
            r.set(stampede_key, "result", ex=5)

    # Ensure key is expired / absent
    rc.delete(stampede_key)
    threads = [threading.Thread(target=fetch_without_mutex, args=(i,)) for i in range(NUM_THREADS)]
    t0 = time.perf_counter()
    for t in threads: t.start()
    for t in threads: t.join()
    elapsed_a = time.perf_counter() - t0

    print(f"  Scenario A (no mutex): {db_hit_counts['no_mutex']} DB hits "
          f"from {NUM_THREADS} threads in {elapsed_a:.2f}s")
    print(f"    → {NUM_THREADS}x DB load; every thread fetched independently")

    # ── Scenario B: mutex via Redis SETNX ─────────────────────────
    rc.delete(stampede_key)
    rc.delete(lock_key)

    def fetch_with_mutex(thread_id):
        r = get_cluster()
        val = r.get(stampede_key)
        if val is not None:
            return  # cache hit — fast path

        # Try to acquire mutex (NX = only set if not exists, EX = auto-expire)
        acquired = r.set(lock_key, "1", nx=True, ex=2)
        if acquired:
            # This thread owns the lock — fetch from DB
            time.sleep(DB_LATENCY)
            with db_lock:
                db_hit_counts["mutex"] += 1
            r.set(stampede_key, "result", ex=5)
            r.delete(lock_key)
        else:
            # Lock is held by another thread — wait briefly and retry from cache
            retries = 0
            while retries < 10:
                time.sleep(0.01)
                val = r.get(stampede_key)
                if val is not None:
                    return  # cache is warm now
                retries += 1
            # Fallback: could return stale value or error; here we just return

    threads = [threading.Thread(target=fetch_with_mutex, args=(i,)) for i in range(NUM_THREADS)]
    t0 = time.perf_counter()
    for t in threads: t.start()
    for t in threads: t.join()
    elapsed_b = time.perf_counter() - t0

    print(f"\n  Scenario B (mutex):    {db_hit_counts['mutex']} DB hit  "
          f"from {NUM_THREADS} threads in {elapsed_b:.2f}s")
    print(f"    → 1x DB load; {NUM_THREADS - db_hit_counts['mutex']} threads waited and read from cache")

    print(f"""
  Stampede reduction: {db_hit_counts['no_mutex']}x DB calls → {db_hit_counts['mutex']}x DB call

  Mutex implementation details:
    SET lock:key 1 NX EX <ttl>
      NX   = only set if key does NOT exist (atomic compare-and-set)
      EX   = auto-expire the lock even if the lock-holder crashes
      ttl  = must exceed expected DB query time, else next thread
             re-acquires lock before cache is warm → partial stampede

  Trade-offs of the mutex approach:
    + Reduces DB fan-out from N threads to 1
    + Atomic — no race condition between NX check and set
    - Threads block (or return nothing) while lock is held
    - If lock TTL < DB latency, another thread steals the lock
    - Better alternative for reads: return stale value while one
      thread refreshes in background (stale-while-revalidate pattern)
""")

    # ── Summary ───────────────────────────────────────────────────
    section("Summary — Redis Cluster vs Sentinel")

    print("""
  Redis Cluster (sharding + HA):
    • Data distributed across N masters via 16,384 hash slots
    • Each master can have replicas for HA
    • No single point of failure
    • Supports up to ~1000 nodes
    • Trade-off: multi-key operations must use hash tags

  Redis Sentinel (HA without sharding):
    • All data on one master (or read replicas)
    • Sentinel processes monitor master health
    • Auto-failover: promote replica to master if master fails
    • Simpler than Cluster for small datasets
    • No horizontal scaling of writes

  Eviction policies — must configure before memory fills:
    allkeys-lru   → pure cache, uniform access pattern (safe default)
    allkeys-lfu   → pure cache, power-law/hot-key access (better hit rate)
    volatile-lru  → mixed store (some keys have no TTL and must survive)
    noeviction    → writes fail on OOM — NEVER use for a cache layer

  Cache warm-up strategies:
    1. Lazy (cache-aside): populate on first miss — cold start latency
    2. Eager: pre-populate before traffic arrives (from DB dump or script)
    3. Background refresh: separate worker refreshes hot keys before TTL
    4. Write-through: write to cache + DB on every write

  Thundering herd at warm-up:
    • All servers deploy simultaneously → empty cache → all hit DB
    • Fix: stagger deploys, use a "warm-up" phase before serving traffic,
      or use a distributed mutex so only one server repopulates each key

  Cache stampede (single key, Phase 6):
    • One popular key expires → N concurrent misses → N DB hits
    • Fix: SET lock:key 1 NX EX <ttl> — only one thread fetches
    • Alternative: stale-while-revalidate (return old value, refresh async)

  Next: ../09-search-systems/
""")


if __name__ == "__main__":
    main()
