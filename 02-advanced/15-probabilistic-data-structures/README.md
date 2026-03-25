# Probabilistic Data Structures

**Prerequisites:** `../14-idempotency-exactly-once/`
**Next:** `../../03-case-studies/01-url-shortener/`

---

## Concept

Some questions in distributed systems do not require exact answers. "Have I seen this URL before?" doesn't need a perfect yes/no if a 1% false positive rate is acceptable. "How many unique visitors today?" doesn't need to be exactly 4,382,917 — "approximately 4.4 million ± 1%" is sufficient for dashboards and capacity planning. Probabilistic data structures exploit this tolerance to achieve orders-of-magnitude improvements in memory efficiency over exact approaches.

The fundamental trade-off is accuracy for space. A Python `set` that tracks 10 million unique user IDs uses roughly 500MB of memory. A HyperLogLog structure tracking the same cardinality uses 12KB — a 40,000x improvement — with ±0.81% error. A Bloom filter that can tell you whether a given item is "definitely not in the set" or "probably in the set" for 100 million items costs about 120MB for a 0.1% false positive rate, compared to several gigabytes for an exact set. These structures are not approximations for lack of correctness — they are carefully engineered structures with provable error bounds.

The Bloom filter, invented by Burton Bloom in 1970, is the canonical probabilistic membership test. It uses a bit array of m bits and k hash functions. Adding an item sets k bits (one per hash function). Querying checks whether all k bits are set. A false positive occurs when all k bits happen to be set by other items. The probability is bounded by the formula `(1 - e^(-kn/m))^k` where n is the number of inserted items. This formula drives the parameter choices: given a desired false positive rate p and capacity n, you can compute the optimal m and k. Crucially, false negatives are impossible — if an item was added, all its bits are definitely set.

HyperLogLog estimates the cardinality (number of distinct elements) of a multiset. The insight is elegant: if you hash items to a uniform random bit string, the maximum number of leading zeros in any hash is statistically related to the total number of distinct items. One leading zero means roughly 2^1 = 2 distinct items; three leading zeros means roughly 2^3 = 8 distinct items. A single max-leading-zeros register is noisy, but averaging over 16,384 registers (using sub-hash values to choose which register to update) produces a cardinality estimate with ±0.81% standard error, using only 12KB of memory regardless of whether you have 1,000 or 1,000,000,000 distinct items.

Count-Min Sketch estimates item frequencies in a data stream. It uses a w×d matrix of counters and d hash functions. Incrementing an item updates d counters (one per row). Querying returns the minimum across d counters — the minimum is used because hash collisions can only increase a counter, never decrease it, so the minimum is the best estimate of the true count. The error bound is: estimated count ≤ true count + ε×N, where ε = e/w and δ = e^(-d) is the failure probability. By choosing appropriate w and d, you can trade memory for accuracy.

## How It Works

**Bloom filter bit operations:**

```
  Bit array (m=20 bits), k=3 hash functions:

  Add "apple":
    hash0("apple") % 20 = 4  → set bit 4
    hash1("apple") % 20 = 8  → set bit 8
    hash2("apple") % 20 = 15 → set bit 15
  bit array: 0000100010000001000

  Add "banana":
    hash0("banana") % 20 = 3
    hash1("banana") % 20 = 8  ← collision with "apple"!
    hash2("banana") % 20 = 17
  bit array: 0001100010000001010

  Query "cherry":
    hash0("cherry") % 20 = 3  → bit 3 is set (by banana)
    hash1("cherry") % 20 = 8  → bit 8 is set (by apple+banana)
    hash2("cherry") % 20 = 17 → bit 17 is set (by banana)
    All 3 set → "probably in set" ← FALSE POSITIVE (cherry was never added)
```

**HyperLogLog register update:**

```
  Hash item to 64-bit string: "user_12345" → 0b0001 10110...
  Use first 14 bits to select register (2^14 = 16384 registers)
  In remaining bits, count leading zeros + 1 → update register if > current max

  Cardinality estimate = α * m² * harmonic_mean(2^register_values)
  where α ≈ 0.7213 / (1 + 1.079/m) is a bias correction constant

  Redis HLL: 12KB for ALL cardinalities from 1 to 2^64
```

**Count-Min Sketch example:**

