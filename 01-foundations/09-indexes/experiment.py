#!/usr/bin/env python3
"""
Indexes Lab — B-tree, composite, partial indexes in PostgreSQL

Prerequisites: docker compose up -d (wait ~10s)

What this demonstrates:
  1. Sequential scan (no index) vs B-tree index on a single column
  2. Composite index: wrong column order (unused prefix) vs correct order
  3. Partial index: index only the subset of rows you query
  4. Write overhead: INSERT timing with 0 indexes vs 4 indexes
  5. EXPLAIN ANALYZE output: understanding cost, rows, actual time
"""

import re
import time
import random
import string
import subprocess
from datetime import datetime, timedelta

try:
    import psycopg2
    import psycopg2.extras
except ImportError:
    print("Installing dependencies...")
    subprocess.run(["pip", "install", "psycopg2-binary", "-q"], check=True)
    import psycopg2
    import psycopg2.extras

PG_DSN = "host=localhost port=5432 dbname=indextest user=postgres password=postgres connect_timeout=5"
NUM_ROWS = 500_000
CITIES = ["New York", "Los Angeles", "Chicago", "Houston", "Phoenix",
          "Philadelphia", "San Antonio", "San Diego", "Dallas", "San Jose"]


def section(title):
    print(f"\n{'=' * 64}")
    print(f"  {title}")
    print("=" * 64)


def rand_str(n=8):
    return "".join(random.choices(string.ascii_lowercase, k=n))


def get_conn():
    conn = psycopg2.connect(PG_DSN)
    conn.autocommit = True
    return conn


def extract_timing(explain_output):
    """Parse 'Execution Time: X ms' from EXPLAIN ANALYZE output."""
    for line in explain_output:
        m = re.search(r"Execution Time:\s+([\d.]+)\s+ms", line[0])
        if m:
            return float(m.group(1))
    return None


def extract_plan_type(explain_output):
    """Extract the top-level scan type from EXPLAIN output."""
    for line in explain_output:
        text = line[0]
        for scan in ["Seq Scan", "Index Scan", "Index Only Scan", "Bitmap Index Scan", "Bitmap Heap Scan"]:
            if scan in text:
                return scan
    return "Unknown"


def run_explain(conn, query, params=None, buffers=False):
    """Run EXPLAIN (ANALYZE, BUFFERS) and return rows."""
    prefix = "EXPLAIN (ANALYZE, BUFFERS, FORMAT TEXT)" if buffers else "EXPLAIN (ANALYZE, FORMAT TEXT)"
    full = f"{prefix} {query}"
    with conn.cursor() as cur:
        cur.execute(full, params)
        return cur.fetchall()


def seed_table(conn):
    """Create and seed the users table with NUM_ROWS rows."""
    with conn.cursor() as cur:
        cur.execute("DROP TABLE IF EXISTS users")
        cur.execute("""
            CREATE TABLE users (
                id         SERIAL PRIMARY KEY,
                email      TEXT NOT NULL,
                name       TEXT NOT NULL,
                age        INT NOT NULL,
                city       TEXT NOT NULL,
                created_at TIMESTAMPTZ NOT NULL
            )
        """)
    conn.autocommit = False
    try:
        base_date = datetime(2020, 1, 1)
        batch_size = 10_000
        print(f"  Seeding {NUM_ROWS:,} rows in batches of {batch_size:,}...")
        for batch_start in range(0, NUM_ROWS, batch_size):
            batch_end = min(batch_start + batch_size, NUM_ROWS)
            rows = []
            for i in range(batch_start, batch_end):
                rows.append((
                    f"user{i}@{rand_str(6)}.com",
                    f"User {i}",
                    random.randint(18, 80),
                    random.choice(CITIES),
                    base_date + timedelta(seconds=random.randint(0, 3 * 365 * 86400))
                ))
            with conn.cursor() as cur:
                psycopg2.extras.execute_batch(
                    cur,
                    "INSERT INTO users (email, name, age, city, created_at) VALUES (%s, %s, %s, %s, %s)",
                    rows,
                    page_size=1000
                )
            conn.commit()
            pct = (batch_end / NUM_ROWS) * 100
            print(f"    {batch_end:>7,} / {NUM_ROWS:,}  ({pct:.0f}%)", end="\r", flush=True)
        print(f"    {NUM_ROWS:,} rows inserted.                    ")
    finally:
        conn.autocommit = True


def drop_all_indexes(conn):
    """Drop all non-primary-key indexes on users."""
    with conn.cursor() as cur:
        cur.execute("""
            SELECT indexname FROM pg_indexes
            WHERE tablename = 'users'
              AND indexname != 'users_pkey'
        """)
        for (name,) in cur.fetchall():
            cur.execute(f"DROP INDEX IF EXISTS {name}")


