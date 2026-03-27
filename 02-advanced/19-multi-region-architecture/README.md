# Multi-Region Architecture

**Prerequisites:** `../../01-foundations/04-replication/`, `../02-distributed-transactions/`, `../03-consensus-paxos-raft/`
**Next:** `../../03-case-studies/`

---

## Concept

A single-region system has a single blast radius: a data center power failure, a network partition, or a regional cloud outage takes down the entire service. Multi-region architecture distributes the system across geographically separate locations — different cities, countries, or continents — to achieve two goals: **higher availability** (survive regional failures) and **lower latency** (serve users from a nearby region).

These goals create tension. Data that lives in one region must be replicated to others to survive failure — but replication across regions takes time (inter-region round trips are 50–200ms, vs 1ms within a data center). Strong consistency across regions is prohibitively expensive. Most multi-region systems choose eventual consistency with conflict resolution, accepting a small window of divergence in exchange for low write latency.

### Why Inter-Region Latency Matters

The speed of light is the fundamental limit. Light travels ~200,000km/s through fiber optic cable (roughly two-thirds the speed of light in vacuum). Across a continent (5,000km): ~25ms one-way. Across an ocean (10,000km): ~50ms one-way. A synchronous write that requires acknowledgement from a replica in another region adds 50–200ms of latency to every write. At 100ms per write, you can sustain only 10 synchronous cross-region writes per second per thread — far too slow for most workloads.

This physics constraint drives most multi-region design decisions.

### Active-Passive (Primary-Secondary)

One region is designated the **primary** — it handles all writes. Other regions are **secondaries** that replicate from the primary and handle read-only traffic.

**Advantages:** simple; strong consistency for writes (all writes go to one place); failover to a secondary promotes it to primary.

**Disadvantages:** write latency for users far from the primary; the primary region is still a bottleneck; failover requires promoting a secondary, which may have replication lag — the window of data that was written to the primary but not yet replicated is **lost on failover** unless synchronous replication is used (which reintroduces latency).

**Use when:** workload is read-heavy; write latency to the primary region is acceptable for users; simplicity is valued over global write performance.

### Active-Active (Multi-Primary)

All regions accept reads and writes. Each region has its own local database. Writes are propagated asynchronously to other regions.

**Advantages:** writes are low-latency (they go to the local region); the system survives the loss of any region without failover; users get low-latency reads and writes everywhere.

**Disadvantages:** concurrent writes to the same data in different regions create **conflicts** that must be detected and resolved. This is the central hard problem of active-active architecture.

**Conflict resolution strategies:**

