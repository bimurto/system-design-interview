# CAP Theorem

**Prerequisites:** `../01-scalability/`
**Next:** `../03-consistency-models/`

---

## Concept

The CAP theorem, formalized by Eric Brewer in 2000 and proved by Gilbert and Lynch in 2002, states that a distributed data store can guarantee at most two of three properties simultaneously: **Consistency**, **Availability**, and **Partition Tolerance**. This is not a design choice — it is a mathematical theorem. Understanding it correctly is essential to making sensible trade-offs when building distributed systems.

**Consistency** in CAP means *linearizability*: every read returns the result of the most recent write, and the system appears to behave as a single machine regardless of how many nodes are involved. This is stronger than "consistency" in ACID transactions. If node A accepts a write and node B hasn't yet received it, any read from node B must either return the new value (by coordinating with node A first) or be refused.

**Availability** means every request sent to a non-failed node receives a response — not an error, not a timeout, a response. The response doesn't have to be current, but it must be non-error. An unavailable system is one that refuses or times out requests.

**Partition Tolerance** means the system continues operating even when the network drops or delays messages between nodes. A partition is a network split where some nodes cannot communicate with others. Partition tolerance is not optional for real distributed systems: networks fail, packets are lost, and data centers disconnect. The correct framing is not "should we tolerate partitions?" but "what do we do *when* a partition occurs?"

Since partitions will happen, the real choice is between **CP** (sacrifice availability during partitions — return errors rather than stale data) and **AP** (sacrifice consistency during partitions — return stale data rather than errors). Neither is universally superior; the choice depends on the application's requirements.

> **Important nuance:** A "partition" is not only a full network split. High latency between nodes (e.g., >100ms round-trip) is functionally equivalent to a partition if your system requires synchronous replication: writes either block indefinitely or time out. In practice, CAP's partition case is triggered by any condition that prevents nodes from coordinating within a bounded time.

## How It Works

```
Normal Operation:
  Client ──write──► Node A ──WAL/replication──► Node B
                     ▲                            ▲
                 (primary)                   (replica)
                  balance=$800               balance=$800   ← consistent

Network Partition:
  Client ──write──► Node A   ╳╳╳╳╳╳╳╳╳╳╳╳   Node B
                     ▲       (network split)    ▲
                  balance=$800               balance=$1000  ← DIVERGED

  CP choice: Node B refuses reads (cannot confirm latest value → error)
  AP choice: Node B returns $1000 (stale but available → eventual consistency)
```

**During a Network Partition — CP System Behaviour:**
1. Network partition splits the cluster: Node A (majority side) and Node B (minority side) can no longer communicate
2. Client writes `balance = $800` to Node A; Node A reaches quorum among reachable nodes and accepts the write
3. Client attempts a read from Node B (the minority side)
4. Node B detects it cannot reach quorum — it **refuses the request** and returns an error (sacrifices availability for consistency)
5. Partition heals; Node B receives the missed write from Node A and converges to the correct state
6. After recovery, all nodes agree: `balance = $800`

**During a Network Partition — AP System Behaviour:**
1. Network partition splits the cluster: Node A and Node B cannot communicate
2. Client writes `balance = $800` to Node A; Node A accepts and acknowledges (no quorum required)
3. Client reads from Node B — Node B returns its local stale value (`$1,000`) rather than returning an error (sacrifices consistency for availability)
4. Both nodes continue accepting reads and writes independently during the partition
5. Partition heals; conflict resolution runs (last-write-wins, vector clocks, or CRDTs) to reconcile diverged state

ZooKeeper, etcd, and HBase are CP: they require a quorum (majority of nodes) for any operation. If less than a majority is reachable, they refuse operations entirely. This makes them suitable for leader election and distributed locks, where correctness is paramount.

