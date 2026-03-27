#!/usr/bin/env python3
"""
Databases Lab — SQL vs NoSQL: Postgres, MongoDB, Redis

Prerequisites: docker compose up -d (wait ~15s for all services to be healthy)

What this demonstrates:
  1. Postgres: normalized schema with FK, JOIN query for user+posts
  2. MongoDB: document model with embedded posts array (schema evolution demo)
  3. Redis: Hash + List data structures, pipeline batching
  4. Benchmark: write 100 users + 5 posts each, read user+posts 100 times
  5. N+1 problem: 101 queries vs 1 JOIN — measured difference
  6. Schema evolution: ALTER TABLE (Postgres) vs $set (MongoDB) vs HSET (Redis)
  7. Connection pool exhaustion: what happens when Postgres max_connections is hit
"""

import json
import time
import random
import string
import subprocess
import threading
from contextlib import contextmanager

try:
    import psycopg2
    import psycopg2.extras
    import psycopg2.pool
    import redis
    from pymongo import MongoClient
except ImportError:
    print("Installing dependencies...")
    subprocess.run(
        ["pip", "install", "psycopg2-binary", "redis", "pymongo", "-q"],
        check=True,
    )
    import psycopg2
    import psycopg2.extras
    import psycopg2.pool
    import redis
    from pymongo import MongoClient


PG_DSN    = "host=localhost port=5432 dbname=dbtest user=postgres password=postgres connect_timeout=5"
REDIS_URL = "redis://localhost:6379"
MONGO_URL = "mongodb://localhost:27017"

NUM_USERS      = 100
POSTS_PER_USER = 5


# ── Helpers ────────────────────────────────────────────────────

def section(title: str) -> None:
    print(f"\n{'=' * 66}")
    print(f"  {title}")
    print("=" * 66)


def rand_str(n: int = 8) -> str:
    return "".join(random.choices(string.ascii_lowercase, k=n))


def check_services() -> list[str]:
    """Verify all three databases are reachable before running the lab."""
    errors = []
    try:
        conn = psycopg2.connect(PG_DSN)
        conn.close()
    except Exception as e:
        errors.append(f"Postgres: {e}")
    try:
        r = redis.Redis.from_url(REDIS_URL)
        r.ping()
    except Exception as e:
        errors.append(f"Redis: {e}")
    try:
        c = MongoClient(MONGO_URL, serverSelectionTimeoutMS=3_000)
        c.admin.command("ping")
        c.close()
    except Exception as e:
        errors.append(f"MongoDB: {e}")
    return errors


# ── PostgreSQL ─────────────────────────────────────────────────

def pg_setup(conn: "psycopg2.connection") -> None:
    with conn.cursor() as cur:
        cur.execute("DROP TABLE IF EXISTS posts CASCADE")
        cur.execute("DROP TABLE IF EXISTS users CASCADE")
        cur.execute("""
            CREATE TABLE users (
                id      SERIAL PRIMARY KEY,
                name    TEXT NOT NULL,
                email   TEXT NOT NULL UNIQUE,
                age     INT
            )
        """)
        cur.execute("""
            CREATE TABLE posts (
                id         SERIAL PRIMARY KEY,
                user_id    INT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                title      TEXT NOT NULL,
                body       TEXT NOT NULL,
                created_at TIMESTAMPTZ DEFAULT now()
            )
        """)
        # Covering index: speeds up "give me all posts for user X"
        cur.execute("CREATE INDEX idx_posts_user_id ON posts(user_id)")
    conn.commit()


def pg_write_users(conn: "psycopg2.connection", users: list[dict]) -> dict[str, int]:
    """
    Insert users in a single batch and return {email: id} mapping.

    NOTE: execute_batch does NOT accumulate RETURNING results — it issues
    many small batches internally and discards per-row output. We retrieve
    IDs with a separate SELECT after the batch completes.
    """
    with conn.cursor() as cur:
        psycopg2.extras.execute_batch(
            cur,
            "INSERT INTO users (name, email, age) VALUES (%s, %s, %s)",
            [(u["name"], u["email"], u["age"]) for u in users],
        )
        cur.execute("SELECT id, email FROM users ORDER BY id")
        rows = cur.fetchall()
    conn.commit()
    return {email: uid for uid, email in rows}


