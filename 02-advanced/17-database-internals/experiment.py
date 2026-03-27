#!/usr/bin/env python3
"""
Database Internals Lab — WAL, B-tree write patterns, MVCC + dead tuple accumulation,
simulated LSM-tree with probabilistic Bloom filter, and write amplification

Prerequisites: docker compose up -d (wait ~10s)

What this demonstrates:
  1. WAL (Write-Ahead Log): LSN advances with every write; WAL bytes per operation
  2. B-tree write patterns: random-key vs sequential-key inserts (page splits)
  3. MVCC: snapshot isolation + dead tuple accumulation visible in pg_stat_user_tables
  4. Simulated LSM-tree: memtable flush -> SSTables -> compaction, with a real
     probabilistic Bloom filter showing false positives
  5. Write amplification: quantifying how many bytes hit disk per logical write
"""

import hashlib
import json
import math
import random
import string
import subprocess
import threading
import time
from collections import defaultdict

try:
    import psycopg2
    import psycopg2.extras
except ImportError:
    print("Installing dependencies...")
    subprocess.run(["pip", "install", "psycopg2-binary", "-q"], check=True)
    import psycopg2
    import psycopg2.extras

PG_DSN = "host=localhost port=5432 dbname=dbinternals user=postgres password=postgres connect_timeout=5"


def section(title):
    print(f"\n{'=' * 65}")
    print(f"  {title}")
    print("=" * 65)


def get_pg(autocommit=False):
    conn = psycopg2.connect(PG_DSN)
    conn.autocommit = autocommit
    return conn


def wait_ready(timeout=30):
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            conn = get_pg()
            conn.close()
            print("  Postgres ready.")
            return True
        except Exception:
            time.sleep(1)
    print("  ERROR: Postgres not ready. Run: docker compose up -d")
    return False


# ── Phase 1: WAL (Write-Ahead Log) ───────────────────────────────────────────

def phase1_wal():
    section("Phase 1: WAL — Write-Ahead Log")
    print("""  Every mutation (INSERT/UPDATE/DELETE) is written to the WAL before
  touching any data page. The WAL is an append-only sequential file —
  sequential writes are fast even on spinning disks. Data pages require
  random I/O to the correct page offset.

  On crash, Postgres replays WAL records from the last checkpoint forward,
  restoring all committed transactions.  Uncommitted transactions are rolled
  back because their COMMIT record is absent.

  The same WAL stream is consumed by streaming replicas and logical
  decoding (logical replication, Debezium CDC).

  Key parameter: wal_level
    minimal  - just enough for crash recovery
    replica  - adds info needed for streaming replication
    logical  - adds full column values for logical decoding (CDC)
    (we set wal_level=logical in docker-compose.yml)
""")

    conn = get_pg(autocommit=True)
    cur = conn.cursor()

    cur.execute("CREATE TABLE IF NOT EXISTS wal_demo (id SERIAL PRIMARY KEY, val TEXT)")
    cur.execute("TRUNCATE wal_demo")

    # LSN before any writes
    cur.execute("SELECT pg_current_wal_lsn()")
    lsn_before = cur.fetchone()[0]
    print(f"  LSN before writes:  {lsn_before}")

    # 100 individual inserts (one WAL record per statement)
    for i in range(100):
        cur.execute("INSERT INTO wal_demo (val) VALUES (%s)", (f"row_{i}",))

    cur.execute("SELECT pg_current_wal_lsn()")
    lsn_after_inserts = cur.fetchone()[0]
    cur.execute("SELECT pg_wal_lsn_diff(%s, %s)", (lsn_after_inserts, lsn_before))
    wal_insert_bytes = cur.fetchone()[0]
    print(f"  LSN after 100 INSERTs:   {lsn_after_inserts}")
    print(f"  WAL bytes for 100 INSERTs: {wal_insert_bytes:,}  (~{wal_insert_bytes//100} bytes/row)")

    # Now measure WAL for 100 UPDATEs (updates generate more WAL than inserts)
    lsn_pre_update = lsn_after_inserts
    cur.execute("UPDATE wal_demo SET val = val || '_updated'")

    cur.execute("SELECT pg_current_wal_lsn()")
    lsn_after_updates = cur.fetchone()[0]
    cur.execute("SELECT pg_wal_lsn_diff(%s, %s)", (lsn_after_updates, lsn_pre_update))
    wal_update_bytes = cur.fetchone()[0]
    print(f"  WAL bytes for 100 UPDATEs: {wal_update_bytes:,}  (~{wal_update_bytes//100} bytes/row)")

    print(f"""
  UPDATEs generate more WAL than INSERTs (~{wal_update_bytes/wal_insert_bytes:.1f}x here) because:
    - UPDATE writes the new row version (heap insert) AND marks old version dead
    - With wal_level=logical, full row images (REPLICA IDENTITY) may be logged

  WAL segment files: each is 16MB by default (pg_wal/ directory).
  A checkpoint flushes dirty pages to disk and records the safe replay point.
  WAL before the checkpoint can be recycled.

  fsync gotcha: if fsync=off, WAL writes skip the kernel flush — writes appear
  faster but a power loss can corrupt the database. Never disable fsync in
  production (some cloud providers do this silently in dev tiers).
""")
    cur.close()
    conn.close()


