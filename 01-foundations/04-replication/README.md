# Replication

**Prerequisites:** `../03-consistency-models/`
**Next:** `../05-partitioning-sharding/`

---

## Concept

Replication is the practice of keeping copies of the same data on multiple machines. It serves three purposes: **fault tolerance** (if one machine fails, others can serve the data), **read scalability** (many machines can serve reads in parallel), and **latency reduction** (a replica in a nearby data center serves reads faster than a distant primary). Replication is foundational to every distributed database, and the choices made in its implementation determine consistency, availability, and durability characteristics.

The simplest replication model is **leader-follower** (also called primary-replica or master-slave). One node — the leader — accepts all writes. Changes are replicated to follower nodes, which serve read requests. This model is ubiquitous: PostgreSQL, MySQL, MongoDB, Redis, and Kafka all implement it. The key design question is: does the leader wait for followers to confirm a write before acknowledging it to the client (synchronous) or does it acknowledge immediately and replicate in the background (asynchronous)?

**Synchronous replication** provides the strongest durability guarantee: when the leader acknowledges a write, at least one follower has confirmed it. If the leader crashes immediately after acknowledging, no data is lost — the follower can be promoted. The cost is latency: every write waits for a network round-trip to the slowest synchronized replica. If any sync replica is unavailable, writes block until it returns or is demoted to async. PostgreSQL's `synchronous_standby_names` implements this; most production setups use "semi-sync" — one synchronous replica for durability, and N asynchronous replicas for read scaling.

**Asynchronous replication** is the default for most systems. The leader acknowledges the write to the client immediately and replicates to followers in the background. This is fast and the leader's availability is not affected by replica failures. The downside is replication lag: followers may be seconds (or minutes) behind the leader. If the leader crashes before replication completes, the acknowledged write is lost. This is the "durability vs latency" trade-off in practice.

**Multi-leader replication** allows multiple nodes to accept writes. This is useful for geo-distributed deployments: writes in the US go to a US leader, writes in Europe go to an EU leader, and leaders replicate to each other. The fundamental problem is write conflicts: if two clients in different regions update the same record simultaneously, two diverging histories exist. Conflict resolution is required: last-write-wins (LWW), application-level merging, or CRDTs (conflict-free replicated data types). CouchDB and some configurations of MySQL use multi-leader.

## How It Works

**WAL Streaming Replication Write Path (PostgreSQL):**
1. Client sends a write (INSERT/UPDATE/DELETE) to the primary
2. Primary writes the change to the Write-Ahead Log (WAL) on disk — a sequential append, durable before the write is complete
3. In **async** mode: primary acknowledges the write to the client immediately; WAL streams to replicas in the background
4. In **sync** mode: primary waits until at least one replica confirms it has received and flushed the WAL entry, then acknowledges the client
5. Each replica receives the WAL stream over a persistent TCP connection and appends it to its local WAL
6. Replica replays the WAL entries against its local data copy, advancing its Log Sequence Number (LSN)
7. The gap between the primary's current LSN and the replica's replayed LSN is the **replication lag** — the key health metric to monitor

**Replication lag** is the gap between the primary's current LSN and the replica's applied LSN, expressed as bytes or time. Monitoring this is critical: a replica with 10 minutes of lag cannot be promoted without data loss during a primary failure. `pg_stat_replication` exposes `write_lag`, `flush_lag`, and `replay_lag` for each replica. Use `pg_wal_lsn_diff(sent_lsn, replay_lsn)` for byte-level lag.

**`synchronous_commit` vs. synchronous replication** are frequently confused. `synchronous_commit` is a *session-level* parameter controlling whether a `COMMIT` waits for the WAL to be flushed to the *primary's* local disk. Setting it to `off` defers the local fsync by up to `wal_writer_delay` (200ms default) — a small durability risk on primary crash only, with a significant latency gain. Replication to replicas proceeds independently regardless of `synchronous_commit`. Synchronous *replication* (where the primary waits for a *replica* to confirm) is controlled by `synchronous_standby_names`. The two are orthogonal and commonly misunderstood in interviews.

