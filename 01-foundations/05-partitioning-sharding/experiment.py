#!/usr/bin/env python3
"""
Partitioning & Sharding Lab — 3 PostgreSQL shards

Prerequisites: docker compose up -d (wait ~15s for all 3 shards)

What this demonstrates:
  1. Hash-based sharding: 1000 users distributed across 3 shards
     - Uses SHA-256 (deterministic across processes, unlike Python's hash())
  2. Distribution uniformity and imbalance measurement
  3. Range-based sharding: user_id ranges map to shards
  4. Hotspot problem with range sharding + sequential IDs
  5. Cross-shard query: parallel scatter-gather vs sequential
  6. Resharding cost: how many keys move when shard count changes
  7. Consistent hashing: virtual nodes minimize key movement on scale-out
"""

import hashlib
import random
import subprocess
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

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


# ---------------------------------------------------------------------------
# Routing strategies
# ---------------------------------------------------------------------------

def _sha256_int(key: str) -> int:
    """Deterministic hash of a string key → integer.

    Python's built-in hash() is randomized per-process (PYTHONHASHSEED) and
    must never be used for shard routing — different processes (or restarts)
    would route the same key to different shards.  SHA-256 is deterministic
    and portable across languages, matching how production routers work.
    """
    return int(hashlib.sha256(key.encode()).hexdigest(), 16)


class HashShardRouter:
    """Naive modulo sharding: shard = SHA256(key) % N.

    Fast and balanced, but adding/removing shards reassigns almost every key.
    """
    def __init__(self, connections):
        self.conns = connections

    def get_shard_index(self, user_id: int) -> int:
        return _sha256_int(str(user_id)) % len(self.conns)

    def get_shard(self, user_id: int):
        return self.conns[self.get_shard_index(user_id)]


class RangeShardRouter:
    """Range sharding: explicit key-range → shard mappings.

    Efficient for range scans but prone to hotspots with monotonically
    increasing keys (e.g. auto-increment IDs, timestamps).
    """
    def __init__(self, connections, boundaries):
        # boundaries: list of (low_inclusive, high_inclusive, shard_idx)
        self.conns = connections
        self.boundaries = boundaries

    def get_shard_index(self, user_id: int) -> int:
        for low, high, idx in self.boundaries:
            if low <= user_id <= high:
                return idx
        return len(self.conns) - 1  # overflow → last shard

    def get_shard(self, user_id: int):
        return self.conns[self.get_shard_index(user_id)]


class ConsistentHashRouter:
    """Consistent hashing with virtual nodes.

    Keys and shards are placed on a conceptual ring [0, 2^256).
    Each key routes to the nearest shard clockwise on the ring.
    Virtual nodes (vnodes) per shard improve balance.

    When a shard is added, only keys between the new shard and its predecessor
    on the ring are reassigned — approximately 1/N of all keys.
    With naive modulo hashing, adding one shard moves ~(N-1)/N of all keys.
    """
    def __init__(self, num_shards: int, vnodes_per_shard: int = 150):
        self.num_shards = num_shards
        self.vnodes_per_shard = vnodes_per_shard
        self.ring: list[tuple[int, int]] = []  # (ring_position, shard_idx)
        self._build_ring()

    def _build_ring(self):
        self.ring = []
        for shard_idx in range(self.num_shards):
            for vnode in range(self.vnodes_per_shard):
                vnode_key = f"shard-{shard_idx}-vnode-{vnode}"
                pos = _sha256_int(vnode_key)
                self.ring.append((pos, shard_idx))
        self.ring.sort()

    def get_shard_index(self, user_id: int) -> int:
        if not self.ring:
            raise RuntimeError("Ring is empty")
        pos = _sha256_int(str(user_id))
        # Binary search for the first ring entry >= pos (clockwise successor)
        lo, hi = 0, len(self.ring)
        while lo < hi:
            mid = (lo + hi) // 2
            if self.ring[mid][0] < pos:
                lo = mid + 1
            else:
                hi = mid
        # Wrap around the ring
        return self.ring[lo % len(self.ring)][1]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

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
    for conn in conns:
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
    print(f"\n{'=' * 62}")
    print(f"  {title}")
    print("=" * 62)