def pg_write_posts(conn: "psycopg2.connection", posts: list[dict]) -> None:
    with conn.cursor() as cur:
        psycopg2.extras.execute_batch(
            cur,
            "INSERT INTO posts (user_id, title, body) VALUES (%s, %s, %s)",
            [(p["user_id"], p["title"], p["body"]) for p in posts],
        )
    conn.commit()


def pg_read_user_with_posts(conn: "psycopg2.connection", user_id: int) -> dict | None:
    """Fetch a user and all their posts in a single JOIN query."""
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(
            """
            SELECT u.id, u.name, u.email, u.age,
                   p.id AS post_id, p.title, p.body
            FROM   users u
            LEFT   JOIN posts p ON p.user_id = u.id
            WHERE  u.id = %s
            """,
            (user_id,),
        )
        rows = cur.fetchall()
    if not rows:
        return None
    user = {
        "id":    rows[0]["id"],
        "name":  rows[0]["name"],
        "email": rows[0]["email"],
        "age":   rows[0]["age"],
        "posts": [],
    }
    for row in rows:
        if row["post_id"]:
            user["posts"].append(
                {"id": row["post_id"], "title": row["title"], "body": row["body"]}
            )
    return user


def pg_demo_n_plus_one(conn: "psycopg2.connection", user_ids: list[int]) -> None:
    """
    Concretely measure the N+1 query anti-pattern vs a JOIN.

    N+1: fetch N user IDs, then issue 1 query per user to get their post
    count.  Total: N+1 round-trips to the database.

    JOIN: one query returns all user IDs with their post counts.
    Total: 1 round-trip.
    """
    sample = user_ids[:20]

    # Bad path: N+1
    query_count_n1 = 0
    start = time.perf_counter()
    with conn.cursor() as cur:
        cur.execute("SELECT id FROM users LIMIT 20")
        ids = [r[0] for r in cur.fetchall()]
        query_count_n1 += 1
        for uid in ids:
            cur.execute("SELECT COUNT(*) FROM posts WHERE user_id = %s", (uid,))
            cur.fetchone()
            query_count_n1 += 1
    n1_ms = (time.perf_counter() - start) * 1_000

    # Good path: single JOIN
    start = time.perf_counter()
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT u.id, COUNT(p.id) AS post_count
            FROM   users u
            LEFT   JOIN posts p ON p.user_id = u.id
            WHERE  u.id = ANY(%s)
            GROUP  BY u.id
            """,
            (sample,),
        )
        cur.fetchall()
    join_ms = (time.perf_counter() - start) * 1_000

    speedup = n1_ms / join_ms if join_ms > 0 else float("inf")
    print(f"  N+1 pattern ({query_count_n1} round-trips): {n1_ms:.1f}ms")
    print(f"  JOIN query  (1 round-trip):    {join_ms:.1f}ms")
    print(f"  JOIN is {speedup:.1f}x faster for {len(sample)} users")
    print("""
  At 1,000 users the N+1 approach issues 1,001 queries. With 5ms avg
  network latency to a remote DB, that is 5,005ms vs ~5ms for the JOIN.
  This is the most common performance anti-pattern in ORMs — always
  check the query count, not just execution time in local tests.
""")


def pg_demo_schema_migration(conn: "psycopg2.connection") -> None:
    """
    Show the zero-downtime pattern for adding a NOT NULL column to Postgres.

    Step 1: Add column as nullable (fast — no table rewrite on Postgres 11+)
    Step 2: Backfill in batches (no table lock)
    Step 3: Add NOT NULL constraint (fast via CHECK CONSTRAINT trick on PG 12+)
    """
    with conn.cursor() as cur:
        # Step 1 — add nullable column (instant on PG 11+)
        cur.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS bio TEXT")
        conn.commit()
        print("  Step 1: ALTER TABLE ADD COLUMN bio TEXT (nullable) — instant")

        # Step 2 — backfill in batches (simulate with small batch)
        cur.execute(
            "UPDATE users SET bio = 'No bio yet.' WHERE bio IS NULL AND id <= 50"
        )
        conn.commit()
        cur.execute(
            "UPDATE users SET bio = 'No bio yet.' WHERE bio IS NULL AND id > 50"
        )
        conn.commit()
        print("  Step 2: backfill in two batches — no exclusive table lock")

        # Step 3 — enforce NOT NULL without full rewrite (PG 12+ skips validation scan
        # when a CHECK CONSTRAINT already covers the column)
        cur.execute(
            "ALTER TABLE users ALTER COLUMN bio SET DEFAULT 'No bio yet.'"
        )
        conn.commit()
        print("  Step 3: set DEFAULT so new rows always have bio — no migration needed")

        # Verify
        cur.execute("SELECT COUNT(*) FROM users WHERE bio IS NULL")
        nulls = cur.fetchone()[0]
        print(f"  Rows still NULL after migration: {nulls}  (expected: 0)")

    print("""
  Production zero-downtime ADD NOT NULL column recipe:
    1. ALTER TABLE ... ADD COLUMN new_col TYPE         -- nullable, instant
    2. Deploy app code that writes new_col on new records
    3. Backfill in batches:  UPDATE ... WHERE id BETWEEN ? AND ? AND new_col IS NULL
    4. ALTER TABLE ... ALTER COLUMN new_col SET NOT NULL
       (On PG 12+: use NOT VALID + VALIDATE CONSTRAINT to avoid full-table scan)
    5. Never run step 4 without step 3 on a large table — it will lock the table.
