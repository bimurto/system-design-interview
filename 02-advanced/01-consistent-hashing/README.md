# Consistent Hashing

**Prerequisites:** `../../01-foundations/05-partitioning-sharding/`
**Next:** `../02-distributed-transactions/`

---

## Concept

When you distribute data across multiple servers, you need a rule to decide which server stores which data. The simplest rule — `server = hash(key) % num_servers` — works until the cluster size changes. Add or remove a server and the divisor shifts, meaning almost every key now maps to a different server. In a distributed cache this is catastrophic: on a resize from 3 to 4 nodes roughly 75% of all cached keys become unreachable, causing a thundering herd of cache misses against the database. In a distributed database it means 75% of all data must be physically migrated between nodes.

Consistent hashing solves this by mapping both keys and servers onto the same circular hash space — the "ring." Servers are placed at positions on the ring by hashing their names. Keys are also hashed to a position, then "walk clockwise" until they reach the nearest server. When a server is added, it occupies a position on the ring and absorbs only the keys that were previously owned by its clockwise neighbor. When a server is removed, those keys pass to the next server clockwise. In both cases only 1/N of the total keys move, where N is the number of servers — the rest are completely unaffected.

The naive ring works but has a significant flaw: with only 3 physical servers there are only 3 points on the ring. The gaps between points are random, so one server could own 70% of the ring while another owns 10%, producing severely unbalanced load. Virtual nodes (also called "vnodes" or "replicas") fix this by placing each physical server at multiple positions on the ring. With 150 virtual nodes per server, the three servers each get roughly 150 points distributed around the ring, and by the law of large numbers the gaps average out to ~33% each.

The trade-off with virtual nodes is memory and lookup time. A ring with 3 physical nodes × 150 virtual nodes has 450 ring entries that must be kept sorted. Adding a new node requires inserting 150 new entries. The sorted insertion (using `bisect.insort`) is O(log N) per entry, and a lookup is a binary search O(log(total_virtual_nodes)). For typical deployments (10-100 physical nodes × 150-250 vnodes) this is negligible compared to the network round-trip.

Consistent hashing appears throughout distributed systems under different names and with minor variations. Cassandra calls the ring positions "tokens" and assigns each node a set of token ranges. Amazon DynamoDB uses consistent hashing internally to route requests without a central directory. Redis Cluster uses a slightly different approach — 16,384 fixed "hash slots" pre-assigned to masters — but the core principle is the same: a key maps to a slot, and the slot maps to a node, so adding/removing a node only reassigns a subset of slots.

## How It Works

**Ring construction:** hash the node identifier (e.g., `"node-A:0"`, `"node-A:1"`, ..., `"node-A:149"`) to get a 128-bit integer. Place that integer as a point on a number line from 0 to 2¹²⁸. The line wraps around — the point after `2¹²⁸ - 1` is `0` — forming a ring. Keep the points in a sorted array.

**Key lookup:** hash the key to a 128-bit integer. Binary-search the sorted array to find the first ring point ≥ that hash. The physical server owning that ring point serves the key. If no point is ≥ the hash (key falls past the last point), wrap around to the first point — hence the modulo in `idx % len(sorted_keys)`.

```
  Hash ring (positions 0 .. 2^128, shown as 0..100 for clarity):

  0                                                          100
  |──A:0──B:3──A:7──C:12──B:18──A:24──C:31──B:37──C:44─── ...

  Key "foo" hashes to position 15 → walks clockwise → hits B:18 → served by node-B
  Key "bar" hashes to position 35 → walks clockwise → hits B:37 → served by node-B
  Key "baz" hashes to position 45 → walks clockwise → hits A:52 → served by node-A
```

**Adding a node:** insert its 150 virtual node positions into the sorted array. Each new position "intercepts" keys that used to continue to a further point. Only those keys change owners.

**Removing a node:** delete its 150 positions. Keys that were pointing to those positions now advance to the next point on the ring, which belongs to a neighboring node.

