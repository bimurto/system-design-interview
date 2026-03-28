# Database Internals

**Prerequisites:** `../../01-foundations/08-databases-sql-vs-nosql/`, `../../01-foundations/09-indexes/`
**Next:** `../18-backpressure-flow-control/`

---

## Concept

Understanding how a database stores and retrieves data at the storage engine level is the difference between knowing
*what* a database does and knowing *why* it behaves the way it does under load. FAANG interviewers use database
internals questions to probe depth: "How would you design a key-value store?" is really asking you to describe a storage
engine. "Why is a write-heavy workload slow on Postgres?" requires knowing what happens on every write.

Two storage engine families dominate:

### B-Tree Storage Engines

B-trees are the default structure for PostgreSQL, MySQL (InnoDB), SQLite, and most relational databases. A B-tree is a
self-balancing tree where every node is a disk page (typically 4KB or 16KB). Leaf nodes hold the actual data (or
pointers to it); interior nodes hold keys and child pointers. Tree height stays at log_B(N) — for a branching factor of
500 and a billion rows, the tree is only 4 levels deep.

**Read path:** traverse from root to leaf following key comparisons. Every level is one disk seek. At 4 levels deep, a
point lookup requires 4 page reads — fast.

**Write path:** find the leaf page, insert the key-value pair. If the page is full, **split** it: create a new sibling
page, redistribute entries, promote the median key to the parent. Splits cascade up to the root in the worst case,
causing a structural rewrite of multiple pages. This is expensive. To reduce write amplification, databases use a *
*Write-Ahead Log (WAL)**: every modification is written sequentially to the WAL first (fast, because sequential writes),
then applied to the B-tree pages later. On crash recovery, the database replays the WAL to restore the tree to a
consistent state.

**Page cache:** the database keeps recently accessed pages in memory (the buffer pool in InnoDB, shared_buffers in
Postgres). A hot database where 90%+ of reads are served from the buffer pool is fast; a cold database where every read
is a disk seek is slow. Buffer pool size is the most important tuning parameter for read-heavy OLTP workloads.

**MVCC (Multi-Version Concurrency Control):** rather than locking rows for reads, databases keep multiple versions of
each row. A write creates a new version; readers see the version that was current at their transaction's start time.
This allows reads and writes to proceed concurrently without blocking. The tradeoff: old versions accumulate and must be
cleaned up by a background process (VACUUM in Postgres, purge thread in InnoDB). Tables that receive many updates
without regular VACUUM bloat with dead row versions, causing slow full-table scans.

### LSM-Tree Storage Engines

Log-Structured Merge Trees (LSM-trees) are used by LevelDB, RocksDB, Cassandra, HBase, and InfluxDB. They are optimized
for write-heavy workloads by making all writes sequential.

**Write path:** every write goes to an in-memory buffer (the **memtable**). When the memtable fills, it is flushed to
disk as an immutable sorted file called an **SSTable** (Sorted String Table). Writes are always sequential — no random
disk seeks, no in-place updates. This is why LSM-tree databases have dramatically higher write throughput than B-tree
databases.

**Read path:** to read a key, check the memtable first. If not there, scan SSTables from newest to oldest. To avoid
scanning all SSTables, each one has a **Bloom filter** (a probabilistic structure that can definitively say "key not in
this SSTable") and a sparse index. Reads are more expensive than B-tree reads because multiple files may need to be
consulted — this is the write-optimized vs read-optimized trade-off.

**Compaction:** SSTables accumulate over time. A background compaction process merges SSTables at the same level into a
new, larger SSTable at the next level, discarding deleted and overwritten entries. Compaction keeps read performance
acceptable, but it consumes I/O bandwidth — during heavy compaction, read and write latency spikes. This is **write
amplification**: each byte written by the application may be rewritten multiple times during compaction across levels.

**Tombstones:** deletes are not in-place. Instead, a delete writes a **tombstone** record (a marker with the key and a
deletion flag). The actual entry is removed during the next compaction that merges the tombstone with the original
entry. Until then, readers must recognize tombstones and suppress deleted keys.

### WAL (Write-Ahead Log)

Both B-tree and LSM-tree engines use a WAL (also called a commit log or redo log). The WAL is a sequential append-only
log of all mutations. On write:

1. Write the operation to the WAL (fast — sequential disk write)
2. Apply the change to the in-memory data structure (memtable or buffer pool)
3. Acknowledge the write to the client

On crash, the database replays the WAL from the last checkpoint to restore all in-flight transactions. The WAL is also
used for **replication**: followers stream the primary's WAL and apply it locally to maintain a synchronized copy (
logical replication in Postgres, binlog in MySQL).

### Storage Engine Trade-offs