# ── Phase 2: B-tree write patterns ───────────────────────────────────────────

def phase2_btree_writes():
    section("Phase 2: B-Tree Write Patterns — Sequential vs Random Key Inserts")
    print("""  B-trees store rows sorted by key. Each node is one disk page (~8KB in Postgres).
  Branching factor B ≈ 500 for integer keys.
  Tree height = ceil(log_B(N)):
    1M rows  → height 3
    1B rows  → height 4   (4 page reads for any point lookup)

  Sequential keys (SERIAL, UUIDv7):
    Each insert goes to the rightmost leaf page — the "hot" page stays in buffer
    pool. Page splits happen only at the right edge. Fill ratio stays ~100%.

  Random keys (UUIDv4):
    Insert can land on ANY leaf page. Each insert may evict a cold page from
    buffer pool, load the target page, insert, then possibly split it.
    Fill ratio degrades to 50-70% after many splits → more pages, more I/O.

  In production this matters: a UUID PK table with 100M rows uses ~40% more
  disk space than a BIGSERIAL PK table, and random-insert throughput drops
  significantly once the index exceeds available RAM.
""")

    conn = get_pg(autocommit=True)
    cur = conn.cursor()

    ROW_COUNT = 10_000

    # Sequential inserts (SERIAL key)
    cur.execute("DROP TABLE IF EXISTS seq_inserts")
    cur.execute("CREATE TABLE seq_inserts (id SERIAL PRIMARY KEY, val TEXT)")

    lsn_seq_start = None
    cur.execute("SELECT pg_current_wal_lsn()")
    lsn_seq_start = cur.fetchone()[0]

    t0 = time.perf_counter()
    psycopg2.extras.execute_batch(
        cur,
        "INSERT INTO seq_inserts (val) VALUES (%s)",
        [(f"v{i}",) for i in range(ROW_COUNT)],
        page_size=500,
    )
    seq_elapsed = time.perf_counter() - t0

    cur.execute("SELECT pg_current_wal_lsn()")
    lsn_seq_end = cur.fetchone()[0]
    cur.execute("SELECT pg_wal_lsn_diff(%s, %s)", (lsn_seq_end, lsn_seq_start))
    seq_wal = cur.fetchone()[0]

    # Random UUID inserts
    cur.execute("DROP TABLE IF EXISTS rand_inserts")
    cur.execute("CREATE TABLE rand_inserts (id UUID PRIMARY KEY DEFAULT gen_random_uuid(), val TEXT)")

    cur.execute("SELECT pg_current_wal_lsn()")
    lsn_rand_start = cur.fetchone()[0]

    t0 = time.perf_counter()
    psycopg2.extras.execute_batch(
        cur,
        "INSERT INTO rand_inserts (val) VALUES (%s)",
        [(f"v{i}",) for i in range(ROW_COUNT)],
        page_size=500,
    )
    rand_elapsed = time.perf_counter() - t0

    cur.execute("SELECT pg_current_wal_lsn()")
    lsn_rand_end = cur.fetchone()[0]
    cur.execute("SELECT pg_wal_lsn_diff(%s, %s)", (lsn_rand_end, lsn_rand_start))
    rand_wal = cur.fetchone()[0]

    print(f"  {ROW_COUNT:,} sequential-key inserts: {seq_elapsed:.3f}s  "
          f"({ROW_COUNT/seq_elapsed:,.0f} rows/sec)  WAL: {seq_wal:,} bytes")
    print(f"  {ROW_COUNT:,} random-key (UUID) inserts: {rand_elapsed:.3f}s  "
          f"({ROW_COUNT/rand_elapsed:,.0f} rows/sec)  WAL: {rand_wal:,} bytes")

    wal_ratio = rand_wal / seq_wal if seq_wal > 0 else 1.0
    if rand_elapsed > seq_elapsed:
        print(f"\n  Random inserts {rand_elapsed/seq_elapsed:.1f}x slower; "
              f"{wal_ratio:.1f}x more WAL bytes (page splits logged)")
    else:
        print(f"\n  Both fit in buffer pool — WAL difference ({wal_ratio:.1f}x) still visible.")
        print("  Difference magnifies at larger scales or when index exceeds RAM.")

    # Show index size comparison
    cur.execute("SELECT pg_size_pretty(pg_relation_size('seq_inserts_pkey'))")
    seq_idx_size = cur.fetchone()[0]
    cur.execute("SELECT pg_size_pretty(pg_relation_size('rand_inserts_pkey'))")
    rand_idx_size = cur.fetchone()[0]
    print(f"\n  Index sizes ({ROW_COUNT:,} rows):")
    print(f"    Sequential PK index: {seq_idx_size}")
    print(f"    Random UUID PK index: {rand_idx_size}  ← larger due to page splits + lower fill ratio")

    print("""
  Mitigation strategies:
    - Use BIGSERIAL or UUIDv7 (time-ordered) as primary key
    - If UUIDv4 is required: BRIN index on insertion-time column instead of B-tree
    - Rebuild fragmented indexes: REINDEX CONCURRENTLY <index>
    - Monitor bloat: pgstattuple extension or check pg_class.relpages vs actual
""")

    cur.execute("DROP TABLE seq_inserts")
    cur.execute("DROP TABLE rand_inserts")
    cur.close()
    conn.close()


