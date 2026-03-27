# Consistency Models

**Prerequisites:** `../02-cap-theorem/`
**Next:** `../04-replication/`

---

## Concept

Consistency models define the rules a distributed system makes to clients about what they will observe when reading data. They form a spectrum from the strongest guarantees (linearizability, where the system appears to be a single machine) to the weakest (eventual consistency, where nodes merely agree to converge "at some point"). Choosing the right consistency model is one of the central design decisions for any distributed system because stronger consistency costs more in latency and availability.

The confusion starts because "consistency" is overloaded. ACID consistency (in SQL databases) means constraints are upheld across a transaction. CAP consistency means linearizability — every read reflects the most recent write across all nodes. These are different concepts. This topic covers the CAP/distributed systems definition and everything below it on the consistency spectrum.

**Linearizability** (also called strong consistency or atomic consistency) is the gold standard. Every operation appears to take effect instantaneously at a single point in time, and once a write completes, every subsequent read — from any node, by any client — returns that value or a later one. The system behaves identically to a single-machine system. Achieving this across multiple nodes requires coordination on every read and write (e.g., two-phase commit, Paxos, Raft), which adds latency and reduces availability.

**Sequential consistency** relaxes real-time ordering. All nodes agree on a single global order of operations, but that order doesn't have to match wall-clock time. A write that completes at time T=5 might appear to a distant node as if it happened at T=3 in the global order, as long as all nodes see the same order. This is weaker than linearizability but stronger than causal consistency, and it underpins many CPU memory models.

**Eventual consistency** is the baseline for most large-scale distributed systems. The guarantee is only that if no new writes occur, all replicas will eventually converge to the same value. There is no bound on when convergence occurs, and during the convergence period, different clients may see different values. This enables high availability and low latency at the cost of stale reads and conflict potential.

## How It Works

**Linearizable (Strong Consistency) Read:**
1. Client writes `key=X, value=5` to the primary
2. Primary replicates the write to all replicas synchronously (or via Raft/Paxos consensus) before acknowledging
3. Write is acknowledged to the client only after quorum confirms the value is durable
4. Any subsequent read — from any node — is guaranteed to return `value=5` or a newer value; stale reads are impossible

**Eventually Consistent Read:**
1. Client writes `key=X, value=5` to the primary
2. Primary acknowledges the write immediately; replication to other nodes happens asynchronously in the background
3. Client (or another client) reads `key=X` from a replica that has not yet received the update — returns stale value (e.g., `value=3`)
4. After replication catches up, all replicas converge to `value=5`; the window of staleness depends on replication lag

Between the extremes lie several important client-side consistency guarantees that are composable with any of the above:

**Read-your-writes** (also called read-your-own-writes): after a client writes a value, all subsequent reads by that same client return that value or a newer one. This is violated when the client writes to a primary and then reads from a replica that hasn't caught up. Solutions: always read from primary after a write, use sticky routing (always route a user to the same replica), or use WAIT (Redis) / synchronous replication.

**Monotonic reads**: a client never sees an older version of data than it has already seen. This is violated when a load balancer routes a client to different replicas in round-robin order and one replica is lagging. A client might see value V=5, then V=3 on the next request (to a lagging replica). Solution: stick a client to one replica per session.

**Causal consistency**: if operation A causally precedes operation B (A "happened before" B, e.g., A is a write that B reads), all nodes must see A before B. Unrelated operations can appear in any order. MongoDB's causal sessions and Amazon's Dynamo use vector clocks to track causal ordering.

Redis's `WAIT numreplicas timeout` command is a practical tool for upgrading eventual consistency to strong consistency: it blocks the client until the specified number of replicas have acknowledged all pending writes. Setting `WAIT 1 0` means "block until at least 1 replica has all my writes" — after that, reading from that replica is linearizable with respect to this client's writes.

### Trade-offs

| Model | Latency | Availability | Use Cases |
|-------|---------|--------------|-----------|
| Linearizability | Highest | Lowest | Leader election, distributed locks, counters |
| Sequential consistency | High | Low-medium | Some CPU memory models |
| Causal consistency | Medium | Medium | Social feeds, collaboration tools |
| Read-your-writes | Low | High | User profile reads after update |
| Monotonic reads | Low | High | Timelines, logs |
| Eventual consistency | Lowest | Highest | DNS, caching, shopping cart |

### Failure Modes

**Stale reads causing incorrect decisions:** a system that routes reads to replicas without any consistency guarantee may return values that lead to incorrect business logic — double charges, oversold inventory, outdated permissions. The fix is to identify which reads require stronger guarantees and route those to the primary or use WAIT.

