# Indexes

**Prerequisites:** `../08-databases-sql-vs-nosql/`
**Next:** `../10-networking-basics/`

---

## Concept

A database index is a separate data structure that stores a sorted subset of a table's data alongside pointers to the full rows. Without an index, the database must read every row in the table to find matching records — a sequential scan that costs O(n) time and I/O. With an index on the right column, the database navigates a B-tree in O(log n) time, then fetches only the matching rows from the heap. The difference between a 50ms query and a 50-second query on a large table is almost always the presence or absence of the right index.

Indexes are not free. Every index must be updated on every INSERT, UPDATE, and DELETE that touches the indexed column. A table with ten indexes on it takes roughly ten times as many write operations as a table with no indexes. On write-heavy workloads, this write amplification is the primary tuning lever — dropping indexes before a bulk load and rebuilding them after is a standard technique. The cost of an index is paid at write time; the benefit is collected at read time.

Index **selectivity** determines whether an index is actually useful. A highly selective index covers a small fraction of rows — an index on `email` in a users table, where every email is unique, points to exactly one row per lookup. A low-selectivity index covers a large fraction — an index on a boolean `is_active` column where 99% of rows are `true` is nearly useless because the database would need to fetch almost every row anyway, making a sequential scan faster. The query planner estimates selectivity from table statistics (maintained by ANALYZE / VACUUM) and chooses the scan type with the lowest estimated cost.

The **left-most prefix rule** governs composite indexes. A composite index on (city, age) stores rows sorted first by city, then by age within each city. This structure enables lookups on city alone, or on city+age together. It cannot support a lookup on age alone because age values are not globally sorted within the index — they are only sorted within each city group. Violating this rule is one of the most common index mistakes: creating an index on (a, b, c) and then querying `WHERE b = ? AND c = ?` results in a sequential scan.

## How It Works

**B-Tree Index Lookup Path:**
1. Query arrives: `SELECT * FROM users WHERE email = 'alice@example.com'`
2. Query planner checks available indexes and table statistics; selects the B-tree index on `email` (O(log n) vs O(n) sequential scan)
3. Index scan starts at the **root node** — one 8KB page read, binary search finds the correct child pointer
4. Follow the pointer to an **interior node** and repeat — typically 3–4 levels for a 500K-row table (3–4 page reads total)
5. Reach the **leaf node** containing the matching index entry: `(email value, ctid pointer to heap tuple)`
6. Fetch the full row from the **heap** using the ctid — one additional page read
7. **Covering index shortcut:** if the index includes all columns the query needs (`INCLUDE (name, email)`), step 6 is skipped — the result is returned directly from the index (Index Only Scan)

### B-tree Structure

A B-tree (Balanced Tree) is a self-balancing tree where every leaf node is at the same depth. Each node holds multiple keys — in PostgreSQL, each node is one 8KB page. Interior nodes hold separator keys and pointers to child nodes. Leaf nodes hold the actual index entries (key value + pointer to heap tuple). A lookup traverses from root to leaf, reading one page at each level. For a 500,000-row table, the B-tree is roughly 3-4 levels deep — meaning an indexed lookup reads 3-4 pages versus potentially thousands of pages in a sequential scan.

Postgres maintains B-tree balance on every modification: INSERT may require a page split (one node becomes two), DELETE may require a page merge. The FILLFACTOR storage parameter (default 90%) leaves 10% of each page empty, deferring splits on UPDATE workloads where keys are updated in-place.

### Index Types

**B-tree** is the default. Supports equality (`=`), range (`<`, `>`, `BETWEEN`), pattern prefix (`LIKE 'abc%'`), and `IS NULL`. Use B-tree for almost everything.

**Hash** indexes only support equality (`=`). In Postgres, hash indexes are not faster than B-tree for equality lookups in practice and are rarely worth choosing.

**GIN (Generalized Inverted Index)** indexes multi-valued data: arrays, JSONB fields, full-text tsvector. A GIN index on a JSONB column lets you query `WHERE data @> '{"key": "value"}'` efficiently. GIN stores an index entry for every element within each row's value — making it large but powerful for "contains" queries.

**BRIN (Block Range INdex)** stores the minimum and maximum value of a column for each range of physical disk blocks. It is extremely small (a few kilobytes for a billion-row table) but only useful when the physical order of rows correlates with query order — typically time-series data inserted in chronological order. A BRIN index on `created_at` for an events table is a practical choice: each block of rows was inserted at approximately the same time, so block ranges are tight.

**GiST (Generalized Search Tree)** supports geometric types (PostGIS), full-text search, and custom operator classes. Enables nearest-neighbor searches and polygon intersection queries.

