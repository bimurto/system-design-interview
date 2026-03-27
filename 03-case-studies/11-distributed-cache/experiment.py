#!/usr/bin/env python3
"""
Distributed Cache Lab — experiment.py

What this demonstrates:
  1. Pipeline batching: 1000 individual GETs vs pipelined GETs — show RTT dominance
  2. Eviction under memory pressure: fill past maxmemory → LRU eviction, hot vs cold key survival
  3. Pub/sub: publisher sends 20 messages to 3 concurrent subscribers
  4. Lua script for atomic check-and-set (distributed lock simulation)
  5. RDB snapshot: BGSAVE timing and persistence tradeoffs
  6. Cluster hash slots: key routing, hash tags, and cross-slot operation limits
  7. Hot key problem: uneven slot load and mitigation via key sharding

Run:
  docker compose up -d
  # Wait ~20s for cluster-init to complete. Verify with:
  #   docker compose logs cluster-init  # should end with "all 16384 slots covered"
  # Then:
  pip install redis
  python experiment.py
"""

import threading
import time

import redis
from redis.cluster import RedisCluster, ClusterNode

# Cluster nodes exposed on host (mapped via docker-compose ports)
NODES = [
    ("localhost", 7001),
    ("localhost", 7002),
    ("localhost", 7003),
]

import random
random.seed(42)


# ── Helpers ───────────────────────────────────────────────────────────────────

def section(title: str):
    print(f"\n{'=' * 66}")
    print(f"  {title}")
    print("=" * 66)


def connect_cluster() -> RedisCluster:
    startup_nodes = [ClusterNode(host, port) for host, port in NODES]
    return RedisCluster(
        startup_nodes=startup_nodes,
        decode_responses=True,
        skip_full_coverage_check=True,
    )


def connect_single(port: int = 7001) -> redis.Redis:
    """Connect to a single Redis node for operations that don't use cluster routing
    (pub/sub, pipelines within one node, config changes)."""
    return redis.Redis(host="localhost", port=port, decode_responses=True)


def wait_for_cluster(max_wait: int = 90):
    print("  Waiting for Redis cluster to be ready...")
    for i in range(max_wait):
        try:
            rc = connect_cluster()
            rc.ping()
            info = rc.cluster_info()
            if info.get("cluster_state") == "ok":
                print(f"  Cluster ready after {i + 1}s")
                rc.close()
                return
            rc.close()
        except Exception:
            pass
        time.sleep(1)
    raise RuntimeError(
        "Redis cluster not ready after {}s. Check: docker compose logs cluster-init".format(max_wait)
    )


def _crc16(key: str) -> int:
    """CRC16-CCITT used by Redis for hash slot assignment.
    If the key contains {tag}, only the tag portion is hashed."""
    # Extract hash tag if present
    start = key.find("{")
    if start != -1:
        end = key.find("}", start + 1)
        if end != -1 and end > start + 1:
            key = key[start + 1:end]

    crc = 0
    for b in key.encode():
        crc ^= b << 8
        for _ in range(8):
            if crc & 0x8000:
                crc = (crc << 1) ^ 0x1021
            else:
                crc <<= 1
        crc &= 0xFFFF
    return crc


def key_slot(key: str) -> int:
    return _crc16(key) % 16384


# ── Phase 1: Pipeline batching ────────────────────────────────────────────────