# ── Phase 3: MVCC + dead tuple accumulation ───────────────────────────────────

def phase3_mvcc():
    section("Phase 3: MVCC — Snapshot Isolation + Dead Tuple Accumulation")
    print("""  Multi-Version Concurrency Control: writes create new row versions (tuples)
  rather than overwriting in place. Each tuple has xmin (inserted by) and
  xmax (deleted/updated by) transaction IDs. A reader's snapshot determines
  which version is visible.

  No read locks needed — reads and writes never block each other.
  Cost: dead tuples accumulate until VACUUM reclaims them.
""")

    conn_setup = get_pg(autocommit=True)
    cur = conn_setup.cursor()
    cur.execute("CREATE TABLE IF NOT EXISTS mvcc_demo (id INT PRIMARY KEY, balance INT)")
    cur.execute("TRUNCATE mvcc_demo")
    cur.execute("INSERT INTO mvcc_demo VALUES (1, 1000)")
    cur.close()
    conn_setup.close()

    # ── Snapshot isolation demo ────────────────────────────────────────────────
    conn_reader = get_pg()
    conn_writer = get_pg()
    conn_reader.set_isolation_level(psycopg2.extensions.ISOLATION_LEVEL_REPEATABLE_READ)
    conn_writer.set_isolation_level(psycopg2.extensions.ISOLATION_LEVEL_REPEATABLE_READ)

    cur_r = conn_reader.cursor()
    cur_w = conn_writer.cursor()

    # Reader starts first — takes snapshot at this point
    cur_r.execute("BEGIN")
    cur_r.execute("SELECT balance FROM mvcc_demo WHERE id = 1")
    balance_before_write = cur_r.fetchone()[0]
    print(f"  [Reader TX]  BEGIN  →  sees balance = {balance_before_write}  (snapshot taken)")

    # Writer commits a change
    cur_w.execute("BEGIN")
    cur_w.execute("UPDATE mvcc_demo SET balance = balance - 200 WHERE id = 1")
    conn_writer.commit()
    print(f"  [Writer TX]  UPDATE balance -= 200  →  COMMIT  (new version: 800)")

    # Reader still sees old version (snapshot isolation)
    cur_r.execute("SELECT balance FROM mvcc_demo WHERE id = 1")
    balance_in_reader = cur_r.fetchone()[0]
    print(f"  [Reader TX]  SELECT  →  sees balance = {balance_in_reader}  "
          f"({'CORRECT - old snapshot' if balance_in_reader == 1000 else 'unexpected'})")

    # New connection sees committed version
    conn_new = get_pg(autocommit=True)
    cur_new = conn_new.cursor()
    cur_new.execute("SELECT balance FROM mvcc_demo WHERE id = 1")
    balance_new_reader = cur_new.fetchone()[0]
    print(f"  [New reader] SELECT  →  sees balance = {balance_new_reader}  (fresh snapshot, sees committed write)")

    conn_reader.commit()

    print("""
  Heap tuple layout:
    Each row version (tuple) is stored in the heap file with:
      xmin  = transaction ID that inserted this version
      xmax  = transaction ID that deleted/updated this version (0 = still live)
    A reader computes visibility: xmin <= snapshot_xid <= xmax
""")

    # ── Dead tuple accumulation demo ───────────────────────────────────────────
    print("  Dead tuple accumulation:")
    print("  Running 500 UPDATEs on the same row to generate dead tuples...\n")

    conn_update = get_pg(autocommit=True)
    cur_u = conn_update.cursor()

    # Baseline dead tuples
    cur_u.execute("""
        SELECT n_live_tup, n_dead_tup
        FROM pg_stat_user_tables
        WHERE relname = 'mvcc_demo'
    """)
    row = cur_u.fetchone()
    live_before, dead_before = (row[0], row[1]) if row else (0, 0)

    for i in range(500):
        cur_u.execute("UPDATE mvcc_demo SET balance = %s WHERE id = 1", (1000 + i,))

    # Stats may need a moment to refresh (pg_stat_user_tables is sampled)
    time.sleep(0.5)
    cur_u.execute("""
        SELECT n_live_tup, n_dead_tup, last_autovacuum
        FROM pg_stat_user_tables
        WHERE relname = 'mvcc_demo'
    """)
    row = cur_u.fetchone()
    live_after, dead_after, last_av = (row[0], row[1], row[2]) if row else (0, 0, None)

    print(f"    Before 500 UPDATEs: live={live_before}, dead={dead_before}")
    print(f"    After  500 UPDATEs: live={live_after}, dead={dead_after}")
    print(f"    Last autovacuum ran: {last_av or 'not yet (may trigger soon)'}")

    print(f"""
  Each UPDATE creates a new tuple version and marks the old one dead (xmax set).
  {dead_after} dead tuples are sitting in the heap, wasting space and slowing scans.

  VACUUM reclaims dead tuples (marks pages reusable). VACUUM FULL compacts
  the heap (exclusive lock, rewrites table — avoid on production under load).

  Critical autovacuum tuning for hot tables:
    autovacuum_vacuum_scale_factor = 0.01   # trigger at 1% dead rows (default 20%)
    autovacuum_vacuum_cost_delay   = 2ms    # reduce I/O throttling
    autovacuum_vacuum_cost_limit   = 400    # allow more work per cycle

  Long-running transactions block VACUUM from reclaiming dead tuples —
  the oldest active transaction determines the minimum xmin horizon.
  A single forgotten idle-in-transaction session can cause table bloat.
""")

    for c in [conn_reader, conn_writer, conn_new, conn_update]:
        try:
            c.close()
        except Exception:
            pass


