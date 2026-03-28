#!/usr/bin/env python3
"""
Probabilistic Data Structures Lab

Phase 1: Bloom Filter — manual Python implementation + Redis BF module
Phase 2: HyperLogLog — Redis PFADD/PFCOUNT + PFMERGE demo vs exact set
Phase 3: Count-Min Sketch — manual Python implementation
Phase 4: Top-K — Redis TOPK.ADD/TOPK.LIST
Phase 5: Bloom Filter Saturation — "break it" demo
Phase 6: MinHash — Jaccard similarity estimation
Phase 7: Comparison Summary
"""

import os
import sys
import math
import random
import time
from collections import defaultdict

import mmh3
import redis

# Fix seed for reproducible output across runs.
# NOTE: mmh3 with different integer seeds is NOT cryptographically independent —
# it is a single hash family with a seed parameter, not k truly independent hash
# functions. For production Bloom filters, use multiple independent hash algorithms
# (SHA-256 + MurmurHash + FNV) or the double-hashing construction
# (h_i(x) = h1(x) + i*h2(x)) to approximate independence. The seed approach used
# here is common in practice and sufficient for the FPR math, but be aware of the
# theoretical gap when explaining this in an interview.
random.seed(42)

REDIS_HOST = os.environ.get("REDIS_HOST", "localhost")

try:
    r = redis.Redis(host=REDIS_HOST, port=6379, decode_responses=True)
    r.ping()
except redis.exceptions.ConnectionError as e:
    print(f"ERROR: Cannot connect to Redis at {REDIS_HOST}:6379 — {e}")
    print("Run: docker compose up -d   then retry.")
    sys.exit(1)


def section(title):
    print(f"\n{'=' * 65}")
    print(f"  {title}")
    print("=" * 65)


# ── Phase 1: Bloom Filter ─────────────────────────────────────────────────

class BloomFilter:
    """
    Probabilistic membership test.
    False positives possible; false negatives impossible.

    Hash independence note: uses mmh3 with integer seeds. This is the
    double-hashing approximation (not k truly independent hash functions),
    which is standard in practice. The FPR math still holds empirically.
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

    def fill_ratio(self):
        """Fraction of bits set to 1. Approaches 1.0 as filter saturates."""
        return sum(bin(b).count('1') for b in self._bytes) / self.m

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

  Hash independence caveat: using mmh3 with integer seeds approximates
  independent hash functions (double-hashing construction). Production systems
  may combine multiple unrelated hash families for stronger independence.
""")

    CAPACITY   = 100_000
    ERROR_RATE = 0.01  # 1% FPR target

    bf = BloomFilter(CAPACITY, ERROR_RATE)
    print(f"  Bloom filter config:")
    print(f"    Capacity:       {CAPACITY:,} items")
    print(f"    Target FPR:     {ERROR_RATE*100:.1f}%")
    print(f"    Bit array:      {bf.m:,} bits = {bf.m // 8 / 1024:.1f} KB")
    print(f"    Hash functions: {bf.k}")
    print(f"    Memory:         {bf.memory_bytes / 1024:.1f} KB")

    # Add 100K items
    print(f"\n  Adding {CAPACITY:,} items...")
    items = [f"user:{i}" for i in range(CAPACITY)]
    for item in items:
        bf.add(item)

    fill = bf.fill_ratio()
    print(f"  Bit array fill ratio after {CAPACITY:,} insertions: {fill:.3f} "
          f"(optimal for 1% FPR ≈ 0.500)")

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

    # Verify the asymmetry guarantee: true members must NEVER be missed
    false_negatives = sum(1 for item in items if item not in bf)
    print(f"    False negatives: {false_negatives}  (MUST be 0 — no exceptions)")
    assert false_negatives == 0, "BUG: Bloom filter produced a false negative!"

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
    print(f"    BF.EXISTS user:500   → {exists_yes} (should be 1 — true member)")
    print(f"    BF.EXISTS user:99999 → {exists_no}  (probably 0 — not added; 1 = false positive)")

    # Info
    info = r.execute_command("BF.INFO", "bf:users")
    info_dict = dict(zip(info[::2], info[1::2]))
    print(f"    Filter size (bits): {info_dict.get('Size', 'N/A')}")
    print(f"    Items inserted:     {info_dict.get('Number of items inserted', 'N/A')}")

    print(f"""
  Real-world uses:
    Cassandra / RocksDB / LevelDB: Bloom filter per SSTable/SST file —
      skip disk reads for keys definitely not in that file. Reduces I/O
      for "key not found" by ~99% at 1% FPR. Critical for LSM-tree engines.
    Chrome:    Bloom filter for malicious URL database (~250MB → ~9MB)
    PostgreSQL: query optimiser uses bloom filters for index-skip scans
""")


