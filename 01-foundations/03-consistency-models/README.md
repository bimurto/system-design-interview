# Consistency Models

**Prerequisites:** `../02-cap-theorem/`
**Next:** `../04-replication/`

---

## Concept

Consistency models define the contract a distributed system makes with clients about what they will observe when reading
data. They form a spectrum from the strongest guarantee (linearizability, where the system appears to be a single
machine) to the weakest (eventual consistency, where replicas merely agree to converge "at some point"). Choosing the
right model is one of the central design decisions for any distributed system: stronger consistency costs more in
latency and availability, but weaker consistency requires more complexity in the application.

The word "consistency" is dangerously overloaded across the industry:

- **ACID consistency** (SQL databases): constraints and invariants are upheld after every transaction (e.g., foreign-key
  integrity, balance never goes negative). This is an application-level guarantee, not a distributed systems concept.
- **CAP consistency** (distributed systems): linearizability — every read reflects the most recent write across all
  nodes. This is a replication guarantee.

This topic covers the distributed systems definition and the full spectrum below it.

---

## The Consistency Spectrum

### Linearizability (Strong Consistency)

Every operation appears to take effect instantaneously at a single point in real time between its invocation and
response. Once a write completes, every subsequent read — from any node, by any client — returns that value or a newer
one. The system is indistinguishable from a single-machine system.

Achieving this across multiple nodes requires every read and write to go through coordination (Paxos, Raft, two-phase
commit), which adds round-trip latency and reduces availability under network partitions.

**How it works:**

1. Client writes `key=X, value=5` to the leader
2. Leader replicates to a quorum of followers synchronously via Raft; entry is committed only after quorum confirms
3. Write is acknowledged to the client only after commit
4. Any subsequent read from any node that checks the commit index is guaranteed to return `value=5` or newer — stale
   reads are impossible

**Systems:** etcd, ZooKeeper, Google Spanner (for linearizable reads), Redis WAIT + acked-replica reads.

### Sequential Consistency

All nodes agree on a single global order of operations, but that order is not required to match wall-clock time. A write
that completes at T=10 might appear "logically before" an operation that started at T=8 in the global order, as long as
every node sees the same sequence. This is weaker than linearizability but stronger than causal consistency, and it
underpins many CPU memory models (x86 Total Store Order, Java `volatile`).

### Causal Consistency

If operation A causally precedes operation B (A "happened before" B — e.g., A is a post and B is a reply that references
A), then all nodes must see A before B. Causally unrelated operations can appear in any order across nodes.

Causality is tracked using **vector clocks** or **version vectors**: each write carries a vector of logical timestamps,
one per node. A node only applies write B if all writes that B depends on (per the vector) have already been applied.

**Systems:** MongoDB causal sessions (using cluster time), Amazon Dynamo (version vectors), Bayou.

**Critical failure mode — reply before post:** Alice writes a post to replica1; Bob reads the post from replica1 and
writes a reply to master; Carol reads from replica2, which has received the reply but not yet the post. Carol sees a
reply to a non-existent post. Vector clocks prevent this by refusing to serve the reply until the causal dependency (the
post) has arrived.

### Eventual Consistency

The weakest useful guarantee: if no new writes occur, all replicas will eventually converge to the same value. There is
no bound on when convergence occurs, and during the convergence window different clients may see different values, and
the same client reading from different replicas may see values out of order.

**Conflict resolution:** when two replicas independently accept conflicting writes, eventual consistency systems need a
strategy:

- **Last-Write-Wins (LWW):** the write with the highest timestamp wins. Simple but loses data when clocks are skewed.
- **CRDTs (Conflict-free Replicated Data Types):** data structures (counters, sets, maps) mathematically guaranteed to
  merge without conflict. Used by Redis CRDT Enterprise and Riak.
- **Application-level merge:** the application receives all conflicting versions and resolves them (Dynamo's shopping
  cart approach). Most flexible, most complex.

**Systems:** Cassandra/DynamoDB default read mode, DNS, CDN caches.

---

## Client-Side Session Guarantees

These four guarantees are orthogonal to the server-side consistency model. They describe what a single client session
observes, and can be layered on top of an eventually consistent system:

| Guarantee               | Definition                                                       | Common Violation                                               | Fix                                                          |
|-------------------------|------------------------------------------------------------------|----------------------------------------------------------------|--------------------------------------------------------------|
| **Read-your-writes**    | After writing V, all your subsequent reads return V or newer     | Write to master, read from lagging replica                     | Sticky routing, WAIT, or always read from master post-write  |
| **Monotonic reads**     | You never see a version older than one you've already seen       | Load balancer routes you to replicas at different offsets      | Pin client to one replica per session; session offset tokens |
| **Monotonic writes**    | Your writes are applied in the order you issued them             | Async replication with out-of-order delivery                   | Single-stream replication, sequence numbers on writes        |
| **Writes-follow-reads** | A write that follows a read is ordered after that read's version | Write based on stale read appears to precede the read causally | Include read version in write request; causal sessions       |