""")


# ── MongoDB ────────────────────────────────────────────────────

def mongo_setup(db: "MongoClient") -> None:
    db.users.drop()


def mongo_write_users(db: "MongoClient", users: list[dict]) -> list:
    result = db.users.insert_many(users)
    return result.inserted_ids


def mongo_read_user_with_posts(db: "MongoClient", user_id) -> dict | None:
    return db.users.find_one({"_id": user_id})


def mongo_demo_schema_evolution(db: "MongoClient") -> None:
    """
    Demonstrate that different documents in the same collection can have
    different shapes — no ALTER TABLE required.

    This is the core flexibility of the document model, and also its risk:
    without validation, schema drift accumulates over years of iteration.
    """
    # Add 'bio' to one document only
    db.users.update_one({"name": "User 0"}, {"$set": {"bio": "I love systems design!"}})

    doc_with_bio    = db.users.find_one({"name": "User 0"},  {"name": 1, "bio": 1, "_id": 0})
    doc_without_bio = db.users.find_one({"name": "User 1"},  {"name": 1, "bio": 1, "_id": 0})

    print(f"  User 0 (updated):  {doc_with_bio}")
    print(f"  User 1 (untouched): {doc_without_bio}  ← no 'bio' field, no error")
    print("""
  MongoDB allows heterogeneous documents in the same collection.
  This enables rapid schema iteration but creates "schema drift":
  after years of feature adds, a collection may have 15 optional fields
  where only 3 exist on any given document. Mitigate with:
    - JSON Schema validation (db.createCollection with validator)
    - Pydantic models in application code
    - Explicit migration scripts when field meanings change
""")


# ── Redis ──────────────────────────────────────────────────────

def redis_setup(r: redis.Redis) -> None:
    r.flushdb()


def redis_write_user(r: redis.Redis, user_id: int, user: dict, posts: list[dict]) -> None:
    """
    Model user+posts in Redis:
      user:{id}        → Hash  { name, email, age }
      user:{id}:posts  → List  [ JSON(post), ... ]

    Using a pipeline batches all commands into one TCP round-trip.
    Without pipeline: 1 + POSTS_PER_USER round-trips per user.
    With pipeline:    1 round-trip per user.
    """
    pipe = r.pipeline()
    pipe.hset(f"user:{user_id}", mapping={
        "name":  user["name"],
        "email": user["email"],
        "age":   str(user["age"]),
    })
    for post in posts:
        pipe.rpush(f"user:{user_id}:posts", json.dumps(post))
    pipe.execute()


def redis_read_user_with_posts(r: redis.Redis, user_id: int) -> dict | None:
    pipe = r.pipeline()
    pipe.hgetall(f"user:{user_id}")
    pipe.lrange(f"user:{user_id}:posts", 0, -1)
    user_data, posts_raw = pipe.execute()
    if not user_data:
        return None
    return {
        "id":    user_id,
        **user_data,
        "posts": [json.loads(p) for p in posts_raw],
    }


def redis_demo_eviction(r: redis.Redis) -> None:
    """
    The docker-compose.yml caps Redis at 64 MB with allkeys-lru eviction.
    Write enough data to approach the limit and show keys being evicted.

    This demonstrates why Redis should NOT be your primary database for
    data that must survive memory pressure.
    """
    print("  Writing 5,000 large keys to approach the 64MB memory limit...")
    pipe = r.pipeline()
    large_val = "x" * 10_000  # ~10 KB per key
    for i in range(5_000):
        pipe.set(f"eviction_test:{i}", large_val, ex=60)
    pipe.execute()

    info = r.info("memory")
    used_mb   = info["used_memory"] / (1024 * 1024)
    max_mem   = info.get("maxmemory", 0)
    max_mb    = max_mem / (1024 * 1024) if max_mem else "unlimited"
    evicted   = r.info("stats").get("evicted_keys", 0)

    print(f"  Memory used:  {used_mb:.1f} MB")
    print(f"  Memory limit: {max_mb} MB  (set via --maxmemory in docker-compose.yml)")
    print(f"  Keys evicted: {evicted}")
    print("""
  allkeys-lru policy evicts the least-recently-used key when the limit
  is reached — old cache entries are dropped automatically. This is ideal
  for cache-only Redis. For a primary store, use noeviction (writes start
  failing at the limit, which is safer than silently losing data).

  Key insight: if you see evicted_keys > 0 in production, either:
    a) Redis memory is undersized for the working set, or
    b) TTLs are too long and stale data is consuming RAM.
