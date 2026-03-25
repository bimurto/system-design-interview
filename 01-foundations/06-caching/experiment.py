#!/usr/bin/env python3
"""
Caching Lab — Cache-Aside Pattern with Redis + PostgreSQL

Prerequisites: docker compose up -d (wait ~10s)

What this demonstrates:
  1. Cache-aside (lazy loading): check cache → miss → query DB → populate cache
  2. Latency comparison: DB-only vs cache-warmed reads
  3. Cache hit rate climbing as cache warms up
  4. Cache invalidation: update DB, observe stale cache, then invalidate
  5. Cache stampede: multiple concurrent requests race to populate a cold key
"""

import json
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
    """Simulates a slow DB read with a small delay."""
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
  Cache-aside (lazy loading):
    1. App checks cache first
    2. On miss: read from DB, store in cache, return result
    3. On hit: return cached value directly

  Goal: demonstrate latency reduction and hit rate behavior.
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

    print(f"  {READ_COUNT} sequential DB reads: {db_only_elapsed:.3f}s ({db_only_avg:.2f} ms/read)")

    # ── Phase 2: Cache warming ─────────────────────────────────────
    section("Phase 2: Cache Warming — Hit Rate Over Time")

    global cache_hits, cache_misses
    cache_hits = cache_misses = 0

    # Use a hot set of 20 users (accessed repeatedly, like a 80/20 distribution)
    hot_users = random.sample(range(1, 1001), 20)
    hit_rates = []

    TOTAL_REQUESTS = 200
    start = time.perf_counter()
    for i in range(TOTAL_REQUESTS):
        # 80% chance to access hot user
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
        print(f"    After {(i+1)*40:4d} requests: hit rate = {rate:5.1f}%  {bar}")

    total = cache_hits + cache_misses
    final_hit_rate = cache_hits / total * 100
    cache_avg = cache_elapsed / TOTAL_REQUESTS * 1000

    print(f"\n  Final: {cache_hits} hits / {cache_misses} misses = {final_hit_rate:.1f}% hit rate")
    print(f"  Cache-warmed avg latency: {cache_avg:.2f} ms/read  (vs {db_only_avg:.2f} ms DB-only)")
    if cache_avg < db_only_avg:
        print(f"  Speedup: {db_only_avg/cache_avg:.1f}x faster with cache")

    # ── Phase 3: Cache invalidation ───────────────────────────────
    section("Phase 3: Cache Invalidation")

    print("""  The hardest problem in computer science.
  If you update the DB but not the cache, reads return stale data.
""")
    # Warm the cache for user 42
    get_user(42, r, pg)
    cached_before = r.get("user:42")
    print(f"  Cached value for user 42: {cached_before}")

    # Update DB (name change)
    with pg.cursor() as cur:
        cur.execute("UPDATE users SET name='Alice (updated)', updated_at=now() WHERE id=42")

    # Read from cache — still stale!
    cached_after_update = r.get("user:42")
    db_after_update = query_db(pg, 42)

    print(f"\n  After DB update:")
    print(f"    Cache  says: {cached_after_update}  ← STALE")
    print(f"    DB     says: {json.dumps(db_after_update)}")

    # Invalidate the cache key
    r.delete("user:42")
    print(f"\n  Cache invalidated (r.delete('user:42'))")

    fresh, source = get_user(42, r, pg)
    print(f"  Next read: {source} → {json.dumps(fresh)}")
    print("  Cache now has the fresh value.")

    print("""
  Invalidation strategies:
    1. Delete on write (cache-aside invalidation): simplest
    2. Write-through: write to cache AND DB simultaneously
    3. TTL expiry: accept staleness up to TTL; no explicit invalidation
    4. Event-driven: DB triggers or CDC (Debezium) push invalidation events
""")

    # ── Phase 4: Cache stampede ────────────────────────────────────
    section("Phase 4: Cache Stampede (Thundering Herd)")

    print("""  Cache stampede: a popular key expires → many concurrent requests
  all miss the cache simultaneously → all query the DB at once.
  Under high traffic this can overwhelm the database.
""")

    # Expire the hot key
    r.delete("user:1")
    db_queries = Counter()
    lock = threading.Lock()

    def stampede_get(thread_id, use_mutex=False):
        """Simulate concurrent requests for the same key."""
        nonlocal db_queries
        if use_mutex:
            mutex_key = "lock:user:1"
            # Simple setnx-based mutex
            acquired = r.set(mutex_key, "1", nx=True, ex=2)
            if acquired:
                # Winner: query DB and populate cache
                time.sleep(0.01)  # simulate DB query
                user = query_db(pg, 1)
                r.setex("user:1", CACHE_TTL, json.dumps(user))
                r.delete(mutex_key)
                with lock:
                    db_queries["with_mutex"] += 1
            else:
                # Loser: wait briefly and retry from cache
                time.sleep(0.015)
                cached = r.get("user:1")
                if not cached:
                    with lock:
                        db_queries["with_mutex"] += 1  # fallback to DB
        else:
            cached = r.get("user:1")
            if not cached:
                time.sleep(0.01)  # simulate DB query
                user = query_db(pg, 1)
                r.setex("user:1", CACHE_TTL, json.dumps(user))
                with lock:
                    db_queries["no_mutex"] += 1

    NUM_CONCURRENT = 10

    # Without mutex: all threads hit DB
    r.delete("user:1")
    threads = [threading.Thread(target=stampede_get, args=(i, False)) for i in range(NUM_CONCURRENT)]
    for t in threads: t.start()
    for t in threads: t.join()

    # With mutex: only one thread hits DB
    r.delete("user:1")
    threads = [threading.Thread(target=stampede_get, args=(i, True)) for i in range(NUM_CONCURRENT)]
    for t in threads: t.start()
    for t in threads: t.join()

    print(f"  {NUM_CONCURRENT} concurrent requests for expired key:")
    print(f"    Without mutex: {db_queries['no_mutex']} DB queries  ← stampede!")
    print(f"    With mutex:    {db_queries['with_mutex']} DB queries  ← protected")
    print("""
  Stampede solutions:
    1. Mutex / distributed lock (Redis SETNX): only one thread repopulates
    2. Probabilistic early expiration: randomly refresh before TTL expires
    3. Background refresh: async worker keeps hot keys fresh
    4. Staggered TTLs: add random jitter (TTL = base + rand(0, 60s))
""")

    # ── Phase 5: Cache penetration ─────────────────────────────────
    section("Phase 5: Cache Penetration")

    print("""  Cache penetration: requests for keys that don't exist in DB.
  Every request misses cache AND misses DB → DB is hammered.

  Example: attacker queries user_id=-1 repeatedly.
""")
    start = time.perf_counter()
    for _ in range(20):
        cached = r.get("user:-1")
        if not cached:
            result = query_db(pg, -1)
            # result is None — no user with id=-1
            # WITHOUT null caching: always goes to DB
    penetration_elapsed = (time.perf_counter() - start) * 1000
    print(f"  20 requests for non-existent key (no null caching): {penetration_elapsed:.1f}ms total")

    # WITH null caching: cache the None result
    r.setex("user:-1", 60, "null")  # cache the null result
    start = time.perf_counter()
    for _ in range(20):
        cached = r.get("user:-1")
        if cached and cached != "null":
            pass  # real hit
        elif cached == "null":
            pass  # null hit — no DB query
        else:
            query_db(pg, -1)
    null_cached_elapsed = (time.perf_counter() - start) * 1000
    print(f"  20 requests for non-existent key (null caching):    {null_cached_elapsed:.1f}ms total")
    print(f"  Speedup: {penetration_elapsed/null_cached_elapsed:.1f}x")
    print("""
  Solutions:
    1. Cache null results (null caching): store "null" with short TTL
    2. Bloom filter: probabilistic structure; if key not in filter, skip DB
    3. Rate limiting: reject excessive queries for the same non-existent key
""")

    # ── Summary ───────────────────────────────────────────────────
    section("Summary")
    print("""
  Caching Patterns:
  ─────────────────────────────────────────────────────────────
  Cache-aside (lazy loading)  — app reads cache; misses query DB
  Write-through               — writes go to cache + DB (synchronous)
  Write-behind (write-back)   — write to cache; async flush to DB
  Read-through                — cache layer handles DB reads transparently

  Eviction Policies:
    LRU   — evict least recently used (most common)
    LFU   — evict least frequently used (better for skewed access)
    TTL   — expire after fixed time (simple, predictable)
    FIFO  — evict oldest entry (rarely used)

  Cache Problems and Solutions:
    Stampede    → mutex, staggered TTLs, background refresh
    Penetration → null caching, Bloom filters
    Avalanche   → random TTL jitter (avoid mass expiry at same time)
    Invalidation → delete-on-write, TTL, event-driven (CDC)

  When NOT to cache:
    - Financial data requiring strict consistency
    - Unique-per-user data with low reuse
    - Data that changes more often than it's read
    - Very small datasets (DB is fast enough)

  Next steps: ../07-load-balancing/
""")


if __name__ == "__main__":
    main()