def main():
    section("INDEXES LAB: B-tree, Composite, Partial Indexes")
    print(f"""
  Table: users ({NUM_ROWS:,} rows)
  Columns: id, email, name, age, city, created_at

  We run EXPLAIN ANALYZE for each query and extract:
    - Scan type (Seq Scan vs Index Scan)
    - Execution time (ms)

  A sequential scan reads every row in the table — O(n).
  A B-tree index scan jumps directly to matching rows — O(log n).
""")

    conn = get_conn()
    try:
        conn.cursor().execute("SELECT 1")
    except Exception as e:
        print(f"  ERROR: {e}")
        print("  Run: docker compose up -d")
        return

    # ── Seed ───────────────────────────────────────────────────────
    section("Seeding 500,000 rows")
    seed_table(conn)

    # Disable sequential scan for clearer demonstrations
    # (in production, Postgres chooses scan type based on statistics)
    with conn.cursor() as cur:
        cur.execute("ANALYZE users")
    print("  ANALYZE complete — statistics updated.")

    # ── Phase 1: Sequential Scan ───────────────────────────────────
    section("Phase 1: Sequential Scan — No Index on email")
    print("""
  Query: SELECT * FROM users WHERE email = 'user42@...'
  No index exists on email → Postgres reads ALL 500,000 rows.
""")
    drop_all_indexes(conn)

    # Pick a real email from the table
    with conn.cursor() as cur:
        cur.execute("SELECT email FROM users OFFSET 42 LIMIT 1")
        target_email = cur.fetchone()[0]

    rows = run_explain(conn, "SELECT * FROM users WHERE email = %s", (target_email,))
    seq_time = extract_timing(rows)
    scan_type = extract_plan_type(rows)
    print(f"  Scan type : {scan_type}")
    print(f"  Exec time : {seq_time:.2f} ms")
    print(f"  Top plan  : {rows[0][0].strip()}")
    print("""
  Sequential scan cost is O(n) — scales linearly with table size.
  At 1M rows it would take twice as long; at 10M rows, 10x as long.
""")

    # ── Phase 2: B-tree Index on email ────────────────────────────
    section("Phase 2: B-tree Index on email")
    print("""
  CREATE INDEX idx_users_email ON users(email);
  B-tree is the default index type in Postgres.
  Structure: balanced tree with O(log n) lookup.
  Perfect for equality (=) and range (<, >, BETWEEN) queries.
""")
    with conn.cursor() as cur:
        t0 = time.perf_counter()
        cur.execute("CREATE INDEX idx_users_email ON users(email)")
        idx_build_ms = (time.perf_counter() - t0) * 1000
    print(f"  Index build time: {idx_build_ms:.0f}ms")

    rows = run_explain(conn, "SELECT * FROM users WHERE email = %s", (target_email,))
    btree_time = extract_timing(rows)
    scan_type = extract_plan_type(rows)
    print(f"  Scan type : {scan_type}")
    print(f"  Exec time : {btree_time:.2f} ms")
    print(f"  Top plan  : {rows[0][0].strip()}")

    speedup = seq_time / btree_time if btree_time > 0 else float("inf")
    print(f"\n  Speedup over sequential scan: {speedup:.0f}x")
    print("""
  B-tree internals:
    - Root node → internal nodes → leaf nodes (contain actual row pointers)
    - Tree depth is O(log n): 500k rows = ~19 levels deep
    - Each level is typically one disk page (8KB) read
    - Equality lookup: traverse ~4 levels, fetch 1 heap page
    - Maintenance: INSERT/UPDATE/DELETE must update the tree (write overhead)
""")

    # ── Phase 3: Composite Index — Wrong Order ────────────────────
    section("Phase 3: Composite Index — Wrong Column Order")
    print("""
  Query: SELECT * FROM users WHERE age = 30 AND city = 'Chicago'

  Composite index: (city, age) — city is the LEFT-MOST prefix.
  But the query filters on age FIRST, then city.

  The LEFT-MOST PREFIX rule:
    Index (city, age) can be used for:
      WHERE city = ?              ✓ uses first column
      WHERE city = ? AND age = ? ✓ uses both columns
    But NOT for:
      WHERE age = ?               ✗ skips the first column
      WHERE age = ? AND city = ? ✗ same issue (optimizer may reorder,
                                    but read on for the nuance)
""")
    drop_all_indexes(conn)
    with conn.cursor() as cur:
        cur.execute("CREATE INDEX idx_city_age ON users(city, age)")

    # Query with age first — Postgres optimizer may still use it, but let's show
    rows_wrong = run_explain(conn,
        "SELECT * FROM users WHERE age = %s AND city = %s", (30, "Chicago"))
    wrong_time = extract_timing(rows_wrong)
    wrong_scan = extract_plan_type(rows_wrong)

    print(f"  Query: WHERE age = 30 AND city = 'Chicago'  (age first in SQL)")
    print(f"  Scan type : {wrong_scan}")
    print(f"  Exec time : {wrong_time:.2f} ms")
    print(f"  Top plan  : {rows_wrong[0][0].strip()}")
    print("""
  Note: Postgres's query optimizer is smart enough to reorder
  WHERE clause predicates to match the index prefix. So (city, age)
  IS used even when the SQL says "age = ? AND city = ?".

  The real trap: querying on ONLY the second column.
""")
    # Real trap: query only on age (skips prefix entirely)
    rows_age_only = run_explain(conn,
        "SELECT * FROM users WHERE age = %s", (30,))
    age_only_time = extract_timing(rows_age_only)
    age_only_scan = extract_plan_type(rows_age_only)
    print(f"  Query: WHERE age = 30  (only second column — prefix skipped)")
    print(f"  Scan type : {age_only_scan}")
    print(f"  Exec time : {age_only_time:.2f} ms")
    print("  → Seq scan because index on (city, age) cannot help when city is absent.")

    # ── Phase 4: Composite Index — Correct Order ──────────────────
    section("Phase 4: Composite Index — Correct Column Order")
    print("""
  Create index in the order your queries filter: (city, age).
  Now query: WHERE city = 'Chicago' AND age > 25
""")
    rows_correct = run_explain(conn,
        "SELECT * FROM users WHERE city = %s AND age > %s", ("Chicago", 25))
    correct_time = extract_timing(rows_correct)
    correct_scan = extract_plan_type(rows_correct)
    print(f"  Query: WHERE city = 'Chicago' AND age > 25")
    print(f"  Scan type : {correct_scan}")
    print(f"  Exec time : {correct_time:.2f} ms")
    print(f"  Top plan  : {rows_correct[0][0].strip()}")
    print("""
  Composite index rule:
    Put the EQUALITY columns first (city = 'X'), then RANGE columns (age > N).
    Put HIGH-CARDINALITY columns first for better selectivity.
    Index (city, age) works for: WHERE city=? / WHERE city=? AND age=?
    Index (city, age) does NOT help: WHERE age=? (missing leftmost prefix)
""")

    # ── Phase 5: Partial Index ─────────────────────────────────────
    section("Phase 5: Partial Index — WHERE age > 30")
    print("""
  A partial index only indexes rows satisfying a WHERE condition.
  Smaller index → faster lookups + less write overhead.

  Use case: "Find active users in New York"
  If 70% of users have age > 30, a partial index covers that subset
  with a much smaller tree.
""")
    drop_all_indexes(conn)
    with conn.cursor() as cur:
        t0 = time.perf_counter()
        cur.execute("""
            CREATE INDEX idx_city_age_partial
            ON users(city, age)
            WHERE age > 30
        """)
        partial_build_ms = (time.perf_counter() - t0) * 1000
    print(f"  Partial index build time: {partial_build_ms:.0f}ms")

    # Query that matches the partial index condition
    rows_partial = run_explain(conn,
        "SELECT * FROM users WHERE city = %s AND age > %s", ("New York", 35))
    partial_time = extract_timing(rows_partial)
    partial_scan = extract_plan_type(rows_partial)
    print(f"\n  Query: WHERE city = 'New York' AND age > 35  (matches partial)")
    print(f"  Scan type : {partial_scan}")
    print(f"  Exec time : {partial_time:.2f} ms")

    # Query that doesn't match (age = 20, which violates WHERE age > 30)
    rows_no_partial = run_explain(conn,
        "SELECT * FROM users WHERE city = %s AND age = %s", ("New York", 20))
    no_partial_time = extract_timing(rows_no_partial)
    no_partial_scan = extract_plan_type(rows_no_partial)
    print(f"\n  Query: WHERE city = 'New York' AND age = 20  (outside partial range)")
    print(f"  Scan type : {no_partial_scan}")
    print(f"  Exec time : {no_partial_time:.2f} ms")
    print("  → Seq scan because age=20 is excluded from the partial index.")

    print("""
  Partial index use cases:
    - Soft-delete: CREATE INDEX ON orders(user_id) WHERE deleted_at IS NULL
    - Active records: CREATE INDEX ON jobs(created_at) WHERE status = 'pending'
    - High-value rows: CREATE INDEX ON orders(customer_id) WHERE total > 1000
""")

    # ── Phase 6: Write Overhead ────────────────────────────────────
    section("Phase 6: Write Overhead — 0 Indexes vs 4 Indexes")
    print("""
  Every index slows down INSERT/UPDATE/DELETE because the index
  B-tree must be updated alongside the heap (table data).

  We'll INSERT 10,000 rows with zero indexes vs four indexes.
""")

    INSERT_COUNT = 10_000

    def insert_batch(conn, count):
        base_date = datetime(2023, 1, 1)
        rows = [
            (
                f"bench{i}@{rand_str(4)}.com",
                f"Bench {i}",
                random.randint(18, 80),
                random.choice(CITIES),
                base_date + timedelta(days=random.randint(0, 365))
            )
            for i in range(count)
        ]
        conn.autocommit = False
        try:
            with conn.cursor() as cur:
                psycopg2.extras.execute_batch(
                    cur,
                    "INSERT INTO users (email, name, age, city, created_at) VALUES (%s,%s,%s,%s,%s)",
                    rows,
                    page_size=500
                )
            conn.commit()
        finally:
            conn.autocommit = True

    # Zero indexes
    drop_all_indexes(conn)
    t0 = time.perf_counter()
    insert_batch(conn, INSERT_COUNT)
    no_idx_ms = (time.perf_counter() - t0) * 1000

    # Four indexes
    with conn.cursor() as cur:
        cur.execute("CREATE INDEX idx_email2     ON users(email)")
        cur.execute("CREATE INDEX idx_city_age2  ON users(city, age)")
        cur.execute("CREATE INDEX idx_created2   ON users(created_at)")
        cur.execute("CREATE INDEX idx_partial2   ON users(city, age) WHERE age > 30")

    t0 = time.perf_counter()
    insert_batch(conn, INSERT_COUNT)
    four_idx_ms = (time.perf_counter() - t0) * 1000

    overhead_pct = ((four_idx_ms - no_idx_ms) / no_idx_ms) * 100
    print(f"  INSERT {INSERT_COUNT:,} rows (0 indexes):  {no_idx_ms:.0f}ms  ({no_idx_ms/INSERT_COUNT:.2f}ms/row)")
    print(f"  INSERT {INSERT_COUNT:,} rows (4 indexes):  {four_idx_ms:.0f}ms  ({four_idx_ms/INSERT_COUNT:.2f}ms/row)")
    print(f"  Write overhead with 4 indexes: +{overhead_pct:.0f}%")
    print("""
  Write amplification: each index adds one B-tree update per row written.
  At extreme scale (Cassandra, HBase), this is why wide-column stores
  write to an in-memory structure (MemTable) first and flush later (LSM tree).

  Postgres mitigation:
    - FILLFACTOR: leave space in pages to avoid page splits on update
    - Partial indexes: smaller trees = less overhead
    - CONCURRENTLY: build index without locking table (but slower)
    - Drop indexes before bulk loads, rebuild after: pg_restore does this
""")

    # ── Summary ────────────────────────────────────────────────────
    section("Summary: Index Types and When to Use Each")
    print(f"""
  Results:
  ┌─────────────────────────────────────┬───────────────┬──────────────┐
  │ Experiment                          │ Scan Type     │ Time (ms)    │
  ├─────────────────────────────────────┼───────────────┼──────────────┤
  │ email lookup — no index             │ Seq Scan      │ {seq_time:>8.2f}     │
  │ email lookup — B-tree index         │ Index Scan    │ {btree_time:>8.2f}     │
  │ (city, age) — age-only query        │ Seq Scan      │ {age_only_time:>8.2f}     │
  │ (city, age) — city+age query        │ {correct_scan:<13s} │ {correct_time:>8.2f}     │
  │ partial (age>30) — matching query   │ {partial_scan:<13s} │ {partial_time:>8.2f}     │
  │ partial (age>30) — outside range    │ Seq Scan      │ {no_partial_time:>8.2f}     │
  └─────────────────────────────────────┴───────────────┴──────────────┘

  Index Types Cheat Sheet:
    B-tree   — equality, range, ORDER BY. Default. Use for almost everything.
    Hash     — equality only. Rarely better than B-tree in Postgres.
    GIN      — full-text search, JSONB, arrays. "Contains" queries.
    BRIN     — block range index. Huge tables with correlated physical order
               (e.g., time-series). Very small index, approximate.
    GiST     — geometric types, full-text. Flexible operator classes.

  Index Selectivity:
    High selectivity (few matching rows) → index is fast.
    Low selectivity (many matching rows, e.g., boolean column) → seq scan faster.
    Rule of thumb: index pays off when < 5-10% of rows match the query.

  Covering Index (Index Only Scan):
    CREATE INDEX ON users(city) INCLUDE (name, email);
    → Fetches data directly from index without touching heap.
    → Eliminates heap page fetch, fastest possible scan.

  Next: ../10-networking-basics/
""")


if __name__ == "__main__":
    main()