**Cassandra token ranges:** instead of virtual nodes with random positions, Cassandra (in manual token mode) assigns each node a specific range like `[25, 50)`. In `vnodes` mode (default since 1.2), each node owns 256 randomly-placed token ranges. Bootstrapping a new node generates 256 new tokens, distributing load from all existing nodes rather than just one neighbor.

**Redis Cluster hash slots:** Redis pre-divides the ring into exactly 16,384 slots. `slot = CRC16(key) % 16384`. Each master is assigned a contiguous range of slots (e.g., node-1 owns 0-5460, node-2 owns 5461-10922, node-3 owns 10923-16383). Clients cache the slot-to-node mapping and connect directly. If a client asks the wrong node, it receives a `MOVED` redirect.

### Trade-offs

| Approach | Remapping on resize | Load balance | Memory overhead | Lookup time |
|---|---|---|---|---|
| Naive modulo | ~(N-1)/N of all keys | Perfect | None | O(1) |
| Consistent ring, no vnodes | ~1/N of keys | Very poor (random gaps) | O(N) | O(log N) |
| Consistent ring, 150 vnodes | ~1/N of keys | Good (~2-5% std dev) | O(N × vnodes) | O(log(N × vnodes)) |
| Redis 16384 slots | ~1/N of slots | Operator-controlled | O(16384) slot map | O(1) slot lookup |

### Failure Modes

**Hot-spot from uneven virtual node placement:** with too few virtual nodes (e.g., replicas=1), one server can end up owning 50%+ of the ring by chance. Mitigation: use replicas ≥ 100. Monitor per-node key counts and rebalance if needed.

**Node removal cascades all keys to one neighbor:** when a node is removed, all its keys move to the single clockwise neighbor. If that neighbor was already at capacity, it may be overwhelmed. Mitigation: use replicas so keys are spread across many neighbors; in Cassandra, bootstrapping a replacement node before decommissioning the old one avoids this.

**Clock skew causes inconsistent ring views:** in distributed systems, two clients may briefly disagree on which nodes are in the ring (e.g., during a rolling upgrade). This can cause the same key to be routed to different servers, leading to cache misses or split-brain writes. Mitigation: use a consistent configuration store (ZooKeeper, etcd) as the source of truth for ring membership.

**Hash collision across virtual nodes:** two different virtual node labels hash to the same ring position. With MD5 (128 bits) and typical deployments (≤100,000 virtual nodes), the birthday-problem probability is negligible (~10⁻²⁷). Not a practical concern.

## Interview Talking Points

- "Without virtual nodes, a ring with 3 physical nodes has 3 points — distribution is controlled by chance, and one node can get 70% of keys. Virtual nodes fix this by giving each server 150+ positions on the ring."
- "Adding or removing a node only remaps the keys that were owned by that node's neighbors on the ring — roughly 1/N of total keys, not 75% like naive modulo."
- "Redis Cluster uses 16,384 hash slots (not a ring) — `CRC16(key) % 16384` — but the principle is the same: slots are assigned to masters, and adding a master means migrating a subset of slots."
- "Consistent hashing is used in CDN routing (same URL → same edge server → cache hit), distributed caches (Memcached client libraries), and distributed databases (Cassandra, DynamoDB)."
- "The number of virtual nodes (replicas) trades CPU and memory for distribution uniformity. 100-200 virtual nodes per physical node is a common default."
- "Cassandra's vnodes (default 256 per node) also make bootstrap faster: a new node takes a small range from many existing nodes, so the data transfer is parallelized across the cluster."

## Hands-on Lab

**Time:** ~5 minutes
**Services:** single Python container (no external services)

### Setup

```bash
cd system-design-interview/02-advanced/01-consistent-hashing/
docker compose up
# Or run directly (no dependencies):
python experiment.py
```

### Experiment

The script runs five phases automatically:

1. Builds a 3-node ring with 150 virtual nodes each, distributes 10,000 keys, and prints each node's share (expect ~33%).
2. Adds a 4th node and shows only ~25% of keys remapped, printing a before/after comparison for 10 sample keys.
3. Runs the same add-node scenario with naive modulo, showing ~75% of keys remapped.
4. Sweeps virtual node counts from 1 to 500, printing std deviation and min/max node load at each level.
5. Prints a summary comparison table.