def phase1_pipeline_batching():
    section("Phase 1: Pipeline Batching — Eliminating Network Round-Trip Overhead")

    r = connect_single(7001)

    N = 1000
    keys = [f"pipeline:key:{i}" for i in range(N)]
    values = [f"value-{i}" for i in range(N)]

    # Pre-fill keys via pipeline (fast path — not timed)
    pipe = r.pipeline(transaction=False)
    for k, v in zip(keys, values):
        pipe.set(k, v, ex=300)
    pipe.execute()

    print(f"\n  Measuring {N} GET operations two ways:\n")

    # Individual GETs — one TCP round-trip per command
    t0 = time.perf_counter()
    results_individual = [r.get(k) for k in keys]
    individual_ms = (time.perf_counter() - t0) * 1000

    # Pipelined GETs — all commands in one TCP write, one TCP read
    t0 = time.perf_counter()
    pipe = r.pipeline(transaction=False)
    for k in keys:
        pipe.get(k)
    results_pipelined = pipe.execute()
    pipelined_ms = (time.perf_counter() - t0) * 1000

    speedup = individual_ms / pipelined_ms if pipelined_ms > 0 else float("inf")

    print(f"  {'Method':<28} {'Time (ms)':>10}  {'Throughput (req/s)':>20}")
    print(f"  {'-'*28}  {'-'*10}  {'-'*20}")
    print(f"  {'Individual GETs':<28}  {individual_ms:>10.1f}  {N * 1000 / individual_ms:>20,.0f}")
    print(f"  {'Pipelined GETs':<28}  {pipelined_ms:>10.1f}  {N * 1000 / pipelined_ms:>20,.0f}")
    print(f"\n  Speedup:          {speedup:.1f}x")
    print(f"  Results identical: {results_individual == results_pipelined}")

    print(f"""
  Why pipelining is faster:
  - Individual GET: each command waits for the previous response before
    sending the next. With a ~0.3ms loopback RTT:
      1000 GETs × 0.3ms = ~300ms of pure network wait time.

  - Pipelined GET: all 1000 commands are written to a single TCP buffer,
    sent in one or a few network packets. Redis processes each in order
    and writes all responses back. Client reads them all in one recv().
      1 RTT + 1000 × (Redis processing ~2µs) ≈ a few ms total.

  Redis processes commands at ~500K ops/sec; the bottleneck is ALWAYS the
  network, never the Redis CPU. Pipelining eliminates N-1 round trips.

  IMPORTANT — pipeline is NOT a transaction:
  - Use MULTI/EXEC for atomicity guarantees
  - A pipeline failure mid-way may leave partial state applied
  - In cluster mode, all pipelined keys MUST be on the same node
    (use hash tags to guarantee co-location)
""")

    r.close()


# ── Phase 2: Eviction under memory pressure ───────────────────────────────────