### Composite Index Left-Most Prefix Rule

```
Index: (city, age)

Can use this index:
  WHERE city = 'NYC'                    ← uses first column
  WHERE city = 'NYC' AND age = 30       ← uses both columns
  WHERE city = 'NYC' AND age > 25       ← equality then range

Cannot use this index:
  WHERE age = 30                        ← skips first column
  WHERE age BETWEEN 20 AND 40          ← skips first column
```

**Column order in composite indexes:**
1. Put equality-filtered columns first (`WHERE city = ?`)
2. Put range-filtered columns after (`WHERE age > ?`)
3. Put high-cardinality columns first (improves selectivity)
4. If you frequently query on column B alone, create a separate index on B

### Covering Indexes (Index Only Scan)

A covering index includes all columns the query needs, so the database never needs to visit the heap:

```sql
CREATE INDEX ON users(city) INCLUDE (name, email);
SELECT name, email FROM users WHERE city = 'NYC';
-- → Index Only Scan: fetches data from index, never touches heap
```

This eliminates the heap fetch step — the most expensive part of a regular index scan for queries returning many rows.

**Visibility Map dependency:** Postgres cannot skip the heap entirely unless it can confirm the row is visible to all transactions. It uses the **visibility map** — a 1-bit-per-page bitmap tracking pages where all tuples are visible. `VACUUM` sets these bits. On a freshly vacuumed table, `Heap Fetches: 0` appears in EXPLAIN ANALYZE for an Index Only Scan. On a high-churn table with stale visibility map, the planner may fall back to heap fetches, degrading to a regular index scan. Monitor with: `EXPLAIN (ANALYZE, BUFFERS)` and check the `Heap Fetches` line.

### Functional (Expression) Indexes

A B-tree index on `email` stores original values. A query `WHERE LOWER(email) = 'user@example.com'` cannot use it — the transformation `LOWER()` produces values the index has never seen. Fix by indexing the expression itself:

```sql
CREATE INDEX ON users(LOWER(email));
-- Now this query uses the index:
SELECT * FROM users WHERE LOWER(email) = 'user@example.com';
```

Any deterministic expression works: `LOWER()`, `UPPER()`, `EXTRACT(year FROM created_at)`, `(data->>'status')` for JSONB. The expression in the WHERE clause must exactly match the expression in the index definition for the planner to use it.

### Partial Indexes

A partial index only indexes rows satisfying a WHERE condition:

```sql
CREATE INDEX ON orders(user_id) WHERE status = 'pending';
```

This index is smaller (only pending orders, not all orders), faster to update (only touched when inserting/updating pending orders), and highly selective (pending orders are a fraction of all orders). Queries that include the partial index predicate use it; queries without it fall back to a full scan.

### Trade-offs

| Index Type | Lookup | Range | Multi-value | Size | Write Cost | Best For |
|------------|--------|-------|-------------|------|------------|----------|
| B-tree | O(log n) | Yes | No | Medium | Medium | General purpose |
| Hash | O(1) | No | No | Small | Low | Equality only |
| GIN | O(log n) | No | Yes | Large | High | Arrays, JSONB, FTS |
| BRIN | O(n/block) | Yes | No | Tiny | Tiny | Time-series, append-only |
| GiST | O(log n) | Depends | Yes | Medium | Medium | Geospatial, FTS |
| Partial | O(log m)* | Yes | No | Small | Low | Filtered subsets |

*m = matching rows, not total rows

### Failure Modes

**Index not used due to function calls:** `WHERE LOWER(email) = 'user@example.com'` cannot use an index on `email` because the index stores the original values, not the lowercased versions. Fix: create a functional index `CREATE INDEX ON users(LOWER(email))` or store email lowercase.

**Index bloat:** deleted rows leave dead entries in index pages. High-churn tables (frequent UPDATE/DELETE) accumulate bloat that makes indexes larger and slower. Fix: `VACUUM` reclaims space; `REINDEX CONCURRENTLY` rebuilds the index from scratch without locking.

**Optimizer choosing wrong plan:** if table statistics are stale (after bulk INSERT without ANALYZE), the query planner may underestimate row counts and choose a sequential scan when an index scan would be faster. Fix: run `ANALYZE tablename` after bulk loads.

**Too many indexes slowing writes:** a table with 15 indexes on it may be acceptable for read-heavy OLAP workloads, but on an OLTP table taking thousands of writes per second, each index adds write latency. Monitor `pg_stat_user_indexes.idx_scan` — indexes with zero scans in the last week can usually be dropped.