# ── Phase 4: Simulated LSM-tree with Bloom filter ────────────────────────────

def phase4_lsm_simulation():
    section("Phase 4: Simulated LSM-Tree — Memtable + SSTables + Compaction + Bloom Filter")
    print("""  LSM-tree design:
    All writes land in the memtable (in-memory sorted map).
    When memtable is full, it is flushed to disk as an immutable SSTable.
    SSTables are sorted — binary search within a file is O(log N).
    Compaction (background) merges SSTables: newer value wins on duplicate keys;
    tombstones suppress deleted keys.

  Bloom filter per SSTable:
    A space-efficient probabilistic structure.
    "Definitely not present" — if Bloom says no, skip the SSTable entirely.
    "Possibly present"       — if Bloom says yes, do the binary search.
    False positive rate ≈ (1 - e^(-kn/m))^k
      k = number of hash functions, n = entries, m = bit array size
    Tuned to <1% false positive rate in practice (e.g., RocksDB default).
""")

    # ── Probabilistic Bloom filter ─────────────────────────────────────────────

    class BloomFilter:
        """Counting Bloom filter using k double-hashing derived functions."""
        def __init__(self, capacity=100, fpr=0.01):
            self.m = max(1, math.ceil(-capacity * math.log(fpr) / (math.log(2) ** 2)))
            self.k = max(1, round((self.m / capacity) * math.log(2)))
            self.bits = bytearray(self.m)
            self._n = 0

        def _hashes(self, item):
            h1 = int(hashlib.md5(item.encode()).hexdigest(), 16)
            h2 = int(hashlib.sha1(item.encode()).hexdigest(), 16)
            for i in range(self.k):
                yield (h1 + i * h2) % self.m

        def add(self, item):
            for idx in self._hashes(item):
                self.bits[idx] = 1
            self._n += 1

        def __contains__(self, item):
            return all(self.bits[idx] for idx in self._hashes(item))

        @property
        def expected_fpr(self):
            if self._n == 0:
                return 0.0
            return (1 - math.exp(-self.k * self._n / self.m)) ** self.k

    # ── SSTable and LSM-tree ───────────────────────────────────────────────────

    class SSTable:
        """Immutable sorted file of key-value pairs with a Bloom filter."""
        def __init__(self, entries, level=0, table_id=0, capacity=200):
            self.data = sorted(entries, key=lambda x: x[0])
            self.level = level
            self.table_id = table_id
            self.bloom = BloomFilter(capacity=max(len(self.data), 1), fpr=0.01)
            for k, v in self.data:
                if v is not None:
                    self.bloom.add(k)
            self._bloom_checks = 0
            self._bloom_hits = 0   # key was in bloom AND in data
            self._bloom_fp = 0     # key was in bloom but NOT in data (false positive)

        def get(self, key):
            self._bloom_checks += 1
            if key not in self.bloom:
                return None, False, "bloom_skip"   # definite miss
            # Bloom says possibly present — do binary search
            lo, hi = 0, len(self.data) - 1
            while lo <= hi:
                mid = (lo + hi) // 2
                if self.data[mid][0] == key:
                    self._bloom_hits += 1
                    return self.data[mid][1], True, "found"
                elif self.data[mid][0] < key:
                    lo = mid + 1
                else:
                    hi = mid - 1
            self._bloom_fp += 1
            return None, False, "bloom_false_positive"

        def __repr__(self):
            return f"SSTable(L{self.level}:T{self.table_id}, {len(self.data)} entries)"


    class LSMTree:
        MEMTABLE_SIZE = 5
        L0_COMPACTION_THRESHOLD = 3

        def __init__(self):
            self.memtable = {}      # key -> value (None = tombstone)
            self.wal = []           # simplified in-memory WAL
            self.sstables = []      # newest first
            self._table_id = 0
            self.write_log = []
            self.write_amp_logical = 0   # bytes written by application
            self.write_amp_physical = 0  # bytes written to disk (flushes + compaction)

        def put(self, key, value):
            self.wal.append(("PUT", key, value))
            self.memtable[key] = value
            self.write_amp_logical += len(key) + len(str(value))
            self.write_log.append(f"  PUT {key}={value!r:12s} → memtable (size={len(self.memtable)})")
            if len(self.memtable) >= self.MEMTABLE_SIZE:
                self._flush()

        def delete(self, key):
            self.wal.append(("DEL", key, None))
            self.memtable[key] = None  # tombstone
            self.write_amp_logical += len(key)
            self.write_log.append(f"  DEL {key} → tombstone in memtable (size={len(self.memtable)})")
            if len(self.memtable) >= self.MEMTABLE_SIZE:
                self._flush()

        def _flush(self):
            entries = list(self.memtable.items())
            sst = SSTable(entries, level=0, table_id=self._table_id)
            self._table_id += 1
            self.sstables.insert(0, sst)  # newest first
            # Physical write: every entry in the flush counts
            flush_bytes = sum(len(k) + len(str(v or "")) for k, v in entries)
            self.write_amp_physical += flush_bytes
            self.write_log.append(f"  FLUSH memtable → {sst}")
            self.memtable = {}
            if sum(1 for s in self.sstables if s.level == 0) >= self.L0_COMPACTION_THRESHOLD:
                self._compact_level0()

        def _compact_level0(self):
            l0 = [s for s in self.sstables if s.level == 0]
            others = [s for s in self.sstables if s.level != 0]
            # Merge: iterate oldest→newest so newer values overwrite older ones
            merged = {}
            for sst in reversed(l0):
                for k, v in sst.data:
                    merged[k] = v
            # Drop tombstones (safe only if no older levels contain this key;
            # in a real multi-level LSM, tombstones must survive until they
            # reach the bottom level to suppress entries in lower SSTables)
            input_entries = sum(len(s.data) for s in l0)
            live_entries = {k: v for k, v in merged.items() if v is not None}
            compacted = SSTable(list(live_entries.items()), level=1, table_id=self._table_id)
            self._table_id += 1
            # Physical write: compacted SSTable counts as a physical write
            compact_bytes = sum(len(k) + len(str(v)) for k, v in live_entries.items())
            self.write_amp_physical += compact_bytes
            removed = input_entries - len(compacted.data)
            self.write_log.append(
                f"  COMPACT {len(l0)} L0 SSTables → {compacted} "
                f"(removed {removed} stale/deleted entries, "
                f"compaction wrote {compact_bytes} bytes)"
            )
            self.sstables = [compacted] + others

        def get(self, key):
            if key in self.memtable:
                val = self.memtable[key]
                return val, "memtable", None
            for sst in self.sstables:
                val, found, reason = sst.get(key)
                if found:
                    return val, str(sst), reason
                if reason == "bloom_false_positive":
                    return None, str(sst), reason
            return None, "not_found", None

        def write_amplification_factor(self):
            if self.write_amp_logical == 0:
                return 0.0
            return self.write_amp_physical / self.write_amp_logical

    # ── Run the demo ───────────────────────────────────────────────────────────
    lsm = LSMTree()

    print("  Writing 14 entries (memtable capacity=5 → multiple flushes + compaction):\n")

    writes = [
        ("user:1", "alice"),    ("user:2", "bob"),     ("user:3", "carol"),
        ("user:4", "dave"),     ("user:5", "eve"),
        ("user:1", "alice_v2"), ("user:6", "frank"),   ("user:7", "grace"),
        ("user:8", "henry"),    ("user:9", "ivy"),
        ("user:2", None),       # delete bob via tombstone
        ("user:10", "judy"),    ("user:11", "kevin"),  ("user:12", "laura"),
    ]

    for key, val in writes:
        if val is None:
            lsm.delete(key)
        else:
            lsm.put(key, val)

    # Force-flush any remaining memtable entries
    if lsm.memtable:
        lsm._flush()

    for line in lsm.write_log:
        print(line)

    print(f"\n  Current SSTables: {lsm.sstables}")
    print(f"  Remaining memtable: {dict(lsm.memtable)}")

    # ── Read demo ──────────────────────────────────────────────────────────────
    print("\n  Read results (showing Bloom filter behavior):")
    test_keys = [
        "user:1",   # updated — should return alice_v2
        "user:2",   # deleted — tombstone
        "user:3",   # plain value
        "user:7",   # in an SSTable
        "user:99",  # never written — Bloom should skip all SSTables
        "user:999", # never written
    ]
    for key in test_keys:
        val, source, reason = lsm.get(key)
        if val is not None:
            status = f"= {val!r}"
        else:
            status = "= (deleted)" if source != "not_found" else "= (not found)"
        bloom_note = f"  [{reason}]" if reason else ""
        print(f"    GET {key:10s}  {status:20s}  from: {source}{bloom_note}")

    # ── Bloom filter stats ─────────────────────────────────────────────────────
    print("\n  Bloom filter stats per SSTable:")
    for sst in lsm.sstables:
        print(f"    {sst}:  checks={sst._bloom_checks}, "
              f"true_hits={sst._bloom_hits}, false_positives={sst._bloom_fp}, "
              f"expected_fpr={sst.bloom.expected_fpr:.4f} ({sst.bloom.expected_fpr*100:.2f}%)")

    # ── Write amplification ────────────────────────────────────────────────────
    waf = lsm.write_amplification_factor()
    print(f"""
  Write Amplification Factor (WAF): {waf:.2f}x
    Logical bytes written (by app):  {lsm.write_amp_logical:,}
    Physical bytes written (flushes + compaction): {lsm.write_amp_physical:,}

  In production RocksDB with leveled compaction: WAF ≈ 10–30x
  Each logical byte may be rewritten at L0→L1, L1→L2, L2→L3...
  At 100k writes/sec and 512-byte values: ~50 MB/s logical, ~1.5 GB/s physical I/O.
  Disk I/O budget must account for compaction amplification, not just application writes.

  Key observations:
    user:1  → latest version wins (alice_v2 over alice) — newer SSTable checked first
    user:2  → tombstone present → returns None (deleted)
    user:99 → Bloom filter returns "not present" for each SSTable — zero binary searches
    Compaction merged L0 SSTables, removed stale versions, dropped tombstones
""")