All four together constitute **session consistency**, which is the minimum bar for most user-facing applications.

---

## Linearizability vs. Serializability — A Classic Interview Trap

These terms are frequently conflated. They are distinct:

**Serializability** is a database transaction guarantee. Multiple concurrent transactions produce the same result as if
they had been executed one at a time in some serial order. The key word is "some" — the serial order does not have to
match wall-clock time. It is concerned with multi-object, multi-operation transactions.

**Linearizability** is a distributed systems guarantee for single-object operations. Every operation appears to take
effect at a single point in real time between invocation and completion. The serial order is constrained by real-time
ordering.

**Strict serializability** = serializability + linearizability. Transactions are both serializable (multi-object
atomicity) and linearizable (real-time ordering). This is what Google Spanner and CockroachDB provide using
TrueTime/hybrid logical clocks.

Most ACID databases (PostgreSQL Serializable Snapshot Isolation) provide serializability but not linearizability — a
transaction that starts at T=8 may read values written at T=10 if the snapshot isolation's version chain allows it.

---

## Trade-offs

| Model                  | Latency | Availability (under partition) | Staleness                   | Conflict risk      | Typical Use Case                                         |
|------------------------|---------|--------------------------------|-----------------------------|--------------------|----------------------------------------------------------|
| Linearizability        | Highest | Lowest (CP)                    | None                        | None               | Leader election, locks, counters, inventory reservations |
| Sequential consistency | High    | Low-medium                     | None (in order)             | None               | CPU memory models, some DB engines                       |
| Causal consistency     | Medium  | Medium                         | Possible for unrelated ops  | Low                | Social feeds, collaborative editing, comment threads     |
| Session consistency    | Low     | High                           | Possible for other sessions | Medium             | User profile reads, shopping, feeds                      |
| Eventual consistency   | Lowest  | Highest (AP)                   | Yes, unbounded              | High without CRDTs | DNS, CDN, caches, counters (with CRDTs)                  |

---

## Failure Modes and Gotchas

**Stale reads causing incorrect business decisions:** routing reads to replicas without a consistency requirement can
return values that lead to double charges, oversold inventory, or stale permissions. The fix is to identify which reads
are decision-critical (balance checks, reservation gates) and always route those to the primary or use WAIT.

**WAIT return value not checked:** Redis `WAIT n timeout_ms` returns the number of replicas that actually acknowledged,
which may be less than `n` if the timeout expired or a replica is down. An application that calls `WAIT 2, 100` but
doesn't check that the return value is exactly 2 may falsely believe the write is durable on 2 replicas.

**Monotonic read violation after replica restart:** sticky sessions break when the pinned replica restarts and session
state is lost, routing the client to a potentially lagging replica. Fix: store a minimum replication offset in the
session token; the new replica refuses to serve reads until it reaches that offset.

**Causal violation with fan-out writes:** if a system writes the "effect" (reply) to a different shard or region from
the "cause" (post), the effect can arrive before the cause on replicas in distant regions. Vector clocks or causal
session tokens are required.

**Split-brain and linearizability:** if a network partition causes two leaders to accept writes simultaneously (e.g., a
misconfigured Paxos/Raft group), linearizability is broken — two different clients may see conflicting "latest" values.
Proper leader lease mechanisms (Raft's leader leases, etcd's linearizable reads via Raft log) prevent this.

**LWW and clock skew:** Last-Write-Wins relies on wall-clock timestamps, but NTP skew of even a few milliseconds can
cause a logically-earlier write to have a higher timestamp and "win" over a logically-later write. CRDTs or
application-level versioning are safer alternatives.

**Confusing replica type with staleness:** a synchronously replicated replica is never stale — the write isn't
acknowledged until the replica confirms. Only asynchronously replicated replicas can be stale. The replication mode
determines staleness, not the fact that it's a replica.

---

## Interview Talking Points

- "Linearizability means the system looks like a single machine — every read reflects the latest write, and the ordering
  respects real time. It requires coordination on every operation via consensus (Raft/Paxos), so it's expensive in both
  latency and availability."
- "Don't confuse linearizability with serializability. Linear is about single-object real-time ordering; serial is about
  multi-object transaction atomicity. Strict serializability gives you both — that's what Spanner provides."
- "Read-your-writes, monotonic reads, monotonic writes, and writes-follow-reads are client-side session guarantees
  orthogonal to the server-side model. You can have an eventually consistent system that still provides read-your-writes
  via sticky routing — they're independent axes."
- "Redis `WAIT n timeout` upgrades eventual consistency to read-your-writes by blocking until n replicas have
  acknowledged. Always check the return value — if fewer than n acked, the timeout expired and you have weaker
  durability than requested."
- "Causal consistency is often the sweet spot for social/collaborative applications: stronger than eventual (no
  reply-before-post), cheaper than linearizable, and sufficient when users mainly see content they or their follows
  produced."
- "For conflict resolution in eventually consistent systems, LWW is simple but loses data under clock skew. CRDTs are
  the principled solution — data structures mathematically guaranteed to merge. If you're designing a shopping cart or
  collaborative document, CRDTs are the right answer."
