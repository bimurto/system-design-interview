# Consensus — Paxos & Raft

**Prerequisites:** `../02-distributed-transactions/`
**Next:** `../04-event-driven-architecture/`

---

## Concept

Consensus is the fundamental problem of getting multiple nodes in a distributed system to agree on a single value — even when some nodes may fail or messages may be delayed. It sounds simple but is provably hard. The FLP impossibility theorem (Fischer, Lynch, Paterson, 1985) proves that no deterministic consensus algorithm can guarantee both safety (all nodes agree on the same value) and liveness (the algorithm always terminates) in an asynchronous system where even one node can fail. In practice, systems sidestep FLP by using timeouts — making partial synchrony assumptions — and accepting that elections may occasionally take multiple rounds.

Consensus is needed in distributed databases and coordination services because without it, you get **split-brain**: two nodes simultaneously believe they are the leader and accept conflicting writes. Split-brain is catastrophic — it causes data divergence that may be impossible to reconcile without human intervention. A bank with two nodes simultaneously accepting deposits and withdrawals to the same account will have an incorrect balance. A Kubernetes cluster with two nodes both believing they are the scheduler will schedule the same pod twice.

**Paxos**, introduced by Leslie Lamport in 1989 (published 1998), was the first correct consensus algorithm. It operates in two phases: Prepare/Promise and Accept/Accepted. In the Prepare phase, a proposer sends a ballot number to all acceptors; acceptors promise not to accept any ballot with a lower number and report the highest value they've already accepted. In the Accept phase, the proposer chooses a value (constrained by the highest already-accepted value from the Promise phase) and sends Accept messages; acceptors accept if they haven't promised a higher ballot. Paxos is notoriously difficult to understand and implement correctly — Lamport joked that his original paper was "submitted to TOCS in 1989; the referees didn't understand it."

**Raft**, introduced by Ongaro and Ousterhout in 2014, was explicitly designed to be understandable. Raft decomposes consensus into three relatively independent sub-problems: leader election (choosing one server as leader), log replication (leader accepts log entries and replicates to followers), and safety (ensuring that at most one leader exists per term and committed entries are never lost). The key insight is that having an explicit leader simplifies reasoning — all writes flow through one node, eliminating the multi-proposer complexity of Paxos.

The practical result is that Raft became the consensus algorithm of choice for new distributed systems: etcd (Kubernetes' backing store), CockroachDB, TiKV (TiDB's storage layer), Consul, InfluxDB, and YugabyteDB use Raft. Paxos variants are still used in Google Spanner and Google Chubby, where Google's substantial internal investment in Paxos expertise makes it viable.

**Critical boundary:** both Paxos and Raft assume **crash-stop failures** — nodes either work correctly or stop responding. They do *not* tolerate **Byzantine faults** (nodes that send incorrect, corrupted, or malicious messages). Byzantine fault tolerance requires BFT algorithms (PBFT, HotStuff, Tendermint) and at least `3f+1` nodes to tolerate `f` Byzantine faults — significantly more overhead.

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
                                    (AppendEntries with no entries)
                                    to reset followers' election timeouts
```

Each node starts as a follower. If a follower doesn't receive a heartbeat from the leader within its election timeout (randomized between `[election_timeout, 2*election_timeout]` to prevent vote-splitting), it becomes a candidate, increments its term number, votes for itself, and sends `RequestVote` RPCs. A node grants a vote if: (1) it hasn't voted in this term yet, and (2) the candidate's log is at least as up-to-date as its own (compared by last log term first, then last log index). The first candidate to receive votes from a quorum (`⌊N/2⌋ + 1`) becomes the new leader.

**Raft log replication:**
1. Client sends write to leader (non-leaders proxy to leader).
2. Leader appends entry `(index=N, term=T, command=SET x=5)` to its log.
3. Leader sends `AppendEntries` RPC with the new entry to all followers in parallel.
4. Once a quorum of nodes have appended the entry and acknowledged, the entry is **committed**.
5. Leader applies the entry to its state machine and responds to the client.
6. Leader's next heartbeat carries the updated commit index; followers apply the entry.

**Safety guarantee:** a leader never overwrites or deletes existing log entries. Followers only accept entries whose preceding entry matches the `prevLogIndex` and `prevLogTerm` in the RPC. This ensures the **Log Matching Property**: if two logs agree on an entry at index N with the same term, they agree on all entries before N.

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
      │               promised value, │
      │               or proposer's   │
      │               own value if ø] │
      │──────────────────────────────►│
      │                               │
      │◄────── ACCEPTED(N, v) ────────│
      │                               │
      │  (notify learners of value)   │
```

