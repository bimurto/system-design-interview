# Probabilistic Data Structures

**Prerequisites:** `../14-idempotency-exactly-once/`
**Next:** `../../03-case-studies/01-url-shortener/`

---

## Concept

Some questions in distributed systems do not require exact answers. "Have I seen this URL before?" doesn't need a
perfect yes/no if a 1% false positive rate is acceptable. "How many unique visitors today?" doesn't need to be exactly
4,382,917 — "approximately 4.4 million ± 1%" is sufficient for dashboards and capacity planning. Probabilistic data
structures exploit this tolerance to achieve orders-of-magnitude improvements in memory efficiency over exact
approaches.

The fundamental trade-off is accuracy for space. A Python `set` that tracks 10 million unique user IDs uses roughly
500MB of memory. A HyperLogLog structure tracking the same cardinality uses 12KB — a 40,000x improvement — with ±0.81%
error. A Bloom filter that can tell you whether a given item is "definitely not in the set" or "probably in the set" for
100 million items costs about 120MB for a 0.1% false positive rate, compared to several gigabytes for an exact set.
These structures are not approximations for lack of correctness — they are carefully engineered structures with provable
error bounds.

The Bloom filter, invented by Burton Bloom in 1970, is the canonical probabilistic membership test. It uses a bit array
of m bits and k hash functions. Adding an item sets k bits (one per hash function). Querying checks whether all k bits
are set. A false positive occurs when all k bits happen to be set by other items. The probability is bounded by the
formula `(1 - e^(-kn/m))^k` where n is the number of inserted items. This formula drives the parameter choices: given a
desired false positive rate p and capacity n, you can compute the optimal m and k. Crucially, false negatives are
impossible — if an item was added, all its bits are definitely set.

A critical Bloom filter limitation for production systems: **deletion is impossible**. Clearing bits when removing an
item would corrupt membership tests for other items that share those bits. This motivates the **Cuckoo filter** — a more
recent alternative that supports deletion, uses less space for FPR < 3%, and is faster in practice. Cuckoo filters store
fingerprints of items in a hash table using cuckoo hashing; deletion works by finding and removing the fingerprint. The
trade-off: Cuckoo filters have a hard capacity limit (unlike scalable Bloom filters) and can fail to insert if the table
becomes too full (~95%+ occupancy). For read-heavy workloads requiring deletion (e.g., cache invalidation lists, revoked
token sets), prefer Cuckoo filters.

HyperLogLog estimates the cardinality (number of distinct elements) of a multiset. The insight is elegant: if you hash
items to a uniform random bit string, the maximum number of leading zeros in any hash is statistically related to the
total number of distinct items. One leading zero means roughly 2^1 = 2 distinct items; three leading zeros means roughly
2^3 = 8 distinct items. A single max-leading-zeros register is noisy, but averaging over 16,384 registers (using
sub-hash values to choose which register to update) produces a cardinality estimate with ±0.81% standard error, using
only 12KB of memory regardless of whether you have 1,000 or 1,000,000,000 distinct items.

