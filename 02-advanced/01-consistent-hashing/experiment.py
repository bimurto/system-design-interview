#!/usr/bin/env python3
"""
Consistent Hashing Lab — Pure Python, no external services required.

What this demonstrates:
  1. Hash ring with virtual nodes distributes keys ~evenly across nodes
  2. Adding a node only remaps ~25% of keys (not ~75% like naive modulo)
  3. Naive modulo hashing causes mass remapping when cluster size changes
  4. Virtual node count trades memory/CPU for distribution uniformity
  5. Standard deviation as a measure of load balance
"""

import hashlib
import bisect
import random
import statistics
import string
from collections import defaultdict


# ── ConsistentHashRing ─────────────────────────────────────────────────────

class ConsistentHashRing:
    def __init__(self, nodes=None, replicas=150):
        self.replicas = replicas        # virtual nodes per physical node
        self.ring = {}                  # hash position -> node name
        self.sorted_keys = []           # sorted list of hash positions
        for node in (nodes or []):
            self.add_node(node)

    def add_node(self, node):
        for i in range(self.replicas):
            key = self._hash(f"{node}:{i}")
            self.ring[key] = node
            bisect.insort(self.sorted_keys, key)

    def remove_node(self, node):
        for i in range(self.replicas):
            key = self._hash(f"{node}:{i}")
            del self.ring[key]
            self.sorted_keys.remove(key)

    def get_node(self, key):
        if not self.ring:
            return None
        h = self._hash(key)
        idx = bisect.bisect(self.sorted_keys, h) % len(self.sorted_keys)
        return self.ring[self.sorted_keys[idx]]

    def _hash(self, key):
        return int(hashlib.md5(key.encode()).hexdigest(), 16)


# ── Naive modulo hashing ───────────────────────────────────────────────────

def naive_get_node(key, nodes):
    """Simple modulo hashing — not consistent."""
    idx = int(hashlib.md5(key.encode()).hexdigest(), 16) % len(nodes)
    return nodes[idx]


# ── Helpers ────────────────────────────────────────────────────────────────

def section(title):
    print(f"\n{'=' * 60}")
    print(f"  {title}")
    print("=" * 60)


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


# ── Main ───────────────────────────────────────────────────────────────────