def phase2_eviction():
    section("Phase 2: LRU Eviction Under Memory Pressure — Hot vs Cold Key Survival")

    r = connect_single(7001)

    # Flush this node's data first so we start from a clean baseline
    r.flushall()
    time.sleep(0.2)

    print("""
  Strategy:
    1. Write 50 "hot" keys — then immediately read them (updates LRU clock)
    2. Write 200 "cold" keys — never read (LRU clock stays old)
    3. Fill with 2000 large "filler" keys to trigger maxmemory eviction
    4. Check which hot vs cold keys survived

  Redis approximate LRU: samples maxmemory-samples keys (default 5),
  evicts the least-recently-used among the sample. Not exact but efficient.
""")

    # Set a tight memory limit to force eviction quickly
    r.config_set("maxmemory", "4mb")
    r.config_set("maxmemory-policy", "allkeys-lru")
    r.config_set("maxmemory-samples", "10")  # larger sample = more accurate LRU

    hot_keys = [f"hot:key:{i}" for i in range(50)]
    cold_keys = [f"cold:key:{i}" for i in range(200)]

    # Write hot keys, then read them all to set recent LRU timestamps
    pipe = r.pipeline(transaction=False)
    for k in hot_keys:
        pipe.set(k, "hot-value-padding-" * 5, ex=600)
    pipe.execute()
    for k in hot_keys:
        r.get(k)  # update LRU timestamp — these are "recently used"

    # Write cold keys — never accessed after write
    pipe = r.pipeline(transaction=False)
    for k in cold_keys:
        pipe.set(k, "cold-value-padding-" * 5, ex=600)
    pipe.execute()

    # Fill with large filler keys to exceed maxmemory and force evictions
    evictions_before = r.info("stats").get("evicted_keys", 0)

    print(f"  {'After N filler keys':<22} {'Memory used':>14}  {'Evictions':>10}")
    print(f"  {'-'*22}  {'-'*14}  {'-'*10}")

    pipe = r.pipeline(transaction=False)
    for i in range(2000):
        pipe.set(f"filler:{i}", "x" * 800, ex=600)
        if (i + 1) % 500 == 0:
            pipe.execute()
            info = r.info("memory")
            stats = r.info("stats")
            evictions = stats.get("evicted_keys", 0) - evictions_before
            used_mb = info["used_memory"] / (1024 * 1024)
            print(f"  {i + 1:<22}  {used_mb:>12.2f}MB  {evictions:>10}")
            pipe = r.pipeline(transaction=False)
    if pipe.command_stack:
        pipe.execute()

    # Final check: which keys survived?
    hot_survived = sum(1 for k in hot_keys if r.exists(k))
    cold_survived = sum(1 for k in cold_keys if r.exists(k))
    total_evictions = r.info("stats").get("evicted_keys", 0) - evictions_before

    print(f"""
  Eviction results (total evictions: {total_evictions}):
    Hot keys  (recently read):   {hot_survived:3d}/{len(hot_keys)} survived
    Cold keys (never read):      {cold_survived:3d}/{len(cold_keys)} survived

  Hot keys survive because their LRU timestamp is recent.
  Cold keys are evicted first — they look "old" to the LRU sampler.
""")

    print(f"""  Eviction policy comparison:
  ┌────────────────────────┬──────────────────────────────────────────────┐
  │ Policy                 │ Behavior                                     │
  ├────────────────────────┼──────────────────────────────────────────────┤
  │ noeviction             │ Return OOM error — write fails               │
  │ allkeys-lru            │ Evict least recently used (any key)          │
  │ volatile-lru           │ Evict LRU only from keys with TTL set        │
  │ allkeys-lfu            │ Evict least frequently used (any key)        │
  │ volatile-lfu           │ Evict LFU only from keys with TTL set        │
  │ allkeys-random         │ Evict randomly — unpredictable               │
  │ volatile-ttl           │ Evict key with shortest remaining TTL        │
  └────────────────────────┴──────────────────────────────────────────────┘

  LRU vs LFU — when to pick which:
  - allkeys-lru: general-purpose cache (most common). Good when "recent"
    correlates with "important".
  - allkeys-lfu: viral content workloads. A tweet from 3 days ago accessed
    10M times has higher frequency than a tweet from 1 minute ago accessed
    once. LRU would evict the viral tweet; LFU keeps it.
    Twitter uses allkeys-lfu for their timeline caches.
  - volatile-lru: mixed-use Redis (some keys are permanent config/flags).
    Set those without TTL and they'll never be evicted.
""")

    # Reset config
    r.config_set("maxmemory", "0")
    r.config_set("maxmemory-policy", "allkeys-lru")
    r.close()


# ── Phase 3: Pub/Sub ──────────────────────────────────────────────────────────