**HOT updates broken by indexed column changes:** Postgres implements Heap Only Tuple (HOT) updates — if an UPDATE changes only non-indexed columns and the new tuple fits on the same heap page, Postgres chains old and new tuples without touching any index. This eliminates index write overhead for that update. But any update to a column that is part of any index disables HOT entirely, forcing every index on the table to be updated. Over-indexing not only adds per-write overhead but disables HOT for a wider class of updates, compounding write cost on high-churn columns.

**Concurrent index builds and visibility lag:** `CREATE INDEX CONCURRENTLY` avoids locking the table for writes but requires two full table scans and can take significantly longer than a standard `CREATE INDEX`. The index is not usable by queries until the build finishes and the catalog is committed. If you monitor `pg_indexes` during the build, the index row appears with `indisvalid = false` — queries will not use it until it flips to `true`. Under load, never assume a concurrent index is immediately available.

## Interview Talking Points

- "A sequential scan is O(n) — reading every row. A B-tree index scan is O(log n) — traversing a balanced tree to the matching leaf. But 'O(log n)' is misleading without context: a Postgres B-tree node is an 8KB page holding hundreds of keys, so a 500k-row table is only 3-4 levels deep, not log2(500k)=19 levels. An index lookup is 3-4 page reads; a seq scan is thousands."
- "The left-most prefix rule: an index on (city, age) can only be used when the query includes city in the WHERE clause. Queries on age alone get a sequential scan. Always put equality columns before range columns in a composite index. The query optimizer can reorder AND predicates to match the prefix, but it cannot conjure the leading column if it is absent from the query."
- "Indexes have a write cost — every INSERT/UPDATE/DELETE must update every index on the table. On write-heavy workloads, index proliferation kills throughput. Additionally, Postgres's HOT (Heap Only Tuple) optimization — which avoids index updates for updates to non-indexed columns — is disabled the moment an updated column is indexed anywhere. Monitor unused indexes with pg_stat_user_indexes and drop them."
- "Partial indexes are underused: CREATE INDEX ON orders(user_id) WHERE status='pending' creates a small, fast index only for pending orders. Perfect for queries that always include the partial predicate. A soft-delete pattern — WHERE deleted_at IS NULL — is another classic use case; deleted rows are excluded from the index entirely."
- "Covering indexes (INCLUDE) can eliminate heap fetches entirely for the right queries. The scan type changes from 'Index Scan' to 'Index Only Scan' in EXPLAIN ANALYZE. But this only works if the visibility map is current — stale visibility maps force heap checks even on an Index Only Scan. Regular VACUUM keeps it current."
- "Functional indexes solve the most common production index bug: WHERE LOWER(email) = '...' cannot use an index on email. The fix is CREATE INDEX ON users(LOWER(email)). Any non-trivial transformation in a WHERE clause breaks index usage unless a matching functional index exists."
- "EXPLAIN (ANALYZE, BUFFERS) shows the actual execution plan, actual row counts, and I/O page reads. Key things to look for: 'Rows Removed by Filter' (work a missing index would eliminate), 'Heap Fetches' on Index Only Scans (non-zero means visibility map is stale), and 'shared hit/read' buffers (hit = served from cache, read = disk I/O)."

## Hands-on Lab

**Time:** ~15-20 minutes
**Services:** postgres (5432) with 256MB shared_buffers

### Setup

```bash
cd system-design-interview/01-foundations/09-indexes/
docker compose up -d
# Wait ~10 seconds for Postgres to be ready
```

### Experiment

```bash
python experiment.py
```

The script seeds 500,000 rows, then runs EXPLAIN ANALYZE for eight scenarios: sequential scan, B-tree index, composite index with wrong-order query (age-only lookup), composite index with correct-order query (city+age), partial index (inside and outside the predicate), covering index (Index Only Scan vs heap-fetching Index Scan), functional index on LOWER(email) (with and without), and write overhead comparison (0 vs 4 indexes).

### Break It

Force a sequential scan even when an index exists:

```bash
python -c "
import psycopg2
conn = psycopg2.connect('host=localhost port=5432 dbname=indextest user=postgres password=postgres')
conn.autocommit = True
with conn.cursor() as cur:
    # Force seq scan
    cur.execute('SET enable_indexscan = off')
    cur.execute(\"EXPLAIN ANALYZE SELECT * FROM users WHERE email LIKE 'user42%'\")
    for row in cur.fetchall():
        print(row[0])
    print()
    # Re-enable and compare
    cur.execute('SET enable_indexscan = on')
    cur.execute(\"EXPLAIN ANALYZE SELECT * FROM users WHERE email LIKE 'user42%'\")
    for row in cur.fetchall():
        print(row[0])
"
```