def main():
    section("CONSISTENT HASHING LAB")
    print("""
  Visual: 3-node ring with virtual nodes (replicas=150 each)

       node-A (150 virtual points)
      /
  ---●---●---●---●---●---●---   ← hash ring (0 .. 2^128)
          \\           /
         node-B     node-C
         (150 pts)  (150 pts)

  Each key hashes to a point on the ring, then walks clockwise
  to find the nearest virtual node → maps to that physical node.
""")

    ALL_KEYS = generate_keys(10_000)
    SAMPLE   = ALL_KEYS[:100]   # 100 keys used for before/after comparison

    # ── Phase 1: 3-node ring, distribute 10,000 keys ──────────────
    section("Phase 1: 3-Node Ring — Key Distribution")

    nodes_3 = ["node-A", "node-B", "node-C"]
    ring3   = ConsistentHashRing(nodes=nodes_3, replicas=150)

    mapping_3 = {k: ring3.get_node(k) for k in ALL_KEYS}
    counts_3, std_3 = distribution_stats(mapping_3)

    print(f"  10,000 keys distributed across 3 nodes (replicas=150):\n")
    for node, count in sorted(counts_3.items()):
        pct = count / len(ALL_KEYS) * 100
        bar = "#" * int(pct / 1)
        print(f"    {node}: {count:5d} keys ({pct:5.1f}%)  {bar}")
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
        bar = "#" * int(pct / 1)
        print(f"    {node}: {count:5d} keys ({pct:5.1f}%)  {bar}")
    print(f"\n  Ideal: 2,500 keys/node (25.0%)")
    print(f"  Std deviation: {std_4:.1f} keys")
    print(f"\n  Keys remapped: {remapped:,} / {len(ALL_KEYS):,} ({remap_pct:.1f}%)")
    print(f"  Expected ~25% (only keys from node-D's new neighbors move)")

    print(f"\n  Sample of 10 keys — before and after adding node-D:")
    print(f"  {'Key':<14}  {'Before':>8}  {'After':>8}  {'Moved?':>7}")
    print(f"  {'-'*14}  {'-'*8}  {'-'*8}  {'-'*7}")
    for k in SAMPLE[:10]:
        before = mapping_3[k]
        after  = mapping_4[k]
        moved  = "YES" if before != after else "no"
        print(f"  {k:<14}  {before:>8}  {after:>8}  {moved:>7}")

    # ── Phase 3: Naive modulo — show mass remapping ────────────────
    section("Phase 3: Naive Modulo Hashing — The Problem")

    naive_3 = {k: naive_get_node(k, nodes_3) for k in ALL_KEYS}
    naive_4 = {k: naive_get_node(k, nodes_4) for k in ALL_KEYS}

    naive_remapped = sum(1 for k in ALL_KEYS if naive_3[k] != naive_4[k])
    naive_remap_pct = naive_remapped / len(ALL_KEYS) * 100

    print(f"""
  Naive modulo: node = hash(key) % num_nodes

  When num_nodes changes from 3 → 4, almost every key maps
  to a different node because the divisor changed.

  Naive modulo remapped: {naive_remapped:,} / {len(ALL_KEYS):,} ({naive_remap_pct:.1f}%)
  Consistent hashing:    {remapped:,} / {len(ALL_KEYS):,} ({remap_pct:.1f}%)

  Sample of 10 keys — naive modulo before/after:
  {'Key':<14}  {'Before':>8}  {'After':>8}  {'Moved?':>7}""")
    print(f"  {'-'*14}  {'-'*8}  {'-'*8}  {'-'*7}")
    for k in SAMPLE[:10]:
        before = naive_3[k]
        after  = naive_4[k]
        moved  = "YES" if before != after else "no"
        print(f"  {k:<14}  {before:>8}  {after:>8}  {moved:>7}")

    print(f"""
  With naive modulo, scaling from N to N+1 nodes causes ~(N/(N+1))
  of all keys to move — typically 75%+ of all data must be
  re-fetched or migrated. This is catastrophic for caches and shards.
""")

    # ── Phase 4: Virtual nodes — balance vs replicas count ─────────
    section("Phase 4: Virtual Nodes — Balance vs. Replica Count")

    print(f"  {'Replicas':>10}  {'Std Dev':>10}  {'Min%':>7}  {'Max%':>7}  Balance")
    print(f"  {'-'*10}  {'-'*10}  {'-'*7}  {'-'*7}  {'-'*20}")

    for replicas in [1, 5, 10, 50, 150, 500]:
        r = ConsistentHashRing(nodes=nodes_3, replicas=replicas)
        m = {k: r.get_node(k) for k in ALL_KEYS}
        counts, std = distribution_stats(m)
        vals  = list(counts.values())
        mn    = min(vals) / len(ALL_KEYS) * 100
        mx    = max(vals) / len(ALL_KEYS) * 100
        stars = "#" * max(1, int(40 - std / 30))
        print(f"  {replicas:>10}  {std:>10.1f}  {mn:>6.1f}%  {mx:>6.1f}%  {stars}")

    print(f"""
  Low replicas (1-5): wildly uneven — some nodes get 60%+ of load
  High replicas (150+): near-ideal 33.3% each, at cost of more memory
  Rule of thumb: 100-200 virtual nodes per physical node
""")

    # ── Phase 5: Summary comparison ────────────────────────────────
    section("Phase 5: Summary — Consistent Hashing vs Modulo")

    print(f"""
  Approach             Keys Remapped (3→4 nodes)   Std Dev (3 nodes)
  ─────────────────────────────────────────────────────────────────
  Naive modulo         {naive_remap_pct:>5.1f}%                     N/A (always even)
  Consistent (r=1)     varies (1-3 virtual pts)    very high
  Consistent (r=150)   {remap_pct:>5.1f}%                     {std_3:.1f} keys

  Key insight:
  • Consistent hashing: only the new node's neighbors on the ring
    need to shed keys. Everyone else is unaffected.
  • Modulo hashing: changing the modulus reshuffles nearly everything.
  • Virtual nodes: more replicas → better balance at the cost of
    O(replicas * nodes) memory for the ring index.

  Real-world usage:
  • Cassandra: each node owns token ranges on a ring (virtual nodes
    since Cassandra 1.2, default vnodes=256)
  • Redis Cluster: 16,384 hash slots assigned to masters — same
    principle but with a fixed slot count instead of a ring
  • Amazon DynamoDB: consistent hashing routes requests to storage
    nodes without a central directory
  • CDN routing: requests are consistently routed to the same edge
    server for a given URL, maximising cache reuse

  Next: ../02-distributed-transactions/
""")


if __name__ == "__main__":
    main()
