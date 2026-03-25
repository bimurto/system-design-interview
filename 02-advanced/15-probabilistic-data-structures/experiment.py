#!/usr/bin/env python3
"""
Probabilistic Data Structures Lab

Phase 1: Bloom Filter — manual Python implementation + Redis BF module
Phase 2: HyperLogLog — Redis PFADD/PFCOUNT vs exact set
Phase 3: Count-Min Sketch — manual Python implementation
Phase 4: Top-K — Redis TOPK.ADD/TOPK.LIST
"""

import os
import sys
import math
import random
import string
import time
from collections import defaultdict

import mmh3
import redis

REDIS_HOST = os.environ.get("REDIS_HOST", "localhost")
r = redis.Redis(host=REDIS_HOST, port=6379, decode_responses=True)


def section(title):
    print(f"\n{'=' * 65}")
    print(f"  {title}")
    print("=" * 65)


# ── Phase 1: Bloom Filter ─────────────────────────────────────────────────

class BloomFilter:
    """
    Probabilistic membership test.
    False positives possible; false negatives impossible.
    """
    def __init__(self, capacity, error_rate):
        self.capacity   = capacity
        self.error_rate = error_rate
        self.m          = self._optimal_size(capacity, error_rate)  # bits
        self.k          = self._optimal_hash_count(self.m, capacity)
        self._bytes     = bytearray((self.m + 7) // 8)  # ceiling division

    def _set_bit(self, i):
        self._bytes[i // 8] |= (1 << (i % 8))

    def _get_bit(self, i):
        return bool(self._bytes[i // 8] & (1 << (i % 8)))

    def add(self, item):
        for seed in range(self.k):
            idx = mmh3.hash(item, seed) % self.m
            self._set_bit(idx)

    def __contains__(self, item):
        return all(
            self._get_bit(mmh3.hash(item, seed) % self.m)
            for seed in range(self.k)
        )

    def _optimal_size(self, n, p):
        """m = -n * ln(p) / (ln(2)^2)"""
        return int(-n * math.log(p) / (math.log(2) ** 2))

    def _optimal_hash_count(self, m, n):
        """k = (m/n) * ln(2)"""
        return max(1, int((m / n) * math.log(2)))

    @property
    def memory_bytes(self):
        return len(self._bytes)

    def false_positive_rate(self, n_inserted):
        """Actual FPR = (1 - e^(-k*n/m))^k"""
        k = self.k
        m = self.m
        n = n_inserted
        return (1 - math.exp(-k * n / m)) ** k


def phase1_bloom_filter():
    section("Phase 1: Bloom Filter")
    print("""
  Bloom filter: space-efficient probabilistic set membership test.
  Answer: "definitely not in set" or "probably in set"
  False positives: YES (small %)    False negatives: NEVER

  Math: m = -n*ln(p) / ln(2)²    k = (m/n)*ln(2)
    n = capacity, p = false positive rate, m = bit array size, k = hash functions
""")

    CAPACITY   = 100_000
    ERROR_RATE = 0.01  # 1% FPR target

    bf = BloomFilter(CAPACITY, ERROR_RATE)
    print(f"  Bloom filter config:")
    print(f"    Capacity:      {CAPACITY:,} items")
    print(f"    Target FPR:    {ERROR_RATE*100:.1f}%")
    print(f"    Bit array:     {bf.m:,} bits = {bf.m // 8 / 1024:.1f} KB")
    print(f"    Hash functions: {bf.k}")
    print(f"    Memory:        {bf.memory_bytes / 1024:.1f} KB")

    # Add 100K items
    print(f"\n  Adding {CAPACITY:,} items...")
    items = [f"user:{i}" for i in range(CAPACITY)]
    for item in items:
        bf.add(item)

    # Test 10K items NOT in the set → count false positives
    false_positives = 0
    TEST_COUNT = 10_000
    for i in range(CAPACITY, CAPACITY + TEST_COUNT):
        if f"user:{i}" in bf:
            false_positives += 1

    actual_fpr = false_positives / TEST_COUNT
    theoretical_fpr = bf.false_positive_rate(CAPACITY)

    print(f"\n  Results (tested {TEST_COUNT:,} non-members):")
    print(f"    False positives: {false_positives} / {TEST_COUNT:,}")
    print(f"    Actual FPR:      {actual_fpr*100:.2f}%")
    print(f"    Theoretical FPR: {theoretical_fpr*100:.2f}%")

    # Memory comparison
    set_memory_approx = CAPACITY * 20  # ~20 bytes per string in a Python set
    print(f"\n  Memory comparison for {CAPACITY:,} items:")
    print(f"    Python set:    ~{set_memory_approx / 1024 / 1024:.1f} MB (exact, no false positives)")
    bloom_bytes = len(bf._bytes)
    print(f"    Bloom filter:  {bloom_bytes / 1024:.1f} KB ({set_memory_approx / bloom_bytes:.0f}x smaller)")

    # Redis BF module
    print(f"\n  Redis Bloom Filter (BF module):")
    r.delete("bf:users")
    try:
        r.execute_command("BF.RESERVE", "bf:users", ERROR_RATE, CAPACITY)
    except Exception:
        pass  # already exists

    # Add 1000 users to Redis BF
    pipe = r.pipeline()
    for i in range(1000):
        pipe.execute_command("BF.ADD", "bf:users", f"user:{i}")
    pipe.execute()

    # Check membership
    exists_yes = r.execute_command("BF.EXISTS", "bf:users", "user:500")
    exists_no  = r.execute_command("BF.EXISTS", "bf:users", "user:99999")
    print(f"    BF.EXISTS user:500   → {exists_yes} (should be 1)")
    print(f"    BF.EXISTS user:99999 → {exists_no}  (probably 0, may be 1)")

    # Info
    info = r.execute_command("BF.INFO", "bf:users")
    info_dict = dict(zip(info[::2], info[1::2]))
    print(f"    Filter size (bits): {info_dict.get('Size', 'N/A')}")
    print(f"    Items inserted:     {info_dict.get('Number of items inserted', 'N/A')}")

    print(f"""
  Real-world uses:
    Cassandra: Bloom filter per SSTable — check if SSTable may contain a key
               before doing an expensive disk read. Reduces I/O for missing keys.
    Chrome:    Bloom filter for malicious URL database (~250MB → ~9MB)
    PostgreSQL: query optimiser uses bloom filters for index-skip scans
""")


# ── Phase 2: HyperLogLog ──────────────────────────────────────────────────

def phase2_hyperloglog():
    section("Phase 2: HyperLogLog — Unique Count Approximation")
    print("""
  HyperLogLog estimates cardinality (count of distinct items) using
  sub-linear space. Uses the harmonic mean of the positions of the
  leading zeros in hash values to estimate cardinality.

  Memory: 12 KB (Redis HLL with 16,384 registers) regardless of cardinality.
  Accuracy: ±0.81% standard error.
""")

    r.delete("hll:users")

    TOTAL = 100_000
    print(f"  Adding {TOTAL:,} unique user IDs to HyperLogLog...")
    batch_size = 500
    pipe = r.pipeline()
    for i in range(0, TOTAL, batch_size):
        pipe.execute_command("PFADD", "hll:users", *[f"uid_{j}" for j in range(i, min(i + batch_size, TOTAL))])
    pipe.execute()

    hll_count = r.execute_command("PFCOUNT", "hll:users")
    error_pct = abs(hll_count - TOTAL) / TOTAL * 100

    print(f"\n  Exact count:  {TOTAL:,}")
    print(f"  HLL estimate: {hll_count:,}")
    print(f"  Error:        {error_pct:.2f}% (target ±0.81%)")

    # Add 50K duplicates — count should barely change
    print(f"\n  Adding {50_000:,} DUPLICATE IDs (same IDs again)...")
    pipe2 = r.pipeline()
    for i in range(0, 50_000, batch_size):
        pipe2.execute_command("PFADD", "hll:users", *[f"uid_{j}" for j in range(i, min(i + batch_size, 50_000))])
    pipe2.execute()

    hll_after = r.execute_command("PFCOUNT", "hll:users")
    print(f"  HLL estimate after adding duplicates: {hll_after:,}  (should be ~same)")

    # Memory comparison
    hll_mem = 12 * 1024  # Redis HLL is ~12KB
    set_mem = TOTAL * 20
    print(f"\n  Memory comparison for {TOTAL:,} unique items:")
    print(f"    Exact set:    ~{set_mem / 1024 / 1024:.1f} MB")
    print(f"    HyperLogLog:  ~12 KB ({set_mem // hll_mem}x smaller)")

    print(f"""
  Real-world uses:
    Redis PFCOUNT for unique visitor counting (replaces SET cardinality)
    Analytics: count distinct users, sessions, IP addresses per day
    Databases: PostgreSQL uses HLL for query planner cardinality estimates
    Google BigQuery: COUNT(DISTINCT) uses HLL internally
""")


# ── Phase 3: Count-Min Sketch ─────────────────────────────────────────────

class CountMinSketch:
    """
    Frequency estimation for a stream of events.
    Over-estimates (never under-estimates) frequencies.
    Memory: O(w * d) — independent of cardinality.
    """
    def __init__(self, width, depth):
        self.width  = width
        self.depth  = depth
        self.table  = [[0] * width for _ in range(depth)]

    def add(self, item, count=1):
        for i in range(self.depth):
            col = mmh3.hash(item, i) % self.width
            self.table[i][col] += count

    def query(self, item):
        return min(
            self.table[i][mmh3.hash(item, i) % self.width]
            for i in range(self.depth)
        )

    @property
    def memory_bytes(self):
        return self.width * self.depth * 4  # 4 bytes per int


def phase3_count_min_sketch():
    section("Phase 3: Count-Min Sketch — Frequency Estimation")
    print("""
  Count-Min Sketch estimates item frequency in a stream.
  Uses w*d counter array (w=width, d=depth/hash functions).
  Result: always ≥ true frequency (over-estimates due to hash collisions).

  Error bound: true_count ≤ estimate ≤ true_count + ε*N
    where ε = e/w and δ = e^(-d) is the failure probability
""")

    # w=2000, d=5: ε=0.00136, δ=0.0067 (99.3% within 0.14% of total)
    cms = CountMinSketch(width=2000, depth=5)

    # Simulate 100K events: some URLs are very popular
    N = 100_000
    urls = [f"page_{i % 500}" for i in range(N)]  # 500 unique URLs
    # Make top 10 URLs much more popular
    popular = [f"page_{i}" for i in range(10)]
    urls += popular * 5000  # popular pages appear ~5000 extra times

    random.shuffle(urls)

    # True counts
    true_counts = defaultdict(int)
    for u in urls:
        true_counts[u] += 1

    # Add to CMS
    print(f"  Processing {len(urls):,} events...")
    for u in urls:
        cms.add(u)

    # Compare estimated vs true for top pages
    print(f"\n  {'URL':<12} {'True Count':>12} {'CMS Estimate':>14} {'Error %':>9}")
    print(f"  {'─'*12} {'─'*12} {'─'*14} {'─'*9}")
    for url in sorted(popular + ["page_100", "page_200"],
                      key=lambda x: -true_counts[x]):
        true = true_counts[url]
        est  = cms.query(url)
        err  = (est - true) / true * 100 if true > 0 else 0
        print(f"  {url:<12} {true:>12,} {est:>14,} {err:>+8.1f}%")

    exact_mem = len(true_counts) * 20  # dict overhead
    print(f"\n  Memory: exact dict={exact_mem/1024:.1f}KB  CMS={cms.memory_bytes/1024:.1f}KB")
    print(f"  CMS always over-estimates (never under-estimates) due to hash collisions.")

    print(f"""
  Real-world uses:
    Network switches: count per-flow packet frequencies for traffic analysis
    Ad fraud detection: frequency of IP address in ad impression stream
    Rate limiting: approximate per-user request counts
    Databases: query-level access frequency for buffer cache management
""")


# ── Phase 4: Top-K ────────────────────────────────────────────────────────

def phase4_topk():
    section("Phase 4: Top-K — Most Frequent Items (Redis)")
    print("""
  Top-K tracks the K most frequent items in a stream using a
  probabilistic sketch (similar to Count-Min Sketch internally).
  Much more memory-efficient than sorting all items.

  Redis TOPK module: TOPK.RESERVE, TOPK.ADD, TOPK.LIST
""")

    r.delete("topk:pages")
    K = 10

    try:
        r.execute_command("TOPK.RESERVE", "topk:pages", K, 50, 5, 0.9)
    except Exception:
        pass

    # Simulate 100K page views with power-law distribution
    N = 100_000
    pages = [f"page_{random.randint(1, 1000)}" for _ in range(N)]
    # Boost top 10 pages significantly
    top_pages = [f"page_{i}" for i in range(1, 11)]
    pages += top_pages * 1000

    print(f"  Processing {len(pages):,} page view events...")

    # Add in batches
    batch = 200
    true_counts = defaultdict(int)
    for i in range(0, len(pages), batch):
        chunk = pages[i:i+batch]
        r.execute_command("TOPK.ADD", "topk:pages", *chunk)
        for p in chunk:
            true_counts[p] += 1

    top_k_result = r.execute_command("TOPK.LIST", "topk:pages")
    print(f"\n  Top-{K} most frequent pages:")
    print(f"  {'Rank':<6} {'Page':<12} {'True Count':>12}")
    print(f"  {'─'*6} {'─'*12} {'─'*12}")
    for rank, page in enumerate(top_k_result, 1):
        if page:
            print(f"  {rank:<6} {page:<12} {true_counts.get(page, 0):>12,}")

    print(f"""
  Memory comparison:
    Exact sort:  ~{len(true_counts) * 30 // 1024} KB (dict of {len(true_counts)} unique items)
    Top-K sketch: ~{K * 4 // 1024 + 1} KB (only K items tracked)

  Real-world uses:
    Finding trending hashtags (Twitter/X)
    Top products viewed (e-commerce analytics)
    Heavy hitter detection (network monitoring)
    Most queried keys in a cache (for pre-warming)
""")


# ── Summary ───────────────────────────────────────────────────────────────

def phase5_summary():
    section("Phase 5: Comparison Summary")
    print(f"""
  {'Structure':<22} {'Question':<35} {'Memory':<20} {'Error'}
  {'─'*22} {'─'*35} {'─'*20} {'─'*15}
  {'Bloom Filter':<22} {'Is item in set?':<35} {'O(n)→sub-linear':<20} {'FP only'}
  {'HyperLogLog':<22} {'How many distinct items?':<35} {'O(1) ~12KB':<20} {'±0.81%'}
  {'Count-Min Sketch':<22} {'How often does item appear?':<35} {'O(w×d)':<20} {'Over-estimate'}
  {'Top-K':<22} {'What are the K most frequent?':<35} {'O(K)':<20} {'Approx rank'}
  {'MinHash':<22} {'How similar are two sets?':<35} {'O(bands)':<20} {'Tunable'}

  Key insight: trade exact answers for dramatically less memory.
  Acceptable for analytics, monitoring, recommendations.
  NOT acceptable for financial counts, exact billing, legal compliance.

  Bloom filter false positive formula:
    FPR = (1 - e^(-kn/m))^k
    k = hash functions, n = items inserted, m = bit array size

  HyperLogLog harmonic mean:
    Uses leading zeros in hash values as a proxy for cardinality.
    m registers, each tracking the max leading zeros seen.
    Harmonic mean of 2^(max_zeros) across registers = cardinality estimate.

  Next: ../../03-case-studies/01-url-shortener/
""")


def main():
    section("PROBABILISTIC DATA STRUCTURES LAB")
    print("""
  These data structures answer approximate questions using
  dramatically less memory than exact approaches.

  All four are backed by Redis Stack (has BF, HLL, TopK modules built in).
  Bloom filter and Count-Min Sketch also have manual Python implementations
  so you can see the underlying algorithm.
""")

    phase1_bloom_filter()
    phase2_hyperloglog()
    phase3_count_min_sketch()
    phase4_topk()
    phase5_summary()


if __name__ == "__main__":
    main()
