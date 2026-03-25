#!/usr/bin/env python3
"""
Partitioning & Sharding Lab — 3 PostgreSQL shards

Prerequisites: docker compose up -d (wait ~15s for all 3 shards)

What this demonstrates:
  1. Hash-based sharding: 1000 users distributed across 3 shards
  2. Distribution uniformity (should be ~333 per shard)
  3. Range-based sharding: user_id ranges map to shards
  4. Hotspot problem with range sharding + sequential IDs
  5. Cross-shard query: scatter-gather to find users by name
"""

import time
import random
import subprocess

try:
    import psycopg2
except ImportError:
    print("Installing psycopg2-binary...")
    subprocess.run(["pip", "install", "psycopg2-binary", "-q"], check=True)
    import psycopg2

SHARD_DSNS = [
    "host=localhost port=5432 dbname=shard0 user=postgres password=postgres connect_timeout=5",
    "host=localhost port=5433 dbname=shard1 user=postgres password=postgres connect_timeout=5",
    "host=localhost port=5434 dbname=shard2 user=postgres password=postgres connect_timeout=5",
]

FIRST_NAMES = ["Alice", "Bob", "Carol", "Dave", "Eve", "Frank", "Grace", "Hank",
               "Iris", "Jack", "Karen", "Leo", "Mia", "Nina", "Oscar"]


class HashShardRouter:
    """Routes user_id to shard via modulo hash sharding (NOT consistent hashing)."""
    def __init__(self, connections):
        self.conns = connections

    def get_shard_index(self, user_id: int) -> int:
        return hash(user_id) % len(self.conns)

    def get_shard(self, user_id: int):
        return self.conns[self.get_shard_index(user_id)]


class RangeShardRouter:
    """Routes user_id to shard via range boundaries."""
    def __init__(self, connections, boundaries):
        # boundaries: [(0, 333, conn0), (334, 666, conn1), (667, inf, conn2)]
        self.conns = connections
        self.boundaries = boundaries  # list of (low, high_inclusive, shard_idx)

    def get_shard_index(self, user_id: int) -> int:
        for low, high, idx in self.boundaries:
            if low <= user_id <= high:
                return idx
        return len(self.conns) - 1  # overflow to last shard

    def get_shard(self, user_id: int):
        return self.conns[self.get_shard_index(user_id)]


def connect_shards():
    conns = []
    for i, dsn in enumerate(SHARD_DSNS):
        try:
            conn = psycopg2.connect(dsn)
            conn.autocommit = True
            conns.append(conn)
        except Exception as e:
            print(f"  [!] Cannot connect to shard-{i}: {e}")
            conns.append(None)
    return conns


def setup_schema(conns):
    for i, conn in enumerate(conns):
        if conn is None:
            continue
        with conn.cursor() as cur:
            cur.execute("""
                CREATE TABLE IF NOT EXISTS users (
                    id    INTEGER PRIMARY KEY,
                    name  TEXT NOT NULL,
                    email TEXT NOT NULL
                )
            """)
            cur.execute("TRUNCATE users")
    print("  Schema ready on all shards.")


def section(title):
    print(f"\n{'=' * 60}")
    print(f"  {title}")
    print("=" * 60)