```
  Width=5, depth=3 (3 hash functions × 5 counters):

  Add "page_A" 100 times:
    row0[hash0("page_A")%5]  += 100
    row1[hash1("page_A")%5]  += 100
    row2[hash2("page_A")%5]  += 100

  Query "page_A":
    min(row0[hash0(...)], row1[hash1(...)], row2[hash2(...)]) = min(100+collisions...)
    ≥ 100 (true count), may be higher due to collisions from other items
```

**MinHash for set similarity (Jaccard):**

```
  Jaccard similarity: |A ∩ B| / |A ∪ B|

  MinHash approximation:
    Apply k hash functions to both sets, take minimum hash from each
    P(min_hash_i(A) == min_hash_i(B)) = Jaccard(A, B)
    Estimate = (# matching min-hashes) / k

  Use: finding similar documents, near-duplicate detection
  LinkedIn uses MinHash to find users with similar skill/connection sets
```

### Trade-offs

| Structure | Space | Answer Type | Error Guarantee | Use Case |
|---|---|---|---|---|
| Bloom Filter | O(n) sub-linear | "Definitely not" or "probably yes" | FP rate bounded by math | Membership test at scale |
| HyperLogLog | O(1) ~12KB | Cardinality estimate | ±0.81% std error | Unique visitor count |
| Count-Min Sketch | O(w×d) | Frequency estimate | Always ≥ true count | Heavy hitter detection |
| Top-K | O(K) | Most frequent K items | Approximate ranks | Trending items |
| MinHash | O(bands) | Similarity score | Tunable by band count | Near-duplicate detection |

### Failure Modes

**Bloom filter saturation:** If more items are added than the designed capacity, the bit array fills up and the false positive rate rises sharply — approaching 100% as the array becomes all 1s. Solution: monitor the actual insertion count against the designed capacity; provision a new Bloom filter when approaching capacity. Redis BF supports scaling with `BF.RESERVE` expansion factors.

**HyperLogLog small cardinality error:** For very small sets (< 2.5×m where m=16384 registers), HyperLogLog switches to an exact linear counting algorithm internally. This transition can cause a discontinuity in estimates around cardinality ~40,000. In practice, Redis's implementation handles this correctly, but naive implementations may show a spike in error at this boundary.

**Count-Min Sketch width too small — high collision rate:** With a width of 100 and 1 million distinct items, every counter has ~10,000 items hashing to it on average. The estimate for any item will be hugely inflated. Rule of thumb: width should be at least 2× the maximum expected single-item count / total event count ratio, multiplied by the stream size. For typical heavy-hitter detection, width=1000-10000 with depth=5 covers most cases.

**Using probabilistic structures where exact counts are required:** Using HyperLogLog for billing (billing a customer for approximate API calls) or using a Bloom filter as the only security check (an attacker who knows the hash functions can craft false positives) are category errors. Probabilistic structures are for analytics and optimization, not for correctness-critical operations.

## Interview Talking Points

- "Bloom filters are used in Cassandra for SSTable lookup — before reading from disk, check the Bloom filter to see if the key might be in this SSTable. A false positive means an unnecessary disk read; a false negative is impossible. This dramatically reduces disk I/O for missing keys."
- "HyperLogLog uses 12KB to count distinct items with ±0.81% accuracy, regardless of whether there are 1,000 or 1 billion distinct items. Redis uses it for `PFCOUNT` — unique visitor counting without storing individual user IDs."
- "The Bloom filter false positive formula is `(1 - e^(-kn/m))^k`. To hit 1% FPR with 100M items, you need ~960MB of bits — still much less than storing the items themselves."
- "Count-Min Sketch is like a compressed frequency table. It trades exact counts for a sketch that always over-estimates (never under-estimates). Perfect for finding 'heavy hitters' in network traffic or event streams."
- "MinHash estimates Jaccard similarity between two sets in O(k) space rather than O(|A| + |B|). LinkedIn uses it to find users with similar profiles for recommendations without comparing every pair."
- "The key question before using a probabilistic structure: can your application tolerate false positives? Chrome's safe browsing blocklist uses a Bloom filter — a false positive just means an extra server check, not a security failure."

## Hands-on Lab

**Time:** ~3 minutes
**Services:** redis-stack-server (includes BF, HLL, TopK modules)

### Setup

```bash
cd system-design-interview/02-advanced/15-probabilistic-data-structures/
docker compose up
```

### Experiment

The script runs five phases automatically:

