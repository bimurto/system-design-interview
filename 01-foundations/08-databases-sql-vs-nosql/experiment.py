#!/usr/bin/env python3
"""
Databases Lab — SQL vs NoSQL: Postgres, MongoDB, Redis

Prerequisites: docker compose up -d (wait ~15s)

What this demonstrates:
  1. Postgres: normalized schema with FK, JOIN query for user+posts
  2. MongoDB: document model with embedded posts array
  3. Redis: hash for user, list for posts
  4. Benchmark: write 100 users + 5 posts each, read user+posts 100 times
  5. Trade-off analysis: latency, flexibility, consistency model
"""

import json
import time
import random
import string
import subprocess
from collections import defaultdict

try:
    import psycopg2
    import psycopg2.extras
    import redis
    from pymongo import MongoClient
except ImportError:
    print("Installing dependencies...")
    subprocess.run(
        ["pip", "install", "psycopg2-binary", "redis", "pymongo", "-q"],
        check=True
    )
    import psycopg2
    import psycopg2.extras
    import redis
    from pymongo import MongoClient


PG_DSN   = "host=localhost port=5432 dbname=dbtest user=postgres password=postgres connect_timeout=5"
REDIS_URL = "redis://localhost:6379"
MONGO_URL = "mongodb://localhost:27017"

NUM_USERS  = 100
POSTS_PER_USER = 5


def section(title):
    print(f"\n{'=' * 64}")
    print(f"  {title}")
    print("=" * 64)


def rand_str(n=8):
    return "".join(random.choices(string.ascii_lowercase, k=n))


def check_services():
    """Verify all three databases are reachable."""
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
        c = MongoClient(MONGO_URL, serverSelectionTimeoutMS=3000)
        c.admin.command("ping")
        c.close()
    except Exception as e:
        errors.append(f"MongoDB: {e}")
    return errors


# ── PostgreSQL ─────────────────────────────────────────────────

def pg_setup(conn):
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
        cur.execute("CREATE INDEX ON posts(user_id)")
    conn.commit()


def pg_write_users(conn, users):
    with conn.cursor() as cur:
        psycopg2.extras.execute_batch(
            cur,
            "INSERT INTO users (name, email, age) VALUES (%s, %s, %s) RETURNING id",
            [(u["name"], u["email"], u["age"]) for u in users]
        )
        cur.execute("SELECT id, email FROM users ORDER BY id")
        rows = cur.fetchall()
    conn.commit()
    return {email: uid for uid, email in rows}


def pg_write_posts(conn, posts):
    with conn.cursor() as cur:
        psycopg2.extras.execute_batch(
            cur,
            "INSERT INTO posts (user_id, title, body) VALUES (%s, %s, %s)",
            [(p["user_id"], p["title"], p["body"]) for p in posts]
        )
    conn.commit()


def pg_read_user_with_posts(conn, user_id):
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute("""
            SELECT u.id, u.name, u.email, u.age,
                   p.id AS post_id, p.title, p.body
            FROM users u
            LEFT JOIN posts p ON p.user_id = u.id
            WHERE u.id = %s
        """, (user_id,))
        rows = cur.fetchall()
    if not rows:
        return None
    user = {"id": rows[0]["id"], "name": rows[0]["name"],
            "email": rows[0]["email"], "age": rows[0]["age"], "posts": []}
    for row in rows:
        if row["post_id"]:
            user["posts"].append({"id": row["post_id"], "title": row["title"], "body": row["body"]})
    return user


# ── MongoDB ────────────────────────────────────────────────────

def mongo_setup(db):
    db.users.drop()


def mongo_write_users(db, users):
    result = db.users.insert_many(users)
    return result.inserted_ids


def mongo_read_user_with_posts(db, user_id):
    return db.users.find_one({"_id": user_id})


# ── Redis ──────────────────────────────────────────────────────

def redis_setup(r):
    r.flushdb()


def redis_write_user(r, user_id, user, posts):
    pipe = r.pipeline()
    pipe.hset(f"user:{user_id}", mapping={
        "name": user["name"],
        "email": user["email"],
        "age": str(user["age"])
    })
    posts_key = f"user:{user_id}:posts"
    for post in posts:
        pipe.rpush(posts_key, json.dumps(post))
    pipe.execute()