| Property            | B-Tree                    | LSM-Tree                           |
|---------------------|---------------------------|------------------------------------|
| Write throughput    | Moderate (random writes)  | High (sequential writes)           |
| Read throughput     | High (point lookups fast) | Moderate (may scan multiple files) |
| Write amplification | Moderate                  | High (compaction rewrites data)    |
| Read amplification  | Low                       | Moderate (Bloom filters help)      |
| Space amplification | Low                       | Higher (SSTables + tombstones)     |
| Good for            | OLTP, mixed read/write    | Write-heavy, time-series, append   |

### Column-Oriented Storage

Row-oriented storage (Postgres, MySQL) stores all columns of a row together — efficient for fetching complete rows (OLTP
queries). Column-oriented storage (Parquet, Redshift, BigQuery) stores all values of a column together — efficient for
aggregations over a subset of columns (OLAP queries like `SELECT avg(price) FROM orders WHERE year=2024`).

Column storage enables dramatically better compression (values in a column are often similar type and range — delta
encoding, run-length encoding, dictionary encoding all work well). It also allows vectorized execution: CPUs can apply
SIMD instructions to process 8–32 column values simultaneously. For analytical queries scanning billions of rows, column
storage can be 10–100x faster than row storage.

## Interview Talking Points

- "B-trees are read-optimized: a point lookup is O(log N) disk reads. LSM-trees are write-optimized: every write is
  sequential. The trade-off is read amplification vs write amplification"
- "Every write goes to the WAL first — this is how crash recovery works. The WAL is also the replication stream
  followers consume"
- "MVCC lets reads and writes proceed without blocking each other by keeping multiple row versions. Old versions must be
  garbage-collected; VACUUM in Postgres does this — without it, tables bloat"
- "Compaction in LSM-tree engines is the hidden cost of high write throughput — during heavy compaction, latency spikes
  as compaction competes for I/O bandwidth"
- "A Bloom filter in each SSTable answers 'definitely not present' — a key can be skipped without reading the SSTable at
  all, making reads much cheaper"
- "For time-series or append-only workloads (logs, events, metrics), LSM-tree engines like RocksDB or Cassandra
  outperform Postgres significantly because writes never do random I/O"

## Hands-on Lab

**Time:** ~25 minutes
**Services:** postgres (5432), a Python script that drives RocksDB-like LSM behavior via a simulated engine

### Setup

```bash
cd system-design-interview/02-advanced/17-database-internals/
docker compose up -d
# Wait ~10 seconds for Postgres to be ready
```

### Experiment

```bash
python experiment.py
```

The script runs four phases: (1) WAL anatomy — shows the Postgres WAL LSN advancing with each write; (2) B-tree vs
sequential write throughput — inserts 10k rows in random-key order vs sequential-key order, measuring the difference; (
3) MVCC observation — two concurrent transactions observe different row versions; (4) simulated LSM-tree — a pure-Python
memtable + SSTable implementation showing compaction merging multiple sorted files into one.

### Teardown

```bash
docker compose down -v
```

## Real-World Examples

- **RocksDB at Meta:** Meta uses RocksDB (an LSM-tree engine) as the storage layer for MyRocks (MySQL with RocksDB
  backend). For their UDB (user database) — write-heavy with hundreds of millions of writes per day — RocksDB reduced
  storage by 50% and write amplification by 10x vs InnoDB — source: Meta Engineering Blog, "MyRocks: A space- and
  write-optimized MySQL database" (2016).
- **Postgres VACUUM:** Slack's Postgres clusters ran into severe VACUUM lag during high write periods. Dead row versions
  accumulated faster than VACUUM could clean them, causing table bloat that slowed down scans. They resolved it by
  tuning autovacuum aggressiveness and partitioning hot tables to reduce per-partition bloat — source: Slack Engineering
  Blog, "Scaling Slack's Job Queue" (2017).
- **Cassandra LSM compaction:** Discord switched from Cassandra to ScyllaDB (a Cassandra-compatible engine with better
  compaction scheduling) after compaction I/O spikes caused read latency P99 to spike 10x during peak traffic.
  Understanding compaction scheduling is critical for operating LSM-tree databases at scale — source: Discord
  Engineering Blog, "How Discord Stores Billions of Messages" (2017).

## Common Mistakes

- **Confusing the WAL with the data file.** The WAL is a sequential durability log — not what you query. Data lives in
  the B-tree pages or SSTables. The WAL is replayed on crash; it's also streamed to replicas.
- **Ignoring write amplification in LSM-tree systems.** Each byte written by the app may be compacted 10–30x before it
  reaches the bottom level. At 100k writes/sec, compaction can generate 3MB/s of I/O — account for this in disk I/O
  budget.
- **Forgetting VACUUM for Postgres.** Every UPDATE and DELETE in Postgres leaves a dead row version. Without regular
  VACUUM, tables bloat. For tables with high update rates (e.g., a counter or a status column), autovacuum may not run
  frequently enough — tune `autovacuum_vacuum_scale_factor` down for hot tables.
- **Choosing column storage for OLTP.** Column stores are terrible for point lookups and single-row updates — they must
  reconstruct the full row from multiple column files. Use column storage only for analytical (read-heavy, full-scan)
  workloads.