**The split-brain problem** occurs when a primary fails and a new leader is elected while the old primary is still running (it's merely unreachable, not crashed). Both nodes believe they are the primary and accept writes. When connectivity is restored, the two histories must be reconciled, and one side's writes must be discarded. **Fencing tokens** prevent this: the old primary receives a token during its tenure; the new primary uses a higher token. Storage systems reject writes with stale tokens, preventing the zombie primary from accepting any writes.

**Read-your-own-writes (RYOW) consistency** is broken when a client writes to the primary and then reads from an async replica before the WAL has been applied. The client just created a record, but the replica returns "not found." This is a correctness issue, not a performance issue. Common mitigations: (1) route reads-after-writes to the primary for the remainder of that session, (2) compare the client's last write LSN against `pg_last_wal_replay_lsn()` on the replica before routing the read there, or (3) use a sync replica for reads that require monotonic guarantees.

**Physical vs. logical replication:** Physical (WAL-level) replication copies raw block changes — replicas are byte-for-byte identical and cannot serve a different PostgreSQL major version or replicate a subset of tables. Logical replication (introduced in PostgreSQL 10) replicates SQL-level row changes and supports: partial replication (select tables), cross-version upgrades, and fan-out to heterogeneous subscribers (e.g., a data warehouse). The trade-off is higher CPU overhead and no support for DDL changes automatically.

**Write amplification** in replication: a single logical write to the primary becomes N writes (primary + N replicas). For write-heavy workloads, this can be a significant overhead. Optimized replication (e.g., compression, batching WAL entries) reduces the overhead.

### Trade-offs

| Replication Mode | Durability | Write Latency | Read Scale | Complexity |
|-----------------|------------|---------------|------------|------------|
| Async (default) | Lower (lag window) | Lowest | High | Low |
| Sync (one replica) | High | Medium | High | Low |
| Sync (all replicas) | Highest | Highest | High | Medium |
| Multi-leader | Medium (conflicts) | Lowest | High | High |
| Leaderless (quorum) | Configurable | Configurable | High | High |

### Failure Modes

**Replication lag cascading:** under heavy write load, replicas fall behind. Reads from lagging replicas return stale data. If the primary fails, the replica with the least lag is promoted — but it may still be missing some commits. Monitoring `pg_stat_replication` and setting alerts on lag > N seconds is essential.

**Stale replica promoted to leader:** in automatic failover, if the promoted replica is significantly behind, the "new primary" starts accepting writes on a stale state. Subsequent reads will see data that was written before the old primary failed, but not after. This is a correctness issue, not just a performance issue.

**Replication slot bloat:** in PostgreSQL, if a replica disconnects but the replication slot is not dropped, the primary retains all WAL since the slot's last confirmed LSN. This can fill the disk entirely, taking down the primary. Monitor `pg_replication_slots` and set `max_slot_wal_keep_size`.

## Interview Talking Points

- "Async replication is fast but has a durability window — data acknowledged to the client may not be on any replica yet. Sync replication eliminates that window at the cost of latency. In PostgreSQL, `synchronous_standby_names` controls this at the replication level, while `synchronous_commit=off` is a separate per-session setting that only defers WAL fsync on the *primary's* local disk — they are orthogonal knobs."
- "Replication lag is the key operational metric for replica health — monitor `write_lag`, `flush_lag`, `replay_lag` in `pg_stat_replication`, or use `pg_wal_lsn_diff(sent_lsn, replay_lsn)` for byte-level lag per replica."
- "The split-brain problem requires a fencing mechanism — either a quorum-based leader election (Raft/Paxos) or external coordination (ZooKeeper/etcd). Patroni uses etcd; AWS RDS uses its own distributed lease mechanism."
- "Multi-leader solves geo-distributed write latency but introduces write conflicts — LWW (last-write-wins) silently discards data, CRDTs merge without coordination but only work for commutative operations, application-level merging is correct but expensive."
- "Physical WAL replication makes replicas byte-for-byte identical but can only replicate the entire cluster to the same major version. Logical replication (pg 10+) is row-level and enables cross-version upgrades, partial replication, and heterogeneous subscribers — useful for live migrations and ETL fan-out."
- "Read-your-own-writes is broken by default with async replicas — a client that writes to primary then reads from an async replica may see a stale snapshot. Solutions: sticky-primary routing for that session, LSN-based read routing (`pg_last_wal_replay_lsn()`), or a synchronous standby for read traffic that requires monotonic guarantees."
- "Replication slot bloat is a silent killer: an unused slot holds WAL on the primary indefinitely. Set `max_slot_wal_keep_size` and alert on `pg_replication_slots` where `inactive = true`."

## Hands-on Lab

**Time:** ~20-30 minutes
**Services:** postgres-primary (5432), postgres-replica1 (5433), postgres-replica2 (5434)

### Setup

```bash
cd system-design-interview/01-foundations/04-replication/
docker compose up -d
# Wait ~15 seconds for all 3 instances to be ready
```

### Experiment

```bash
python experiment.py
```

The script installs `psycopg2-binary` if needed, then runs eight phases: schema setup, live `pg_stat_replication` output, write throughput comparing `synchronous_commit=ON` vs `OFF`, real replication lag measurement (sentinel-row probe), a read-your-own-writes violation demonstration, concurrent read throughput scaling across all nodes, replica failure with graceful degradation, and replica reconnection with WAL catchup.

### Break It

```bash
# Stop the primary, see that replicas still serve reads but reject writes
docker compose stop postgres-primary

python -c "
import psycopg2

# Reads from replica still work
try:
    r = psycopg2.connect('host=localhost port=5433 dbname=replication_test user=postgres password=postgres')
    r.autocommit = True
    cur = r.cursor()
    cur.execute('SELECT COUNT(*) FROM users')
    print(f'Replica1 reads OK: {cur.fetchone()[0]} rows')
except Exception as e:
    print(f'Replica1 error: {e}')

# Writes fail (no primary)
try:
    p = psycopg2.connect('host=localhost port=5432 dbname=replication_test user=postgres password=postgres connect_timeout=2')
    print('Primary connected — unexpected')
except Exception as e:
    print(f'Primary unavailable (expected): {e}')
    print('In production: automatic failover promotes replica1 to primary')
"

docker compose start postgres-primary
```

### Observe

- **Phase 3** shows `synchronous_commit=OFF` is typically 1.5–3x faster than `ON` on a local Docker setup (the gap is larger on real storage). Note that this is purely a primary-local durability trade-off; it does not change how replication works.
- **Phase 4** shows real WAL streaming latency — typically sub-millisecond on localhost, which illustrates how fast async replication can be under low load.
- **Phase 5** demonstrates the read-your-own-writes hazard: the row written to primary may not yet be visible on a replica queried immediately after.
- **Phase 6** shows concurrent read throughput scaling — threads against primary+replicas achieve close to N× the single-primary throughput.
- **Phases 7–8** show `pg_stat_replication` drop from 2 connected replicas to 1, then return to 2 with zero lag after WAL replay.

### Teardown

```bash
docker compose down -v
```

## Real-World Examples

- **PostgreSQL at GitLab:** Uses streaming replication with one synchronous replica for durability and multiple async replicas for read scaling. The Geo feature replicates entire PostgreSQL instances across data centers for DR — source: GitLab Handbook, "GitLab Database Architecture."
- **MySQL at Facebook (now Meta):** Built a custom semi-synchronous replication system ("Wormhole") to replicate writes across data centers with sub-second lag at massive scale — source: Facebook Engineering, "Wormhole: Pub-Sub System Moving Data Through Space and Time" (2013).
- **Amazon Aurora:** Uses a custom replication protocol that replicates only redo log records (not full pages) to 6 storage nodes across 3 AZs. A write is acknowledged after 4 of 6 nodes confirm, providing durability without the overhead of full page replication — source: Verbitski et al., "Amazon Aurora: Design Considerations for High Throughput Cloud-Native Relational Databases," SIGMOD 2017.

## Common Mistakes

- **Confusing `synchronous_commit` with synchronous replication.** `synchronous_commit=off` only defers WAL fsync on the *primary's local disk*; it has nothing to do with whether replicas are synchronous or asynchronous. Many engineers think "I turned off synchronous_commit so replication is async now" — it was already async. The actual knob for replica synchrony is `synchronous_standby_names`.
- **Not monitoring replication lag.** A replica that is 30 minutes behind is useless for real-time reads and dangerous for failover. Set alerts at meaningful thresholds (e.g., > 30s lag = warning, > 5 min = critical).
- **Assuming replica promotion is instantaneous.** In async replication, promoting a replica means accepting that some writes may be lost. The application must handle this (retry, re-read, inform the user). Automatic failover tools like Patroni or Orchestrator handle the mechanics, but the data loss window is not zero.
- **Ignoring read-your-own-writes violations.** Routing reads to replicas without session-level monotonic guarantees breaks the most basic user expectation: "I just created it, why is it missing?" Implement sticky-primary routing or LSN-based read routing before deploying read replicas in production.
- **Replication slots left dangling.** When a replica is decommissioned, its replication slot must be dropped. Otherwise the primary accumulates WAL indefinitely. This is a common cause of primary disk exhaustion in PostgreSQL deployments. Always set `max_slot_wal_keep_size` as a safety cap.
- **Treating multi-leader as a solution to everything.** Multi-leader replication is complex and conflicts are hard to resolve correctly. Use it only when geo-distributed write latency is a real business requirement, not as a premature optimization.