def phase3_pubsub():
    section("Phase 3: Pub/Sub — Fire-and-Forget Fan-out to N Subscribers")

    print("""
  Redis Pub/Sub: a lightweight fan-out mechanism.
  Publisher sends one message; ALL active subscribers receive a copy.
  Messages are NOT queued — late subscribers miss past messages.
  For durability and replay, use Redis Streams (XADD/XREAD) instead.
""")

    CHANNEL = "lab:events"
    NUM_MESSAGES = 20
    received = {"sub1": [], "sub2": [], "sub3": []}
    sub_ready = threading.Barrier(4)  # 3 subscribers + main thread

    def subscriber(name: str):
        r = connect_single(7001)
        ps = r.pubsub()
        ps.subscribe(CHANNEL)
        # Consume the initial subscribe-confirmation message
        next(m for m in ps.listen() if m["type"] == "subscribe")
        sub_ready.wait()  # signal that we are subscribed and listening
        for msg in ps.listen():
            if msg["type"] == "message":
                received[name].append(msg["data"])
                if len(received[name]) >= NUM_MESSAGES:
                    break
        ps.unsubscribe()
        r.close()

    threads = []
    for name in ["sub1", "sub2", "sub3"]:
        t = threading.Thread(target=subscriber, args=(name,), daemon=True)
        t.start()
        threads.append(t)

    sub_ready.wait()  # all 3 subscribers are confirmed-subscribed before publishing

    r_pub = connect_single(7001)
    t0 = time.perf_counter()
    for i in range(NUM_MESSAGES):
        r_pub.publish(CHANNEL, f"event-{i:03d}")
    elapsed_ms = (time.perf_counter() - t0) * 1000
    r_pub.close()

    for t in threads:
        t.join(timeout=10)

    print(f"  Published {NUM_MESSAGES} messages in {elapsed_ms:.1f}ms\n")
    print(f"  {'Subscriber':<12} {'Received':>10}  {'First msg':>12}  {'Last msg':>12}")
    print(f"  {'-'*12}  {'-'*10}  {'-'*12}  {'-'*12}")
    all_correct = True
    for name, msgs in received.items():
        first = msgs[0] if msgs else "(none)"
        last = msgs[-1] if msgs else "(none)"
        ok = len(msgs) == NUM_MESSAGES
        if not ok:
            all_correct = False
        print(f"  {name:<12}  {len(msgs):>10}  {first:>12}  {last:>12}  {'OK' if ok else 'MISSING MSGS'}")

    print(f"\n  All subscribers received all {NUM_MESSAGES} messages: {all_correct}")

    print("""
  Pub/Sub use cases:
  - Real-time notifications: chat messages, live scoreboards
  - Cache invalidation signals: "key X is stale, recompute"
  - Live dashboards: broadcast metrics to all connected clients

  NOT suitable for:
  - Guaranteed delivery (subscriber must be connected at publish time)
  - Message history / replay (use Redis Streams: XADD/XREAD/XACK)
  - Work queues with at-most-once/at-least-once semantics (use BLPOP or Kafka)
  - Cluster mode note: Pub/Sub messages stay on the node they are published to.
    In cluster mode, all subscribers and publishers must hit the same node,
    or use Redis 7's sharded pub/sub (SSUBSCRIBE) for cluster-wide fan-out.
""")


# ── Phase 4: Lua script atomic check-and-set (distributed lock) ───────────────

def phase4_distributed_lock():
    section("Phase 4: Distributed Lock — Atomic Check-and-Set with Lua")

    r = connect_single(7001)

    print("""
  Distributed lock (single-node Redlock pattern):
    Acquire: SET lock:{resource} {owner_id} NX EX {ttl_seconds}
      NX  — only SET if key does NOT exist (atomically)
      EX  — auto-expire if owner crashes (prevents lock starvation)

    Release: Lua script — atomic check-then-delete
      if GET(key) == owner_id then DEL(key)
      Without Lua: check-then-delete is two commands — window for race condition
      where owner's lock expires between the GET and DEL, deleting a new owner's lock.

  The Lua script runs atomically in Redis's single-threaded executor — no
  other command can interleave between the GET and the DEL.
""")

    LOCK_ACQUIRE_LUA = """
local key   = KEYS[1]
local owner = ARGV[1]
local ttl   = tonumber(ARGV[2])
if redis.call("SET", key, owner, "NX", "EX", ttl) then
    return 1
else
    return 0
end
"""

    LOCK_RELEASE_LUA = """
local key   = KEYS[1]
local owner = ARGV[1]
if redis.call("GET", key) == owner then
    return redis.call("DEL", key)
else
    return 0  -- not the owner, refuse to release
end
"""

    acquire = r.register_script(LOCK_ACQUIRE_LUA)
    release = r.register_script(LOCK_RELEASE_LUA)
    LOCK_KEY = "lock:critical-section"

    print("  Scenario: worker-1 holds lock, worker-2 contests, then worker-1 releases\n")

    def try_acquire(owner: str, ttl: int = 10) -> bool:
        return bool(acquire(keys=[LOCK_KEY], args=[owner, ttl]))

    def try_release(owner: str) -> bool:
        return bool(release(keys=[LOCK_KEY], args=[owner]))

    steps = []

    # worker-1 acquires
    ok = try_acquire("worker-1")
    steps.append(("worker-1 acquire", ok, True))

    # worker-2 attempts while lock held — must fail
    ok = try_acquire("worker-2")
    steps.append(("worker-2 acquire (contested)", ok, False))

    # worker-2 retries — still fails
    ok = try_acquire("worker-2")
    steps.append(("worker-2 retry", ok, False))

    # worker-1 tries to release using wrong owner ID — must be rejected
    ok = try_release("wrong-owner")
    steps.append(("wrong-owner release attempt", ok, False))

    # worker-1 correctly releases
    ok = try_release("worker-1")
    steps.append(("worker-1 release", ok, True))

    # worker-2 now succeeds
    ok = try_acquire("worker-2")
    steps.append(("worker-2 acquire (after release)", ok, True))

    print(f"  {'Step':<38} {'Result':>8}  {'Expected':>8}  {'Pass':>5}")
    print(f"  {'-'*38}  {'-'*8}  {'-'*8}  {'-'*5}")
    all_pass = True
    for step, result, expected in steps:
        match = result == expected
        if not match:
            all_pass = False
        r_str = "YES" if result else "NO"
        e_str = "YES" if expected else "NO"
        print(f"  {step:<38}  {r_str:>8}  {e_str:>8}  {'OK' if match else 'FAIL':>5}")

    print(f"\n  All assertions passed: {all_pass}")

    # Cleanup
    try_release("worker-2")
    r.close()

    print("""
  Key properties:
  - NX flag:     atomic "set if not exists" — eliminates TOCTOU race condition
  - EX flag:     auto-expire prevents deadlock if owner process crashes
  - Lua release: atomic check-then-delete — only the lock owner can release

  Single-node Redlock limitations:
  - Redis restart before EX expires: lock disappears → two concurrent owners
  - Async replication: if master crashes after SET but before replication,
    replica promoted without the lock — two owners again
  - Fix: Redlock uses 3 or 5 independent Redis nodes.
    Acquire majority (N/2 + 1) within validity time. Release all.
    Surviving minority nodes can't grant the lock to another client.

  For strongest correctness: use ZooKeeper (ephemeral sequential nodes) or
  etcd (compare-and-swap with lease TTL). Redis Redlock is suitable for
  advisory locking where brief double-ownership is acceptable.
""")