# ── Phase 5: Write amplification quantification ───────────────────────────────

def phase5_write_amplification():
    section("Phase 5: Write Amplification — Postgres WAL + Heap + Index")
    print("""  Every write in Postgres touches multiple locations:
    1. WAL (sequential write)         — the durability record
    2. Heap page (random write)       — where the row lives
    3. Each index page (random write) — one write per index per row
    4. Visibility map (occasional)    — tracks all-visible pages for seq scans
    5. Free space map (occasional)    — tracks page free space for inserts

  Write amplification factor = total bytes written to storage / app payload bytes

  For a simple INSERT of a 100-byte row with 2 indexes:
    WAL record     ≈ 200 bytes  (row + overhead)
    Heap tuple     ≈ 128 bytes  (row + tuple header)
    Index entry ×2 ≈  50 bytes each
    Total physical ≈ 428 bytes for 100 bytes of user data → WAF ≈ 4x

  Measuring WAL amplification for INSERT vs UPDATE vs DELETE:
""")

    conn = get_pg(autocommit=True)
    cur = conn.cursor()

    cur.execute("""
        CREATE TABLE IF NOT EXISTS waf_demo (
            id SERIAL PRIMARY KEY,
            name TEXT,
            score INT,
            ts TIMESTAMPTZ DEFAULT now()
        )
    """)
    cur.execute("CREATE INDEX IF NOT EXISTS waf_demo_name_idx ON waf_demo(name)")
    cur.execute("CREATE INDEX IF NOT EXISTS waf_demo_score_idx ON waf_demo(score)")
    cur.execute("TRUNCATE waf_demo RESTART IDENTITY")

    N = 1000
    payload_bytes = N * (8 + 20 + 4 + 8)  # id + name + score + ts rough estimate

    operations = [
        ("INSERT", lambda: psycopg2.extras.execute_batch(
            cur,
            "INSERT INTO waf_demo (name, score) VALUES (%s, %s)",
            [(f"user_{i:04d}", random.randint(1, 1000)) for i in range(N)],
            page_size=250,
        )),
        ("UPDATE", lambda: psycopg2.extras.execute_batch(
            cur,
            "UPDATE waf_demo SET score = %s WHERE id = %s",
            [(random.randint(1, 1000), i) for i in range(1, N + 1)],
            page_size=250,
        )),
        ("DELETE", lambda: cur.execute("DELETE FROM waf_demo WHERE id <= %s", (N // 2,))),
    ]

    for op_name, op_fn in operations:
        cur.execute("SELECT pg_current_wal_lsn()")
        lsn_before = cur.fetchone()[0]
        op_fn()
        cur.execute("SELECT pg_current_wal_lsn()")
        lsn_after = cur.fetchone()[0]
        cur.execute("SELECT pg_wal_lsn_diff(%s, %s)", (lsn_after, lsn_before))
        wal_bytes = cur.fetchone()[0]
        waf = wal_bytes / payload_bytes if payload_bytes > 0 else 0
        count = N // 2 if op_name == "DELETE" else N
        print(f"    {op_name:6s} {count:,} rows:  WAL={wal_bytes:,} bytes  "
              f"(~{wal_bytes//count} bytes/row WAL overhead)")

    print(f"""
  DELETE generates significant WAL because it marks each tuple dead in the heap
  and updates index entries. With HOT (Heap Only Tuple) updates — where only
  non-indexed columns change — Postgres avoids index updates and reduces WAF.

  HOT updates: when an UPDATE only changes non-indexed columns AND the new
  tuple fits on the same heap page, Postgres skips updating all index entries.
  This can reduce write amplification by 2–4x for update-heavy workloads.

  Monitor HOT efficiency:
    SELECT n_tup_upd, n_tup_hot_upd,
           round(n_tup_hot_upd::numeric / nullif(n_tup_upd,0) * 100, 1) AS hot_pct
    FROM pg_stat_user_tables WHERE relname = 'waf_demo';
""")

    cur.execute("""
        SELECT n_tup_upd, n_tup_hot_upd,
               round(n_tup_hot_upd::numeric / nullif(n_tup_upd,0) * 100, 1) AS hot_pct
        FROM pg_stat_user_tables WHERE relname = 'waf_demo'
    """)
    row = cur.fetchone()
    if row:
        print(f"    waf_demo: total_updates={row[0]}, hot_updates={row[1]}, HOT%={row[2]}")
        print("    (HOT% is low here because we update 'score' which is indexed)")

    cur.execute("DROP TABLE IF EXISTS waf_demo")
    cur.close()
    conn.close()


def main():
    section("DATABASE INTERNALS LAB")
    print("""
  Two storage engine families:
    B-tree  — read-optimized, sorted pages, in-place updates via WAL
    LSM     — write-optimized, append-only, compaction merges sorted files

  Topics:
    1. WAL: sequential durability log, LSN tracking, crash recovery, replication
    2. B-tree writes: sequential keys vs random UUID keys (page splits, index bloat)
    3. MVCC: snapshot isolation + dead tuple accumulation + autovacuum
    4. LSM simulation: memtable -> SSTable flush -> compaction + probabilistic Bloom filter
    5. Write amplification: quantifying WAL overhead per operation type (INSERT/UPDATE/DELETE)
""")

    if not wait_ready():
        return

    phase1_wal()
    phase2_btree_writes()
    phase3_mvcc()
    phase4_lsm_simulation()
    phase5_write_amplification()

    section("Summary — Storage Engine Cheat Sheet")
    print("""
  B-tree (Postgres, MySQL InnoDB, SQLite)
    reads:   O(log_B N) disk pages, branching factor B ≈ 500
    writes:  WAL + heap page + index page(s), random I/O for random keys
    MVCC:    dead tuples accumulate → VACUUM required
    HOT:     non-indexed updates skip index writes → lower WAF
    use:     OLTP, mixed read/write, point lookups, range scans

  LSM-tree (RocksDB, Cassandra, LevelDB, ScyllaDB, InfluxDB)
    writes:  memtable (memory) → SSTable flush (sequential) → compaction
    reads:   memtable + SSTables newest→oldest (Bloom filters skip most)
    WAF:     10–30x in production (compaction rewrites data multiple times)
    tombstones: deletes are markers, removed only when compacted to bottom level
    use:     write-heavy, time-series, append-only, key-value at scale

  WAL (both engines)
    sequential append-only log written before data pages
    enables: crash recovery (replay from checkpoint), streaming replication
    fsync:   must be ON in production — disabling risks corruption on power loss

  MVCC (Postgres, MySQL, CockroachDB)
    xmin/xmax per tuple determine visibility at reader's snapshot time
    reads never block writes; writes never block reads
    cost: dead tuples → VACUUM; long transactions block VACUUM → table bloat

  Write Amplification Factor
    B-tree INSERT: WAF ≈ 3–5x (WAL + heap + indexes)
    LSM compaction: WAF ≈ 10–30x (data rewritten across levels)
    Monitor: pg_stat_user_tables (n_tup_hot_upd), iostat, RocksDB properties

  Column storage (Parquet, Redshift, BigQuery, DuckDB)
    values stored column-by-column → compression (RLE, delta, dictionary)
    vectorized execution: SIMD over 8–32 values per instruction
    10–100x faster for analytical aggregations over large datasets
    terrible for OLTP: reconstructing a row requires reading N column files

  Next: ../18-backpressure-flow-control/
""")


if __name__ == "__main__":
    main()
