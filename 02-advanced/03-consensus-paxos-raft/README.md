# Consensus — Paxos & Raft

**Prerequisites:** `../02-distributed-transactions/`
**Next:** `../04-event-driven-architecture/`

---

## Concept

Consensus is the fundamental problem of getting multiple nodes in a distributed system to agree on a single value — even when some nodes may fail or messages may be delayed. It sounds simple but is provably hard. The FLP impossibility theorem (Fischer, Lynch, Paterson, 1985) proves that no deterministic consensus algorithm can guarantee both safety (all nodes agree on the same value) and liveness (the algorithm always terminates) in an asynchronous system where even one node can fail. In practice, systems sidestep FLP by using timeouts — making partial synchrony assumptions — and accepting that elections may occasionally take multiple rounds.

Consensus is needed in distributed databases and coordination services because without it, you get **split-brain**: two nodes simultaneously believe they are the leader and accept conflicting writes. Split-brain is catastrophic — it causes data divergence that may be impossible to reconcile without human intervention. A bank with two nodes simultaneously accepting deposits and withdrawals to the same account will have an incorrect balance. A Kubernetes cluster with two nodes both believing they are the scheduler will schedule the same pod twice.

**Paxos**, introduced by Leslie Lamport in 1989 (published 1998), was the first correct consensus algorithm. It operates in two phases: Prepare/Promise and Accept/Accepted. In the Prepare phase, a proposer sends a ballot number to all acceptors; acceptors promise not to accept any ballot with a lower number and report the highest value they've already accepted. In the Accept phase, the proposer chooses a value (constrained by the highest already-accepted value from the Promise phase) and sends Accept messages; acceptors accept if they haven't promised a higher ballot. Paxos is notoriously difficult to understand and implement correctly — Lamport joked that his original paper was "submitted to TOCS in 1989; the referees didn't understand it."

**Raft**, introduced by Ongaro and Ousterhout in 2014, was explicitly designed to be understandable. Raft decomposes consensus into three relatively independent sub-problems: leader election (choosing one server as leader), log replication (leader accepts log entries and replicates to followers), and safety (ensuring that at most one leader exists per term and committed entries are never lost). The key insight is that having an explicit leader simplifies reasoning — all writes flow through one node, eliminating the multi-proposer complexity of Paxos.

The practical result is that Raft became the consensus algorithm of choice for new distributed systems: etcd (Kubernetes' backing store), CockroachDB, TiKV (TiDB's storage layer), Consul, and many others use Raft. Paxos variants are still used in Google Spanner and Google Chubby, where Google's substantial internal investment in Paxos expertise makes it viable.

## How It Works

**Raft leader election:**

```
  Node states:

  FOLLOWER  ──── election timeout ───► CANDIDATE
     ▲                                     │
     │ loses election or sees              │ wins majority vote
     │ higher-term message                 ▼
     └────────────────────────────────── LEADER
                                           │
                                    sends heartbeats
                                    (AppendEntries)
                                    to reset followers'
                                    election timeouts
```

Each node starts as a follower. If a follower doesn't receive a heartbeat from the leader within its election timeout (randomized 150–300ms in etcd), it becomes a candidate, increments its term number, votes for itself, and sends `RequestVote` RPCs. A node grants a vote if: (1) it hasn't voted in this term yet, and (2) the candidate's log is at least as up-to-date as its own. The first candidate to receive votes from a quorum (⌊N/2⌋ + 1) becomes the new leader.

**Raft log replication:**
1. Client sends write to leader.
2. Leader appends entry `(index=N, term=T, command=SET x=5)` to its log.
3. Leader sends `AppendEntries` RPC with the new entry to all followers.
4. Once a quorum of nodes have appended the entry and acknowledged, the entry is **committed**.
5. Leader applies the entry to its state machine and responds to the client.
6. Leader's next heartbeat informs followers of the new commit index; followers apply the entry.

**Safety guarantee:** a leader never overwrites or deletes existing log entries. Followers only accept entries whose preceding entry matches. This ensures the **Log Matching Property**: if two logs agree on an entry at index N, they agree on all entries before N.