# ── Phase 5: RDB snapshot ─────────────────────────────────────────────────────

def phase5_rdb_snapshot():
    section("Phase 5: RDB Snapshot vs AOF — Persistence Trade-offs")

    r = connect_single(7001)

    print("""
  Redis persistence: two mechanisms, different trade-offs.

  RDB (Redis Database file) — point-in-time snapshots:
    redis.call("BGSAVE"):
      1. fork() a child process — O(1), uses copy-on-write (COW)
      2. Child serializes the entire keyspace to dump.rdb (binary, compressed)
      3. Parent continues serving commands; modified memory pages are COW-copied
      4. On restart: load dump.rdb — fast (binary format, no replay needed)
    Data loss: all writes since the last snapshot are lost on crash
    Typical schedule: every 60s (if ≥1 write) or every 5min (if ≥100 writes)
    Memory overhead during save: COW can double memory in write-heavy workloads

  AOF (Append-Only File) — write-ahead log:
    Every write command is appended to appendonly.aof before the response
    is sent to the client (depending on appendfsync setting).
    On restart: replay AOF from beginning — slower but more durable.
    appendfsync options:
      always   — fsync after every command: safest, slowest (~1K writes/s)
      everysec — fsync once per second: ≤1s data loss, fast (default)
      no       — OS decides: fastest, highest data loss risk

  AOF Rewrite: AOF grows without bound. BGREWRITEAOF compacts it by
  writing only the current state (not the full mutation history).
  Redis auto-triggers this when AOF exceeds auto-aof-rewrite-percentage.
""")

    print(f"  {'Aspect':<30} {'RDB':>20}  {'AOF (everysec)':>20}")
    print(f"  {'-'*30}  {'-'*20}  {'-'*20}")
    comparisons = [
        ("Restart speed",         "Fast (binary load)",   "Slow (log replay)"),
        ("Max data loss",         "Minutes (last snap)",  "~1 second"),
        ("File size",             "Compact (compressed)", "Large (grows)"),
        ("Write amplification",   "None",                 "Low (1 fsync/s)"),
        ("Memory during save",    "COW overhead ~10-50%", "None"),
        ("Human readable",        "No (binary)",          "Yes (text cmds)"),
        ("Production default",    "Use both",             "Use both"),
    ]
    for aspect, rdb, aof in comparisons:
        print(f"  {aspect:<30}  {rdb:>20}  {aof:>20}")

    # Pre-fill data for a meaningful snapshot
    print(f"\n  Pre-filling 5000 keys and triggering BGSAVE ...")
    pipe = r.pipeline(transaction=False)
    for i in range(5000):
        pipe.set(f"snapshot:key:{i}", f"value-{'x' * 20}-{i}", ex=600)
    pipe.execute()
    key_count = r.dbsize()

    t0 = time.perf_counter()
    r.bgsave()

    # Poll until BGSAVE completes
    for _ in range(300):
        info = r.info("persistence")
        if info.get("rdb_bgsave_in_progress") == 0:
            break
        time.sleep(0.1)

    elapsed_ms = (time.perf_counter() - t0) * 1000
    info = r.info("persistence")

    print(f"""
  BGSAVE results:
    Keys in keyspace:          {key_count:,}
    BGSAVE duration:           {elapsed_ms:.1f}ms
    Last bgsave status:        {info.get('rdb_last_bgsave_status', 'N/A')}
    Changes since last save:   {info.get('rdb_changes_since_last_save', 0)}
    AOF enabled:               {info.get('aof_enabled', 'N/A')}

  The fork() is fast (O(1)) because Linux uses copy-on-write.
  The child only reads memory; dirty pages accumulate in the parent.
  On a write-heavy workload during BGSAVE, memory can temporarily double.
  This is why Redis nodes are typically provisioned with 2× their working set.

  Production recommendation: enable both RDB + AOF.
    - RDB for fast restarts after planned maintenance
    - AOF for ≤1s data loss on unplanned crash
    - appendfsync everysec: the right balance for most workloads
""")

    r.close()


