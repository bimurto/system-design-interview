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

## How It Works

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

During a network partition, nodes on each side of the split continue operating, but they cannot coordinate. Suppose node A accepts a write (balance = $800) but node B, isolated by the partition, still has the old value ($1000). What does a read to node B return?

- **CP choice:** Node B recognizes it cannot confirm it has the latest value (quorum unreachable), so it refuses the read. The client gets an error. The system is unavailable for reads during the partition but will never return stale data.
- **AP choice:** Node B returns $1000 — the best value it has. The client gets a response. The data may be stale. When the partition heals, the system reconciles the conflict (last-write-wins, vector clocks, CRDTs, etc.).

ZooKeeper, etcd, and HBase are CP: they require a quorum (majority of nodes) for any operation. If less than a majority is reachable, they refuse operations entirely. This makes them suitable for leader election and distributed locks, where correctness is paramount.

Cassandra and DynamoDB are AP: they accept reads and writes on any available replica, replicate asynchronously, and use eventual consistency to converge. They offer "consistency levels" that let you trade availability for stronger consistency on a per-request basis (e.g., Cassandra's `QUORUM` read achieves strong consistency at the cost of higher latency and reduced availability).

**PACELC** extends CAP to the normal case (no partition): even when the network is healthy, there is a trade-off between **Latency** and **Consistency**. Synchronous replication ensures all replicas have the write before acknowledging — this is consistent but adds latency equal to the slowest replica's round-trip. Asynchronous replication acknowledges immediately and replicates in the background — fast, but the replicas may be stale. Most systems fall somewhere on this spectrum, and PACELC forces you to reason about *both* the partition case and the normal case.

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

### Failure Modes

**Split-brain:** During a partition, both sides think they are the primary and accept conflicting writes. When the partition heals, the system has two diverging histories. CP systems prevent split-brain by design (quorum required). AP systems must detect and resolve it (last-write-wins discards one side's writes; CRDTs merge both sides automatically).

**False availability:** A system that returns errors during a partition is not "available" even if it is technically "up." The CAP theorem's definition of availability is strict: every request to a live node must receive a response. A system that is up but returns "cluster unavailable" errors is CP, not AP.

**Stale reads after reconnect:** In an AP system, a client that was reading from node B during a partition may have seen stale data. After the partition heals and the system converges, the same client reading again will see newer data — but data it read *during* the partition may have been acted upon already. This is why AP systems must be designed for "eventual consistency" at the application level.

## Interview Talking Points

- "CAP stands for Consistency, Availability, Partition Tolerance — and you can only guarantee two of three. Since partitions are unavoidable, the real choice is CP vs AP"
- "CP systems (ZooKeeper, etcd) refuse requests they can't serve consistently; AP systems (Cassandra, DynamoDB) return stale data rather than erroring"
- "The 'C' in CAP means linearizability — stronger than ACID consistency. Don't confuse them"
- "PACELC extends CAP: even without partitions, synchronous replication adds latency. Most real systems trade consistency for lower latency"
- "Cassandra's consistency levels let you tune the CP/AP trade-off per request — QUORUM reads are strongly consistent but slower"

## Hands-on Lab

**Time:** ~20-30 minutes
**Services:** postgres-primary (port 5432), postgres-replica (port 5433)

### Setup

```bash
cd system-design-interview/01-foundations/02-cap-theorem/
docker compose up -d
# Wait ~10 seconds for both Postgres instances to start
```

### Experiment

```bash
python experiment.py
```

The experiment installs `psycopg2-binary` if needed, then walks through five phases: normal operation, partition simulation (stopping the replica container), AP behavior (primary stays up), CP behavior (replica unreachable = no consistent reads), and partition healing.

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
- Phase 3: write to primary succeeds even while replica is stopped (AP behavior)
- Phase 3: connection to replica returns `None` / error (partition effect visible)
- Phase 5: replica comes back and catches up (convergence)

### Teardown

```bash
docker compose down -v
```

## Real-World Examples

- **Amazon DynamoDB:** Designed as an AP system after the 2004 "Dynamo" paper. Every request gets a response; consistency is eventual by default. Strong consistency reads are available at 2x the cost/latency — source: DeCandia et al., "Dynamo: Amazon's Highly Available Key-value Store," SOSP 2007.
- **Google Spanner:** Achieves CP with external consistency (a form of linearizability across data centers) using TrueTime — GPS and atomic clocks to bound clock skew — source: Corbett et al., "Spanner: Google's Globally Distributed Database," OSDI 2012.
- **Apache Cassandra:** AP by default. Used by Apple (850+ nodes) and Netflix for availability-critical workloads. Tunable consistency per operation — source: Lakshman & Malik, "Cassandra: A Decentralized Structured Storage System," LADIS 2009.

## Common Mistakes

- **"I want all three of CAP."** You can't. This is a theorem, not a design guideline. The question is which property you sacrifice *during a partition*.
- **Confusing CAP consistency with ACID consistency.** ACID consistency means the database moves from one valid state to another (constraints hold). CAP consistency means linearizability — every read sees the latest write. They are different concepts that share a word.
- **Treating "eventual consistency" as "broken."** AP systems with eventual consistency are correct — they guarantee all nodes converge to the same value after the partition heals. The application must be designed to tolerate temporary staleness (idempotent operations, conflict resolution, compensating transactions).
- **Not considering PACELC.** Even in the normal, no-partition case, there is a real trade-off between consistency and latency. Synchronous writes to replicas add round-trip latency. This affects every request, not just requests during partitions.
