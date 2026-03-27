#!/usr/bin/env python3
"""
Consistent Hashing Lab — Pure Python, no external services required.

What this demonstrates:
  1. Hash ring with virtual nodes distributes keys ~evenly across nodes
  2. Adding a node only remaps ~25% of keys (not ~75% like naive modulo)
  3. Removing a node only remaps ~33% of keys to the clockwise neighbor
  4. Naive modulo hashing causes mass remapping when cluster size changes
  5. Virtual node count trades memory/CPU for distribution uniformity
  6. Replication: returning N distinct physical nodes for fault tolerance
  7. Weighted vnodes: heterogeneous hardware gets proportional ring share
"""

import hashlib
import bisect
import random
import statistics
import string
from collections import defaultdict
from typing import List, Optional


# ── ConsistentHashRing ─────────────────────────────────────────────────────

class ConsistentHashRing:
    """
    Hash ring using MD5 for position assignment.

    Implementation notes:
      - _hash() returns a 128-bit integer (full MD5 digest as hex → int)
      - sorted_keys is kept sorted via bisect.insort — O(log N) insert
      - remove_node uses bisect.bisect_left for O(log N) position lookup
        followed by an O(1) del — avoids the O(N) list.remove() pitfall
      - get_node wraps around via modulo when key hash > last ring position
    """

    def __init__(self, nodes=None, replicas=150, weights=None):
        """
        Args:
            nodes:    iterable of node name strings
            replicas: virtual nodes per physical node (default 150)
            weights:  dict {node_name: weight} where weight is a multiplier
                      relative to 1.0. A node with weight=2.0 gets 2x the
                      virtual nodes and thus ~2x the key share.
                      If None, all nodes are treated as weight=1.0.
        """
        self.replicas = replicas
        self.weights = weights or {}
        self.ring = {}           # hash position -> node name
        self.sorted_keys = []    # sorted list of hash positions
        for node in (nodes or []):
            self.add_node(node)

    def _vnode_count(self, node):
        """Return effective virtual node count for this node."""
        w = self.weights.get(node, 1.0)
        return max(1, int(self.replicas * w))

    def add_node(self, node):
        count = self._vnode_count(node)
        for i in range(count):
            pos = self._hash(f"{node}:{i}")
            self.ring[pos] = node
            bisect.insort(self.sorted_keys, pos)

    def remove_node(self, node):
        """
        Remove a node from the ring.

        Uses bisect_left for O(log N) position search rather than
        list.remove() which is O(N) — important at scale (10k+ vnodes).
        """
        count = self._vnode_count(node)
        for i in range(count):
            pos = self._hash(f"{node}:{i}")
            del self.ring[pos]
            idx = bisect.bisect_left(self.sorted_keys, pos)
            del self.sorted_keys[idx]

    def get_node(self, key) -> Optional[str]:
        """Return the single responsible node for key (clockwise lookup)."""
        if not self.ring:
            return None
        h = self._hash(key)
        idx = bisect.bisect(self.sorted_keys, h) % len(self.sorted_keys)
        return self.ring[self.sorted_keys[idx]]

    def get_nodes(self, key, n: int) -> List[str]:
        """
        Return up to n *distinct physical* nodes responsible for key.

        This is how real systems (Cassandra, Dynamo) implement replication:
        walk clockwise from the key's position and collect n unique physical
        nodes. Virtual nodes for the same physical server are skipped.

        Returns fewer than n nodes if the ring has fewer than n physical nodes.
        """
        if not self.ring:
            return []
        h = self._hash(key)
        start = bisect.bisect(self.sorted_keys, h) % len(self.sorted_keys)
        seen = set()
        result = []
        for offset in range(len(self.sorted_keys)):
            idx = (start + offset) % len(self.sorted_keys)
            node = self.ring[self.sorted_keys[idx]]
            if node not in seen:
                seen.add(node)
                result.append(node)
                if len(result) == n:
                    break
        return result

    def _hash(self, key):
        return int(hashlib.md5(key.encode()).hexdigest(), 16)


# ── Naive modulo hashing ───────────────────────────────────────────────────

def naive_get_node(key, nodes):
    """Simple modulo hashing — not consistent."""
    idx = int(hashlib.md5(key.encode()).hexdigest(), 16) % len(nodes)
    return nodes[idx]


# ── Helpers ────────────────────────────────────────────────────────────────

def section(title):
    print(f"\n{'=' * 64}")
    print(f"  {title}")
    print("=" * 64)


def generate_keys(n=10_000):
    random.seed(42)
    return [
        "".join(random.choices(string.ascii_lowercase + string.digits, k=12))
        for _ in range(n)
    ]