### Break It

```bash
python -c "
import hashlib, bisect, statistics
from collections import defaultdict

# Ring with only 1 virtual node per server
class TinyRing:
    def __init__(self, nodes):
        self.ring = {}
        self.keys = []
        for n in nodes:
            h = int(hashlib.md5(n.encode()).hexdigest(), 16)
            self.ring[h] = n
            bisect.insort(self.keys, h)
    def get(self, k):
        h = int(hashlib.md5(k.encode()).hexdigest(), 16)
        idx = bisect.bisect(self.keys, h) % len(self.keys)
        return self.ring[self.keys[idx]]

import random, string
random.seed(42)
keys = [''.join(random.choices(string.ascii_lowercase, k=8)) for _ in range(10000)]
ring = TinyRing(['A','B','C'])
counts = defaultdict(int)
for k in keys:
    counts[ring.get(k)] += 1
for n,c in sorted(counts.items()):
    print(f'{n}: {c} keys ({c/100:.1f}%)')
print('Std dev:', round(statistics.stdev(counts.values()), 1))
print('Moral: 1 vnode per server = terrible balance')
"
```

### Observe

With `replicas=1`, one node may receive 50-60% of keys while another gets 10-20%. With `replicas=150`, the std deviation drops below 100 keys (~1% of ideal). The naive-modulo section shows 74-76% of keys remapping when adding a 4th server — compare to ~25% for consistent hashing.

### Teardown

```bash
docker compose down
```

## Real-World Examples

- **Amazon DynamoDB (2007):** the original Dynamo paper by DeCandia et al. described consistent hashing with virtual nodes as the core routing mechanism. Each node is assigned multiple tokens on the ring, and the preference list for a key spans the next N distinct physical nodes clockwise from the key's hash position. Source: DeCandia et al., "Dynamo: Amazon's Highly Available Key-value Store," SOSP 2007.
- **Apache Cassandra:** Cassandra uses consistent hashing with configurable vnodes (default 256 per node) for data distribution. The partitioner (Murmur3 by default) hashes row keys to 64-bit tokens. Adding a node triggers Cassandra's "streaming" process where the new node pulls its token ranges from existing nodes — because vnodes spread those ranges across the whole cluster, the load is distributed evenly during bootstrap. Source: Apache Cassandra documentation, "Consistent Hashing and Virtual Nodes."
- **Akamai CDN routing:** Akamai's original 1998 system (Leighton & Lewin, patent US6108703) used consistent hashing to assign web objects to cache servers so that the same URL consistently routes to the same edge server, maximising cache hit rates across thousands of servers. The same principle underlies most modern CDN request routing today.

## Common Mistakes

- **Using too few virtual nodes.** A ring with 1-5 virtual nodes per physical server has wildly uneven distribution. Always benchmark with your actual number of physical nodes and set virtual nodes to at least 100-150.
- **Forgetting to handle node failures in the ring membership.** If a node crashes and the application continues routing to it (because the ring hasn't been updated), requests fail. Ring membership must be kept in sync with cluster health — use a separate health-check loop to call `remove_node` on failure.
- **Assuming consistent hashing solves all data skew problems.** Consistent hashing distributes keys evenly only if key access frequency is also even. A "hot key" (one key accessed by millions of requests) always lands on the same node regardless of the ring — consistent hashing does not help. Solutions are application-level: key splitting, local caching, or read replicas.
- **Confusing Redis Cluster hash slots with a ring.** Redis does not use a circular ring; it uses a flat array of 16,384 slots. The important difference is that slot assignment is explicit and operator-controlled, not computed from node positions. Cluster rebalancing requires explicitly migrating slot ranges with `CLUSTER SETSLOT`.
- **Not accounting for data migration time when adding nodes.** Consistent hashing tells you which keys should move — it does not move them automatically. In a live system, there is a window after a node is added but before data migration completes where the new node serves stale or missing data. Cassandra handles this with hinted handoff and read repair.
