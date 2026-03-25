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

**PostgreSQL streaming replication** works via the Write-Ahead Log (WAL). Every write operation is first durably written to the WAL (a sequential log on disk). The WAL is then streamed to replicas via a persistent TCP connection. Replicas apply WAL entries in order, keeping their state exactly synchronized with the primary. Because the WAL contains every state transition, replicas are byte-for-byte identical to the primary at a given LSN (Log Sequence Number). The `pg_replication_slots` mechanism ensures replicas don't fall too far behind by blocking WAL cleanup on the primary.

**Replication lag** is the gap between the primary's current LSN and the replica's applied LSN, expressed as bytes or time. Monitoring this is critical: a replica with 10 minutes of lag cannot be promoted without data loss during a primary failure. pg's `pg_stat_replication` view exposes `write_lag`, `flush_lag`, and `replay_lag` for each replica.

**The split-brain problem** occurs when a primary fails and a new leader is elected while the old primary is still running (it's merely unreachable, not crashed). Both nodes believe they are the primary and accept writes. When connectivity is restored, the two histories must be reconciled, and one side's writes must be discarded. **Fencing tokens** prevent this: the old primary receives a token during its tenure; the new primary uses a higher token. Storage systems reject writes with stale tokens, preventing the zombie primary from accepting any writes.

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

- "Async replication is fast but has a durability window — data acknowledged to the client may not be on any replica yet. Sync replication eliminates that window at the cost of latency"
- "Replication lag is the key operational metric for replica health — monitor write_lag, flush_lag, replay_lag in pg_stat_replication"
- "The split-brain problem requires a fencing mechanism — either a quorum-based leader election (Raft/Paxos) or external coordination (ZooKeeper)"
- "Multi-leader solves geo-distributed write latency but introduces write conflicts — LWW discards data, CRDTs merge it without coordination"
- "WAL shipping in PostgreSQL means replicas are physically identical to the primary at a given LSN — logical replication (by contrast) replicates SQL-level changes and allows partial replication"

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

The script installs `psycopg2-binary` if needed, then runs five phases: schema setup, write throughput measurement, async replication lag simulation (replica lags behind primary by a batch), read scaling comparison (primary-only vs distributed), and graceful degradation when replica2 is stopped.

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

The read scaling phase shows throughput improvement from distributing reads. The replication lag phase shows the primary having more rows than the replica for a brief window — the core practical consequence of async replication.

### Teardown

```bash
docker compose down -v
```

## Real-World Examples

- **PostgreSQL at GitLab:** Uses streaming replication with one synchronous replica for durability and multiple async replicas for read scaling. The Geo feature replicates entire PostgreSQL instances across data centers for DR — source: GitLab Handbook, "GitLab Database Architecture."
- **MySQL at Facebook (now Meta):** Built a custom semi-synchronous replication system ("Wormhole") to replicate writes across data centers with sub-second lag at massive scale — source: Facebook Engineering, "Wormhole: Pub-Sub System Moving Data Through Space and Time" (2013).
- **Amazon Aurora:** Uses a custom replication protocol that replicates only redo log records (not full pages) to 6 storage nodes across 3 AZs. A write is acknowledged after 4 of 6 nodes confirm, providing durability without the overhead of full page replication — source: Verbitski et al., "Amazon Aurora: Design Considerations for High Throughput Cloud-Native Relational Databases," SIGMOD 2017.

## Common Mistakes

- **Not monitoring replication lag.** A replica that is 30 minutes behind is useless for real-time reads and dangerous for failover. Set alerts at meaningful thresholds (e.g., > 30s lag = warning, > 5 min = critical).
- **Assuming replica promotion is instantaneous.** In async replication, promoting a replica means accepting that some writes may be lost. The application must handle this (retry, re-read, inform the user). Automatic failover tools like Patroni or Orchestrator handle the mechanics, but the data loss window is not zero.
- **Replication slots left dangling.** When a replica is decommissioned, its replication slot must be dropped. Otherwise the primary accumulates WAL indefinitely. This is a common cause of primary disk exhaustion in PostgreSQL deployments.
- **Treating multi-leader as a solution to everything.** Multi-leader replication is complex and conflicts are hard to resolve correctly. Use it only when geo-distributed write latency is a real business requirement, not as a premature optimization.