# ── Phase 6: Cluster hash slots and hash tags ──────────────────────────────────

def phase6_cluster_slots_and_hash_tags():
    section("Phase 6: Redis Cluster — Hash Slots, Hash Tags, and Cross-Slot Limits")

    print("""
  Redis Cluster: 16,384 hash slots distributed across master nodes.
  Key routing: slot = CRC16(key) % 16384
  Client downloads slot→node mapping on connect; routes directly (no proxy).
  On MOVED error (stale mapping), client refreshes topology and retries.
  On ASK redirect (slot migration in progress), client sends ASKING then retries.
""")

    rc = connect_cluster()

    # Show live slot distribution from cluster
    print("  Live cluster topology:")
    try:
        nodes = rc.cluster_nodes()
        for node_id, info in nodes.items():
            role = "master" if "master" in info.get("flags", "") else "replica"
            slots = info.get("slots", [])
            slot_count = sum(e - s + 1 for s, e in slots) if slots else 0
            host = info.get("host", "?")
            port = info.get("port", "?")
            print(f"    {host}:{port}  {role:<8}  {slot_count:>5} slots")
    except Exception as e:
        print(f"    (topology unavailable: {e})")

    # Show hash slot assignment for sample keys
    sample_keys = [f"user:{i}" for i in range(1, 11)]
    print(f"\n  Hash slot assignment for sample keys:")
    print(f"  {'Key':<22} {'Slot':>6}  Note")
    print(f"  {'-'*22}  {'-'*6}  {'-'*30}")
    for key in sample_keys:
        slot = key_slot(key)
        rc.set(key, f"data-for-{key}", ex=60)
        print(f"  {key:<22}  {slot:>6}")

    # Hash tags — demonstrate co-location
    print(f"""
  Hash tags: force keys to the same slot for co-located multi-key ops.
  If a key contains {{tag}}, only the tag portion is CRC16-hashed.

  Example — WITHOUT hash tags (keys land on different slots → different nodes):""")

    keys_no_tag = ["user:42:profile", "user:42:settings", "user:42:sessions"]
    for k in keys_no_tag:
        slot = key_slot(k)
        print(f"    {k:<30} → slot {slot}")

    print(f"""
  Example — WITH hash tags (all land on the same slot → same node):""")

    keys_with_tag = ["{user:42}.profile", "{user:42}.settings", "{user:42}.sessions"]
    slots = set()
    for k in keys_with_tag:
        slot = key_slot(k)
        slots.add(slot)
        rc.set(k, f"data", ex=60)
        print(f"    {k:<30} → slot {slot}")

    same_slot = len(slots) == 1
    print(f"\n  All hash-tagged keys on same slot: {same_slot}")

    # Demonstrate cross-slot MGET fails without hash tags
    print(f"""
  Cross-slot operation behavior:
  - MGET without hash tags: keys span multiple nodes → cluster raises
    CROSSSLOT error (keys in request don't hash to the same slot)
  - MGET with hash tags: all keys on same slot → works correctly
""")

    # Safe: tagged keys are on same slot
    try:
        vals = rc.mget(*keys_with_tag)
        print(f"  MGET with hash tags: OK — returned {len([v for v in vals if v is not None])} values")
    except Exception as e:
        print(f"  MGET with hash tags: {e}")

    # This will fail in cluster mode (cross-slot)
    try:
        vals = rc.mget(*keys_no_tag)
        print(f"  MGET without hash tags: OK (all happened to land on same node)")
    except redis.exceptions.ResponseError as e:
        print(f"  MGET without hash tags: CROSSSLOT error (expected in cluster) — {e}")
    except Exception as e:
        print(f"  MGET without hash tags: {type(e).__name__}: {e}")

    rc.close()

    print("""
  Design rule for cluster mode:
  - Group related keys with a shared hash tag: {user:42}.profile, {user:42}.feed
  - This enables transactions (MULTI/EXEC), pipelines, and MGET across those keys
  - Trade-off: if all users share the same tag ({user}), all keys land on one slot
    → hot slot problem (next phase)
""")