- "The practical question in any design is: which operations are decision-critical and need strong consistency, and
  which can tolerate staleness? Applying linearizability uniformly is expensive and unnecessary. Apply it surgically."

---

## Hands-on Lab

**Time:** ~25–35 minutes
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

The script runs six phases:

1. **Eventual consistency** — write to master, read from replica immediately across 20 trials; counts stale reads
2. **WAIT latency** — compares average write+read latency for no-WAIT, WAIT 1, and WAIT 2 across 15 iterations
3. **Read-your-writes** — demonstrates the violation path and WAIT-based fix
4. **Monotonic reads** — forces replica2 to lag using `DEBUG SLEEP` and alternates reads to produce a visible value
   regression
5. **Causal consistency** — simulates the reply-before-post violation with forced replica lag
6. **Linearizability vs. serializability** — explains the distinction and shows WAIT's scope

### Break It

```bash
# Stop replica2, then try WAIT 2 with a short timeout
docker compose stop redis-replica2

python -c "
import redis
r = redis.Redis(host='localhost', port=6379, decode_responses=True)
r.set('x', 42)
acked = r.wait(2, 200)  # wait for 2 replicas, 200ms timeout
print(f'WAIT returned: {acked} (expected 2)')
print('WAIT return value MUST be checked — fewer acks = weaker durability')
print('Application logic that ignores this silently degrades consistency')
"

docker compose start redis-replica2
```

### Observe

Latency numbers from a typical localhost run:

```
No WAIT (eventual):   0.07 ms
WAIT 1 replica:       0.31 ms
WAIT 2 replicas:      0.48 ms
```

The overhead is the replication round-trip over loopback. Across data centers (50ms RTT), WAIT 1 adds ~50ms and WAIT 2
adds ~50ms (not additive — limited by the slowest replica that acks within the timeout). That is the real latency cost
of global strong consistency.

### Teardown

```bash
docker compose down -v
```

---

## Real-World Examples

- **Amazon DynamoDB:** Two read modes per request — eventually consistent (default, cheaper, potentially stale) and
  strongly consistent (costs 2x read capacity, always reads from the leader, linearizable). Engineers choose
  per-operation. Source: AWS DynamoDB Developer Guide, "Read Consistency."
- **MongoDB:** Causal consistency via client sessions and cluster time — a logical Lamport clock advanced with each
  operation. Reads in a causally consistent session are guaranteed to reflect all writes from that session, regardless
  of which node is read from. Source: MongoDB Docs, "Causal Consistency and Read and Write Concerns."
- **Apache Cassandra:** Tunable consistency per operation. `ONE` reads from any replica (fastest, potentially stale).
  `QUORUM` requires majority (strongly consistent if the write also used `QUORUM` — the read-repair intersection
  guarantees overlap). `ALL` reads from every replica (highest consistency, lowest availability). Source: Cassandra
  Docs, "How Are Cassandra's Consistency Levels Calculated?"
- **Google Spanner:** Uses TrueTime (GPS + atomic clock bounded uncertainty intervals) to provide strict serializability
  globally. Reads acquire a timestamp, then wait until the clock uncertainty window closes before returning — ensuring
  the read is globally ordered. Source: Corbett et al., "Spanner: Google's Globally Distributed Database," OSDI 2012.
- **Redis CRDT (Enterprise):** Uses CRDTs for multi-master replication without conflicts — counters, sets, and maps
  merge mathematically. No coordination required for writes; convergence is guaranteed by data structure properties
  rather than consensus protocols.

---

## Common Mistakes

- **Treating eventual consistency as "occasionally wrong."** Eventual consistency means "temporarily stale," not
  incorrect. The system will converge. Design for temporary inconsistency (idempotent operations, UI timestamps,
  optimistic UI with reconciliation) rather than for permanent corruption.
- **Applying strong consistency uniformly.** Linearizability on every read is expensive. Identify which operations are
  decision-critical (balance checks, lock acquisition, inventory reservation) and apply strong consistency only there.
  Most reads in most applications tolerate staleness.
- **Ignoring client-side session guarantees.** A system can be eventually consistent server-side but still need
  read-your-writes client-side. These are orthogonal concerns. Sticky routing and session offset tokens are often
  cheaper than upgrading the server-side model.
- **Not checking WAIT return values.** `WAIT n timeout` is not a guarantee — it's a best-effort block. If fewer than n
  replicas ack, the application silently has weaker consistency than intended. Always assert the return value equals the
  desired replica count.
- **Using LWW without understanding clock skew.** NTP drift of even 1–2ms can cause a logically-earlier write to have a
  higher timestamp and overwrite a logically-later write. Use hybrid logical clocks (HLC), CRDTs, or application-level
  versioning in systems where data loss is unacceptable.
- **Confusing causal consistency with read-your-writes.** Read-your-writes is a single-client guarantee about that
  client's own writes. Causal consistency is a multi-client guarantee about the ordering of causally related operations
  from different clients. They address different problems.