- **Last-write-wins (LWW):** the write with the most recent timestamp wins. Simple, but clock skew between nodes makes this unreliable — a slightly fast clock can cause a stale write to overwrite a newer one. Use with logical clocks (vector clocks, Lamport timestamps) rather than wall clocks.
- **CRDT (Conflict-free Replicated Data Types):** data structures mathematically guaranteed to merge without conflicts. A CRDT counter can be incremented in multiple regions simultaneously and merged deterministically. CRDTs are limited to specific data types (counters, sets, registers with specific merge semantics) — not all business logic can be modeled as CRDTs.
- **Application-level resolution:** the application explicitly handles conflicts based on business rules. Example: in a shopping cart, two concurrent add-item operations in different regions are both preserved (union merge). A concurrent checkout and item-removal may require manual review.
- **Avoid conflicts by design:** route all writes for a given entity to a home region (the user's "home region" always handles their data). This is the simplest conflict avoidance strategy — it eliminates cross-region write conflicts at the cost of write latency for users accessing data from a non-home region.

### Geo-Routing and Anycast

Users should be routed to the nearest region automatically. DNS-based geo-routing (AWS Route 53 latency routing, Cloudflare) resolves the service hostname to the IP of the nearest region based on the client's location. Anycast routing (used by CDNs and DNS infrastructure) advertises the same IP from multiple locations — routers automatically send packets to the nearest anycast node.

### Data Residency and Compliance

Some data must not leave a jurisdiction. GDPR requires EU citizens' personal data to remain in the EU. HIPAA has requirements for US health data. Financial regulators in some countries require financial data to be stored domestically. Multi-region architecture must account for these constraints — not all data can be replicated everywhere. Common patterns:

- **Regional data stores:** each region has its own database containing only that region's users' data. Cross-region queries require explicit API calls across regions.
- **Data classification:** classify data as globally-replicable vs region-pinned. Only globally-replicable data (e.g., product catalog) is replicated everywhere; user data stays in its home region.
- **Metadata replication:** replicate only metadata globally (e.g., "user X is in the EU region"); the actual data remains in the home region.

### Replication Lag and Read-Your-Writes

With asynchronous replication, a user who writes to region A may read from region B (after being geo-routed there) and not see their own write yet — the replication hasn't completed. This is called **read-your-writes violation** and is confusing and frustrating for users.

Solutions:
- **Session stickiness:** after a write, route the user's subsequent reads to the region where they wrote (at least until replication is confirmed). Achieved via a cookie or token that encodes the last-write region.
- **Read from primary:** for operations immediately following a write, route reads to the primary region for a short window.
- **Monotonic reads:** track the replication sequence number of the user's last write; route reads to regions that have caught up to at least that sequence number.

### Failure Modes

**Regional outage:** a full region goes offline (power, network, or provider failure). In active-passive, the secondary must be promoted to primary; DNS records updated; remaining regions take over traffic. This process typically takes 1–10 minutes — the RTO (Recovery Time Objective) for a regional outage.

**Partial partition:** the region is reachable by some clients but not others (BGP misroute, partial fiber cut). Geo-routing may direct some clients to a degraded region. Solutions: health checks at the DNS/routing layer that detect regional degradation and remove the region from rotation.

**Replication lag spike:** a network congestion event or compaction job causes replication lag to grow from milliseconds to seconds or minutes. Reads from secondary regions return stale data. Solutions: alert on replication lag; set a max-lag threshold above which the secondary stops serving reads (it would rather return an error than stale data).

**Split-brain:** two regions each believe they are the primary (network partition between regions, both remain reachable by clients). Both accept writes, which diverge. Resolution requires detecting the partition, merging the diverged writes (conflict resolution), and designating one as the authoritative timeline. This is a very hard problem — most systems avoid it via consensus protocols or by having only one primary.

### Hybrid Logical Clocks (HLC)

Pure Lamport clocks advance only on events — they can drift arbitrarily far from wall time, making it difficult to reason about "how stale" a value is. **Hybrid Logical Clocks** (HLC, Kulkarni & Demirbas 2014) combine a physical component (NTP wall time, truncated to milliseconds) with a logical counter for tie-breaking within the same millisecond. HLC guarantees:

1. `hlc(e) > hlc(f)` whenever `e` causally follows `f` (same guarantee as Lamport clocks).
2. `|hlc(e) - wallclock(e)|` is bounded (typically < NTP offset, ~10ms) — so staleness can be expressed in real time.

This is why CockroachDB, YugabyteDB, and Spanner-compatible systems use HLC: you get causal ordering AND meaningful real-time bounds on how stale a replica can be.

### Write Amplification in Active-Active

When a write is accepted by any region, it must be propagated to every other region. With N regions:

- **Write fan-out:** 1 local write triggers N-1 cross-region replication writes.
- **Bandwidth cost:** each write crosses potentially multiple inter-region links. For large values (images, blobs), this is significant — avoid replicating large objects; replicate references/metadata and serve blobs from a CDN or region-local object store.
- **Conflict probability scales with N:** with 2 regions, conflicts require two concurrent writes. With 5 regions, the same key can have 5 simultaneous conflicting writes. Test conflict resolution logic at N > 2 before going live.

### CAP Theorem Position

Multi-region architecture makes the CAP theorem concrete:

- **Active-passive with synchronous replication:** CP — sacrifices Availability (writes block until secondary acknowledges) to maintain Consistency.
- **Active-passive with asynchronous replication:** CA within a region, but during a partition the secondary may serve stale data — effectively AP under partition.
- **Active-active:** AP — prioritizes Availability and Partition tolerance; accepts that concurrent writes may produce inconsistent state that requires reconciliation.

The practical framing for interviews: "In a multi-region system, partitions between regions are not hypothetical — they happen regularly (fiber cuts, BGP route leaks). You must choose: do writes block until all regions confirm (C), or do you accept eventual consistency (A)?"

### Quorum-Based Promotion and Split-Brain Prevention

In active-passive, when the primary goes offline, the secondary must be promoted. The danger: if the network partition is asymmetric (the primary is unreachable by the secondary but still reachable by some clients), both nodes may believe they are the current primary — **split-brain**.

Prevention strategies:

- **STONITH (Shoot The Other Node In The Head):** before promotion, the new primary uses an out-of-band channel (IPMI, AWS instance termination) to forcibly shut down the old primary, guaranteeing at most one active primary.
- **Quorum/arbitrator:** require majority agreement before promotion. With 3 nodes (primary, secondary, arbiter), promotion requires 2 of 3 nodes to agree. The arbiter can be a lightweight witness service in a third AZ — it doesn't hold data, just casts a vote. This is how etcd, Zookeeper, and managed databases (RDS Multi-AZ) prevent split-brain.
- **Fencing tokens:** the consensus system issues a monotonically increasing fencing token; the new primary includes this token in all writes; storage layers reject writes with stale tokens. Used in distributed lock managers (Chubby, ZooKeeper).

### Trade-offs

| Architecture | Write Latency | Availability | Consistency | Conflict Risk | RPO | CAP |
|-------------|---------------|--------------|-------------|---------------|-----|-----|
| Single region | Low | Lower | Strong | None | ~0 (within DC) | CA |
| Active-passive (sync) | High (primary RTT) | Medium | Strong | None | 0 | CP |
| Active-passive (async) | Low (local) | Higher | Eventual (secondary) | None (one writer) | Replication lag | AP |
| Active-active | Low (local write) | Highest | Eventual | High | Near-zero | AP |

## Interview Talking Points

- "Active-passive is simpler but all writes pay cross-region latency. Active-active has lower write latency but requires conflict resolution — the hard part is deciding what to do when two regions write the same data simultaneously"
- "The speed of light is the fundamental constraint. 10,000km across an ocean is 50ms one-way — synchronous replication across regions adds 100ms per write, which limits throughput to 10 syncs/sec per thread"
- "Read-your-writes violations are the most common consistency bug in multi-region systems — a user writes to region A, is routed to region B on the next request, and doesn't see their own write. Fix with session affinity or monotonic read guarantees"
- "Data residency requirements (GDPR, financial regulations) mean not all data can be freely replicated globally — classify data as global vs region-pinned early in the design"
- "Active-active conflict resolution strategies: last-write-wins (simple but clock-skew vulnerable), CRDTs (limited to specific data types), or avoid conflicts by routing writes for a given entity to a home region"
- "RTO and RPO drive the architecture: RTO (how fast can you recover?) determines active-passive vs active-active; RPO (how much data can you lose?) determines synchronous vs asynchronous replication"
- "Don't use wall-clock timestamps for LWW — use Hybrid Logical Clocks. HLC gives causal ordering AND a bounded real-time staleness window, which is why CockroachDB and YugabyteDB use it over pure Lamport clocks"
- "Split-brain in active-passive is prevented with a quorum arbiter or STONITH. With 3 nodes (primary, secondary, witness arbiter), promotion requires 2-of-3 agreement — the arbiter holds no data, just a vote. This is the pattern behind RDS Multi-AZ"
- "Active-active write amplification scales with N regions — one write fans out to N-1 cross-region replication writes. At 5 regions, conflict probability and bandwidth cost increase non-linearly; test your conflict resolution at N > 2"
- "CAP theorem forces a concrete choice in multi-region: partitions between regions happen regularly (fiber cuts, BGP issues). You must decide up-front whether writes block for cross-region confirmation (CP) or proceed locally with eventual merge (AP)"

## Hands-on Lab

**Time:** ~30 minutes
**Services:** three Redis instances — region-A (primary), region-B (secondary), region-arbiter (witness for quorum-based promotion) — and a Python script simulating cross-region replication and conflict scenarios

### Setup

```bash
cd system-design-interview/02-advanced/19-multi-region-architecture/
docker compose up -d
# Wait ~5 seconds for all three instances to be ready
```

### Experiment

```bash
python experiment.py
```

The script simulates: (1) active-passive replication — writes go to region-A, async replication to region-B with configurable lag; (2) replication lag visibility — reads from region-B return stale data during the lag window; (3) read-your-writes session affinity — routing the user back to region-A after a write to avoid stale reads; (4) active-active conflict — concurrent writes to the same key in both regions, demonstrating both last-write-wins (with logical clock) and CRDT set-merge as a contrast; (5) regional failover with quorum — region-A goes down, the arbiter casts the deciding vote to promote region-B, and the RPO data-loss window is measured; (6) replication lag monitoring — a background monitor alerts when lag exceeds a configured threshold.

### Teardown

```bash
docker compose down -v
```

## Real-World Examples

- **DynamoDB Global Tables:** AWS DynamoDB's active-active multi-region feature uses last-write-wins conflict resolution (based on timestamp) and propagates writes to all regions within ~1 second under normal conditions. Customers use it for user session state, shopping carts, and leaderboards — source: AWS re:Invent, "Amazon DynamoDB Global Tables" (2017).
- **Cloudflare's Durable Objects:** Cloudflare routes each Durable Object (a unit of state) to a single region — the object's home region. All writes for that object go to the home region; reads can be served from any edge location (with potential staleness). This is "avoid conflicts by design via home-region routing" — source: Cloudflare Blog, "Durable Objects: Consistent and Available" (2022).
- **Stripe's active-active:** Stripe operates active-active across multiple AWS regions. They use idempotency keys to handle duplicate requests during failover and rely on strong consistency only for financial transactions (routing these to a single primary). Lower-consistency data (dashboard analytics, logs) is replicated eventually — source: Stripe Engineering Blog, "Designing robust and predictable APIs with idempotency" (2017).

## Common Mistakes

- **Assuming clocks are synchronized.** Last-write-wins with wall clocks is broken under clock skew — a node whose clock is 1ms fast will always "win" conflicts, silently overwriting correct data. Use logical clocks (vector clocks, Hybrid Logical Clocks) for LWW.
- **Not planning for read-your-writes violations.** This is almost always encountered in the first multi-region deployment. Build session affinity into the architecture from the start.
- **Ignoring replication lag in SLAs.** An SLA of "data is consistent within 5 seconds" is meaningless if you haven't measured your actual replication lag under peak load. Lag spikes during compaction, maintenance windows, and traffic spikes — measure P99 lag, not just average.
- **Active-active for everything.** Not all data needs active-active. User profile data that is rarely written can be active-passive. Real-time session state that is written frequently by users everywhere genuinely benefits from active-active. Over-applying active-active adds complexity without benefit.
- **Forgetting DNS TTL during failover.** If DNS TTL is 300s (5 minutes), it takes up to 5 minutes for all clients to see a region failover in their DNS responses. For low-RTO failover, set DNS TTL to 30–60s (accepting higher DNS query volume) and use Anycast routing where possible.