Cassandra and DynamoDB are AP: they accept reads and writes on any available replica, replicate asynchronously, and use eventual consistency to converge. They offer "consistency levels" that let you trade availability for stronger consistency on a per-request basis (e.g., Cassandra's `QUORUM` read achieves strong consistency at the cost of higher latency and reduced availability).

**PACELC** extends CAP to the normal case (no partition): even when the network is healthy, there is a trade-off between **Latency** and **Consistency**. Synchronous replication ensures all replicas have the write before acknowledging — this is consistent but adds latency equal to the slowest replica's round-trip. Asynchronous replication acknowledges immediately and replicates in the background — fast, but the replicas may be stale. Most systems fall somewhere on this spectrum, and PACELC forces you to reason about *both* the partition case and the normal case.

```
PACELC matrix:

                    | During Partition  | Else (normal ops)
--------------------|-------------------|------------------
  Cassandra         |  AP               |  EL (favors low latency)
  DynamoDB          |  AP               |  EL (favors low latency)
  HBase / ZooKeeper |  CP               |  EC (favors consistency)
  Google Spanner    |  CP               |  EC (TrueTime bounds skew)
  PostgreSQL async  |  AP               |  EL
  PostgreSQL sync   |  CP               |  EC
```

### Trade-offs

| System | CAP Classification | Real Behavior |
|--------|-------------------|---------------|
| ZooKeeper | CP | Refuses ops if quorum unavailable; used for coordination |
| etcd | CP | Same as ZooKeeper; backs Kubernetes |
| HBase | CP | Requires HDFS quorum; consistent reads |
| Cassandra | AP | Eventual consistency default; tunable consistency levels |
| DynamoDB | AP | Eventually consistent default; strong consistency opt-in |
| CouchDB | AP | Multi-master with conflict resolution |
| MongoDB (default) | AP | Async replication to secondaries; eventual |
| MongoDB (majority writes) | CP | Waits for majority ack; linearizable |
| PostgreSQL (sync replication) | CP | Blocks writes until replica confirms |
| PostgreSQL (async replication) | AP | Acknowledges write before replica confirms |

### Session Guarantees and Why They Matter

The CAP theorem describes system-wide properties, but clients also care about *session-level* consistency guarantees. These are weaker than linearizability but stronger than pure eventual consistency:

- **Read-your-writes:** After a client writes, it always reads its own write — even if served by a different replica. Implemented via sticky sessions or by attaching a write timestamp and only routing reads to replicas that have caught up to that LSN (PostgreSQL) or vector clock (Dynamo/Cassandra).
- **Monotonic reads:** Once a client reads value V, it will never read an older value. Prevents a client from "going back in time" when its requests are load-balanced across replicas at different replication offsets.
- **Monotonic writes:** Writes from a single client are applied in order — critical for multi-step operations like "reserve seat then charge card."
- **Writes-follow-reads:** A write following a read is guaranteed to occur after the value that was read — prevents causality violations.

AP systems sacrifice linearizability but can still provide session guarantees. Cassandra's `LOCAL_QUORUM` + sticky coordinator is one approach. DynamoDB's global tables offer read-your-writes within a region.

### Failure Modes

**Split-brain:** During a partition, both sides think they are the primary and accept conflicting writes. When the partition heals, the system has two diverging histories. CP systems prevent split-brain by design (quorum required). AP systems must detect and resolve it (last-write-wins discards one side's writes; CRDTs merge both sides automatically).

**False availability:** A system that returns errors during a partition is not "available" even if it is technically "up." The CAP theorem's definition of availability is strict: every request to a live node must receive a response. A system that is up but returns "cluster unavailable" errors is CP, not AP.

**Stale reads after reconnect:** In an AP system, a client that was reading from node B during a partition may have seen stale data. After the partition heals and the system converges, the same client reading again will see newer data — but data it read *during* the partition may have been acted upon already. This is why AP systems must be designed for "eventual consistency" at the application level (idempotent operations, compensating transactions).

**Herd effect on partition heal:** When a large cluster heals, all minority-side nodes attempt to sync simultaneously. This can overwhelm the primary with WAL replay requests or snapshot transfers. Cassandra's anti-entropy (Merkle tree repair) and PostgreSQL's WAL streaming both handle this gracefully, but it's a real operational concern at scale.

**Write amplification in CP systems:** A CP system with synchronous replication to N replicas waits for all N acknowledgements before returning to the client. If one replica is slow (GC pause, disk flush), the write latency spikes for everyone. This is why Spanner uses TrueTime rather than waiting for a slow replica, and why most CP systems use a quorum (majority) rather than all-replicas synchronous replication.

## Interview Talking Points

**Core framing (always lead with this):**
- "CAP stands for Consistency, Availability, Partition Tolerance — and since partitions are unavoidable in any distributed system, the real design choice is CP vs AP."
- "The 'C' in CAP means *linearizability* — every read sees the most recent write as if the system were a single machine. This is much stronger than ACID's 'consistency,' which just means constraints hold."

**Going deeper (staff-level differentiation):**
- "CP systems like ZooKeeper and etcd require a quorum for every operation. If fewer than (N/2 + 1) nodes are reachable, they refuse all requests — that's the availability sacrifice, and it's intentional. You would never use ZooKeeper for user-facing data."
- "AP systems like Cassandra and DynamoDB accept writes on any available node and resolve conflicts on read or at repair time. Their tunable consistency (e.g., `QUORUM` reads) lets you slide the dial toward CP behavior on a per-operation basis — at the cost of higher latency and reduced write availability."
- "PACELC is more useful day-to-day than CAP: even with no partition, synchronous replication adds latency proportional to the slowest replica's RTT. That affects every write, not just writes during failures."
- "Session guarantees (read-your-writes, monotonic reads) are often more important to application correctness than system-wide linearizability. You can provide read-your-writes in an AP system by routing reads to the replica that acknowledged your write's LSN."
- "Vector clocks and CRDTs let AP systems merge diverged state without application-level conflict resolution. DynamoDB dropped vector clocks (too complex for clients) in favor of last-write-wins plus conditional writes. Cassandra's LWTs (lightweight transactions) use Paxos to get CP behavior on specific keys."

**Scenario-based answers:**
- For a bank ledger: "I'd choose CP — a wrong balance is worse than a brief outage. Use synchronous replication with quorum writes. Accept that during a partition, writes block or fail rather than risk double-spend."
- For a shopping cart: "I'd choose AP — losing a cart item during a brief inconsistency is tolerable. Use Dynamo-style last-write-wins or CRDT sets so items are always merged, never lost."
- For a social feed: "AP. Seeing a post 200ms late is fine. Availability is critical. Use async replication and accept eventual consistency."
- For distributed locks / leader election: "CP, always. ZooKeeper or etcd. Wrong leader election causes catastrophic split-brain. The brief unavailability during partition is acceptable."

## Hands-on Lab

**Time:** ~20-30 minutes
**Services:** postgres-primary (port 5432), postgres-replica (port 5433)

### Setup

```bash
cd system-design-interview/01-foundations/02-cap-theorem/
docker compose up -d
# Wait ~15-20 seconds for pg_basebackup to complete and streaming to establish
```

### Experiment

```bash
python experiment.py
```

The experiment walks through six phases:
1. **Normal operation** — streaming replication established, reads consistent on both nodes
2. **Network partition** — replica container stopped, pg_stat_replication shows disconnect
3. **AP behavior** — primary accepts writes during partition; replica is unreachable/stale
4. **CP behavior** — primary configured with `synchronous_commit=remote_write`; writes block until replica confirms (or error during partition)
5. **Partition healing** — replica restarts, WAL catchup measured in real time
6. **Summary** — key takeaways printed with concrete numbers from the experiment

### Break It

```bash
# While experiment is NOT running, stop the primary instead of the replica
docker compose stop postgres-primary

# Now try to write — primary is gone, system is fully unavailable
python -c "
import psycopg2
try:
    conn = psycopg2.connect('host=localhost port=5432 dbname=captest user=postgres password=postgres connect_timeout=2')
    print('Connected — unexpected')
except Exception as e:
    print(f'Primary unavailable: {e}')
    print('In a CP system with no quorum, all writes fail.')
    print('In an AP system with multi-master, the replica would take over.')
"
docker compose start postgres-primary
```

### Observe

The experiment prints clear phase markers. Key observations:
- Phase 1: `pg_stat_replication` shows live lag in bytes and LSN — real WAL streaming
- Phase 3: write to primary succeeds even while replica is stopped (AP behavior)
- Phase 4: write to primary with `synchronous_commit=remote_write` times out or errors during partition (CP behavior)
- Phase 5: replica comes back and WAL catchup time is measured in seconds

### Teardown

```bash
docker compose down -v
```

## Real-World Examples

- **Amazon DynamoDB:** Designed as an AP system after the 2004 "Dynamo" paper. Every request gets a response; consistency is eventual by default. Strong consistency reads are available at 2x the cost/latency — source: DeCandia et al., "Dynamo: Amazon's Highly Available Key-value Store," SOSP 2007.
- **Google Spanner:** Achieves CP with external consistency (a form of linearizability across data centers) using TrueTime — GPS and atomic clocks to bound clock skew — source: Corbett et al., "Spanner: Google's Globally Distributed Database," OSDI 2012.
- **Apache Cassandra:** AP by default. Used by Apple (850+ nodes) and Netflix for availability-critical workloads. Tunable consistency per operation — source: Lakshman & Malik, "Cassandra: A Decentralized Structured Storage System," LADIS 2009.
- **etcd (Kubernetes):** CP. Uses Raft consensus — a leader is elected by quorum, and all writes go through the leader. If fewer than (N/2 + 1) nodes are alive, etcd refuses all writes. This is intentional: cluster state must be correct, not merely available.

## Common Mistakes

- **"I want all three of CAP."** You can't. This is a theorem, not a design guideline. The question is which property you sacrifice *during a partition*.
- **Confusing CAP consistency with ACID consistency.** ACID consistency means the database moves from one valid state to another (constraints hold). CAP consistency means linearizability — every read sees the latest write. They are different concepts that share a word.
- **Treating "eventual consistency" as "broken."** AP systems with eventual consistency are correct — they guarantee all nodes converge to the same value after the partition heals. The application must be designed to tolerate temporary staleness (idempotent operations, conflict resolution, compensating transactions).
- **Not considering PACELC.** Even in the normal, no-partition case, there is a real trade-off between consistency and latency. Synchronous writes to replicas add round-trip latency. This affects every request, not just requests during partitions.
- **Ignoring session guarantees.** Linearizability is often overkill. Read-your-writes and monotonic reads satisfy most application needs and are achievable in AP systems. Know which guarantee your application actually requires before reaching for a CP system.
- **Assuming CP means "safe" and AP means "risky."** CP systems that lose quorum become completely unavailable — which can be more dangerous than stale reads in some operational contexts. Choose based on which failure mode is less catastrophic for your use case.
