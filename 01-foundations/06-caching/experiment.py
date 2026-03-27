#!/usr/bin/env python3
"""
Caching Lab — Cache-Aside Pattern with Redis + PostgreSQL

Prerequisites: docker compose up -d (wait ~10s)

What this demonstrates:
  1. Cache-aside (lazy loading): check cache → miss → query DB → populate cache
  2. Latency comparison: DB-only vs cache-warmed reads
  3. Cache hit rate climbing as the hot working set warms up
  4. Cache invalidation: update DB, observe stale cache, then invalidate
  5. Cache stampede: concurrent requests race to populate a cold key (with vs without mutex)
  6. Cache penetration: non-existent keys bypass cache and hammer DB (null caching fix)
  7. Cache avalanche: mass key expiry — uniform TTL vs jittered TTL
  8. Hot key: a single key receiving disproportionate traffic (skewed access)
"""

import json
import math
import time
import random
import threading
import subprocess
from collections import Counter

try:
    import redis
    import psycopg2
    import psycopg2.extras
except ImportError:
    print("Installing dependencies...")
    subprocess.run(["pip", "install", "redis", "psycopg2-binary", "-q"], check=True)
    import redis
    import psycopg2
    import psycopg2.extras

REDIS_URL = "redis://localhost:6379"
PG_DSN    = "host=localhost port=5432 dbname=cachetest user=postgres password=postgres connect_timeout=5"
CACHE_TTL = 300  # 5 minutes


# ── Data Layer ─────────────────────────────────────────────────

def get_pg():
    conn = psycopg2.connect(PG_DSN)
    conn.autocommit = True
    return conn


def get_redis():
    return redis.Redis.from_url(REDIS_URL, decode_responses=True)


def setup(pg, r):
    with pg.cursor() as cur:
        cur.execute("""
            CREATE TABLE IF NOT EXISTS users (
                id         SERIAL PRIMARY KEY,
                name       TEXT NOT NULL,
                email      TEXT NOT NULL,
                updated_at TIMESTAMPTZ DEFAULT now()
            )
        """)
        cur.execute("TRUNCATE users RESTART IDENTITY")
        # Seed 1000 users
        args = [(f"User {i}", f"user{i}@example.com") for i in range(1, 1001)]
        psycopg2.extras.execute_batch(
            cur,
            "INSERT INTO users (name, email) VALUES (%s, %s)",
            args
        )
    r.flushall()
    print("  Seeded 1000 users in Postgres, flushed Redis cache.")


# ── Cache-Aside Pattern ────────────────────────────────────────

cache_hits   = 0
cache_misses = 0


def query_db(pg, user_id):
    """Simulates a DB read (index scan on primary key)."""
    with pg.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute("SELECT id, name, email FROM users WHERE id = %s", (user_id,))
        row = cur.fetchone()
        return dict(row) if row else None


def get_user(user_id, r, pg):
    """Cache-aside: check cache → miss → query DB → store in cache."""
    global cache_hits, cache_misses
    cached = r.get(f"user:{user_id}")
    if cached:
        cache_hits += 1
        return json.loads(cached), "HIT"
    # Cache miss
    cache_misses += 1
    user = query_db(pg, user_id)
    if user:
        r.setex(f"user:{user_id}", CACHE_TTL, json.dumps(user))
    return user, "MISS"


def section(title):
    print(f"\n{'=' * 60}")
    print(f"  {title}")
    print("=" * 60)