Demonstrate index bloat from updates:

```bash
python -c "
import psycopg2, random
conn = psycopg2.connect('host=localhost port=5432 dbname=indextest user=postgres password=postgres')
conn.autocommit = True
with conn.cursor() as cur:
    # Check index size before
    cur.execute(\"SELECT pg_size_pretty(pg_relation_size('idx_email2'))\")
    print('Index size before:', cur.fetchone()[0])
    # 10k updates (creates dead tuples in index)
    for _ in range(1000):
        cur.execute('UPDATE users SET name = name || chr(32) WHERE id = %s', (random.randint(1, 500000),))
    cur.execute(\"SELECT pg_size_pretty(pg_relation_size('idx_email2'))\")
    print('Index size after 1k updates:', cur.fetchone()[0])
    print('Run VACUUM ANALYZE users to reclaim space.')
"
```

### Observe

Expected output:
```
Phase 1 (no index):                Seq Scan        ~80-200ms
Phase 2 (B-tree email):            Index Scan      ~0.1-1ms      (100-1000x speedup)
Phase 3 (wrong order, age only):   Seq Scan        ~50-150ms
Phase 4 (correct order, city+age): Index Scan      ~0.5-5ms
Phase 5 (partial, hit):            Index Scan      ~0.5-3ms
Phase 5 (partial, miss):           Seq Scan        ~50-150ms
Phase 6 (covering, SELECT *):      Index Scan      ~5-30ms       (heap fetch required)
Phase 6 (covering, SELECT 2 cols): Index Only Scan ~3-15ms       (no heap fetch)
Phase 7 (LOWER, no func index):    Seq Scan        ~80-200ms
Phase 7 (LOWER, func index):       Index Scan      ~0.1-1ms
Phase 8 (write overhead):          4 indexes adds ~20-60% write latency vs 0 indexes
```

### Teardown

```bash
docker compose down -v
```

## Real-World Examples

- **PostgreSQL at Notion:** Notion migrated their primary database from MongoDB to Postgres. One of their key optimizations was adding partial indexes on `WHERE deleted_at IS NULL` for soft-delete patterns — reducing index size by 30-40% since most rows are not deleted — source: Notion Engineering Blog, "Herding elephants: Lessons learned from sharding Postgres at Notion" (2021).
- **MySQL EXPLAIN at GitHub:** GitHub's MySQL team published their practice of requiring EXPLAIN output review for all schema migrations. They found that adding indexes on foreign key columns (often missed by developers) eliminated full-table joins that degraded under load — source: GitHub Engineering Blog, "MySQL infrastructure testing at GitHub" (2019).
- **Cassandra LSM vs B-tree:** Cassandra deliberately avoids B-tree indexes for its primary storage engine. Instead it uses an LSM tree (Log-Structured Merge Tree): writes go to an in-memory MemTable (no index update latency), then flush to disk as immutable SSTables. Read latency is higher (must merge multiple SSTables), but write throughput is orders of magnitude higher than a B-tree store. This is the fundamental trade-off behind NoSQL write scalability — source: Cassandra documentation, "How Cassandra reads and writes data."

## Common Mistakes

- **Creating an index on a low-cardinality column.** An index on `is_active BOOLEAN` where 95% of rows are `true` gives minimal benefit for `WHERE is_active = true` queries because the database still needs to fetch 95% of rows. The query planner will choose a sequential scan. Use partial indexes or composite indexes instead.
- **Forgetting to ANALYZE after bulk loads.** After inserting millions of rows, table statistics are stale. The query planner relies on pg_statistics to estimate row counts and choose plans. Without ANALYZE, it may choose sequential scans even when indexes exist. Always run `ANALYZE tablename` after bulk data operations.
- **Indexing every column.** Some developers add an index on every column "just in case." Each index adds write overhead and maintenance cost. Index only the columns used in WHERE clauses, JOIN conditions, and ORDER BY on large tables. Monitor pg_stat_user_indexes to find unused indexes.
- **Using LIKE '%pattern%' expecting an index to help.** A leading wildcard (`LIKE '%smith'`) cannot use a B-tree index because the tree is sorted by prefix, not suffix. Only `LIKE 'smith%'` (prefix match) uses the index. For arbitrary substring search, use full-text indexing (GIN on tsvector) or a search engine like Elasticsearch.
- **Ignoring index bloat on high-churn tables.** Tables with frequent UPDATEs or DELETEs accumulate dead index entries. A bloated index is larger and slower than a compact one. Schedule regular VACUUM or use autovacuum tuning for high-churn tables.