""")
    # Clean up test keys so subsequent reads still work
    pipe = r.pipeline()
    for i in range(5_000):
        pipe.delete(f"eviction_test:{i}")
    pipe.execute()


# ── Connection Pool Exhaustion Demo ───────────────────────────

def pg_demo_connection_exhaustion() -> None:
    """
    The docker-compose.yml sets max_connections=50 for Postgres.
    Attempt to open 55 connections to show what happens when the limit
    is exceeded — and why PgBouncer / connection pooling is essential.
    """
    connections = []
    failed_at = None
    try:
        for i in range(55):
            c = psycopg2.connect(PG_DSN)
            connections.append(c)
    except psycopg2.OperationalError as e:
        failed_at = len(connections)
        print(f"  Connection #{failed_at + 1} failed: {str(e).strip()}")
    finally:
        for c in connections:
            try:
                c.close()
            except Exception:
                pass

    if failed_at is not None:
        print(f"  Successfully opened {failed_at} connections, then hit the limit.")
    else:
        print("  All 55 connections succeeded (limit not reached at this pool size).")
    print("""
  In production, application servers open connection pools (e.g., 10 per
  process). With 20 app servers x 10 connections = 200 connections, but
  Postgres default max_connections=100. Result: connection errors under load.

  Solution: PgBouncer in transaction pooling mode multiplexes thousands of
  client connections over a small pool of actual Postgres connections.
  PgBouncer is nearly mandatory at production scale.
""")


# ── Main ───────────────────────────────────────────────────────

def main() -> None:
    section("DATABASES LAB: SQL vs NoSQL")
    print("""
  Three databases, same data model: users with posts.

  Postgres  — relational (ACID, normalized schema, JOIN queries)
  MongoDB   — document   (flexible schema, embedded documents)
  Redis     — key-value  (in-memory, data structures, pipeline)

  Lab sections:
    1. Write benchmark: 100 users + 5 posts each
    2. Read benchmark:  user+posts fetch x100
    3. N+1 query anti-pattern vs JOIN (Postgres)
    4. Schema evolution: ALTER TABLE vs $set vs HSET
    5. Redis eviction under memory pressure
    6. Postgres connection pool exhaustion
    7. Summary comparison table
""")

    errors = check_services()
    if errors:
        print("  ERROR: Some services are not ready:")
        for e in errors:
            print(f"    {e}")
        print("  Run: docker compose up -d && sleep 15")
        return
    print("  All services ready.\n")

    # ── Generate test data ─────────────────────────────────────
    users = [
        {
            "name":  f"User {i}",
            "email": f"user{i}_{rand_str()}@example.com",
            "age":   random.randint(18, 65),
        }
        for i in range(NUM_USERS)
    ]

    # ── Phase 1: PostgreSQL ────────────────────────────────────
    section("Phase 1: PostgreSQL — Normalized Relational Schema")
    print("""
  Schema (two tables, one foreign key relationship):
    users (id PK, name, email UNIQUE, age)
    posts (id PK, user_id FK → users.id ON DELETE CASCADE, title, body, created_at)

  The FK enforces referential integrity: a post for a non-existent user is
  rejected at the database level, not the application level. ON DELETE CASCADE
  removes orphaned posts automatically when a user is deleted.

  Index on posts(user_id) makes "give me all posts for user 42" O(log n + k)
  where k is the number of matching rows — not a full table scan.