def distribution_stats(mapping):
    """Return count-per-node dict and std deviation of counts."""
    counts = defaultdict(int)
    for node in mapping.values():
        counts[node] += 1
    std = statistics.stdev(counts.values()) if len(counts) > 1 else 0
    return dict(counts), std


def bar(pct, width=40):
    filled = int(pct / 100 * width)
    return "#" * filled + "." * (width - filled)


# ── Main ───────────────────────────────────────────────────────────────────

def main():
    section("CONSISTENT HASHING LAB")
    print("""
  Visual: 3-node ring with virtual nodes (replicas=150 each)

  Each ● is a virtual node position on the 128-bit hash space.
  Physical nodes A, B, C each occupy ~150 of these positions.

  0                                                       2^128
  ·──●──●──●──●──●──●──●──●──●──●──●──●──●──●──●──●──●──·
     A  C  B  A  C  A  B  C  A  B  C  B  A  C  B  A  C   (wraps)

  Key "foo" hashes to position X → walk clockwise → nearest ● → owner.

  Adding node D inserts 150 new ●s.  Only keys between each new ●
  and its clockwise predecessor change owner.  All others stay put.
""")

    ALL_KEYS = generate_keys(10_000)
    SAMPLE   = ALL_KEYS[:10]   # 10 keys used for before/after comparison

    # ── Phase 1: 3-node ring, distribute 10,000 keys ──────────────
    section("Phase 1: 3-Node Ring — Key Distribution")

    nodes_3 = ["node-A", "node-B", "node-C"]
    ring3   = ConsistentHashRing(nodes=nodes_3, replicas=150)

    mapping_3 = {k: ring3.get_node(k) for k in ALL_KEYS}
    counts_3, std_3 = distribution_stats(mapping_3)

    print(f"  10,000 keys distributed across 3 nodes (replicas=150):\n")
    for node, count in sorted(counts_3.items()):
        pct = count / len(ALL_KEYS) * 100
        print(f"    {node}: {count:5d} keys ({pct:5.1f}%)  [{bar(pct)}]")
    print(f"\n  Ideal: 3,333 keys/node (33.3%)")
    print(f"  Std deviation: {std_3:.1f} keys  (lower = more balanced)")

    # ── Phase 2: Add 4th node — measure key remapping ─────────────
    section("Phase 2: Adding a 4th Node — Consistent Hashing Remapping")

    ring4 = ConsistentHashRing(nodes=nodes_3, replicas=150)
    ring4.add_node("node-D")
    nodes_4 = nodes_3 + ["node-D"]

    mapping_4 = {k: ring4.get_node(k) for k in ALL_KEYS}
    counts_4, std_4 = distribution_stats(mapping_4)

    remapped = sum(1 for k in ALL_KEYS if mapping_3[k] != mapping_4[k])
    remap_pct = remapped / len(ALL_KEYS) * 100

    print(f"  After adding node-D (4 nodes total):\n")
    for node, count in sorted(counts_4.items()):
        pct = count / len(ALL_KEYS) * 100
        print(f"    {node}: {count:5d} keys ({pct:5.1f}%)  [{bar(pct)}]")
    print(f"\n  Ideal: 2,500 keys/node (25.0%)")
    print(f"  Std deviation: {std_4:.1f} keys")
    print(f"\n  Keys remapped: {remapped:,} / {len(ALL_KEYS):,} ({remap_pct:.1f}%)")
    print(f"  Expected ~25% — only keys from node-D's new ring segments move.")
    print(f"  The other 75% of keys never changed owners.")

    print(f"\n  Sample of 10 keys — before and after adding node-D:")
    print(f"  {'Key':<14}  {'Before':>8}  {'After':>8}  {'Moved?':>7}")
    print(f"  {'-'*14}  {'-'*8}  {'-'*8}  {'-'*7}")
    for k in SAMPLE:
        before = mapping_3[k]
        after  = mapping_4[k]
        moved  = "YES <--" if before != after else "no"
        print(f"  {k:<14}  {before:>8}  {after:>8}  {moved}")

    # ── Phase 3: Remove a node — measure key remapping ────────────
    section("Phase 3: Removing a Node — Consistent Hashing Remapping")

    # Start from the 4-node ring, remove node-D
    ring4_minus_d = ConsistentHashRing(nodes=nodes_4, replicas=150)
    ring4_minus_d.remove_node("node-D")

    mapping_after_remove = {k: ring4_minus_d.get_node(k) for k in ALL_KEYS}
    counts_rm, std_rm = distribution_stats(mapping_after_remove)

    removed_keys = sum(1 for k in ALL_KEYS if mapping_4[k] != mapping_after_remove[k])
    removed_pct  = removed_keys / len(ALL_KEYS) * 100

    print(f"""
  Removing node-D from the 4-node ring (back to 3 nodes):

  Keys that were owned by node-D's virtual positions must move
  clockwise to the next physical node.  All other keys stay put.
""")
    for node, count in sorted(counts_rm.items()):
        pct = count / len(ALL_KEYS) * 100
        print(f"    {node}: {count:5d} keys ({pct:5.1f}%)  [{bar(pct)}]")
    print(f"\n  Keys remapped after removing node-D: {removed_keys:,} ({removed_pct:.1f}%)")
    print(f"  Expected ~25% (node-D's share).  The rest: 0 disruption.")
    print(f"""
  ⚠  Operational note: in a live database, node removal means those
     keys' data must be migrated to the new owners BEFORE the node
     goes dark.  Cassandra calls this "decommission" + streaming.
     If the node crashes without graceful removal, the data is gone
     (unless you have replicas — see Phase 6).
""")

    # ── Phase 4: Naive modulo — show mass remapping ────────────────
    section("Phase 4: Naive Modulo Hashing — The Problem")

    naive_3 = {k: naive_get_node(k, nodes_3) for k in ALL_KEYS}
    naive_4 = {k: naive_get_node(k, nodes_4) for k in ALL_KEYS}

    naive_remapped = sum(1 for k in ALL_KEYS if naive_3[k] != naive_4[k])
    naive_remap_pct = naive_remapped / len(ALL_KEYS) * 100

    print(f"""
  Naive modulo: node = hash(key) % num_nodes

  When num_nodes changes from 3 → 4, almost every key maps
  to a different node because the divisor shifts.

  Naive modulo remapped:  {naive_remapped:,} / {len(ALL_KEYS):,}  ({naive_remap_pct:.1f}%)
  Consistent hashing:      {remapped:,} / {len(ALL_KEYS):,}  ({remap_pct:.1f}%)

  For a distributed cache: {naive_remap_pct:.0f}% cache miss storm vs {remap_pct:.0f}%.
  For a distributed DB:    {naive_remap_pct:.0f}% of data must physically migrate.

  Sample of 10 keys — naive modulo before/after adding node-D:
  {'Key':<14}  {'Before':>8}  {'After':>8}  {'Moved?':>7}""")
    print(f"  {'-'*14}  {'-'*8}  {'-'*8}  {'-'*7}")
    for k in SAMPLE:
        before = naive_3[k]
        after  = naive_4[k]
        moved  = "YES <--" if before != after else "no"
        print(f"  {k:<14}  {before:>8}  {after:>8}  {moved}")

    print(f"""
  With naive modulo, scaling from N to N+1 nodes causes ~N/(N+1)
  of all keys to remap — typically 75%+ must be re-fetched or migrated.
  This is catastrophic for caches (thundering herd) and sharded DBs.
""")

    # ── Phase 5: Virtual nodes — balance vs replicas count ─────────
    section("Phase 5: Virtual Nodes — Balance vs. Replica Count")

    print(f"  {'Replicas':>10}  {'Std Dev':>10}  {'Min%':>7}  {'Max%':>7}  Distribution quality")
    print(f"  {'-'*10}  {'-'*10}  {'-'*7}  {'-'*7}  {'-'*30}")

    for replicas in [1, 5, 10, 50, 150, 500]:
        r = ConsistentHashRing(nodes=nodes_3, replicas=replicas)
        m = {k: r.get_node(k) for k in ALL_KEYS}
        counts, std = distribution_stats(m)
        vals  = list(counts.values())
        mn    = min(vals) / len(ALL_KEYS) * 100
        mx    = max(vals) / len(ALL_KEYS) * 100
        quality = "POOR   ← never use this" if replicas <= 5 else \
                  "fair   ← marginal"        if replicas <= 10 else \
                  "good"                      if replicas <= 50 else \
                  "excellent ← production range"
        print(f"  {replicas:>10}  {std:>10.1f}  {mn:>6.1f}%  {mx:>6.1f}%  {quality}")

    print(f"""
  Memory cost of the ring index:
    3 nodes × 150 replicas = 450 entries  (negligible)
    100 nodes × 200 replicas = 20,000 entries × ~50 bytes ≈ 1 MB
    Ring lookup is O(log(total_vnodes)) — binary search — fast.

  Rule of thumb: 100-200 virtual nodes per physical node.
  Cassandra default: 256 vnodes per node.
""")

    # ── Phase 6: Replication — get_nodes(key, n) ──────────────────
    section("Phase 6: Replication — N-Node Preference Lists")

    print(f"""
  Real systems store each key on N physical nodes (replication factor).
  Dynamo / Cassandra walk clockwise from the key's hash and collect
  N *distinct physical* nodes — this is the "preference list."

  Using ring3 (node-A, node-B, node-C) with RF=3 and RF=2:
""")
    sample_keys = ["user:1001", "order:9872", "session:abc", "cache:img42"]
    for k in sample_keys:
        rf3 = ring3.get_nodes(k, 3)
        rf2 = ring3.get_nodes(k, 2)
        print(f"    key={k!r:<20}  RF=3 → {rf3}   RF=2 → {rf2}")

    print(f"""
  With RF=3, every key is served by all 3 nodes — any single node
  failure is tolerable.  The coordinator (first node in the list)
  handles client requests and replicates to the others.

  ⚠  Consistent hashing tells you *which* nodes own a key.
     It does NOT handle:
       - Quorum writes/reads (W + R > N) — that's the replication protocol
       - Conflict resolution on concurrent writes — vector clocks / LWW
       - Hot keys — a single wildly popular key always hits the same nodes
""")

    # ── Phase 7: Weighted vnodes (heterogeneous hardware) ─────────
    section("Phase 7: Weighted Virtual Nodes — Heterogeneous Hardware")

    print(f"""
  Problem: not all servers are equal.  A node with 2× the RAM/CPU
  should absorb 2× the keys.  Weighted vnodes solve this by giving
  larger nodes proportionally more positions on the ring.

  Setup: node-A (weight=1.0, 150 vnodes), node-B (weight=2.0, 300 vnodes),
         node-C (weight=1.0, 150 vnodes)  → ideal shares: A≈25%, B≈50%, C≈25%
""")
    weighted_ring = ConsistentHashRing(
        nodes=["node-A", "node-B", "node-C"],
        replicas=150,
        weights={"node-A": 1.0, "node-B": 2.0, "node-C": 1.0},
    )
    wm = {k: weighted_ring.get_node(k) for k in ALL_KEYS}
    wcounts, wstd = distribution_stats(wm)
    for node, count in sorted(wcounts.items()):
        pct = count / len(ALL_KEYS) * 100
        w   = weighted_ring.weights.get(node, 1.0)
        print(f"    {node} (weight={w:.1f}): {count:5d} keys ({pct:5.1f}%)  [{bar(pct)}]")
    print(f"\n  node-B has 2× weight → absorbs ~50% of keys as intended.")
    print(f"  Cassandra achieves this with explicit token assignment.")

    # ── Phase 8: Summary comparison ────────────────────────────────
    section("Phase 8: Summary — Consistent Hashing vs Modulo")

    print(f"""
  Approach             Keys Remapped (3→4 nodes)   Std Dev (3 nodes, r=150)
  ──────────────────────────────────────────────────────────────────────────
  Naive modulo         {naive_remap_pct:>5.1f}%                     0 (always perfectly even)
  Consistent (r=1)     ~33% but uneven owners      very high (60%+ skew possible)
  Consistent (r=150)   {remap_pct:>5.1f}%                     {std_3:.1f} keys (~1% of ideal)

  Key insights:
  • Consistent hashing: only the new node's ring neighbors shed keys.
    Everyone else is completely unaffected.
  • Modulo hashing: changing the modulus reshuffles ~75% of everything.
  • Virtual nodes: more replicas → better balance, O(N×vnodes) ring size.
  • Replication: get_nodes(key, N) returns N distinct physical nodes,
    enabling fault tolerance without a central routing table.
  • Weighted vnodes: proportional ring share matches hardware capacity.

  Real-world usage:
  • Cassandra: Murmur3 partitioner, 256 vnodes/node, streaming on bootstrap
  • Redis Cluster: 16,384 fixed hash slots (not a ring, but same principle)
  • Amazon DynamoDB: consistent hashing with preference lists (Dynamo paper)
  • CDN routing: same URL → same edge server → cache reuse

  Gotchas interviewers probe:
  • "What happens when a node crashes mid-migration?" → data loss without RF>1
  • "How do you handle a hot key?" → consistent hashing doesn't help; use
    key splitting, read replicas, or client-side caching
  • "Why does Redis use 16,384 slots?" → small enough for a gossip-propagated
    bitmap (2 KB), large enough for ~1000 nodes with manageable granularity

  Next: ../02-distributed-transactions/
""")


if __name__ == "__main__":
    main()