Multi-Paxos extends this to a sequence of values (a log). A stable leader can skip the Prepare phase after the first round, making subsequent rounds a single Accept/Accepted exchange — similar to Raft's normal operation. The key difference: Paxos can have **log holes** (gaps at index N if a proposer failed mid-round), which require a later proposer to fill with a no-op or the previously started value. Raft prevents holes by requiring the leader to fill all gaps before committing new entries.

**Quorum math:**
- `N = 2f + 1` nodes tolerates `f` failures.
- 3 nodes: quorum = 2, tolerates 1 failure.
- 5 nodes: quorum = 3, tolerates 2 failures.
- 7 nodes: quorum = 4, tolerates 3 failures.
- Why 2f+1? You need a quorum that is guaranteed to overlap with any other quorum. With `2f+1` nodes, any two sets of `f+1` nodes must overlap by at least 1 node — that shared node ensures both quorums saw the same committed value.

### Trade-offs

| Property | Paxos | Raft | ZAB (Zookeeper) |
|---|---|---|---|
| Understandability | Hard | Designed to be easy | Moderate |
| Leader | Any proposer | Single explicit leader | Single explicit leader |
| Log holes | Possible (gaps need filling) | Never (sequential) | Never |
| Membership change | Complex (requires sub-protocols) | Joint consensus or single-server | Complex |
| Widely used in | Google Spanner/Chubby | etcd, CockroachDB, Consul, TiKV | Apache Zookeeper |
| Byzantine tolerance | No (crash-stop only) | No (crash-stop only) | No (crash-stop only) |

### Failure Modes

**Split-brain from stale leader:** a leader that is network-partitioned from the rest of the cluster may continue to believe it is the leader and serve reads. Meanwhile the other nodes elected a new leader. If clients can still reach the stale leader, it may serve stale reads. Raft prevents this for writes (the stale leader cannot commit without quorum), but stale reads are possible without linearizable read mode.

- **Linearizable reads** (etcd default): each read is routed through the leader, which confirms it still has quorum before responding. Prevents stale reads but adds a quorum round-trip.
- **Serializable reads**: served locally from any node. Lower latency, but may return stale data if the node hasn't applied recent commits.
- **Leader leases**: the leader assumes it remains leader for `(election_timeout - heartbeat_interval)` and serves reads locally without a quorum round-trip. Faster, but requires clocks between nodes to be synchronized well enough to bound the lease — clock skew can cause two nodes to simultaneously believe they hold the lease.

**Election livelock:** if multiple candidates repeatedly start elections at the same time, splitting votes, no candidate achieves a quorum and the cluster is leaderless. Raft's randomized election timeouts make this extremely unlikely. If it occurs, the cluster becomes temporarily unavailable until one candidate wins. The **prevote** optimization (enabled in etcd by default) adds a pre-election round where a candidate first checks it can reach a quorum *without* incrementing its term — preventing a rejoining partitioned node from disrupting the cluster by repeatedly triggering term increments.

**Log divergence during network partition:** during a partition, two leaders may both be elected (each with their own quorum on different sides of the partition). They may both accept writes. When the partition heals, the logs diverge. Raft resolves this by having the new leader (with the higher term number) overwrite the old leader's *uncommitted* entries via AppendEntries. Entries committed by the old leader are safe — they were replicated to a quorum that overlaps with the new leader's quorum, so the new leader already has them. Uncommitted entries from the old leader are lost; this is correct because the old leader never acknowledged those writes to the client.