def redis_read_user_with_posts(r, user_id):
    pipe = r.pipeline()
    pipe.hgetall(f"user:{user_id}")
    pipe.lrange(f"user:{user_id}:posts", 0, -1)
    user_data, posts_raw = pipe.execute()
    if not user_data:
        return None
    return {
        "id": user_id,
        **user_data,
        "posts": [json.loads(p) for p in posts_raw]
    }


# ── Main ───────────────────────────────────────────────────────

def main():
    section("DATABASES LAB: SQL vs NoSQL")
    print("""
  Three databases, same data model: users with posts.

  Postgres  — relational (ACID, normalized, JOIN queries)
  MongoDB   — document (flexible schema, embedded documents)
  Redis     — key-value (in-memory, simple data structures)

  We will write 100 users with 5 posts each, then read
  "user with all posts" 100 times. Comparing:
  - Write latency
  - Read latency (JOIN vs embedded vs pipeline)
  - Data model trade-offs
""")

    errors = check_services()
    if errors:
        print("  ERROR: Some services are not ready:")
        for e in errors:
            print(f"    {e}")
        print("  Run: docker compose up -d && sleep 15")
        return

    print("  All services ready.")

    # Generate test data
    users = [
        {
            "name": f"User {i}",
            "email": f"user{i}_{rand_str()}@example.com",
            "age": random.randint(18, 65)
        }
        for i in range(NUM_USERS)
    ]

    # ── Phase 1: PostgreSQL ─────────────────────────────────────
    section("Phase 1: PostgreSQL — Normalized Schema")
    print("""
  Schema:
    users  (id PK, name, email UNIQUE, age)
    posts  (id PK, user_id FK → users.id, title, body, created_at)

  Relationship enforced by foreign key: can't insert a post for
  a non-existent user. ON DELETE CASCADE removes posts when user is deleted.
  A JOIN query fetches user + all their posts in one round-trip.
""")
    pg = psycopg2.connect(PG_DSN)
    pg.autocommit = False

    pg_setup(pg)

    # Write benchmark
    start = time.perf_counter()
    user_id_map = pg_write_users(pg, users)
    pg_user_ids = list(user_id_map.values())

    all_posts = []
    for uid in pg_user_ids:
        for j in range(POSTS_PER_USER):
            all_posts.append({
                "user_id": uid,
                "title": f"Post {j} by user {uid}",
                "body": rand_str(50)
            })
    pg_write_posts(pg, all_posts)
    pg_write_elapsed = (time.perf_counter() - start) * 1000

    print(f"  Write {NUM_USERS} users + {NUM_USERS * POSTS_PER_USER} posts: {pg_write_elapsed:.1f}ms")

    # Read benchmark
    sample_ids = random.choices(pg_user_ids, k=100)
    start = time.perf_counter()
    for uid in sample_ids:
        pg_read_user_with_posts(pg, uid)
    pg_read_elapsed = (time.perf_counter() - start) * 1000

    print(f"  Read user+posts (JOIN) x100: {pg_read_elapsed:.1f}ms  ({pg_read_elapsed/100:.2f}ms avg)")
    print("""
  Postgres strengths demonstrated:
    ✓ Foreign key guarantees referential integrity
    ✓ JOIN fetches related data in a single query
    ✓ ACID: both tables updated in one transaction
    ✓ Normalized: post changes don't require updating user rows

  Postgres limitations:
    ✗ Schema changes require migrations (ALTER TABLE)
    ✗ Horizontal scaling (sharding) is complex
    ✗ JOIN performance degrades on very large tables without careful indexing
""")

    # ── Phase 2: MongoDB ────────────────────────────────────────
    section("Phase 2: MongoDB — Document Model (Embedded)")
    print("""
  Document model: user document CONTAINS the posts array.
  No JOIN needed — the entire user+posts tree is one document.

  {
    "_id": ObjectId(...),
    "name": "User 0",
    "email": "user0@example.com",
    "age": 32,
    "posts": [
      {"title": "Post 0", "body": "..."},
      {"title": "Post 1", "body": "..."}
    ]
  }

  Trade-off: document size grows with each post. MongoDB's max
  document size is 16MB. For users with thousands of posts,
  a "referenced" model (separate posts collection) is better.
""")
    mongo_client = MongoClient(MONGO_URL)
    db = mongo_client["dbtest"]
    mongo_setup(db)

    # Build embedded documents
    mongo_docs = []
    for user in users:
        posts = [
            {"title": f"Post {j}", "body": rand_str(50)}
            for j in range(POSTS_PER_USER)
        ]
        mongo_docs.append({**user, "posts": posts})

    start = time.perf_counter()
    inserted_ids = mongo_write_users(db, mongo_docs)
    mongo_write_elapsed = (time.perf_counter() - start) * 1000

    print(f"  Write {NUM_USERS} user+posts documents: {mongo_write_elapsed:.1f}ms")

    sample_mongo_ids = random.choices(inserted_ids, k=100)
    start = time.perf_counter()
    for oid in sample_mongo_ids:
        mongo_read_user_with_posts(db, oid)
    mongo_read_elapsed = (time.perf_counter() - start) * 1000

    print(f"  Read user+posts (no JOIN) x100: {mongo_read_elapsed:.1f}ms  ({mongo_read_elapsed/100:.2f}ms avg)")
    print("""
  MongoDB strengths demonstrated:
    ✓ No JOIN: one document fetch returns user + all posts
    ✓ Flexible schema: add new fields without migrations
    ✓ Natural data model for hierarchical/nested data
    ✓ Horizontal scaling (sharding) built-in

  MongoDB limitations:
    ✗ No foreign key constraints — referential integrity is app responsibility
    ✗ Document size limit (16MB) limits deeply embedded arrays
    ✗ Updating a single post requires fetching the entire document
    ✗ Multi-document transactions added in v4.0, but are expensive
""")

    # ── Phase 3: Redis ──────────────────────────────────────────
    section("Phase 3: Redis — Key-Value / In-Memory")
    print("""
  Redis data model for user+posts:
    user:{id}        → Hash  { name, email, age }
    user:{id}:posts  → List  [ JSON post1, JSON post2, ... ]

  Accessed via pipeline (batches commands in one round-trip):
    HGETALL user:42
    LRANGE user:42:posts 0 -1

  Redis is in-memory: all data lives in RAM. Writes and reads
  are ~microseconds vs milliseconds for Postgres/MongoDB.
  Trade-off: data size is limited by RAM; durability requires
  either RDB snapshots or AOF (append-only file) persistence.
""")
    r = redis.Redis.from_url(REDIS_URL, decode_responses=True)
    redis_setup(r)

    start = time.perf_counter()
    for i, user in enumerate(users):
        posts = [
            {"title": f"Post {j}", "body": rand_str(50)}
            for j in range(POSTS_PER_USER)
        ]
        redis_write_user(r, i, user, posts)
    redis_write_elapsed = (time.perf_counter() - start) * 1000

    print(f"  Write {NUM_USERS} users + posts (pipeline): {redis_write_elapsed:.1f}ms")

    sample_redis_ids = random.choices(range(NUM_USERS), k=100)
    start = time.perf_counter()
    for uid in sample_redis_ids:
        redis_read_user_with_posts(r, uid)
    redis_read_elapsed = (time.perf_counter() - start) * 1000

    print(f"  Read user+posts (pipeline) x100: {redis_read_elapsed:.1f}ms  ({redis_read_elapsed/100:.2f}ms avg)")
    print("""
  Redis strengths demonstrated:
    ✓ In-memory: read/write latency in microseconds
    ✓ Pipeline: multiple commands in one round-trip
    ✓ Rich data structures: Hash, List, Set, Sorted Set, Stream
    ✓ Atomic operations: INCR, LPUSH, ZADD are all atomic

  Redis limitations:
    ✗ Data must fit in RAM (though Redis 7 supports disk-backed structures)
    ✗ No query language: can't do "find all users where age > 30"
    ✗ Complex relationships are hard (no JOINs, manual foreign keys)
    ✗ Default persistence is async — power loss can lose recent writes
""")

    # ── Phase 4: Benchmark Comparison ──────────────────────────
    section("Phase 4: Benchmark Comparison")
    print(f"""
  ┌─────────────────┬──────────────────────┬──────────────────────┐
  │ Database        │ Write 100u+500p (ms) │ Read u+posts x100 ms │
  ├─────────────────┼──────────────────────┼──────────────────────┤
  │ PostgreSQL      │ {pg_write_elapsed:>20.1f} │ {pg_read_elapsed:>18.1f}   │
  │ MongoDB         │ {mongo_write_elapsed:>20.1f} │ {mongo_read_elapsed:>18.1f}   │
  │ Redis           │ {redis_write_elapsed:>20.1f} │ {redis_read_elapsed:>18.1f}   │
  └─────────────────┴──────────────────────┴──────────────────────┘

  Notes:
  - All running locally (no network latency)
  - Redis advantage grows with distance (latency is microseconds vs ms)
  - MongoDB advantage is schema flexibility and horizontal scaling
  - Postgres advantage is ACID, complex queries, and referential integrity

  Avg read latency:
    Postgres : {pg_read_elapsed/100:.2f}ms/read  (JOIN query, on-disk)
    MongoDB  : {mongo_read_elapsed/100:.2f}ms/read  (document fetch, on-disk)
    Redis    : {redis_read_elapsed/100:.2f}ms/read  (pipeline, in-memory)
""")

    # ── Phase 5: Schema Evolution Demo ─────────────────────────
    section("Phase 5: Schema Evolution Trade-off")
    print("""
  Adding a new field "bio" to users:

  PostgreSQL (requires ALTER TABLE):
    ALTER TABLE users ADD COLUMN bio TEXT;
    -- Must run migration on potentially large table
    -- In production: may need pg_repack to avoid long locks
    -- Zero-downtime requires: add column nullable → deploy app →
    --   backfill data → add NOT NULL constraint

  MongoDB (schema-less — just insert the new field):
    db.users.updateOne({"name": "User 0"}, {"$set": {"bio": "Hello!"}})
    -- Existing documents simply don't have the field
    -- New documents can have it immediately
    -- Downside: no schema enforcement, validation must be in app code

  Redis (no schema at all):
    HSET user:0 bio "Hello!"
    -- Completely free-form; total flexibility
    -- Total responsibility on application code
""")
    # Demonstrate Mongo schema flexibility
    db.users.update_one(
        {"name": "User 0"},
        {"$set": {"bio": "I love systems design!"}}
    )
    doc = db.users.find_one({"name": "User 0"}, {"name": 1, "bio": 1, "_id": 0})
    doc2 = db.users.find_one({"name": "User 1"}, {"name": 1, "bio": 1, "_id": 0})
    print(f"  User 0 after update: {doc}")
    print(f"  User 1 (unchanged):  {doc2}  ← no 'bio' field, no error")
    print("""
  MongoDB allows different documents in the same collection to have
  different fields. This is powerful for evolving schemas, but
  requires application-level validation (use JSON Schema validation
  in MongoDB 3.6+ or a validation library like pydantic).
""")

    # ── Summary ───────────────────────────────────────────────
    section("Summary: When to Use What")
    print("""
  Use RELATIONAL (Postgres, MySQL):
    ✓ Complex queries with multiple JOIN conditions
    ✓ Strong consistency and ACID transactions (financial, inventory)
    ✓ Normalized data with many relationships
    ✓ Reporting and ad-hoc analytics
    ✓ When data model is stable and well-understood

  Use DOCUMENT (MongoDB, DynamoDB document mode):
    ✓ Hierarchical / nested data (e.g., orders with line items)
    ✓ Schema evolves frequently (product catalogs with varied attributes)
    ✓ Need to scale writes horizontally across many nodes
    ✓ Data access patterns are simple (fetch by ID, no complex JOINs)

  Use KEY-VALUE (Redis, DynamoDB):
    ✓ Caching (session store, computed results)
    ✓ Simple fast lookups (user preferences, feature flags)
    ✓ Real-time data (rate limiting counters, leaderboards)
    ✓ Pub/sub messaging, task queues

  Use WIDE-COLUMN (Cassandra, HBase):
    ✓ Time-series data (metrics, events, logs)
    ✓ Extreme write throughput (millions of writes/sec)
    ✓ Data naturally partitioned by a key (user_id, sensor_id)
    ✓ Queries always filter by partition key

  Use GRAPH (Neo4j, Amazon Neptune):
    ✓ Highly connected data (social networks, recommendations)
    ✓ Relationship traversal queries ("friends of friends who bought X")
    ✓ Fraud detection (transaction pattern matching)

  Polyglot persistence: use the RIGHT database for EACH job.
  A production system often uses Postgres + Redis + S3 together.

  Next: ../09-indexes/
""")

    pg.close()
    mongo_client.close()


if __name__ == "__main__":
    main()