**Paxos (Classic, single-decree):**
```
  Proposer                     Acceptors (quorum)
      │                               │
      │ PREPARE(ballot=N)             │
      │──────────────────────────────►│
      │                               │
      │◄─────── PROMISE(N, ø or v) ───│  (highest accepted value, if any)
      │                               │
      │ ACCEPT(N, v)  [v = highest    │
      │               promised value] │
      │──────────────────────────────►│
      │                               │
      │◄────── ACCEPTED(N, v) ────────│
      │                               │
      │  (notify learners of value)   │
```

Multi-Paxos extends this to a sequence of values (a log). A stable leader can skip the Prepare phase after the first round, making subsequent rounds a single Accept/Accepted exchange — similar to Raft's normal operation.

**Quorum math:**
- `N = 2f + 1` nodes tolerates `f` failures.
- 3 nodes: quorum = 2, tolerates 1 failure.
- 5 nodes: quorum = 3, tolerates 2 failures.
- 7 nodes: quorum = 4, tolerates 3 failures.
- Why 2f+1? You need a quorum that is guaranteed to overlap with any other quorum. With 2f+1 nodes, any two sets of f+1 nodes must overlap by at least 1 node — that shared node ensures both quorums saw the same committed value.

### Trade-offs

| Property | Paxos | Raft | ZAB (Zookeeper) |
|---|---|---|---|
| Understandability | Hard | Designed to be easy | Moderate |
| Leader | Any proposer | Single explicit leader | Single explicit leader |
| Log gaps | Possible (holes in log) | Never (sequential) | Never |
| Widely used in | Google Spanner/Chubby | etcd, CockroachDB, Consul | Apache Zookeeper |
| Membership change | Complex | Joint consensus or single-server changes | Complex |

### Failure Modes

**Split-brain from stale leader:** a leader that is network-partitioned from the rest of the cluster may continue to believe it is the leader and serve reads. Meanwhile the other nodes elected a new leader. If clients can still reach the stale leader, it may serve stale reads. Raft prevents this for writes (the stale leader cannot commit without quorum), but stale reads are possible without linearizable read mode. etcd's default is linearizable reads (each read goes through Raft), which prevents this at the cost of latency.

**Election livelock:** if multiple candidates repeatedly start elections at the same time, splitting votes, no candidate achieves a quorum and the cluster is leaderless. Raft's randomized election timeouts (each node picks a different timeout between 150-300ms) make this extremely unlikely but not impossible. If it occurs, the cluster becomes temporarily unavailable until one candidate wins.

**Log divergence during network partition:** during a partition, two leaders may both be elected (each with their own quorum). They may both accept writes. When the partition heals, the logs diverge. Raft resolves this by having the new leader (with the higher term number) overwrite the old leader's uncommitted entries. Entries committed by the old leader are safe (they were replicated to a quorum and will be in the new leader's log). Uncommitted entries from the old leader are lost — this is acceptable because the old leader never told the client those writes succeeded.

## Interview Talking Points

- "Raft decomposes consensus into three problems: leader election, log replication, and safety. All writes go through the elected leader — this simplicity is why Raft is now the standard choice for new systems."
- "The quorum formula is N = 2f+1. For 3 nodes, quorum=2, tolerates 1 failure. For 5 nodes, quorum=3, tolerates 2 failures. Adding more nodes improves fault tolerance but also increases write latency because the leader must wait for more acknowledgments."
- "etcd is the backing store for all Kubernetes state — every pod, service, configmap, and secret is stored there. Its durability guarantee (Raft consensus + fsync) is what makes Kubernetes safe."
- "CAP theorem: etcd/Consul/Zookeeper choose CP — consistent and partition-tolerant but unavailable when quorum is lost. DynamoDB/Cassandra can choose AP — available under partition but may return stale data."
- "Paxos is mathematically elegant but notoriously hard to implement correctly — this is why Ongaro explicitly designed Raft for understandability. The Raft paper includes a formal proof and a user study showing engineers understand Raft better than Paxos."
- "FLP impossibility: no deterministic consensus algorithm can guarantee both safety and liveness in a fully asynchronous system. Real systems bypass this by using timeouts — partial synchrony — accepting that elections may occasionally time out and retry."