**Membership changes (joint consensus):** adding or removing a node from a Raft cluster is itself a consensus problem. A naive approach — just updating the cluster configuration — can create two independent majorities during the transition that simultaneously elect different leaders. The safe solution is **joint consensus**: the cluster first enters a transitional state `C_{old,new}` where decisions require majority approval from *both* the old and new configurations simultaneously. Only after `C_{old,new}` is committed does the cluster transition to `C_{new}`. etcd also supports single-server membership changes (one node at a time), which is simpler but slower.

**Short election timeouts in high-latency deployments:** if election timeouts are shorter than typical network round-trip time between nodes, spurious elections fire constantly, degrading performance. For cross-datacenter etcd deployments, election timeouts should be 5–10x the inter-datacenter RTT.

## Interview Talking Points

- "Raft decomposes consensus into three problems: leader election, log replication, and safety. All writes go through the elected leader — this simplicity is why Raft is now the standard choice for new systems over Paxos."
- "The quorum formula is N = 2f+1. For 3 nodes, quorum=2, tolerates 1 failure. For 5 nodes, quorum=3, tolerates 2 failures. Adding more nodes improves fault tolerance but also increases write latency because the leader must wait for more acknowledgments — this is the trade-off that limits etcd to ~10,000 ops/sec, making it unsuitable for application data."
- "etcd is the backing store for all Kubernetes state — every pod, service, configmap, and secret is stored there. Its durability guarantee (Raft consensus + fsync to disk before ACKing) is what makes Kubernetes safe against leader failures."
- "CAP theorem: etcd/Consul/Zookeeper choose CP — consistent and partition-tolerant but unavailable when quorum is lost. DynamoDB/Cassandra can choose AP — available under partition but may return stale data. A more nuanced model is PACELC: under partition (P), etcd chooses Consistency (C); else (E), it trades Latency (L) for Consistency (C) via its quorum reads."
- "Raft and Paxos only tolerate crash-stop failures — nodes that stop responding. They do NOT tolerate Byzantine faults (nodes sending wrong data). For Byzantine tolerance you need BFT algorithms like PBFT or HotStuff, which require 3f+1 nodes and are much more expensive. Blockchain consensus algorithms (Tendermint) are BFT-based."
- "A 2-node cluster is a common anti-pattern. With quorum=2, BOTH nodes must be alive for any write. It has strictly worse availability than a single node. Always use odd-numbered clusters: 3, 5, or 7."
- "Leader election introduces a ~150–300ms unavailability window (configurable). During this window, writes are rejected — they don't silently succeed on the wrong node. After the window, the new leader's first action is to replicate all committed entries to lagging followers."
- "The prevote optimization in etcd prevents a partitioned node from disrupting the cluster on rejoin. Without it, the rejoining node would send RequestVote with a high term, forcing all nodes to step down even though the cluster was healthy."
- "Linearizable reads in etcd go through a quorum round-trip. Serializable reads are served locally but may be stale. Leader leases offer a middle ground — local reads with a time-bounded staleness window — but require tightly synchronized clocks."

## Hands-on Lab

**Time:** ~30-40 minutes
**Services:** 3-node etcd cluster (etcd0: 2379, etcd1: 2479, etcd2: 2381)

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

The script demonstrates:
1. Write replication and linearizable vs serializable reads
2. Observing Raft term number and leader identity
3. Killing the leader and **measuring election latency** (how long the cluster is unavailable)
4. Verifying the Raft term increments on each election
5. Killing a second node to lose quorum — writes fail and time out
6. Restarting a node and watching log catchup restore quorum
7. Compare-and-swap (atomic test-and-set) — the foundation of etcd distributed locks and Kubernetes leader election
8. Final cluster state summary showing term increments across the lab

### Break It

```bash
# Watch Raft leader election happen in real time
# Terminal 1: stream etcd logs filtered to election events
docker logs -f etcd1 2>&1 | grep -iE "election|leader|became|term|heartbeat"

# Terminal 2: repeatedly write to the cluster, then kill the leader
python3 - <<'EOF'
import etcd3, time

clients = [
    etcd3.client(host='localhost', port=p, timeout=2)
    for p in [2379, 2479, 2381]
]

# Find which node is the leader
for i, c in enumerate(clients):
    try:
        s = c.status()
        # Match leader ID to member name
        for m in c.members:
            if m.id == s.leader:
                print(f"Leader is node {i} (port {[2379,2479,2381][i]}): {m.name}")
        break
    except Exception:
        pass
EOF

# Terminal 3: kill the leader container (use name from above output)
docker stop etcd0   # or etcd1 / etcd2

# Observe in Terminal 1: "became candidate", "became leader", new term number
# Observe in Terminal 2: brief write timeout, then writes resume
```