""")
    pg = psycopg2.connect(PG_DSN)
    pg.autocommit = False
    pg_setup(pg)

    start = time.perf_counter()
    user_id_map = pg_write_users(pg, users)
    pg_user_ids = list(user_id_map.values())

    all_posts = []
    for uid in pg_user_ids:
        for j in range(POSTS_PER_USER):
            all_posts.append({
                "user_id": uid,
                "title":   f"Post {j} by user {uid}",
                "body":    rand_str(50),
            })
    pg_write_posts(pg, all_posts)
    pg_write_ms = (time.perf_counter() - start) * 1_000
    print(f"  Write {NUM_USERS} users + {NUM_USERS * POSTS_PER_USER} posts: {pg_write_ms:.1f}ms")

    sample_pg_ids = random.choices(pg_user_ids, k=100)
    start = time.perf_counter()
    for uid in sample_pg_ids:
        pg_read_user_with_posts(pg, uid)
    pg_read_ms = (time.perf_counter() - start) * 1_000
    print(f"  Read user+posts (JOIN) x100:   {pg_read_ms:.1f}ms  ({pg_read_ms / 100:.2f}ms avg)")

    print("""
  Postgres strengths visible here:
    + FK constraint: posts table cannot contain orphaned user_id values
    + JOIN: user + all posts fetched in a single query, one round-trip
    + ACID: both INSERT INTO users and INSERT INTO posts ran in one transaction;
            a crash mid-write leaves the DB in the pre-insert state
    + Index: covering index on posts(user_id) keeps the JOIN efficient

  Postgres limitations:
    - Schema changes (ALTER TABLE) require careful migration on large tables
    - Horizontal sharding is not built-in; needs Citus, Vitess, or app-level sharding
    - Connection model (one OS thread per connection) limits concurrency without pooling
""")

    # ── Phase 2: MongoDB ───────────────────────────────────────
    section("Phase 2: MongoDB — Embedded Document Model")
    print("""
  Document model: posts are embedded INSIDE the user document.
  No JOIN needed — fetching user:42 returns user metadata + all posts in
  one key lookup.

  {
    "_id": ObjectId("..."),
    "name": "User 0",
    "email": "user0_abc@example.com",
    "age": 32,
    "posts": [
      {"title": "Post 0", "body": "..."},
      {"title": "Post 1", "body": "..."}
    ]
  }

  Trade-off: the entire document is rewritten on every update. If a user
  has 10,000 posts, updating post title #5 reads and rewrites the whole
  document. At that point, switch to a "referenced" model (separate posts
  collection with user_id field) — which reintroduces the JOIN problem via
  MongoDB's $lookup aggregation pipeline operator.
""")
    mongo_client = MongoClient(MONGO_URL)
    db = mongo_client["dbtest"]
    mongo_setup(db)

    mongo_docs = []
    for user in users:
        posts = [
            {"title": f"Post {j}", "body": rand_str(50)}
            for j in range(POSTS_PER_USER)
        ]
        mongo_docs.append({**user, "posts": posts})

    start = time.perf_counter()
    inserted_ids = mongo_write_users(db, mongo_docs)
    mongo_write_ms = (time.perf_counter() - start) * 1_000
    print(f"  Write {NUM_USERS} embedded user+posts documents: {mongo_write_ms:.1f}ms")

    sample_mongo_ids = random.choices(inserted_ids, k=100)
    start = time.perf_counter()
    for oid in sample_mongo_ids:
        mongo_read_user_with_posts(db, oid)
    mongo_read_ms = (time.perf_counter() - start) * 1_000
    print(f"  Read user+posts (no JOIN) x100: {mongo_read_ms:.1f}ms  ({mongo_read_ms / 100:.2f}ms avg)")

    print("""
  MongoDB strengths visible here:
    + No JOIN: one document fetch returns the complete user+posts tree
    + Flexible schema: add new fields to any document without migrations
    + Horizontal scaling: sharding built-in via mongos + config servers
    + Natural JSON representation matches API response structure

  MongoDB limitations:
    - No referential integrity: app code must enforce user_id exists in posts
    - Document size limit: 16 MB cap; unbounded embedded arrays hit this
    - Multi-document transactions added in v4.0 but carry significant overhead
    - Eventual consistency by default on replica sets (secondaries may lag)