A key operational capability: **HyperLogLog structures are mergeable**. Redis `PFMERGE dest src1 src2 ...` combines
multiple HLL counters into one. This means you can maintain per-shard or per-hour HLLs and merge them to get a
cross-shard or cross-time-window distinct count — something impossible with most exact approaches without centralizing
raw data. HyperLogLog++ (Google's 2013 improvement, used in BigQuery) adds a sparse representation for low cardinalities
and a bias correction lookup table, reducing error at cardinalities below ~11,500 from several percent to near-zero.

Count-Min Sketch estimates item frequencies in a data stream. It uses a w×d matrix of counters and d hash functions.
Incrementing an item updates d counters (one per row). Querying returns the minimum across d counters — the minimum is
used because hash collisions can only increase a counter, never decrease it, so the minimum is the best estimate of the
true count. The error bound is: estimated count ≤ true count + ε×N, where ε = e/w and δ = e^(-d) is the failure
probability. By choosing appropriate w and d, you can trade memory for accuracy.

MinHash estimates the Jaccard similarity between two sets using only O(k) space per set. The insight: apply k hash
functions to every element of each set, take the minimum hash from each function. The probability that the i-th minimum
hash matches across two sets equals `|A ∩ B| / |A ∪ B|` — the Jaccard coefficient. So the fraction of matching
min-hashes directly estimates similarity. Typically k=128 gives ~9% standard error; k=400 gives ~5% standard error.
MinHash is the basis for LSH (Locality-Sensitive Hashing), which finds candidate near-duplicate pairs in sub-quadratic
time.

## How It Works

**Bloom Filter — Add and Membership Check:**

1. Initialise a bit array of `m` bits (all zeros) and choose `k` independent hash functions
2. **To add an item:** compute `k` hash positions (`hash_i(item) % m` for i = 1..k) and set those `k` bits to 1
3. **To check membership:** compute the same `k` hash positions for the query item
4. If **any** of the `k` bits is 0: the item is **definitely not** in the set — return false (no false negatives)
5. If **all** `k` bits are 1: the item is **probably** in the set — return true (may be a false positive if those bits
   were set by other items)
6. False positive rate is bounded by: `FPR ≈ (1 − e^(−kn/m))^k` where `n` = items inserted; tune `m` (bits) and `k` (
   hash functions) to hit a target FPR (e.g., 1% FPR for 100M items requires ~960MB of bits)

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

  Standard error ≈ 1/sqrt(k)
    k=128 → std_err ≈ 8.8%    (512 bytes per set)
    k=400 → std_err ≈ 5.0%    (1.6 KB per set)

  Use: finding similar documents, near-duplicate detection
  LinkedIn uses MinHash to find users with similar skill/connection sets.
  LSH bands: split k hashes into b bands of r rows — candidate pairs share
  at least one band. Reduces O(n²) comparison to O(n) candidate generation.
```

### Trade-offs

| Structure        | Space           | Answer Type                        | Error Guarantee                                 | Supports Deletion   | Use Case                                                       |
|------------------|-----------------|------------------------------------|-------------------------------------------------|---------------------|----------------------------------------------------------------|
| Bloom Filter     | O(n) sub-linear | "Definitely not" or "probably yes" | FP rate bounded by math                         | No                  | Membership test at scale                                       |
| Cuckoo Filter    | O(n) sub-linear | Same as Bloom                      | FP rate bounded by math (lower space at FPR<3%) | Yes                 | Membership with deletions (revoked tokens, cache invalidation) |
| HyperLogLog      | O(1) ~12KB      | Cardinality estimate               | ±0.81% std error                                | No                  | Unique visitor count                                           |
| Count-Min Sketch | O(w×d)          | Frequency estimate                 | Always ≥ true count                             | No (only increment) | Heavy hitter detection                                         |
| Top-K            | O(K)            | Most frequent K items              | Approximate ranks                               | No                  | Trending items                                                 |
| MinHash          | O(k) ~512B      | Similarity score                   | ±1/sqrt(k)                                      | No                  | Near-duplicate detection                                       |

### Quick Sizing Cheat-Sheet

| Goal                               | Parameters         | Memory         |
|------------------------------------|--------------------|----------------|
| Bloom filter, 1% FPR, 100M items   | k=7, m=958M bits   | ~120 MB        |
| Bloom filter, 0.1% FPR, 100M items | k=10, m=1.44B bits | ~180 MB        |
| HyperLogLog, any cardinality       | 16,384 registers   | 12 KB          |
| Count-Min Sketch, ε=1%, δ=1%       | width=272, depth=5 | ~5.3 KB        |
| MinHash, ~9% Jaccard error         | k=128 hashes       | 512 B per set  |
| MinHash, ~5% Jaccard error         | k=400 hashes       | 1.6 KB per set |

### Failure Modes

**Bloom filter saturation:** If more items are added than the designed capacity, the bit array fills up and the false
positive rate rises sharply — approaching 100% as the array becomes all 1s. The **fill ratio** (fraction of bits set to
1) is the key operational metric: at the optimal operating point the fill ratio is ~0.5 (maximum entropy); above 0.8 the
filter is significantly degraded; at 1.0 every query returns "probably yes". Solution: monitor the actual insertion
count against the designed capacity; provision a new Bloom filter when approaching capacity. Redis BF supports scaling
with `BF.RESERVE` expansion factors.

**HyperLogLog small cardinality error:** For very small sets (< 2.5×m where m=16384 registers), HyperLogLog switches to
an exact linear counting algorithm internally. This transition can cause a discontinuity in estimates around
cardinality ~40,000. In practice, Redis's implementation handles this correctly, but naive implementations may show a
spike in error at this boundary.

**Count-Min Sketch width too small — high collision rate:** With a width of 100 and 1 million distinct items, every
counter has ~10,000 items hashing to it on average. The estimate for any item will be hugely inflated. Rule of thumb:
width should be at least `e / ε` where ε is your target relative error (e.g., for 1% error, width ≥ 272). For typical
heavy-hitter detection, width=1000-10000 with depth=5 covers most cases.

**Cuckoo filter insertion failure near capacity:** Unlike Bloom filters (which degrade gracefully but predictably),
Cuckoo filters can fail hard — `insert()` returns false when the table is too full (~95%+ load factor) and the cuckoo
eviction cycle cannot terminate. This failure is silent if not checked. Production Cuckoo filters must handle insert
failures explicitly (fall back to an exact store, or refuse insertion and alert). Never use a Cuckoo filter without
checking the return value of insert.

**HyperLogLog PFMERGE always produces dense output:** Redis HLLs start in a sparse representation (a few hundred bytes)
for low cardinalities and automatically promote to dense (12KB) above a threshold. PFMERGE always produces a dense
output — even if both inputs are sparse. If you merge thousands of per-user or per-session HLLs frequently (e.g., in a
streaming pipeline), you convert all sparse HLLs from ~200 bytes to 12KB. At 10,000 small HLLs, that is 2MB (sparse)
versus 120MB (post-merge dense). Design merge strategies to be terminal aggregation operations (daily rollup,
cross-shard aggregate) rather than intermediate steps.

**Hash function independence is assumed, not always guaranteed:** The FPR math for Bloom filters assumes k truly
independent hash functions. Using a single hash family with integer seeds (e.g., MurmurHash3 with seeds 0,1,...,k-1)
approximates this via the double-hashing construction but is not provably independent. In practice, this works well for
non-adversarial inputs. For adversarial inputs (an attacker who knows your hash seeds), the FPR guarantee breaks —
independent hash families from different algorithms (SHA-256, FNV, xxHash) provide stronger guarantees.

**Using probabilistic structures where exact counts are required:** Using HyperLogLog for billing (billing a customer
for approximate API calls) or using a Bloom filter as the only security check (an attacker who knows the hash functions
can craft false positives) are category errors. Probabilistic structures are for analytics and optimization, not for
correctness-critical operations.

## Interview Talking Points

- "Bloom filters are used in Cassandra, RocksDB, and LevelDB for SSTable/SST file lookup — before reading from disk,
  check the Bloom filter to see if the key might be in this file. A false positive means an unnecessary disk read; a
  false negative is impossible. This dramatically reduces disk I/O for missing keys in any LSM-tree storage engine.
  Cassandra tunes the FPR per table — a 1% FPR means at most 1 extra disk read per 100 'key not found' lookups."
- "HyperLogLog uses 12KB to count distinct items with ±0.81% accuracy, regardless of whether there are 1,000 or 1
  billion distinct items. Redis uses it for `PFCOUNT`. Critically, HLLs are mergeable via `PFMERGE` — you can keep one
  HLL per hour or per shard and merge them for multi-day or cross-region distinct counts. That's impossible with exact
  approaches without centralizing raw IDs."
- "The Bloom filter false positive formula is `(1 - e^(-kn/m))^k`. To hit 1% FPR with 100M items, you need ~120MB of
  bits and 7 hash functions — the optimal k = (m/n) × ln(2). The fill ratio (fraction of bits set to 1) is your
  operational health metric: optimal is ~0.5; above 0.8 you need to start provisioning a replacement."
- "If you need membership testing with deletions — like a revoked JWT token list or a CDN cache invalidation set — use a
  Cuckoo filter instead of a Bloom filter. Cuckoo filters support deletion and use ~25% less space than Bloom for the
  same FPR below 3%, but they can fail hard on insert near full capacity (~95% load factor), so you must check the
  insert return value and have a fallback."
- "Count-Min Sketch is like a compressed frequency table. It trades exact counts for a sketch that always
  over-estimates (never under-estimates) by at most `ε * N` with probability `1 - δ`, where you tune ε and δ by choosing
  width=e/ε and depth=ln(1/δ). The over-estimate-only property is actually useful for rate limiting: you may throttle a
  user slightly early, but you will never under-count and allow excess traffic."
- "HyperLogLog++ (used in BigQuery's `APPROX_COUNT_DISTINCT`) adds a sparse representation for small cardinalities and a
  bias correction table, virtually eliminating the error spikes that occur in vanilla HLL at cardinalities below ~
  40,000. When asked about approximate distinct counts in a data warehouse context, mention HLL++ specifically."
- "MinHash estimates Jaccard similarity between two sets in O(k) space rather than O(|A| + |B|). The key insight:
  `P(min_hash_i(A) == min_hash_i(B)) = |A ∩ B| / |A ∪ B|`, so the fraction of matching min-hashes directly estimates
  Jaccard similarity with standard error 1/sqrt(k). LinkedIn uses MinHash LSH to find candidate similar users in O(n)
  rather than O(n²) comparisons."
- "The key question before using a probabilistic structure: can your application tolerate false positives, and is the
  error one-sided or two-sided? Bloom/Cuckoo have one-sided error (false positives only). HyperLogLog has symmetric
  error. Count-Min Sketch has one-sided error (over-estimates only). MinHash has symmetric error. Choose based on which
  direction of error is safe for your use case."

## Hands-on Lab

**Time:** ~3 minutes
**Services:** redis-stack-server (includes BF, HLL, TopK modules)

### Setup

```bash
cd system-design-interview/02-advanced/16-probabilistic-data-structures/
docker compose up
```

### Experiment

The script runs seven phases automatically:

1. **Bloom Filter:** Builds a Python Bloom filter (capacity=100K, 1% FPR). Adds 100K items, tests 10K non-members,
   measures actual vs theoretical false positive rate. Verifies zero false negatives (the fundamental guarantee).
   Compares memory: Python set (~2MB) vs Bloom filter (~120KB). Also uses Redis `BF.ADD`/`BF.EXISTS`.
2. **HyperLogLog:** Adds 100K unique IDs to Redis HLL with `PFADD`, reads cardinality with `PFCOUNT`. Shows ±<1% error.
   Then adds 50K duplicates — count barely changes. Also demonstrates `PFMERGE` across 3 shards with overlapping user
   sets, showing cross-shard distinct count without raw data transfer. Includes the sparse→dense memory warning.
3. **Count-Min Sketch:** Python implementation. Shows how width controls the error bound by running the same event
   stream through CMS instances of width 50, 200, and 2000. Then compares estimated vs true frequencies for top items,
   confirming over-estimate-only behavior with an explicit assertion.
4. **Top-K:** Uses Redis `TOPK.ADD`/`TOPK.LIST` to find the top 10 most frequent pages in 110K events. Compares with
   exact top-10 and shows overlap count.
5. **Bloom Filter Saturation:** Runs the "break it" demo — inserts items at 1x, 2x, 5x, and 10x the designed capacity
   and measures actual FPR and fill ratio at each fill level. Demonstrates how FPR rises from 1% to near 100% as the bit
   array saturates.
6. **MinHash:** Manual Python implementation. Shows Jaccard similarity estimation for sets with 90%, 50%, 10%, and 0%
   overlap. Compares accuracy at k=128 vs k=32 hashes, demonstrating the 1/sqrt(k) error tradeoff.
7. **Summary:** Comparison table of all structures including Cuckoo filter, a sizing cheat-sheet, and an error-direction
   cheat-sheet (which structures over-estimate, which have symmetric error, which have false positives only).

### Observe

The Bloom filter will show an actual FPR very close to the theoretical 1%, and exactly 0 false negatives. The
HyperLogLog count should be within 1% of 100,000. The PFMERGE result will correctly account for overlapping user IDs
across shards. The Count-Min Sketch always over-estimates — the top items may show 2-10% above their true counts with
width=2000. The saturation demo will show FPR rising from ~1% at 1x capacity to >90% at 10x capacity, with fill ratio
rising from ~0.5 to ~1.0. The MinHash demo will show that k=128 achieves ~5-10% error on Jaccard estimates, while k=32
is noticeably noisier.

### Teardown

```bash
docker compose down
```

## Real-World Examples

- **Cassandra / RocksDB / LevelDB Bloom Filters:** LSM-tree storage engines (Apache Cassandra, RocksDB used in TiKV and
  MyRocks, Google's LevelDB) use a Bloom filter for each SSTable/SST file. Before performing a disk read to look up a
  key, the engine checks the file's Bloom filter. If the filter says "definitely not here," the disk read is skipped.
  With typical 1% FPR, 99% of "missing key" lookups skip disk I/O entirely. This is critical for write-heavy workloads
  where compaction creates many files. Source: Apache Cassandra documentation, "How Cassandra reads data"; RocksDB
  documentation, "Bloom Filters."
- **Redis HyperLogLog for Unique Visitors:** Redis introduced native HyperLogLog support with `PFADD`/`PFCOUNT`/
  `PFMERGE` commands in Redis 2.8.9 (2014). This enables systems to count unique daily active users, unique IP
  addresses, or unique search queries using 12KB of memory per counter, regardless of the actual user count. Twitter
  uses Redis HyperLogLog for real-time unique visitor metrics across their analytics pipeline. Source: Salvatore
  Sanfilippo (antirez), "An introduction to Redis data types and abstractions," Redis documentation.
- **Google BigQuery Approximate Aggregation:** BigQuery's `APPROX_COUNT_DISTINCT` and `HLL_COUNT` functions use
  HyperLogLog++ (an improved HLL with better accuracy at low cardinalities) to compute distinct counts over billions of
  rows in seconds rather than minutes. The functions trade ±1% accuracy for 10-100x faster query execution and lower
  memory usage. This makes them the standard choice for cardinality estimation in large-scale data warehouse queries.
  Source: Google Cloud, "Approximate aggregation functions in BigQuery," product documentation.
- **LinkedIn MinHash LSH for Recommendations:** LinkedIn's "People You May Know" system uses MinHash LSH to find
  candidate users with similar skill sets and connection graphs. Without LSH, finding all similar pairs requires O(n²)
  comparisons — infeasible at LinkedIn's scale. MinHash reduces each user's skill/connection set to a ~512-byte
  signature, and LSH banding finds candidate pairs in sub-linear time. Source: LinkedIn Engineering Blog, "The Anatomy
  of a Large-Scale People Search Engine."

## Common Mistakes

- **Using Bloom filters as a security mechanism.** A Bloom filter is a performance optimisation (skip unnecessary work),
  not a security check. An adversary who knows the hash functions can craft inputs that produce false positives,
  bypassing a "membership required" gate. Bloom filters should be backed by an authoritative data store for any
  security-relevant check.
- **Not accounting for the growth of Bloom filter FPR over time.** A Bloom filter tuned for 100M items at 1% FPR will be
  near-useless at 1 billion items — the FPR approaches 100%. Production Bloom filters need a growth strategy: either
  pre-provision for peak capacity with margin, or use a scalable Bloom filter (SBF) that adds new layers as capacity is
  exceeded. Monitor fill ratio as a gauge metric; alert at 0.8, provision replacement at 0.95.
- **Using HyperLogLog for exact counts in billing or compliance.** HyperLogLog's ±0.81% error on 10 million events is
  ±81,000 events — significant if each event is a billable unit. Always use exact counting (SQL `COUNT(DISTINCT)`) for
  anything that affects money or legal compliance.
- **Choosing Count-Min Sketch width too small.** The accuracy of Count-Min Sketch depends on the width (number of
  columns) relative to the total stream size. If your stream has 1 billion events and your width is 100, every counter
  tracks ~10 million items on average — estimates will be off by orders of magnitude. Size width to be at least `e / ε`
  where ε is your target relative error (e.g., for 1% error, width ≥ 272).
- **Not handling Cuckoo filter insertion failures.** Cuckoo filters return false when insertion fails near full
  capacity — unlike Bloom filters, there is no graceful degradation, just a hard failure. Any production system using a
  Cuckoo filter must check the insert return value and have a fallback (typically an exact overflow store). Ignoring
  insert failures means silently losing membership data, which can cause false negatives — a property the structure was
  supposed to guarantee against.
- **Assuming HyperLogLog PFMERGE preserves sparse encoding.** Merging two Redis HLLs always produces a dense output (
  12KB), even if both inputs were sparse (a few hundred bytes). If your design merges many small-cardinality HLLs
  frequently, you may inadvertently inflate memory usage by orders of magnitude compared to keeping them separate.
- **Treating mmh3 seeds as truly independent hash functions.** Using MurmurHash3 with integer seeds 0,1,...,k-1 is a
  double-hashing approximation, not k provably independent hash functions. For non-adversarial inputs this is fine in
  practice, and the FPR math holds empirically. For adversarial inputs (security-sensitive contexts), use multiple
  unrelated hash families (SHA-256, FNV-1a, xxHash) to prevent an attacker from crafting targeted false positives.