### Observe

When you kill etcd0 while it's the leader, there's a 1–2 second pause before the cluster elects a new leader (configurable via `--election-timeout`; the lab uses 1000ms). After that, writes succeed through the surviving nodes. When you kill a second node, writes fail with a timeout — the system refuses to accept writes rather than risk inconsistency. When the second node restarts, it fetches all missing log entries from the leader via AppendEntries and immediately participates in quorum decisions.

### Teardown

```bash
docker compose down -v
```

## Real-World Examples

- **Kubernetes / etcd:** Every Kubernetes cluster uses etcd as its backing store. All cluster state — pods, services, replication controllers, namespaces — is stored in etcd using Raft consensus. A 3-node etcd cluster can survive one node failure; most production clusters use 3 or 5 nodes. etcd's write throughput is ~10,000 ops/sec — it is designed for cluster coordination, not application data. Source: Kubernetes documentation, "Operating etcd clusters for Kubernetes."
- **CockroachDB:** CockroachDB uses Raft at the range level — each 512MB range of data has its own Raft group with its own leader. This distributes write load across the cluster (different Raft leaders for different key ranges). Range splits create new Raft groups; range merges collapse them. Source: CockroachDB blog, "Consensus, Made Thrive," 2016.
- **Google Chubby / Multi-Paxos:** Google's distributed lock service uses Multi-Paxos. It provides coarse-grained distributed locking and small-file storage used by Bigtable and other Google services for leader election and configuration. After the first Prepare/Promise round, a stable leader skips Prepare entirely — each subsequent log entry costs one Accept/Accepted round-trip, the same as Raft's normal operation. Source: Burrows, "The Chubby lock service for loosely-coupled distributed systems," OSDI 2006.
- **Kubernetes controller leader election:** Kubernetes controllers (scheduler, controller-manager) use etcd leases for leader election. The active instance renews a lease every few seconds; if it crashes, the lease expires and a standby wins the next compare-and-swap — exactly the pattern demonstrated in Phase 6 of the experiment.

## Common Mistakes

- **Confusing consensus with replication.** Consensus (Raft/Paxos) is about agreeing on the *order* of operations. Replication is about copying data. You can replicate without consensus (MySQL async replication), but you lose durability guarantees. Consensus-based replication (Raft) is synchronous and quorum-based — it slows down writes but guarantees no committed writes are lost even if `f` nodes fail simultaneously.
- **Setting election timeouts too short.** If election timeouts are shorter than typical network round-trip time between nodes, spurious elections fire constantly. For cross-datacenter deployments (50–100ms RTT), election timeouts should be 5–10x the RTT. etcd defaults (100ms heartbeat, 1000ms election timeout) are tuned for same-datacenter deployments.
- **Using a 2-node cluster "for cost savings."** A 2-node cluster (quorum=2) requires *both* nodes to be alive for any write. It has strictly worse availability than a single node. Always use an odd number of nodes: 3, 5, or 7.
- **Assuming linearizable reads are free.** etcd's default linearizable reads route through the leader (via a quorum round-trip). This adds latency proportional to the network RTT between leader and followers. Serializable reads bypass quorum and may return stale data. Choose deliberately based on your consistency requirements.
- **Treating Raft/Paxos as Byzantine fault tolerant.** These algorithms assume crash-stop faults (nodes stop responding). A single Byzantine node (one that sends arbitrary wrong messages) can violate their safety guarantees. BFT requires PBFT/HotStuff with 3f+1 nodes — a different algorithm class entirely.
- **Ignoring membership change complexity.** Adding or removing a node mid-operation requires joint consensus or single-server changes. Skipping this and manually reconfiguring can create split-brain during the transition window. Always use the cluster's built-in membership change API.