""")

    # ── Phase 3: Redis ─────────────────────────────────────────
    section("Phase 3: Redis — In-Memory Key-Value Store")
    print("""
  Redis data model for user+posts:
    user:{id}        → Hash  { name, email, age }
    user:{id}:posts  → List  [ JSON(post0), JSON(post1), ... ]

  Pipeline batches all HSET + RPUSH commands for one user into a single
  TCP round-trip. Without pipeline: 1 + POSTS_PER_USER = 6 round-trips
  per user × 100 users = 600 round-trips. With pipeline: 1 per user = 100.

  Redis is single-threaded for command execution — no lock contention,
  no MVCC overhead. This is why sub-millisecond latency is achievable.
""")
    r = redis.Redis.from_url(REDIS_URL, decode_responses=True)
    redis_setup(r)

    start = time.perf_counter()
    for i, user in enumerate(users):
        posts = [{"title": f"Post {j}", "body": rand_str(50)} for j in range(POSTS_PER_USER)]
        redis_write_user(r, i, user, posts)
    redis_write_ms = (time.perf_counter() - start) * 1_000
    print(f"  Write {NUM_USERS} users + posts (pipeline per user): {redis_write_ms:.1f}ms")

    sample_redis_ids = random.choices(range(NUM_USERS), k=100)
    start = time.perf_counter()
    for uid in sample_redis_ids:
        redis_read_user_with_posts(r, uid)
    redis_read_ms = (time.perf_counter() - start) * 1_000
    print(f"  Read user+posts (pipeline) x100: {redis_read_ms:.1f}ms  ({redis_read_ms / 100:.2f}ms avg)")

    print("""
  Redis strengths visible here:
    + In-memory: read/write latency in microseconds (vs milliseconds for disk)
    + Pipeline: multiple commands in one round-trip cuts network overhead
    + Atomic operations: INCR, LPUSH, ZADD — no read-modify-write cycles needed
    + Rich data structures: Hash, List, Set, Sorted Set, Stream, HyperLogLog

  Redis limitations:
    - Data must fit in RAM (64 MB in this lab; production: size to working set)
    - No query language: can't do "find all users WHERE age > 30"
    - No joins, no referential integrity — relationships are manual
    - Default persistence is async RDB snapshot; use AOF for durability
""")

    # ── Phase 4: Benchmark Comparison ──────────────────────────
    section("Phase 4: Benchmark Comparison")
    print(f"""
  ┌──────────────────┬───────────────────────┬───────────────────────┐
  │ Database         │ Write 100u+500p (ms)  │ Read u+posts x100 ms  │
  ├──────────────────┼───────────────────────┼───────────────────────┤
  │ PostgreSQL       │ {pg_write_ms:>21.1f} │ {pg_read_ms:>19.1f}   │
  │ MongoDB          │ {mongo_write_ms:>21.1f} │ {mongo_read_ms:>19.1f}   │
  │ Redis            │ {redis_write_ms:>21.1f} │ {redis_read_ms:>19.1f}   │
  └──────────────────┴───────────────────────┴───────────────────────┘

  Avg read latency:
    Postgres : {pg_read_ms / 100:.2f} ms/read  — JOIN query, disk-backed, buffer pool
    MongoDB  : {mongo_read_ms / 100:.2f} ms/read  — document lookup, disk-backed, WiredTiger cache
    Redis    : {redis_read_ms / 100:.2f} ms/read  — Hash+List pipeline, in-memory

  Key insight: Postgres and MongoDB perform similarly for SIMPLE reads on
  small datasets. The performance gap opens at scale due to:
    - Data volume exceeding buffer/cache → disk I/O dominates
    - Query complexity (multi-table JOINs vs single-key lookup)
    - Write throughput under concurrent load (MVCC vs lock-free)
    - Replication lag under write-heavy workloads