def main():
    section("PARTITIONING & SHARDING LAB")
    print("""
  Sharding = horizontal partitioning across multiple databases.
  Each shard holds a disjoint subset of the data.
  A shard router decides which shard a given key belongs to.
""")

    conns = connect_shards()
    live_shards = sum(1 for c in conns if c is not None)
    if live_shards == 0:
        print("  ERROR: No shards reachable. Run 'docker compose up -d' first.")
        return
    print(f"  Connected to {live_shards}/3 shards.")
    setup_schema(conns)

    # ── Phase 1: Hash-based sharding ──────────────────────────────
    section("Phase 1: Hash-Based Sharding (1000 users)")

    print("  Inserting 1000 users with hash(user_id) % 3 routing...")
    router = HashShardRouter(conns)
    shard_counts = [0, 0, 0]

    start = time.perf_counter()
    for user_id in range(1, 1001):
        name  = f"{random.choice(FIRST_NAMES)}{user_id}"
        email = f"user{user_id}@example.com"
        shard = router.get_shard(user_id)
        idx   = router.get_shard_index(user_id)
        if shard:
            with shard.cursor() as cur:
                cur.execute(
                    "INSERT INTO users VALUES (%s,%s,%s) ON CONFLICT DO NOTHING",
                    (user_id, name, email)
                )
            shard_counts[idx] += 1
    elapsed = time.perf_counter() - start

    print(f"\n  Inserted 1000 users in {elapsed:.3f}s")
    print("\n  Distribution across shards:")
    for i, count in enumerate(shard_counts):
        pct = count / 10
        bar = "#" * (count // 10)
        print(f"    shard-{i}: {count:4d} users ({pct:.1f}%)  {bar}")

    max_count = max(shard_counts)
    min_count = min(shard_counts)
    imbalance = (max_count - min_count) / (sum(shard_counts) / 3) * 100
    print(f"\n  Imbalance: {imbalance:.1f}%  (ideal = 0%)")
    print("  Hash sharding distributes evenly, but queries by range require ALL shards.")

    # ── Phase 2: Lookup a user ─────────────────────────────────────
    section("Phase 2: Shard Lookup by user_id")

    for test_id in [42, 500, 999]:
        idx = router.get_shard_index(test_id)
        shard = router.get_shard(test_id)
        if shard:
            with shard.cursor() as cur:
                cur.execute("SELECT id, name, email FROM users WHERE id = %s", (test_id,))
                row = cur.fetchone()
            print(f"  user_id={test_id} → shard-{idx} → {row}")

    # ── Phase 3: Range-based sharding ─────────────────────────────
    section("Phase 3: Range-Based Sharding")

    print("""  Range routing: user_id 1-333 → shard-0, 334-666 → shard-1, 667+ → shard-2
  Advantage: range queries stay on one shard (e.g., users 100-200)
  Disadvantage: monotonically increasing IDs create HOTSPOTS
""")
    range_router = RangeShardRouter(
        conns,
        [(1, 333, 0), (334, 666, 1), (667, 10_000_000, 2)]
    )

    # Clear and re-insert with range routing
    setup_schema(conns)
    range_counts = [0, 0, 0]
    for user_id in range(1, 1001):
        name  = f"{random.choice(FIRST_NAMES)}{user_id}"
        email = f"user{user_id}@example.com"
        idx   = range_router.get_shard_index(user_id)
        shard = range_router.get_shard(user_id)
        if shard:
            with shard.cursor() as cur:
                cur.execute(
                    "INSERT INTO users VALUES (%s,%s,%s) ON CONFLICT DO NOTHING",
                    (user_id, name, email)
                )
            range_counts[idx] += 1

    print("  Distribution with range sharding (IDs 1-1000):")
    for i, count in enumerate(range_counts):
        print(f"    shard-{i}: {count:4d} users")

    # ── Phase 4: Hotspot problem ───────────────────────────────────
    section("Phase 4: The Hotspot Problem")

    print("""  New users always get the next available ID.
  IDs > 666 always go to shard-2 → HOTSPOT!

  Inserting 200 "new" users (IDs 1001-1200):
""")
    new_counts = [0, 0, 0]
    for user_id in range(1001, 1201):
        idx = range_router.get_shard_index(user_id)
        new_counts[idx] += 1

    for i, count in enumerate(new_counts):
        bar = "#" * (count // 2)
        hotspot = " ← ALL NEW TRAFFIC" if count > 100 else ""
        print(f"    shard-{i} receives: {count:4d} new users  {bar}{hotspot}")
    print("""
  Solutions to hotspot:
    - Prefix IDs with shard number (shard is part of the key)
    - Use UUID/random IDs instead of sequential integers
    - Pre-split ranges: anticipate growth and assign wider ranges
    - Consistent hashing: avoids the resharding problem entirely
""")

    # ── Phase 5: Cross-shard query ─────────────────────────────────
    section("Phase 5: Cross-Shard Query (Scatter-Gather)")

    print('  Finding all users named "Alice" across all 3 shards...')
    # Re-insert with hash routing so "Alice" can be on any shard
    setup_schema(conns)
    hash_router = HashShardRouter(conns)
    for user_id in range(1, 1001):
        name  = f"{'Alice' if user_id % 20 == 0 else random.choice(FIRST_NAMES)}{user_id}"
        email = f"user{user_id}@example.com"
        shard = hash_router.get_shard(user_id)
        if shard:
            with shard.cursor() as cur:
                cur.execute(
                    "INSERT INTO users VALUES (%s,%s,%s) ON CONFLICT DO NOTHING",
                    (user_id, name, email)
                )

    start = time.perf_counter()
    all_alices = []
    for i, conn in enumerate(conns):
        if conn:
            with conn.cursor() as cur:
                cur.execute("SELECT id, name FROM users WHERE name LIKE 'Alice%'")
                rows = cur.fetchall()
                all_alices.extend(rows)
                print(f"    shard-{i}: {len(rows)} Alices found")
    elapsed = time.perf_counter() - start

    print(f"\n  Total Alices found: {len(all_alices)} (queried all {live_shards} shards in {elapsed*1000:.1f}ms)")
    print("""
  Cross-shard query cost:
    - Must query ALL shards (scatter)
    - Merge results in application (gather)
    - Latency = max(shard latency), not average
    - N shards = N parallel queries or N sequential queries

  Solutions:
    - Maintain secondary indexes on a dedicated lookup service
    - Denormalize: store frequently queried attributes redundantly
    - Global secondary indexes (DynamoDB GSI, Vitess)
    - Accept the scatter-gather cost for rare queries
""")

    # ── Summary ───────────────────────────────────────────────────
    section("Summary")
    print("""
  Sharding Decision Framework:
  ─────────────────────────────
  DON'T shard until you have to:
    - Read replicas handle most read-heavy workloads
    - Vertical scaling is simpler
    - Sharding adds enormous operational complexity

  Shard Key Selection:
    - High cardinality (many distinct values)
    - Evenly distributed (avoid hotspots)
    - Stable (doesn't change after insert)
    - Aligned with access patterns (avoid cross-shard queries)

  Hash vs Range vs Directory:
    Hash:      even distribution, no range queries, simple
    Range:     range queries on one shard, hotspot risk
    Directory: flexible, extra lookup hop, single point of failure

  The resharding nightmare:
    Adding a 4th shard with hash(id) % 4 invalidates ALL routes.
    Consistent hashing (next topic) reduces resharding cost to 1/N keys.

  Next steps: ../06-caching/
""")


if __name__ == "__main__":
    main()