def bar_chart(counts, total, label="shard", bar_scale=30):
    for i, count in enumerate(counts):
        pct = count / total * 100 if total else 0
        bar = "#" * int(pct / 100 * bar_scale)
        print(f"    {label}-{i}: {count:5d}  ({pct:5.1f}%)  {bar}")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    section("PARTITIONING & SHARDING LAB")
    print("""
  Sharding = horizontal partitioning across multiple databases.
  Each shard holds a disjoint subset of the data.
  A shard router decides which shard a given key belongs to.

  KEY INSIGHT: The routing function is the central contract of a
  sharded system. Every write AND every read must pass through it.
  Changing the routing function requires migrating data live.
""")

    conns = connect_shards()
    live_shards = sum(1 for c in conns if c is not None)
    if live_shards == 0:
        print("  ERROR: No shards reachable. Run 'docker compose up -d' first.")
        return
    print(f"  Connected to {live_shards}/3 shards.")
    setup_schema(conns)

    # ── Phase 1: Hash-based sharding ──────────────────────────────────────
    section("Phase 1: Hash-Based Sharding (1000 users, SHA-256 % 3)")
    print("""  IMPORTANT: Python's built-in hash() is randomized per-process via
  PYTHONHASHSEED. Using it for shard routing would route the same key to
  different shards on each restart — catastrophic for a database.
  We use SHA-256, which is deterministic and matches production routers.

  Inserting 1000 users with sha256(user_id) % 3 routing...""")

    router = HashShardRouter(conns)
    shard_counts = [0, 0, 0]

    start = time.perf_counter()
    for user_id in range(1, 1001):
        name = f"{random.choice(FIRST_NAMES)}{user_id}"
        email = f"user{user_id}@example.com"
        shard = router.get_shard(user_id)
        idx = router.get_shard_index(user_id)
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
    bar_chart(shard_counts, 1000)

    max_c, min_c = max(shard_counts), min(shard_counts)
    imbalance = (max_c - min_c) / (1000 / 3) * 100
    print(f"\n  Imbalance: {imbalance:.1f}%  (0% = perfect)")
    print("""
  Hash sharding distributes writes evenly, but:
    - Range queries (user_id 100-200) must hit ALL shards (scatter-gather)
    - Adding a 4th shard forces rehashing of ~75% of all keys
""")

    # ── Phase 2: Single-key lookup ─────────────────────────────────────────
    section("Phase 2: Single-Key Lookup (O(1) routing)")
    print("  The shard router resolves a key to exactly one shard — no broadcast.\n")
    for test_id in [42, 500, 999]:
        idx = router.get_shard_index(test_id)
        shard = router.get_shard(test_id)
        if shard:
            with shard.cursor() as cur:
                cur.execute("SELECT id, name, email FROM users WHERE id = %s", (test_id,))
                row = cur.fetchone()
            print(f"  user_id={test_id:4d} → sha256({test_id}) % 3 = shard-{idx} → {row}")

    # ── Phase 3: Range-based sharding ─────────────────────────────────────
    section("Phase 3: Range-Based Sharding")
    print("""  Range routing: user_id 1-333 → shard-0, 334-666 → shard-1, 667+ → shard-2
  Advantage: range queries stay on ONE shard (great for time-series, sorted scans)
  Disadvantage: monotonically increasing IDs concentrate ALL new writes on one shard
""")
    range_router = RangeShardRouter(
        conns,
        [(1, 333, 0), (334, 666, 1), (667, 10_000_000, 2)]
    )

    setup_schema(conns)
    range_counts = [0, 0, 0]
    for user_id in range(1, 1001):
        name = f"{random.choice(FIRST_NAMES)}{user_id}"
        email = f"user{user_id}@example.com"
        idx = range_router.get_shard_index(user_id)
        shard = range_router.get_shard(user_id)
        if shard:
            with shard.cursor() as cur:
                cur.execute(
                    "INSERT INTO users VALUES (%s,%s,%s) ON CONFLICT DO NOTHING",
                    (user_id, name, email)
                )
            range_counts[idx] += 1

    print("  Distribution with range sharding (IDs 1-1000, static data):")
    bar_chart(range_counts, 1000)

    # ── Phase 4: Hotspot problem ───────────────────────────────────────────
    section("Phase 4: The Hotspot Problem (Range + Sequential IDs)")
    print("""  New users always receive the next auto-increment ID.
  IDs > 666 always route to shard-2 → it becomes a WRITE HOTSPOT.
  Shard-0 and shard-1 become COLD (idle capacity, no new writes).

  Simulating 200 new user signups (IDs 1001-1200):
""")
    new_counts = [0, 0, 0]
    for user_id in range(1001, 1201):
        idx = range_router.get_shard_index(user_id)
        new_counts[idx] += 1

    bar_chart(new_counts, 200)
    print("""
  Solutions to hotspot with range sharding:
    - Prefix IDs with a shard number (embed shard in the key itself)
    - Use UUID v4 (random) instead of sequential integers
    - Pre-split ranges: allocate wider ranges than current data volume
    - Hash before range: hash(user_id) → range shard (hybrid approach)
    - Consistent hashing (see Phase 6)
""")

    # ── Phase 5: Cross-shard query — sequential vs parallel ───────────────
    section("Phase 5: Cross-Shard Query — Sequential vs Parallel Scatter-Gather")
    print('  Finding all users named "Alice*" across all shards.')
    print('  Alice is inserted for every 20th user_id to ensure spread.\n')

    setup_schema(conns)
    hash_router = HashShardRouter(conns)
    for user_id in range(1, 1001):
        name = f"{'Alice' if user_id % 20 == 0 else random.choice(FIRST_NAMES)}{user_id}"
        email = f"user{user_id}@example.com"
        shard = hash_router.get_shard(user_id)
        if shard:
            with shard.cursor() as cur:
                cur.execute(
                    "INSERT INTO users VALUES (%s,%s,%s) ON CONFLICT DO NOTHING",
                    (user_id, name, email)
                )

    # Sequential scatter-gather
    print("  -- Sequential scatter-gather (naive implementation) --")
    start = time.perf_counter()
    all_alices_seq = []
    for i, conn in enumerate(conns):
        if conn:
            with conn.cursor() as cur:
                cur.execute("SELECT id, name FROM users WHERE name LIKE 'Alice%'")
                rows = cur.fetchall()
                all_alices_seq.extend(rows)
                print(f"    shard-{i}: {len(rows):3d} Alices")
    seq_elapsed = time.perf_counter() - start
    print(f"  Sequential total: {len(all_alices_seq)} Alices in {seq_elapsed*1000:.1f}ms")

    # Parallel scatter-gather
    print("\n  -- Parallel scatter-gather (production implementation) --")

    def query_shard(args):
        i, conn = args
        if not conn:
            return i, []
        with conn.cursor() as cur:
            cur.execute("SELECT id, name FROM users WHERE name LIKE 'Alice%'")
            return i, cur.fetchall()

    start = time.perf_counter()
    all_alices_par = []
    with ThreadPoolExecutor(max_workers=len(conns)) as executor:
        futures = {executor.submit(query_shard, (i, c)): i for i, c in enumerate(conns)}
        shard_results = {}
        for future in as_completed(futures):
            i, rows = future.result()
            shard_results[i] = rows
            all_alices_par.extend(rows)
    par_elapsed = time.perf_counter() - start

    for i in sorted(shard_results):
        print(f"    shard-{i}: {len(shard_results[i]):3d} Alices")
    print(f"  Parallel total:    {len(all_alices_par)} Alices in {par_elapsed*1000:.1f}ms")

    print(f"""
  Sequential latency = sum(shard latencies)  →  {seq_elapsed*1000:.1f}ms
  Parallel latency   = max(shard latencies)  →  {par_elapsed*1000:.1f}ms
  Parallel speedup:  {seq_elapsed/par_elapsed:.1f}x

  At 100 shards: a sequential cross-shard query is 100x slower.
  Even parallel scatter-gather at 100 shards generates 100 DB connections
  and 100 result sets that must be merged and sorted in the application.

  Production mitigations:
    - Secondary index service (Elasticsearch, dedicated lookup table)
    - Global secondary indexes (DynamoDB GSI, Vitess scatter)
    - Denormalize: store name→shard mappings in a separate lookup store
    - Accept scatter-gather cost only for infrequent admin queries
""")

    # ── Phase 6: Resharding cost — modulo vs consistent hashing ───────────
    section("Phase 6: Resharding Cost — Modulo Hashing vs Consistent Hashing")
    print("""  THE CORE RESHARDING PROBLEM:
  Your system starts with 3 shards. Traffic grows. You add a 4th shard.
  With naive modulo hashing (sha256(key) % N), changing N from 3 to 4
  forces almost all keys to move to different shards.
  Data migration at scale is expensive and risky.

  Measuring how many of 10,000 keys change shard when going 3 → 4 shards:
""")
    n_keys = 10_000
    keys = [str(i) for i in range(n_keys)]

    # Modulo: 3 → 4 shards
    modulo_3 = [_sha256_int(k) % 3 for k in keys]
    modulo_4 = [_sha256_int(k) % 4 for k in keys]
    modulo_moved = sum(1 for a, b in zip(modulo_3, modulo_4) if a != b)
    modulo_pct = modulo_moved / n_keys * 100

    # Consistent hashing: 3 → 4 shards
    ch_3 = ConsistentHashRouter(num_shards=3, vnodes_per_shard=150)
    ch_4 = ConsistentHashRouter(num_shards=4, vnodes_per_shard=150)
    ch_3_assignments = [ch_3.get_shard_index(int(k)) for k in keys]
    ch_4_assignments = [ch_4.get_shard_index(int(k)) for k in keys]
    ch_moved = sum(1 for a, b in zip(ch_3_assignments, ch_4_assignments) if a != b)
    ch_pct = ch_moved / n_keys * 100
    theoretical_ch_pct = 1 / 4 * 100  # adding 1 shard to 4 → ~25% should move

    print(f"  Modulo hashing  (sha256 % N):  {modulo_moved:5d} / {n_keys} keys moved  ({modulo_pct:.1f}%)")
    print(f"  Consistent hash (150 vnodes):  {ch_moved:5d} / {n_keys} keys moved  ({ch_pct:.1f}%)")
    print(f"  Theoretical minimum:           {int(n_keys * theoretical_ch_pct / 100):5d} / {n_keys} keys moved  ({theoretical_ch_pct:.1f}%)")

    print(f"""
  Modulo moved {modulo_pct:.0f}% of keys — nearly all data must be migrated.
  Consistent hashing moved ~{ch_pct:.0f}% — only the keys that naturally fall
  between the new shard and its predecessor on the ring move.

  How consistent hashing works:
    - Keys and shards are mapped to positions on a ring [0, 2^256)
    - Each key is assigned to the nearest shard CLOCKWISE on the ring
    - Virtual nodes (vnodes): each physical shard owns multiple ring positions
      → improves balance, especially with few shards
    - Adding a shard: insert its vnode positions; only keys between each new
      vnode and its predecessor move — all others are unaffected

  Why vnodes matter for balance:
    - With 1 vnode/shard: 3 random points on a ring → very uneven arcs
    - With 150 vnodes/shard: 450 points → arcs converge to ~equal thirds
    - Production systems: Cassandra uses 256 vnodes/node by default

  Used in: Amazon DynamoDB, Apache Cassandra, Redis Cluster (hash slots
  are a practical approximation of consistent hashing with 16384 slots)
""")

    # Vnode balance demo
    print("  Consistent hash balance with varying vnode counts:")
    print(f"  {'Vnodes/shard':<15} {'shard-0':>8} {'shard-1':>8} {'shard-2':>8} {'imbalance':>12}")
    for vnodes in [1, 5, 20, 50, 150]:
        ch = ConsistentHashRouter(num_shards=3, vnodes_per_shard=vnodes)
        counts = [0, 0, 0]
        for k in keys:
            counts[ch.get_shard_index(int(k))] += 1
        avg = n_keys / 3
        imb = (max(counts) - min(counts)) / avg * 100
        print(f"  {vnodes:<15} {counts[0]:>8} {counts[1]:>8} {counts[2]:>8} {imb:>11.1f}%")

    # ── Summary ────────────────────────────────────────────────────────────
    section("Summary")
    print("""
  Sharding Decision Framework:
  ─────────────────────────────
  DON'T shard until you have to:
    - Read replicas + caching handle most read-heavy workloads
    - Vertical scaling is simpler and more operationally sound
    - A well-tuned Postgres instance handles tens of thousands of writes/sec
    - Sharding adds enormous operational complexity: cross-shard txns,
      scatter-gather queries, resharding migrations, shard routing bugs

  Shard Key Selection (the most important decision):
    - HIGH CARDINALITY: many distinct values (user_id, not country)
    - EVENLY DISTRIBUTED: avoid skewed access patterns
    - STABLE: never changes after the row is inserted
    - ACCESS PATTERN ALIGNED: if 90% of queries filter by user_id, shard
      by user_id — misaligned shard keys force cross-shard queries

  Routing Strategy Comparison:
    Modulo hash:     even distribution, no range queries, resharding = migrate all data
    Range:           range queries on one shard, hotspot risk with sequential keys
    Consistent hash: even distribution, low resharding cost (~1/N keys move)
    Directory:       maximum flexibility, extra lookup hop, SPF risk on lookup store

  Cross-shard transactions:
    - ACID across shards requires 2-phase commit (2PC)
    - 2PC is slow, error-prone, and a distributed systems anti-pattern
    - Design data models so transactional operations stay within ONE shard
    - If you can't avoid cross-shard writes, use sagas / compensating transactions

  Logical shards vs physical shards:
    - Start with many logical shards (e.g., 1024) mapped to few physical hosts
    - Resharding = remap logical → physical, no key rehashing needed
    - Used by: Discord (Cassandra), DynamoDB (hash slots), Redis Cluster

  Next steps: ../06-caching/
""")


if __name__ == "__main__":
    main()