# ── Phase 7: Hot key problem ──────────────────────────────────────────────────

def phase7_hot_key_problem():
    section("Phase 7: Hot Key Problem — Uneven Load and Mitigation Strategies")

    print("""
  The hot key problem: one key (or slot) receives disproportionate traffic.
  Common causes:
  - Trending content (viral tweet, popular product listing)
  - Poorly chosen hash tags (all user keys share same tag → one slot)
  - Thundering herd: cache miss for popular key → all requests hit DB simultaneously

  Symptoms:
  - One Redis node at 100% CPU while others are idle
  - P99 latency spike on that node's slot range
  - Client timeouts for keys on that node only
""")

    r1 = connect_single(7001)
    r2 = connect_single(7002)
    r3 = connect_single(7003)

    N_OPS = 500
    NODE_KEYS = {
        7001: "pipeline:key:0",   # from phase 1, likely on node 7001
        7002: "pipeline:key:5",
        7003: "pipeline:key:10",
    }

    print(f"  Simulating {N_OPS} reads to a single key on each node (hot key scenario):\n")
    timings = {}
    for port, conn in [(7001, r1), (7002, r2), (7003, r3)]:
        key = NODE_KEYS[port]
        conn.set(key, "hot-value", ex=300)
        t0 = time.perf_counter()
        for _ in range(N_OPS):
            conn.get(key)
        elapsed_ms = (time.perf_counter() - t0) * 1000
        timings[port] = elapsed_ms
        print(f"  Node :{port}  {N_OPS} reads in {elapsed_ms:.1f}ms  ({N_OPS * 1000 / elapsed_ms:,.0f} ops/s)")

    print(f"""
  Mitigation strategies for hot keys:

  1. Client-side caching (local in-process cache):
     Cache the hot value in application memory for 100ms.
     - Pro: zero Redis load for the duration of the local cache TTL
     - Con: stale data window; memory pressure in application tier

  2. Key sharding (read replicas via key suffix):
     Store value under N keys: hot-key:0, hot-key:1, ... hot-key:N-1
     Each reader picks a random shard: hot-key:{{random(0,N-1)}}
     - Pro: N× read throughput across N nodes
     - Con: write must update all N shards (fan-out write); consistency window

  3. Redis read replicas (replica routing):
     Configure client to route read commands to replicas.
     - Pro: linear read scale with replica count
     - Con: replication lag; replicas are eventually consistent

  4. Layered caching (L1 local + L2 Redis):
     App checks local caffeine/guava cache first; falls back to Redis.
     - Pro: eliminates hot key pressure for repeated reads within same pod
     - Con: cache invalidation across pods is complex (need pub/sub signal)

  5. Thundering herd (cache stampede) prevention:
     When a hot key expires, N threads all miss simultaneously and all query DB.
     Solutions:
       a. Probabilistic early expiration (PER): re-compute before TTL expires
          with probability ∝ 1/(TTL remaining). One thread refreshes early;
          others still get the stale value.
       b. Lock-based fetch: first thread acquires a lock and populates cache;
          others wait and read from cache on lock release.
       c. Stale-while-revalidate: return stale value immediately; async refresh.
""")

    r1.close()
    r2.close()
    r3.close()