def main():
    section("CACHING LAB: Cache-Aside Pattern")
    print("""
  Cache-aside (lazy loading) flow:

    Client
      │
      ▼
    App ──► Redis ──► HIT? ──► return cached value
      │       │
      │      MISS
      │       │
      └──► Postgres ──► store in Redis ──► return value

  Goal: demonstrate latency reduction, hit rate behaviour,
        and the four main cache failure modes.
""")

    pg = get_pg()
    r  = get_redis()

    try:
        pg.cursor()
        r.ping()
    except Exception as e:
        print(f"  ERROR: {e}")
        print("  Run: docker compose up -d")
        return

    setup(pg, r)

    # ── Phase 1: DB-only baseline ──────────────────────────────────
    section("Phase 1: DB-Only Baseline (no cache)")

    READ_COUNT = 100
    user_ids = [random.randint(1, 1000) for _ in range(READ_COUNT)]

    start = time.perf_counter()
    for uid in user_ids:
        query_db(pg, uid)
    db_only_elapsed = time.perf_counter() - start
    db_only_avg = db_only_elapsed / READ_COUNT * 1000

    print(f"  {READ_COUNT} sequential DB reads: {db_only_elapsed:.3f}s  ({db_only_avg:.2f} ms/read)")
    print(f"  This is the cost every single request pays without a cache.")

    # ── Phase 2: Cache warming ─────────────────────────────────────
    section("Phase 2: Cache Warming — Hit Rate Over Time")

    print("""  80/20 rule: 20% of users receive 80% of traffic.
  The hot working set fits in the cache → hit rate climbs fast.
""")

    global cache_hits, cache_misses
    cache_hits = cache_misses = 0

    # Hot set: 20 users that receive 80% of traffic
    hot_users = random.sample(range(1, 1001), 20)
    hit_rates = []

    TOTAL_REQUESTS = 200
    start = time.perf_counter()
    for i in range(TOTAL_REQUESTS):
        uid = random.choice(hot_users) if random.random() < 0.8 else random.randint(1, 1000)
        get_user(uid, r, pg)
        if (i + 1) % 40 == 0:
            total = cache_hits + cache_misses
            hit_rate = cache_hits / total * 100
            hit_rates.append(hit_rate)
    cache_elapsed = time.perf_counter() - start

    print(f"  {TOTAL_REQUESTS} requests with 80/20 access pattern:")
    for i, rate in enumerate(hit_rates):
        bar = "#" * int(rate / 2)
        print(f"    After {(i+1)*40:4d} requests: hit rate = {rate:5.1f}%  |{bar:<50}|")

    total = cache_hits + cache_misses
    final_hit_rate = cache_hits / total * 100
    cache_avg = cache_elapsed / TOTAL_REQUESTS * 1000

    print(f"\n  Final: {cache_hits} hits / {cache_misses} misses = {final_hit_rate:.1f}% hit rate")
    print(f"  Cache-warmed avg latency: {cache_avg:.3f} ms/read  (vs {db_only_avg:.2f} ms DB-only)")
    if cache_avg < db_only_avg:
        speedup = db_only_avg / cache_avg
        print(f"  Speedup: {speedup:.1f}x faster with warm cache")

    print("""
  Key insight: hit rate is determined by the ratio of hot working-set
  size to cache size — NOT by cache size alone. Doubling cache is
  worthless if the working set is already fully cached.
""")

    # ── Phase 3: Cache invalidation ───────────────────────────────
    section("Phase 3: Cache Invalidation — Stale Data Problem")

    print("""  Phil Karlton: "There are only two hard things in Computer Science:
  cache invalidation and naming things."

  Scenario: DB is updated, but the cache is not. Reads return stale data.
""")
    # Warm the cache for user 42
    get_user(42, r, pg)
    cached_before = json.loads(r.get("user:42"))
    print(f"  Cached user 42: {cached_before}")

    # Update DB
    with pg.cursor() as cur:
        cur.execute("UPDATE users SET name='Alice (updated)', updated_at=now() WHERE id=42")

    cached_after = json.loads(r.get("user:42"))
    db_after     = query_db(pg, 42)

    print(f"\n  After DB update — divergence:")
    print(f"    Cache says: name = '{cached_after['name']}'   ← STALE (old data)")
    print(f"    DB    says: name = '{db_after['name']}'")
    stale_window = CACHE_TTL
    print(f"\n  Without explicit invalidation, clients read stale data for up to {stale_window}s (the TTL).")

    # Safe invalidation: delete AFTER the DB write
    r.delete("user:42")
    print(f"\n  Cache key deleted (r.delete('user:42'))")

    fresh, source = get_user(42, r, pg)
    print(f"  Next read: {source} → name = '{fresh['name']}'  ← fresh from DB, now re-cached")

    print("""
  Invalidation strategies (trade-off: simplicity vs consistency lag):
    1. Delete-on-write (cache-aside invalidation)  — simple; next read re-populates
    2. Write-through                               — write cache + DB atomically; no lag
    3. TTL expiry only                             — accept staleness up to TTL; zero code
    4. Event-driven (CDC via Debezium/Kafka)       — DB WAL drives invalidation; complex

  Safe delete pattern: always delete the key AFTER the DB write succeeds.
  If the process crashes between DB write and cache delete, the TTL acts
  as the fallback safety net — staleness is bounded, not infinite.
""")

    # ── Phase 4: Cache stampede ────────────────────────────────────
    section("Phase 4: Cache Stampede (Thundering Herd)")

    print("""  A hot key expires. N concurrent requests all miss the cache
  simultaneously and all query the database at once. Under production
  traffic this burst can saturate the DB connection pool instantly.
""")

    # Without mutex: all threads detect a miss and hit DB
    r.delete("user:1")
    db_queries_no_mutex = 0
    lock = threading.Lock()

    def stampede_no_mutex():
        nonlocal db_queries_no_mutex
        cached = r.get("user:1")
        if not cached:
            time.sleep(0.01)  # simulate DB query latency
            user = query_db(pg, 1)
            r.setex("user:1", CACHE_TTL, json.dumps(user))
            with lock:
                db_queries_no_mutex += 1

    NUM_CONCURRENT = 10
    threads = [threading.Thread(target=stampede_no_mutex) for _ in range(NUM_CONCURRENT)]
    for t in threads: t.start()
    for t in threads: t.join()

    # With mutex: only the winner queries DB; losers wait then read from cache
    r.delete("user:1")
    db_queries_with_mutex = 0

    def stampede_with_mutex():
        nonlocal db_queries_with_mutex
        mutex_key = "lock:user:1"
        acquired = r.set(mutex_key, "1", nx=True, ex=2)
        if acquired:
            # Winner: repopulate the cache
            try:
                time.sleep(0.01)  # simulate DB query latency
                user = query_db(pg, 1)
                r.setex("user:1", CACHE_TTL, json.dumps(user))
                with lock:
                    db_queries_with_mutex += 1
            finally:
                r.delete(mutex_key)
        else:
            # Loser: wait for winner to populate, then serve from cache
            # (retry up to 3 times with brief backoff before falling through to DB)
            for _ in range(3):
                time.sleep(0.02)
                cached = r.get("user:1")
                if cached:
                    return  # served from cache — no DB query
            # Fallback: mutex held too long; query DB directly
            user = query_db(pg, 1)
            with lock:
                db_queries_with_mutex += 1

    threads = [threading.Thread(target=stampede_with_mutex) for _ in range(NUM_CONCURRENT)]
    for t in threads: t.start()
    for t in threads: t.join()

    print(f"  {NUM_CONCURRENT} concurrent requests for the same expired key:")
    print(f"    Without mutex: {db_queries_no_mutex:2d} DB queries  ← every thread hammers the DB (stampede!)")
    print(f"    With mutex:    {db_queries_with_mutex:2d} DB queries  ← only the lock winner queries DB")
    reduction = (1 - db_queries_with_mutex / db_queries_no_mutex) * 100 if db_queries_no_mutex else 0
    print(f"    DB query reduction: {reduction:.0f}%")

    print("""
  Stampede solutions (pick based on acceptable complexity):
    1. Mutex / distributed lock (Redis SETNX + EX)  — exactly one writer; others wait
    2. Probabilistic early expiration               — randomly refresh before TTL expires
       Formula: re-cache if rand() < (elapsed / ttl) * beta  (XFetch algorithm)
    3. Background refresh                           — async worker refreshes hot keys proactively
    4. Staggered TTLs                               — add random jitter; reduces but doesn't eliminate
""")

    # ── Phase 5: Cache penetration ─────────────────────────────────
    section("Phase 5: Cache Penetration — Non-Existent Keys")

    print("""  Cache penetration: requests for keys that don't exist in the DB.
  The cache never stores a result → every request falls through to DB.
  An attacker querying millions of non-existent IDs bypasses the cache
  entirely, putting full DB load on what should be a cheap cache check.
""")

    # Flush any prior null-cache entry
    r.delete("user:-1")

    start = time.perf_counter()
    for _ in range(20):
        cached = r.get("user:-1")
        if not cached:
            query_db(pg, -1)  # returns None — no such user — but still hits DB
    penetration_elapsed = (time.perf_counter() - start) * 1000
    print(f"  20 requests for non-existent key (no null caching): {penetration_elapsed:.1f}ms  — 20 DB round-trips")

    # Fix: cache the null result with a short TTL
    r.setex("user:-1", 60, "__null__")
    start = time.perf_counter()
    db_hits = 0
    for _ in range(20):
        cached = r.get("user:-1")
        if cached == "__null__":
            pass  # null hit — no DB query needed
        elif cached:
            pass  # real cache hit
        else:
            query_db(pg, -1)
            db_hits += 1
    null_cached_elapsed = (time.perf_counter() - start) * 1000
    print(f"  20 requests for non-existent key (null caching):    {null_cached_elapsed:.1f}ms  — {db_hits} DB round-trips")
    print(f"  Speedup: {penetration_elapsed/null_cached_elapsed:.1f}x  (all 20 served from cache)")

    print("""
  Solutions:
    1. Null caching           — store "__null__" with a short TTL (30–60 s)
                                Memory cost: one key per non-existent ID queried
    2. Bloom filter           — probabilistic bitset; definitively rules out non-members
                                without a DB query; zero false negatives, tiny false-positive rate
                                (e.g., 1 MB Bloom filter can represent 10 M IDs at 0.1% error)
    3. Rate limiting          — reject excessive queries for the same non-existent key
    4. Request validation     — reject syntactically invalid IDs before touching cache or DB
""")

    # ── Phase 6: Cache avalanche — TTL jitter ─────────────────────
    section("Phase 6: Cache Avalanche — TTL Jitter")

    print("""  Cache avalanche: many keys expire at exactly the same time.
  This is common after a deploy that flushes the cache, or when keys
  are seeded in bulk during a warm-up — all with the same TTL.

  Compare: uniform TTL=5s vs jittered TTL=5s ± 2s
""")

    BASE_TTL   = 5      # seconds
    JITTER     = 2      # ± seconds
    NUM_KEYS   = 50
    CHECK_STEP = 0.5    # check expiry every 500 ms

    # --- Uniform TTL ---
    for i in range(NUM_KEYS):
        r.setex(f"aval:uniform:{i}", BASE_TTL, f"data_{i}")

    uniform_expired_by = {}   # second → cumulative expired count
    elapsed = 0.0
    while elapsed <= BASE_TTL + JITTER + 1:
        time.sleep(CHECK_STEP)
        elapsed += CHECK_STEP
        expired = sum(1 for i in range(NUM_KEYS) if r.get(f"aval:uniform:{i}") is None)
        uniform_expired_by[round(elapsed, 1)] = expired
        if expired == NUM_KEYS:
            break

    # --- Jittered TTL ---
    for i in range(NUM_KEYS):
        jittered = BASE_TTL + random.uniform(-JITTER, JITTER)
        r.setex(f"aval:jitter:{i}", max(1, int(jittered)), f"data_{i}")

    jitter_expired_by = {}
    elapsed = 0.0
    while elapsed <= BASE_TTL + JITTER + 1:
        time.sleep(CHECK_STEP)
        elapsed += CHECK_STEP
        expired = sum(1 for i in range(NUM_KEYS) if r.get(f"aval:jitter:{i}") is None)
        jitter_expired_by[round(elapsed, 1)] = expired
        if expired == NUM_KEYS:
            break

    # Find the second where the most keys expired in a single step (worst-case burst)
    def max_burst(series):
        times = sorted(series.keys())
        prev = 0
        worst_t, worst_delta = 0, 0
        for t in times:
            delta = series[t] - prev
            if delta > worst_delta:
                worst_delta, worst_t = delta, t
            prev = series[t]
        return worst_t, worst_delta

    uni_t, uni_burst   = max_burst(uniform_expired_by)
    jit_t, jit_burst   = max_burst(jitter_expired_by)

    # Print a timeline showing expiry spread
    print(f"  Key expiry timeline ({NUM_KEYS} keys, BASE_TTL={BASE_TTL}s, JITTER=±{JITTER}s):\n")
    all_times = sorted(set(list(uniform_expired_by.keys()) + list(jitter_expired_by.keys())))
    print(f"    {'Time':>6}  {'Uniform expired':>16}  {'Jittered expired':>16}")
    print(f"    {'------':>6}  {'----------------':>16}  {'----------------':>16}")
    prev_u = prev_j = 0
    for t in all_times:
        u = uniform_expired_by.get(t, prev_u)
        j = jitter_expired_by.get(t, prev_j)
        delta_u = u - prev_u
        delta_j = j - prev_j
        # Highlight rows where at least one expiry happened
        if delta_u > 0 or delta_j > 0:
            bar_u = "#" * delta_u
            bar_j = "#" * delta_j
            print(f"    {t:>6.1f}s  {delta_u:>5} new (+{bar_u:<20})  {delta_j:>5} new (+{bar_j:<20})")
        prev_u, prev_j = u, j

    print(f"\n  Worst-case burst:")
    print(f"    Uniform TTL:  {uni_burst} keys expired in one {CHECK_STEP}s window (at t={uni_t}s)")
    print(f"    Jittered TTL: {jit_burst} keys expired in one {CHECK_STEP}s window (at t={jit_t}s)")
    burst_reduction = (1 - jit_burst / uni_burst) * 100 if uni_burst else 0
    print(f"    Peak burst reduction: {burst_reduction:.0f}%")

    # Cleanup avalanche keys
    for i in range(NUM_KEYS):
        r.delete(f"aval:uniform:{i}")
        r.delete(f"aval:jitter:{i}")

    print("""
  Fix: TTL = base_ttl + random.uniform(-jitter, jitter)
  This spreads expiry across a window instead of a single instant,
  converting a spike of N simultaneous DB queries into a smooth ramp.

  At FAANG scale: a cache flush after a deploy without jitter can produce
  tens of thousands of simultaneous DB queries — enough to saturate the
  connection pool and trigger a full site outage.
""")

    # ── Phase 7: Hot key problem ───────────────────────────────────
    section("Phase 7: Hot Key — Skewed Access Under Concurrency")

    print("""  Hot key problem: a single cache key receives a disproportionate
  share of all traffic. Even with a cache hit, a single Redis node
  handling 500K req/s for one key becomes a throughput bottleneck.

  Common examples: a celebrity post, a trending product, a global config key.

  Mitigation strategies:
    1. Local in-process cache (LRU dict/functools.lru_cache) — absorbs reads
       before they reach Redis. Staleness = local TTL (typically 1–5 s).
    2. Key replication — store the value under N keys ("key:0".."key:N-1")
       and read from key:shard_id to spread load across Redis shards.
    3. Read replicas — route hot-key reads to Redis replicas, writes to primary.
""")

    HOT_KEY   = "config:global"
    HOT_VALUE = json.dumps({"feature_flags": {"new_ui": True, "dark_mode": False}})
    r.set(HOT_KEY, HOT_VALUE)

    N_THREADS  = 20
    N_READS    = 50   # per thread — 1000 total reads of the same key
    local_cache: dict = {}
    LOCAL_TTL  = 1.0  # seconds

    hits_redis      = Counter()
    hits_local      = Counter()
    read_lock       = threading.Lock()

    def read_hot_key(strategy: str):
        for _ in range(N_READS):
            if strategy == "redis_only":
                r.get(HOT_KEY)
                with read_lock:
                    hits_redis["redis_only"] += 1
            else:  # local_cache_first
                now = time.monotonic()
                cached = local_cache.get(HOT_KEY)
                if cached and now - cached[1] < LOCAL_TTL:
                    with read_lock:
                        hits_local["local"] += 1
                else:
                    val = r.get(HOT_KEY)
                    local_cache[HOT_KEY] = (val, now)
                    with read_lock:
                        hits_redis["local_fallback"] += 1

    # Redis-only: all 1000 reads go to Redis
    start = time.perf_counter()
    threads = [threading.Thread(target=read_hot_key, args=("redis_only",)) for _ in range(N_THREADS)]
    for t in threads: t.start()
    for t in threads: t.join()
    redis_only_elapsed = time.perf_counter() - start

    # Local cache first: Redis reads only on cache miss or TTL expiry
    local_cache.clear()
    start = time.perf_counter()
    threads = [threading.Thread(target=read_hot_key, args=("local_first",)) for _ in range(N_THREADS)]
    for t in threads: t.start()
    for t in threads: t.join()
    local_first_elapsed = time.perf_counter() - start

    total_reads = N_THREADS * N_READS
    redis_calls_local = hits_redis["local_fallback"]
    print(f"  {total_reads} reads of the same hot key ({N_THREADS} threads × {N_READS} reads):")
    print(f"    Redis-only:         {hits_redis['redis_only']:5d} Redis calls  in {redis_only_elapsed:.3f}s")
    print(f"    Local cache first:  {redis_calls_local:5d} Redis calls  in {local_first_elapsed:.3f}s"
          f"  ({hits_local['local']} served from local cache)")
    if redis_calls_local > 0:
        reduction = (1 - redis_calls_local / hits_redis["redis_only"]) * 100
        print(f"    Redis call reduction: {reduction:.0f}%")

    print("""
  Trade-off: local cache introduces a second staleness layer (local TTL
  on top of Redis TTL). For config or feature flags this is acceptable;
  for financial data or permissions it is not.
""")

    # ── Summary ───────────────────────────────────────────────────
    section("Summary")
    print("""
  Caching Patterns:
  ─────────────────────────────────────────────────────────────
  Cache-aside (lazy loading)  — app manages cache; misses query DB
  Write-through               — writes go to cache + DB (synchronous)
  Write-behind (write-back)   — write to cache; async flush to DB
  Read-through                — cache layer handles DB reads transparently

  Eviction Policies (Redis maxmemory-policy):
    allkeys-lru      — evict least-recently-used across ALL keys (recommended for caches)
    volatile-lru     — LRU eviction only on keys with TTL set
    allkeys-lfu      — evict least-frequently-used (better for highly skewed workloads)
    volatile-ttl     — evict keys with shortest remaining TTL first
    noeviction       — reject writes when memory full (use for session stores, not caches)

  Cache Failure Modes and Mitigations:
  ┌─────────────────┬────────────────────────────────────────────────────┐
  │ Failure         │ Mitigation                                         │
  ├─────────────────┼────────────────────────────────────────────────────┤
  │ Stampede        │ Distributed mutex (SETNX), probabilistic early     │
  │ (thundering     │ expiration (XFetch), background proactive refresh  │
  │  herd)          │                                                    │
  ├─────────────────┼────────────────────────────────────────────────────┤
  │ Penetration     │ Null caching (__null__ sentinel), Bloom filter,    │
  │ (non-existent   │ request validation, rate limiting                  │
  │  keys)          │                                                    │
  ├─────────────────┼────────────────────────────────────────────────────┤
  │ Avalanche       │ Random TTL jitter (±20–30% of base TTL), gradual  │
  │ (mass expiry)   │ cache warm-up, staggered deploys                   │
  ├─────────────────┼────────────────────────────────────────────────────┤
  │ Hot key         │ Local in-process cache (lru_cache), key sharding,  │
  │ (skewed access) │ Redis read replicas, CDN for public data           │
  ├─────────────────┼────────────────────────────────────────────────────┤
  │ Stale data      │ Delete-on-write, write-through, event-driven       │
  │ (invalidation)  │ invalidation via CDC (Debezium + Kafka)            │
  └─────────────────┴────────────────────────────────────────────────────┘

  When NOT to cache:
    - Financial data requiring strict consistency (balances, permissions)
    - Unique-per-user data with very low reuse (personalised real-time feeds)
    - Data that changes more often than it is read (write:read ratio > 1)
    - Very small datasets that fit comfortably in the DB buffer pool

  Next steps: ../07-load-balancing/
""")


if __name__ == "__main__":
    main()