**Monotonic read violations in sticky sessions:** if a replica restarts and its session state is lost, the load balancer may route the client to a different (potentially lagging) replica, causing a regression. Use server-side session tokens that include a minimum replication offset.

**Causal consistency violations with parallel writes:** if two clients concurrently write causally unrelated values, a third client reading from different replicas may see them in different orders. This is only a problem if the application treats these as causally related. Solution: vector clocks or version vectors.

## Interview Talking Points

- "Linearizability means the system looks like a single machine — every read sees the latest write. It requires coordination on every operation, so it's expensive"
- "Read-your-writes and monotonic reads are client-side guarantees that are orthogonal to the server-side consistency model — you can have eventual consistency with read-your-writes via sticky routing"
- "Redis WAIT upgrades eventual to strong consistency by blocking until N replicas acknowledge — useful for critical writes that must be immediately readable from replicas"
- "Most user-facing applications need read-your-writes (show me my own post after I submit) but can tolerate eventual consistency for other users' data"
- "Causal consistency is often the sweet spot: stronger than eventual, cheaper than linearizable, and sufficient for most social/collaborative applications"

## Hands-on Lab

**Time:** ~20-30 minutes
**Services:** redis-master (6379), redis-replica1 (6380), redis-replica2 (6381)

### Setup

```bash
cd system-design-interview/01-foundations/03-consistency-models/
docker compose up -d
# Wait ~10 seconds for replicas to connect and sync
```

### Experiment

```bash
python experiment.py
```

The script runs four phases: demonstrating eventual consistency (write to master, read from replica immediately — may be stale), comparing WAIT 0 / WAIT 1 / WAIT 2 latencies, demonstrating read-your-writes with WAIT, and showing how monotonic read violations can occur with replica alternation.

### Break It

```bash
# Stop replica2, then try WAIT 2 with a short timeout
docker compose stop redis-replica2

python -c "
import redis
r = redis.Redis(host='localhost', port=6379, decode_responses=True)
r.set('x', 42)
acked = r.wait(2, 200)  # wait for 2 replicas, 200ms timeout
print(f'Acked by {acked} replicas (expected 2, got fewer because replica2 is down)')
print('WAIT returns how many actually acked — application must check the return value')
"

docker compose start redis-replica2
```

### Observe

Look for the WAIT latency numbers in the output. On localhost, the difference is small (sub-millisecond). Across data centers (10–50ms RTT), WAIT 2 would add 50–100ms per write — a significant latency cost for strong consistency.

```
No WAIT (eventual):  0.05 ms
WAIT 1 replica:      0.30 ms
WAIT 2 replicas:     0.45 ms
```

### Teardown

```bash
docker compose down -v
```

## Real-World Examples

- **Amazon DynamoDB:** Offers two read modes per request: eventually consistent reads (default, cheaper, potentially stale) and strongly consistent reads (costs 2x read capacity, always fresh). Engineers choose per operation — source: AWS DynamoDB Developer Guide, "Read Consistency."
- **MongoDB:** Implements causal consistency via client sessions and cluster time — a logical clock advanced with each operation. Reads in a session are guaranteed to reflect all previous writes in that session — source: MongoDB Docs, "Causal Consistency and Read and Write Concerns."
- **Apache Cassandra:** Tunable consistency per operation. `ONE` reads any replica (fastest, potentially stale). `QUORUM` reads from a majority of replicas (strongly consistent if write was also `QUORUM`). `ALL` reads from every replica (most consistent, highest latency) — source: Cassandra Docs, "How Are Cassandra's Consistency Levels Calculated?"

## Common Mistakes

- **Assuming eventual consistency means "occasionally wrong."** Eventual consistency means "temporarily stale." The system will converge. Applications need to be designed for temporary inconsistency (idempotent operations, UI that shows "last updated" timestamps), not for correctness violations.
- **Using strong consistency everywhere.** Linearizability on every read is expensive. Most reads in most applications don't need it. Only critical operations (balance checks, inventory reservation, lock acquisition) need strong consistency.
- **Ignoring client-side guarantees.** A system might be eventually consistent server-side but still need read-your-writes client-side. These are separate concerns. Getting client-side guarantees right (sticky routing, version tokens) is often more impactful than upgrading the server-side model.
- **Confusing "replica" with "stale."** A synchronously replicated replica is never stale — it has all writes before the primary acknowledged them. Only asynchronously replicated replicas can be stale. The replication mode determines staleness, not the fact that it's a replica.