# ── Main ─────────────────────────────────────────────────────────────────────

def main():
    section("DISTRIBUTED CACHE LAB — Redis Internals at Scale")
    print("""
  Architecture under test:
    3-node Redis Cluster (16,384 hash slots across 3 masters, no replicas)
    Each master owns ~5,461 slots. No replicas (lab constraint — 3 containers).

  Experiments:
    1. Pipeline batching    — eliminate network RTT overhead (10-100x speedup)
    2. LRU eviction         — hot vs cold key survival under memory pressure
    3. Pub/sub              — fire-and-forget fan-out with guaranteed ordering
    4. Distributed lock     — atomic Lua check-and-set (single-node Redlock)
    5. RDB snapshot         — BGSAVE timing, COW mechanics, AOF trade-offs
    6. Cluster hash slots   — key routing, hash tags, cross-slot MGET limits
    7. Hot key problem      — uneven load symptoms and mitigation patterns
""")

    wait_for_cluster()

    phase1_pipeline_batching()
    phase2_eviction()
    phase3_pubsub()
    phase4_distributed_lock()
    phase5_rdb_snapshot()
    phase6_cluster_slots_and_hash_tags()
    phase7_hot_key_problem()

    section("Lab Complete — Key Takeaways")
    print("""
  1. PIPELINING: eliminates N-1 network round trips. Network RTT dominates
     Redis latency — not CPU. Batch all reads you can in a single pipeline.
     Cluster caveat: all pipelined keys must be on the same node (use hash tags).

  2. EVICTION: allkeys-lru evicts "cold" (least recently used) keys first.
     allkeys-lfu is better for viral content (high-frequency keys survive longer).
     Approximate LRU is efficient; increase maxmemory-samples for accuracy.

  3. PUB/SUB: fire-and-forget, no persistence, no replay. Use Redis Streams
     (XADD/XREAD) when you need durable messaging or consumer groups.
     In Redis Cluster, use sharded pub/sub (Redis 7+) for cluster-wide fan-out.

  4. DISTRIBUTED LOCK: NX+EX is the correct atomic acquire pattern.
     Always release via Lua script — non-atomic check-then-delete has a
     race condition. For strong guarantees, use Redlock (N/2+1 majority)
     or ZooKeeper/etcd.

  5. PERSISTENCE: RDB for fast restarts (binary snapshot via COW fork).
     AOF for minimal data loss (≤1s with appendfsync everysec). Use both.
     Provision 2x working set memory to accommodate COW during BGSAVE.

  6. CLUSTER ROUTING: slot = CRC16(key) % 16384. Use hash tags to co-locate
     related keys on the same slot/node for multi-key operations and transactions.
     MOVED = stale topology (refresh and retry). ASK = migration in progress.

  7. HOT KEYS: single slot becomes bottleneck. Mitigate with: client-side
     caching, key sharding across N copies, replica read routing, or
     stale-while-revalidate to prevent thundering herd on cache miss.

  Next: 12-payment-system/ — exactly-once semantics with double-entry ledger
""")


if __name__ == "__main__":
    main()