# ── Phase 2: HyperLogLog ──────────────────────────────────────────────────

def phase2_hyperloglog():
    section("Phase 2: HyperLogLog — Unique Count Approximation + PFMERGE")
    print("""
  HyperLogLog estimates cardinality (count of distinct items) using
  sub-linear space. Uses the harmonic mean of the positions of the
  leading zeros in hash values to estimate cardinality.

  Memory: 12 KB (Redis HLL with 16,384 registers) regardless of cardinality.
  Accuracy: ±0.81% standard error.

  Key capability: HLLs are MERGEABLE — combine per-shard or per-hour HLLs
  to get cross-shard/cross-window distinct counts without raw data movement.
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
    print(f"  HLL estimate after adding duplicates: {hll_after:,}  (should be ~same as {hll_count:,})")

    # PFMERGE demo: simulate 3 shards with overlapping users
    print(f"\n  PFMERGE demo — 3 database shards, each tracks its own unique visitors:")
    r.delete("hll:shard0", "hll:shard1", "hll:shard2", "hll:merged")

    SHARD_SIZE = 30_000
    OVERLAP    = 5_000   # users appear in multiple shards

    # shard0: uid_0..29999
    # shard1: uid_25000..54999 (5000 overlap with shard0)
    # shard2: uid_50000..79999 (5000 overlap with shard1)
    true_union = len(set(range(30_000)) | set(range(25_000, 55_000)) | set(range(50_000, 80_000)))

    for shard_idx, (start, end) in enumerate([(0, 30_000), (25_000, 55_000), (50_000, 80_000)]):
        pipe = r.pipeline()
        for i in range(start, end, batch_size):
            pipe.execute_command("PFADD", f"hll:shard{shard_idx}",
                                 *[f"uid_{j}" for j in range(i, min(i + batch_size, end))])
        pipe.execute()
        count = r.execute_command("PFCOUNT", f"hll:shard{shard_idx}")
        print(f"    shard{shard_idx}: uid_{start}..uid_{end-1}  → PFCOUNT={count:,}  (true={end-start:,})")

    r.execute_command("PFMERGE", "hll:merged", "hll:shard0", "hll:shard1", "hll:shard2")
    merged_count = r.execute_command("PFCOUNT", "hll:merged")
    merge_error  = abs(merged_count - true_union) / true_union * 100
    print(f"\n    PFMERGE result: {merged_count:,}  (true union={true_union:,},  error={merge_error:.2f}%)")
    print(f"    Cross-shard distinct count achieved without transferring {true_union:,} raw IDs!")

    # PFMERGE sparse→dense memory note
    print(f"""
  PFMERGE memory note:
    Redis HLLs start in 'sparse' representation (a few hundred bytes) for
    low cardinalities and switch to 'dense' (12KB) once the register count
    is large enough. PFMERGE ALWAYS produces a dense output — even if both
    inputs are sparse. If you merge thousands of per-user HLLs hourly, you
    convert all of them from ~200 bytes to 12KB each. At 10K small HLLs,
    that is 200KB (sparse) vs 120MB (post-merge dense). Design merges as
    terminal aggregation operations, not intermediate pipeline steps.
""")

    # Memory comparison
    hll_mem = 12 * 1024  # Redis HLL is ~12KB
    set_mem = TOTAL * 20
    print(f"  Memory comparison for {TOTAL:,} unique items:")
    print(f"    Exact set:    ~{set_mem / 1024 / 1024:.1f} MB")
    print(f"    HyperLogLog:  ~12 KB ({set_mem // hll_mem}x smaller)")

    print(f"""
  Real-world uses:
    Redis PFCOUNT for unique visitor counting (replaces SET cardinality)
    Analytics: count distinct users, sessions, IP addresses per day
    PFMERGE: aggregate hourly HLLs into daily, or per-shard into global
    Google BigQuery: APPROX_COUNT_DISTINCT uses HLL++ (sparse + bias correction)
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
  Result: always >= true frequency (over-estimates due to hash collisions).

  Error bound: true_count <= estimate <= true_count + epsilon*N
    where epsilon = e/w and delta = e^(-d) is the failure probability

  Width sizing: for epsilon=1% relative error, width >= e/0.01 = 272
  Depth sizing: for delta=1% failure probability, depth >= ln(100) = 5
""")

    # Simulate 150K events: some URLs are very popular (power-law distribution)
    N = 100_000
    urls = [f"page_{i % 500}" for i in range(N)]  # 500 unique URLs, flat distribution
    popular = [f"page_{i}" for i in range(10)]
    urls += popular * 5000  # popular pages appear ~5000 extra times
    random.shuffle(urls)

    true_counts: dict[str, int] = defaultdict(int)
    for u in urls:
        true_counts[u] += 1

    total_events = len(urls)

    # Show effect of width on accuracy
    print(f"  Processing {total_events:,} events through CMS instances of different widths...")
    print(f"  (depth=5 fixed, varying width to show error bound effect)\n")

    print(f"  {'Width':>8}  {'epsilon':>10}  {'page_0 true':>13}  {'page_0 est':>12}  {'error %':>9}  {'mem KB':>8}")
    print(f"  {'─'*8}  {'─'*10}  {'─'*13}  {'─'*12}  {'─'*9}  {'─'*8}")

    for width in [50, 200, 2000]:
        cms = CountMinSketch(width=width, depth=5)
        for u in urls:
            cms.add(u)
        true = true_counts["page_0"]
        est  = cms.query("page_0")
        err  = (est - true) / true * 100
        eps  = math.e / width
        print(f"  {width:>8,}  {eps:>10.4f}  {true:>13,}  {est:>12,}  {err:>+8.1f}%  {cms.memory_bytes/1024:>7.1f}")

    print(f"\n  With width=2000, depth=5: epsilon={math.e/2000:.4f}, delta={math.exp(-5):.4f}")

    # Use well-sized CMS for the full comparison
    cms = CountMinSketch(width=2000, depth=5)
    for u in urls:
        cms.add(u)

    print(f"\n  {'URL':<12} {'True Count':>12} {'CMS Estimate':>14} {'Overcount':>10} {'Guarantee'}")
    print(f"  {'─'*12} {'─'*12} {'─'*14} {'─'*10} {'─'*15}")
    all_over = True
    for url in sorted(popular + ["page_100", "page_200"],
                      key=lambda x: -true_counts[x]):
        true = true_counts[url]
        est  = cms.query(url)
        over = est - true
        # CMS guarantee: estimate MUST be >= true count (over-estimate only)
        ok = "OK (>=true)" if over >= 0 else "VIOLATION!"
        if over < 0:
            all_over = False
        print(f"  {url:<12} {true:>12,} {est:>14,} {over:>+10,} {ok}")

    if all_over:
        print(f"\n  Guarantee confirmed: CMS never under-estimated any item.")
    else:
        print(f"\n  WARNING: CMS violated over-estimate guarantee (implementation bug).")

    exact_mem = len(true_counts) * 20
    print(f"\n  Memory: exact dict={exact_mem/1024:.1f} KB   CMS(2000x5)={cms.memory_bytes/1024:.1f} KB")
    print(f"  Note: CMS memory is fixed regardless of stream size or unique item count.")

    print(f"""
  Real-world uses:
    Network switches: count per-flow packet frequencies for traffic analysis
    Ad fraud detection: frequency of IP address in ad impression stream
    Rate limiting: approximate per-user request counts (always safe-to-reject
      because CMS only over-estimates — you may throttle slightly early but
      you will never under-count and accidentally allow excess traffic)
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
    true_top_10 = sorted(true_counts, key=lambda x: -true_counts[x])[:K]

    print(f"\n  Top-{K} most frequent pages (Redis TOPK vs exact sort):")
    print(f"  {'Rank':<6} {'TOPK Result':<14} {'True Count':>12}  {'In exact top-10?'}")
    print(f"  {'─'*6} {'─'*14} {'─'*12}  {'─'*16}")
    topk_set = set(p for p in top_k_result if p)
    for rank, page in enumerate(top_k_result, 1):
        if page:
            in_exact = "YES" if page in true_top_10 else "no (near boundary)"
            print(f"  {rank:<6} {page:<14} {true_counts.get(page, 0):>12,}  {in_exact}")

    overlap = len(topk_set & set(true_top_10))
    print(f"\n  Overlap with exact top-{K}: {overlap}/{K} items match")

    exact_mem_kb = len(true_counts) * 30 / 1024
    # Top-K internal sketch memory: width * depth * 4 bytes for the CMS counters
    # TOPK.RESERVE width=50, depth=5 by default for K=10 → 50*5*4 = 1000 bytes + K item storage
    topk_sketch_kb = (50 * 5 * 4 + K * 32) / 1024

    print(f"""
  Memory comparison:
    Exact sort:       ~{exact_mem_kb:.1f} KB (dict of {len(true_counts)} unique items + sort overhead)
    Top-K sketch:     ~{topk_sketch_kb:.1f} KB (internal CMS sketch + {K} item slots)
    Savings:          ~{exact_mem_kb / topk_sketch_kb:.0f}x — and Top-K handles unbounded streams

  Real-world uses:
    Finding trending hashtags (Twitter/X)
    Top products viewed (e-commerce analytics)
    Heavy hitter detection (network monitoring)
    Most queried keys in a cache (for pre-warming)
""")


# ── Phase 5: Bloom Filter Saturation ──────────────────────────────────────

def phase5_bloom_saturation():
    section("Phase 5: Bloom Filter Saturation — Break It Demo")
    print("""
  A Bloom filter tuned for N items degrades predictably when over-filled.
  As items exceed capacity, the bit array fills and FPR rises toward 100%.
  This demonstrates why monitoring insertion count vs designed capacity matters.
""")

    CAPACITY   = 500       # intentionally small to make saturation fast
    ERROR_RATE = 0.01      # 1% FPR target
    TEST_COUNT = 2_000     # non-members to test against

    print(f"  Filter designed for: {CAPACITY} items at {ERROR_RATE*100:.0f}% FPR")
    print(f"  We will add items at 1x, 2x, 5x, and 10x capacity and measure actual FPR.\n")

    print(f"  {'Items Added':>12}  {'vs Capacity':>13}  {'Fill Ratio':>11}  {'Theoretical FPR':>17}  {'Actual FPR':>12}  {'Assessment'}")
    print(f"  {'─'*12}  {'─'*13}  {'─'*11}  {'─'*17}  {'─'*12}  {'─'*20}")

    for multiplier in [1, 2, 5, 10]:
        n_insert = CAPACITY * multiplier
        bf = BloomFilter(CAPACITY, ERROR_RATE)

        for i in range(n_insert):
            bf.add(f"item_{i}")

        fill = bf.fill_ratio()

        # Test items never added
        fp = sum(1 for i in range(n_insert, n_insert + TEST_COUNT) if f"item_{i}" in bf)
        actual_fpr = fp / TEST_COUNT
        theoretical_fpr = bf.false_positive_rate(n_insert)

        if actual_fpr < 0.05:
            assessment = "healthy"
        elif actual_fpr < 0.30:
            assessment = "DEGRADED"
        else:
            assessment = "SATURATED (replace!)"

        print(f"  {n_insert:>12,}  {multiplier:>12}x  {fill:>10.3f}  {theoretical_fpr*100:>16.1f}%  {actual_fpr*100:>11.1f}%  {assessment}")

    print(f"""
  Fill ratio interpretation:
    0.500 = optimal operating point (maximum entropy, minimum FPR for capacity)
    0.800 = significantly degraded — FPR rises sharply
    1.000 = all bits set — every query returns "probably yes" (useless)

  Key takeaway:
    At 5x capacity the filter is largely useless (high FPR).
    At 10x it returns true for nearly everything — all bits set.
    Production mitigation: use Redis BF.RESERVE with expansion factor,
    or pre-provision at 2-3x expected peak, or use Scalable Bloom Filters (SBF).

  Monitor: track (items inserted / designed capacity) as a gauge metric.
    Alert at 80% fill. At 95% fill, start provisioning a replacement filter.
""")


# ── Phase 6: MinHash — Jaccard Similarity ─────────────────────────────────

class MinHash:
    """
    Estimates Jaccard similarity between sets using k minimum hash values.

    Insight: P(min_hash_i(A) == min_hash_i(B)) = |A ∩ B| / |A ∪ B|
    So the fraction of matching min-hashes directly estimates Jaccard similarity.

    Memory: O(k) per set — independent of set size.
    """
    def __init__(self, num_hashes=128):
        self.num_hashes = num_hashes
        # Initialize min values to infinity
        self.min_hashes = [float('inf')] * num_hashes

    def update(self, item):
        for seed in range(self.num_hashes):
            h = mmh3.hash(item, seed, signed=False)
            if h < self.min_hashes[seed]:
                self.min_hashes[seed] = h

    def jaccard(self, other):
        """Estimate Jaccard similarity by counting matching min-hashes."""
        matches = sum(
            1 for a, b in zip(self.min_hashes, other.min_hashes) if a == b
        )
        return matches / self.num_hashes


def phase6_minhash():
    section("Phase 6: MinHash — Jaccard Similarity Estimation")
    print("""
  MinHash estimates the Jaccard similarity between two sets.
  Jaccard(A, B) = |A ∩ B| / |A ∪ B|

  Insight: if you apply k hash functions and take the minimum from each,
  P(min_hash_i(A) == min_hash_i(B)) = Jaccard(A, B)
  So: fraction of matching min-hashes ≈ Jaccard similarity

  Memory: O(k) per set (e.g., 128 hashes × 4 bytes = 512 bytes)
  versus storing the full set: O(|set|)

  Use cases:
    Near-duplicate document detection (plagiarism, news clustering)
    Collaborative filtering (users with similar interaction histories)
    LSH (Locality-Sensitive Hashing): band MinHash signatures to find
      candidate pairs efficiently in O(1) per band rather than O(n²)
""")

    # Build sets with known Jaccard similarity
    base_set = set(f"word_{i}" for i in range(1000))

    test_cases = [
        ("90% overlap",  set(f"word_{i}" for i in range(900)) | set(f"unique_A_{i}" for i in range(100))),
        ("50% overlap",  set(f"word_{i}" for i in range(500)) | set(f"unique_B_{i}" for i in range(500))),
        ("10% overlap",  set(f"word_{i}" for i in range(100)) | set(f"unique_C_{i}" for i in range(900))),
        ("0% overlap",   set(f"completely_different_{i}" for i in range(1000))),
    ]

    print(f"  Base set: {len(base_set)} words\n")
    print(f"  {'Scenario':<16} {'True Jaccard':>14} {'MinHash (128)':>14} {'MinHash (32)':>13} {'Error (128)':>12}")
    print(f"  {'─'*16} {'─'*14} {'─'*14} {'─'*13} {'─'*12}")

    for label, other_set in test_cases:
        # True Jaccard
        true_j = len(base_set & other_set) / len(base_set | other_set)

        # MinHash with 128 hashes
        mh_base_128 = MinHash(num_hashes=128)
        mh_other_128 = MinHash(num_hashes=128)
        for w in base_set:
            mh_base_128.update(w)
        for w in other_set:
            mh_other_128.update(w)
        est_128 = mh_base_128.jaccard(mh_other_128)

        # MinHash with 32 hashes (noisier)
        mh_base_32 = MinHash(num_hashes=32)
        mh_other_32 = MinHash(num_hashes=32)
        for w in base_set:
            mh_base_32.update(w)
        for w in other_set:
            mh_other_32.update(w)
        est_32 = mh_base_32.jaccard(mh_other_32)

        err_128 = abs(est_128 - true_j) / max(true_j, 0.001) * 100
        print(f"  {label:<16} {true_j:>14.3f} {est_128:>14.3f} {est_32:>13.3f} {err_128:>+11.1f}%")

    print(f"""
  Key observations:
    128 hashes gives good accuracy (typical error <5%) for most cases.
    32 hashes is noisier but uses 4x less memory — tune k to your accuracy need.
    Standard error of MinHash estimate ≈ 1/sqrt(k).
    At k=128: std_error ≈ {1/math.sqrt(128):.3f} ({100/math.sqrt(128):.1f}%)
    At k=32:  std_error ≈ {1/math.sqrt(32):.3f} ({100/math.sqrt(32):.1f}%)

  LSH (Locality-Sensitive Hashing) extension:
    Split k hashes into b bands of r hashes each (k = b * r).
    Two sets become candidates if they match ALL hashes in ANY band.
    This trades recall for precision: high-Jaccard pairs are very likely
    to share at least one band; low-Jaccard pairs rarely do.
    Used by LinkedIn for "People You May Know" and news deduplication pipelines.
""")


# ── Summary ───────────────────────────────────────────────────────────────

def phase7_summary():
    section("Phase 7: Comparison Summary")
    print(f"""
  {'Structure':<22} {'Question':<35} {'Memory':<20} {'Error'}
  {'─'*22} {'─'*35} {'─'*20} {'─'*15}
  {'Bloom Filter':<22} {'Is item in set?':<35} {'O(n)→sub-linear':<20} {'FP only, no delete'}
  {'Cuckoo Filter':<22} {'Is item in set? (deletable)':<35} {'O(n)→sub-linear':<20} {'FP only, supports delete'}
  {'HyperLogLog':<22} {'How many distinct items?':<35} {'O(1) ~12KB':<20} {'±0.81%, mergeable'}
  {'Count-Min Sketch':<22} {'How often does item appear?':<35} {'O(w×d)':<20} {'Over-estimate only'}
  {'Top-K':<22} {'What are the K most frequent?':<35} {'O(K)':<20} {'Approx rank'}
  {'MinHash':<22} {'How similar are two sets?':<35} {'O(k) ~512B':<20} {'±1/sqrt(k)'}

  Key insight: trade exact answers for dramatically less memory.
  Acceptable for analytics, monitoring, recommendations.
  NOT acceptable for financial counts, exact billing, legal compliance.

  Quick sizing cheat-sheet (memorize for interviews):
    Bloom filter 1% FPR, 100M items  → ~120 MB bits, k=7 hash functions
    Bloom filter 0.1% FPR, 100M items → ~180 MB bits, k=10 hash functions
    HyperLogLog any cardinality        → 12 KB, ±0.81% error
    Count-Min Sketch 1% error, δ=1%   → width≥272, depth≥5
    MinHash 5% Jaccard error           → k=400 hashes, ~1.6 KB per set

  Bloom filter false positive formula:
    FPR = (1 - e^(-kn/m))^k
    k = hash functions, n = items inserted, m = bit array size
    Optimal k = (m/n) * ln(2)  →  gives minimum FPR for a given m/n ratio

  HyperLogLog harmonic mean:
    Uses leading zeros in hash values as a proxy for cardinality.
    m registers, each tracking the max leading zeros seen.
    Harmonic mean of 2^(max_zeros) across registers = cardinality estimate.

  Error direction summary (critical for interview):
    Bloom / Cuckoo Filter:   false positives only (never false negatives)
      → safe for "skip unnecessary work"; unsafe as a security gate
    HyperLogLog:             symmetric error (±0.81%)
      → good for dashboards; never use for billing
    Count-Min Sketch:        over-estimates only (never under-estimates)
      → safe for rate limiting (you throttle early, never under-count)
      → safe for "heavy hitter" detection; over-estimates harmless
    Top-K:                   may miss items near the K boundary
    MinHash:                 symmetric error, controlled by k

  Next: ../../03-case-studies/01-url-shortener/
""")


def main():
    section("PROBABILISTIC DATA STRUCTURES LAB")
    print("""
  These data structures answer approximate questions using
  dramatically less memory than exact approaches.

  Redis Stack (BF, HLL, TopK modules) backs Phases 1, 2, and 4.
  Bloom filter, Count-Min Sketch, and MinHash have manual Python
  implementations so you can see the underlying algorithm directly.

  Phases:
    1 — Bloom Filter:          membership test, FPR measurement, Redis BF
    2 — HyperLogLog:           cardinality estimation + PFMERGE cross-shard
    3 — Count-Min Sketch:      frequency estimation, width sensitivity
    4 — Top-K:                 most frequent items via Redis TOPK
    5 — Bloom Saturation:      "break it" — observe FPR rise to ~100%
    6 — MinHash:               Jaccard similarity estimation
    7 — Summary:               comparison table + error direction cheat-sheet
""")

    phase1_bloom_filter()
    phase2_hyperloglog()
    phase3_count_min_sketch()
    phase4_topk()
    phase5_bloom_saturation()
    phase6_minhash()
    phase7_summary()


if __name__ == "__main__":
    main()