1. **Bloom Filter:** Builds a Python Bloom filter (capacity=100K, 1% FPR). Adds 100K items, tests 10K non-members, measures actual vs theoretical false positive rate. Compares memory: Python set (~2MB) vs Bloom filter (~120KB). Also uses Redis `BF.ADD`/`BF.EXISTS`.
2. **HyperLogLog:** Adds 100K unique IDs to Redis HLL with `PFADD`, reads cardinality with `PFCOUNT`. Shows ±<1% error. Then adds 50K duplicates — count barely changes. Compares 12KB vs ~2MB exact set.
3. **Count-Min Sketch:** Python implementation. Processes 150K events with power-law distribution. Compares estimated vs true frequencies for top items. Shows overestimation pattern.
4. **Top-K:** Uses Redis `TOPK.ADD`/`TOPK.LIST` to find the top 10 most frequent pages in 150K events. Compares with true counts.
5. Prints a comparison table of all four structures.

### Break It

Demonstrate Bloom filter saturation by exceeding capacity:

```python
import mmh3, math

# Small filter: capacity=100, 1% FPR
bf = BloomFilter(100, 0.01)
for i in range(1000):  # Add 10x the capacity
    bf.add(f"item_{i}")

# FPR is now ~100% — nearly all bits set
false_pos = sum(1 for i in range(1000, 2000) if f"item_{i}" in bf)
print(f"FPR after saturation: {false_pos/1000*100:.1f}%  (expected: ~1%, actual: ~100%)")
```

### Observe

The Bloom filter will show an actual FPR very close to the theoretical 1%. The HyperLogLog count should be within 1% of 100,000. The Count-Min Sketch always over-estimates — the top items may show 5-15% over their true counts due to hash collisions. The Top-K result should closely match the true top-10 by frequency.

### Teardown

```bash
docker compose down
```

## Real-World Examples

- **Cassandra Bloom Filters:** Apache Cassandra uses a Bloom filter for each SSTable (immutable on-disk sorted file). Before performing a disk read to look up a key, Cassandra checks the SSTable's Bloom filter. If the filter says "definitely not here," the disk read is skipped. With typical 1% FPR, 99% of "missing key" lookups skip disk I/O entirely. This is critical for write-heavy workloads where SSTables accumulate quickly. Source: Apache Cassandra documentation, "How Cassandra reads data."
- **Redis HyperLogLog for Unique Visitors:** Redis introduced native HyperLogLog support with `PFADD`/`PFCOUNT`/`PFMERGE` commands in Redis 2.8.9 (2014). This enables systems to count unique daily active users, unique IP addresses, or unique search queries using 12KB of memory per counter, regardless of the actual user count. Twitter uses Redis HyperLogLog for real-time unique visitor metrics across their analytics pipeline. Source: Salvatore Sanfilippo (antirez), "An introduction to Redis data types and abstractions," Redis documentation.
- **Google BigQuery Approximate Aggregation:** BigQuery's `APPROX_COUNT_DISTINCT` and `HLL_COUNT` functions use HyperLogLog++ (an improved HLL with better accuracy at low cardinalities) to compute distinct counts over billions of rows in seconds rather than minutes. The functions trade ±1% accuracy for 10-100x faster query execution and lower memory usage. This makes them the standard choice for cardinality estimation in large-scale data warehouse queries. Source: Google Cloud, "Approximate aggregation functions in BigQuery," product documentation.

## Common Mistakes

- **Using Bloom filters as a security mechanism.** A Bloom filter is a performance optimisation (skip unnecessary work), not a security check. An adversary who knows the hash functions can craft inputs that produce false positives, bypassing a "membership required" gate. Bloom filters should be backed by an authoritative data store for any security-relevant check.
- **Not accounting for the growth of Bloom filter FPR over time.** A Bloom filter tuned for 100M items at 1% FPR will be near-useless at 1 billion items — the FPR approaches 100%. Production Bloom filters need a growth strategy: either pre-provision for peak capacity with margin, or use a scalable Bloom filter (SBF) that adds new layers as capacity is exceeded.
- **Using HyperLogLog for exact counts in billing or compliance.** HyperLogLog's ±0.81% error on 10 million events is ±81,000 events — significant if each event is a billable unit. Always use exact counting (SQL `COUNT(DISTINCT)`) for anything that affects money or legal compliance.
- **Choosing Count-Min Sketch width too small.** The accuracy of Count-Min Sketch depends on the width (number of columns) relative to the total stream size. If your stream has 1 billion events and your width is 100, every counter tracks ~10 million items on average — estimates will be off by orders of magnitude. Size width to be at least `e / ε` where ε is your target relative error (e.g., for 1% error, width ≥ 272).