""")

    # ── Phase 5: N+1 Query Anti-Pattern ────────────────────────
    section("Phase 5: N+1 Query Anti-Pattern (Postgres)")
    print("""
  The N+1 problem: fetch N user IDs, then issue 1 query per user to get
  their post count. That is N+1 round-trips to the database.

  This is the most common ORM-related performance bug. ORMs like Django ORM,
  SQLAlchemy, and ActiveRecord make it easy to write N+1 queries accidentally
  (e.g., iterating user.posts without select_related / joinedload).
""")
    pg_demo_n_plus_one(pg, pg_user_ids)

    # ── Phase 6: Schema Evolution ───────────────────────────────
    section("Phase 6: Schema Evolution Trade-off")
    print("""
  Adding a new field "bio" to users — compare the three databases.

  --- PostgreSQL: zero-downtime ALTER TABLE pattern ---
""")
    pg_demo_schema_migration(pg)

    print("  --- MongoDB: add field to one document, others unaffected ---\n")
    mongo_demo_schema_evolution(db)

    print("  --- Redis: just HSET the new field, no schema at all ---")
    r.hset("user:0", "bio", "I love systems design!")
    bio = r.hget("user:0", "bio")
    bio_user1 = r.hget("user:1", "bio")
    print(f"  user:0 bio = {bio}")
    print(f"  user:1 bio = {bio_user1!r}  ← field simply doesn't exist, returns None")
    print("""
  Redis has no schema: any key can have any fields. Total flexibility,
  zero enforcement. Application code is the only guard.
""")

    # ── Phase 7: Redis Eviction Under Memory Pressure ──────────
    section("Phase 7: Redis Eviction Under Memory Pressure")
    print("""
  The docker-compose.yml sets --maxmemory 64mb --maxmemory-policy allkeys-lru.
  Writing enough data to approach this limit triggers key eviction.
  This demonstrates why Redis should not be used as a primary database
  when data must survive memory pressure.
""")
    redis_demo_eviction(r)

    # ── Phase 8: Postgres Connection Pool Exhaustion ────────────
    section("Phase 8: Postgres Connection Pool Exhaustion")
    print("""
  The docker-compose.yml sets max_connections=50 for Postgres.
  Opening 55 connections shows what happens when the limit is exceeded.
  This is the most common operational failure mode for Postgres at scale.
""")
    pg_demo_connection_exhaustion()

    # ── Summary ────────────────────────────────────────────────
    section("Summary: When to Use What")
    print("""
  Use RELATIONAL (Postgres, MySQL):
    + Complex queries: multi-table JOINs, aggregations, window functions
    + ACID transactions (financial systems, inventory, booking)
    + Normalized data: each fact stored once, updated once
    + Reporting and ad-hoc analytics
    + When the data model is stable and well-understood

  Use DOCUMENT (MongoDB, DynamoDB document mode, Firestore):
    + Hierarchical / nested data (orders + line items, blog posts + comments)
    + Schema evolves frequently (e-commerce product catalog)
    + Access patterns are simple: fetch by ID, no complex JOINs
    + Horizontal write scaling needed

  Use KEY-VALUE (Redis, DynamoDB, Memcached):
    + Caching: session storage, computed results, HTML fragment cache
    + Simple fast lookups: feature flags, user preferences, A/B variants
    + Real-time: rate limiting counters, leaderboards (Sorted Set), pub/sub
    + Ephemeral data where RAM capacity is sufficient

  Use WIDE-COLUMN (Cassandra, HBase, ScyllaDB):
    + Time-series data: IoT metrics, event logs, activity feeds
    + Extreme write throughput (millions of writes/sec)
    + Data naturally partitioned by a key (user_id, sensor_id, date)
    + Queries ALWAYS filter by partition key — no full-table scans

  Use GRAPH (Neo4j, Amazon Neptune):
    + Highly connected data: social graphs, knowledge graphs
    + Relationship traversal: "friends-of-friends who bought X"
    + Fraud detection: transaction pattern matching across entities

  Use NEWSQL (CockroachDB, Google Spanner, YugabyteDB):
    + ACID transactions with horizontal scalability
    + Multi-region deployments with consistent reads
    + When you need SQL + scale-out and can tolerate higher write latency

  Polyglot persistence: use the RIGHT database for EACH job.
  A production system often uses Postgres + Redis + S3 together.
  The key is to match the storage model to the access pattern.

  Next: ../09-indexes/
""")

    pg.close()
    mongo_client.close()


if __name__ == "__main__":
    main()