## Hands-on Lab

**Time:** ~25-35 minutes
**Services:** 3-node etcd cluster (ports 2379-2381)

### Setup

```bash
cd system-design-interview/02-advanced/03-consensus-paxos-raft/
docker compose up -d
# Wait ~20 seconds for the 3-node cluster to form and elect a leader
```

### Experiment

```bash
pip install etcd3
python experiment.py
```

The script requires Docker access (to stop/start etcd containers). It: writes a key to etcd0, reads it back confirming replication, kills etcd0 (shows writes still work with 2/3 nodes), kills etcd1 (shows writes fail with 1/3 nodes, no quorum), restarts etcd1 (shows writes resume), and prints cluster member list and leader ID.

### Break It

```bash
# Watch Raft leader election happen in real time
# Terminal 1: watch etcd logs for election events
docker logs -f $(docker ps -q --filter name=etcd1) 2>&1 | grep -E "election|leader|term"

# Terminal 2: repeatedly write to the cluster and kill the leader
python -c "
import etcd3, time, subprocess

c = etcd3.client(host='localhost', port=2379, timeout=5)

# Find current leader
status = c.status()
print(f'Leader ID: {hex(status.leader)}')

# Kill the container running the leader
# (You'll need to identify the container by member ID)
# Then observe election in Terminal 1
"
```

### Observe

When you kill etcd0 while it's the leader, there's a ~300ms pause before the cluster elects a new leader (visible as a brief write timeout). After that, writes succeed through etcd1 or etcd2. When you kill a second node, writes fail with a timeout error — the system refuses to accept writes rather than risk inconsistency. When the second node comes back, writes immediately succeed as the rejoining node catches up via log replication.

### Teardown

```bash
docker compose down -v
```

## Real-World Examples

- **Kubernetes / etcd:** Every Kubernetes cluster uses etcd as its backing store. All cluster state — pods, services, replication controllers, namespaces — is stored in etcd using Raft consensus. A 3-node etcd cluster can survive one node failure; most production clusters use 3 or 5 nodes. Source: Kubernetes documentation, "Operating etcd clusters for Kubernetes."
- **CockroachDB:** CockroachDB uses Raft at the range level — each 512MB range of data has its own Raft group with its own leader. This allows different parts of the keyspace to have different leaders, distributing write load across the cluster. Range splits create new Raft groups; range merges collapse them. Source: CockroachDB blog, "Consensus, Made Thrive," 2016.
- **Google Chubby / Paxos:** Google's distributed lock service (Chubby) uses Multi-Paxos. It provides coarse-grained distributed locking and small-file storage, used by Bigtable and other Google services for leader election and configuration distribution. Source: Burrows, "The Chubby lock service for loosely-coupled distributed systems," OSDI 2006.

## Common Mistakes

- **Confusing consensus with replication.** Consensus (Raft/Paxos) is about agreeing on the *order* of operations. Replication is about copying data. You can replicate without consensus (MySQL async replication), but you lose durability guarantees. Consensus-based replication (Raft) is synchronous and quorum-based — it slows down writes but guarantees no committed writes are lost.
- **Setting election timeouts too short.** If election timeouts are shorter than typical network round-trip time between nodes, spurious elections fire constantly, degrading performance. etcd defaults to 100ms heartbeat interval and 1000ms election timeout. Increasing these (for high-latency cross-datacenter deployments) is necessary but increases the time to detect and recover from a real leader failure.
- **Using a 2-node cluster "for cost savings."** A 2-node cluster (quorum=2) requires *both* nodes to be alive for any write. It actually has *worse* availability than a single node. Always use an odd number of nodes: 3, 5, or 7.
- **Assuming linearizable reads are free.** etcd's default linearizable reads route through the leader (via a Raft quorum read). This adds latency. Serializable reads bypass quorum and may return stale data. Choose based on your consistency requirements, not blindly defaulting to one or the other.
