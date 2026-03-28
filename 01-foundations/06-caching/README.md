# Caching

**Prerequisites:** `../05-partitioning-sharding/`
**Next:** `../07-load-balancing/`

---

## Concept

Caching is the practice of storing copies of frequently accessed data in a faster storage tier so that future requests
can be served without recomputing or re-fetching the data from the slower source of truth. It is one of the
highest-leverage techniques in systems design: a well-placed cache can reduce database load by 90%+, cut read latency
from milliseconds to microseconds, and defer the need for expensive database scaling by months or years.

The fundamental trade-off in caching is between **freshness** and **speed**. The cache holds a snapshot of data at a
point in time. The longer that snapshot lives (higher TTL), the more likely it is to be stale. The shorter the TTL, the
more frequently the cache must be repopulated from the source of truth — and at the extreme (TTL=0), there's no cache
benefit at all. Designing a caching system means choosing acceptable staleness for each type of data: user session
tokens can't be stale at all; product descriptions can be 5 minutes stale; aggregated analytics can be an hour stale.

Cache effectiveness depends on the **hit rate** — the fraction of requests served from cache rather than the backing
store. A 90% hit rate means 90% of requests never touch the database. A 99% hit rate means 10x fewer DB requests than a
90% hit rate. The hit rate is driven by the ratio of the hot working set (the data frequently accessed) to the cache
size. If your 10GB cache holds the top 1% of accessed data, and that 1% accounts for 80% of requests, your hit rate will
be high. If access is uniformly distributed across a 1TB dataset and your cache is 10GB, the hit rate will be near zero.

Caches are not just for reads. Write patterns also determine cache utility: a write-heavy application may get little
benefit from caching because data changes frequently, making cached copies stale quickly. Caching is most effective for
data that is read much more than it is written (high read-to-write ratio), where the data can tolerate some staleness,
and where the same data is requested by many different clients (high reuse).

## How It Works

```
Cache-aside (most common):

  Client
    │
    ▼
  Application ──► Redis ──► HIT? ──► return cached value
      │               │
      │              MISS
      │               │
      └──────────► Database ──► store in Redis ──► return value
```

**Cache-aside (lazy loading)** — the application owns all cache interactions:

1. Check the cache for the key.
2. On cache hit: return the cached value.
3. On cache miss: query the database, store the result in cache (with a TTL), return the result.

The cache is populated lazily — only when data is first requested. Cold starts have poor hit rates; the cache warms
naturally as requests arrive. The application must handle cache misses gracefully (they result in DB reads, which are
slower but always correct). Cache-aside is resilient: if the cache cluster goes down, the application continues to serve
from the DB (at degraded performance).

**Read-through** — the cache layer itself is responsible for fetching from the DB on a miss. The application talks only
to the cache:

1. Application queries the cache.
2. On hit: cache returns the value.
3. On miss: the cache (not the application) fetches from the DB, stores the result, and returns it.

The application code is simpler — no explicit miss-handling logic. The downside is that the cache becomes a required
dependency; if it fails, the application cannot reach the DB directly. Used in managed caching layers (e.g., AWS
ElastiCache DAX for DynamoDB).

**Write-through** — every write goes to cache AND database synchronously:

1. Client sends a write request.
2. Application writes to the cache.
3. Application writes to the database (both must succeed before acknowledging the client).
4. Subsequent reads always hit the cache and see the latest value.

The cache is always current. Writes are slower (two synchronous round trips). The cache fills with data that may never
be read again (especially for write-heavy workloads with infrequent re-reads of the same key).

**Write-behind (write-back)** — prioritises write throughput at the cost of durability:

1. Client sends a write request.
2. Application writes to the cache and immediately acknowledges the client.
3. A background process asynchronously flushes dirty cache entries to the database in batches.
4. If the cache crashes before flushing, those writes are permanently lost.

Used in CPU L1/L2 caches and for non-critical high-write workloads (analytics counters, view counts) where occasional
data loss is acceptable. Never use for financial or transactional data.

**Eviction policies** determine which key is removed when the cache is full:

| Policy         | When to Use                                                                                               |
|----------------|-----------------------------------------------------------------------------------------------------------|
| `allkeys-lru`  | General-purpose caching; evicts the least-recently-used key across all keys (recommended for most caches) |
| `volatile-lru` | Mixed workload: some keys have TTL (cache), others do not (session store); evicts only TTL-keyed items    |
| `allkeys-lfu`  | Highly skewed access (Zipf distribution); celebrity posts, trending items                                 |
| `volatile-ttl` | Prefer to evict keys closest to expiry first                                                              |
| `noeviction`   | Session stores or rate-limit counters where evicting a key is worse than a write failure                  |

Redis defaults to `noeviction` — appropriate for session stores, catastrophic for a cache. Always configure `maxmemory`
and `maxmemory-policy` explicitly when using Redis as a cache.

**Multi-tier caching** adds a local in-process cache (L1) in front of a shared distributed cache (L2):

```
  Client → App process → L1 (in-process LRU, ~1 ms) → L2 (Redis cluster, ~1-5 ms) → DB (~10-50 ms)
```

L1 is typically a small LRU dict or `functools.lru_cache` with a short TTL (1–5 seconds). It absorbs the majority of
hot-key traffic before it reaches Redis, reducing both network round trips and Redis CPU. The trade-off is an additional
staleness layer: data can be stale for up to L1_TTL + L2_TTL.

**Cache pre-warming** — seeding the cache before traffic arrives:

- After a deploy that flushes the cache, the first wave of production traffic all misses and hits the DB
  simultaneously (a form of stampede).
- Mitigation: replay a sample of recent production request logs against the new cache before switching traffic; or use a
  background worker that loads the predicted hot working set at startup.
- Add TTL jitter during warm-up to prevent the entire pre-warmed set from expiring simultaneously later.

### Trade-offs

| Pattern       | Read Latency             | Write Latency     | Consistency          | Use Case                    |
|---------------|--------------------------|-------------------|----------------------|-----------------------------|
| Cache-aside   | Fast (hit) / Slow (miss) | DB speed          | Eventual (TTL-based) | Most read-heavy apps        |
| Write-through | Fast                     | Slower (2 writes) | Strong               | Frequent reads after writes |
| Write-behind  | Fast                     | Fastest           | Weak (flush risk)    | High-write, non-critical    |
| Read-through  | Fast (hit) / Slow (miss) | DB speed          | Eventual             | Transparent caching         |

### Failure Modes

**Cache stampede (thundering herd):** a popular key expires. Simultaneously, many requests arrive for that key, all miss
the cache, and all query the database. The sudden burst of DB queries can overwhelm it. Solutions: (1) distributed
mutex — only the first request to miss repopulates the cache; others wait. (2) Probabilistic early expiration — randomly
re-cache a key before it expires based on the remaining TTL. (3) Background refresh — a worker continuously refreshes
hot keys before they expire.

**Cache penetration:** requests for keys that don't exist in the database. These always miss the cache (because the
cache never stores the non-existent result) and always hit the database. An attacker or misconfigured client querying
millions of non-existent keys bypasses the cache entirely. Solutions: (1) Cache null results with a short TTL. (2) Use a
Bloom filter — a probabilistic data structure that can definitively answer "this key definitely does NOT exist in the
database" without a DB query.

**Cache avalanche:** many keys expire at the same time, causing a wave of simultaneous cache misses and DB queries. This
happens when keys are created in bulk with the same TTL (e.g., during a cache warm-up or after a deploy that flushes the
cache). Solution: add random jitter to TTLs (`TTL = base_ttl + random(0, jitter)`) so expirations are spread over time.

**Stale data after invalidation:** if a key is updated in the database but the cache is not invalidated, reads return
the old value until the TTL expires. For data where correctness is critical (financial balances, permissions, feature
flags), explicit cache invalidation on write is required. The challenge is consistency between the DB write and the
cache delete — if the process crashes between the two, the cache retains stale data. A safe pattern: delete the cache
key after the DB write (not before), and retry deletion on failure.

**Hot key (skewed access):** a single cache key receives far more traffic than any other — a celebrity's post, a
trending product listing, a global configuration map. Even with 100% cache hit rate, a single Redis node handling
millions of requests per second for one key becomes a CPU and network throughput bottleneck. Solutions: (1) local
in-process L1 cache — absorbs reads at the application level before they reach Redis; staleness bounded by local TTL. (
2) Key sharding — replicate the value under N keys (`config:0` … `config:N-1`) and route readers to
`config:hash(client_id) % N`. (3) Redis read replicas — route hot reads to replicas; writes to primary. (4) For
public/cacheable content, push to CDN edge nodes (zero Redis involvement).

## Interview Talking Points

- "Cache-aside is the most common pattern — app checks cache, miss → query DB, populate cache with a TTL. Cache-aside is
  also resilient: if Redis goes down, the app degrades to DB-only, not a full outage."
- "The hardest problem in caching is invalidation — TTL allows staleness up to TTL seconds; explicit delete-on-write is
  precise but adds a race condition window between the DB write and the cache delete. The safe order is always: write DB
  first, then delete cache key."
- "Cache stampede (thundering herd) occurs when a hot key expires and N concurrent threads all miss and all query the DB
  simultaneously. Fix: Redis SETNX mutex — only the winner repopulates; losers wait and retry from cache. Or use
  probabilistic early expiration (XFetch algorithm) to refresh before expiry."
- "Cache penetration means every request misses cache AND misses DB — the cache provides zero protection. Fix: null
  caching (store a sentinel value) for the common case; Bloom filter for at-scale defence (1 MB Bloom filter can
  represent 10 M IDs at 0.1% false-positive rate)."
- "Cache avalanche is mass simultaneous expiry after a deploy or bulk seeding with uniform TTL — convert a spike of DB
  queries into a ramp by adding ±20–30% random jitter to every TTL."
- "Hot key is a single Redis key receiving millions of req/s — even with 100% hit rate, one node becomes the bottleneck.
  Add an in-process L1 cache (lru_cache with 1–5 s TTL) to absorb the load before it reaches Redis."
- "Redis maxmemory-policy defaults to `noeviction` — appropriate for session stores, catastrophic for a cache. Always
  set `allkeys-lru` (or `allkeys-lfu` for skewed workloads) with an explicit `maxmemory` limit."
- "Write-behind gives the lowest write latency but risks data loss — unacknowledged dirty entries vanish if the cache
  crashes before the async flush. Only use it for non-critical counters or with a persistent write queue (e.g., Kafka)
  acting as a crash-safe buffer."
- "Multi-tier caching (L1 in-process + L2 Redis) dramatically reduces Redis load for hot keys, at the cost of a second
  staleness layer. Design each tier's TTL independently — L1 can be 1–5 s; L2 can be minutes."

## Hands-on Lab

**Time:** ~25-35 minutes
**Services:** postgres (5432), redis (6379) — Redis configured with `maxmemory 32mb` and `allkeys-lru` eviction so it
behaves as a real bounded cache.

### Setup

```bash
cd system-design-interview/01-foundations/06-caching/
docker compose up -d
# Wait ~10 seconds for Postgres and Redis to be ready
```

### Experiment

```bash
python experiment.py
```

The script runs eight phases:

| Phase                 | What it shows                                                                          |
|-----------------------|----------------------------------------------------------------------------------------|
| 1. DB-only baseline   | Raw cost of every read hitting Postgres                                                |
| 2. Cache warming      | Hit rate climbing from 0% to 70–80% as the hot working set warms                       |
| 3. Cache invalidation | Stale data in cache after a DB update; explicit delete fix                             |
| 4. Cache stampede     | 10 concurrent threads for one expired key: N DB queries without mutex vs ~1 with mutex |
| 5. Cache penetration  | Non-existent key hammers DB 20× vs 0× with null caching                                |
| 6. Cache avalanche    | TTL jitter spreads key expiry over time instead of a single burst                      |
| 7. Hot key            | 1000 reads of the same key: local L1 cache vs Redis-only; Redis call reduction         |
| 8. Summary            | Reference table of all patterns, eviction policies, failure modes                      |

### Observe

- Phase 2: hit rate should climb from ~0% to 65–80% as the 20-user hot set gets cached. The bar chart shows the warming
  curve.
- Phase 4: without mutex, expect 8–10 DB queries; with mutex, expect 1–2 (only the lock winner + any fallbacks).
- Phase 6: the expiry timeline shows uniform TTL producing a spike (all keys expiring in one 0.5 s window) vs jittered
  TTL spreading expiry across several seconds.
- Phase 7: local L1 cache reduces Redis round trips by 80–95% for hot-key traffic.

### Teardown

```bash
docker compose down -v
```

## Real-World Examples

- **Facebook Memcache:** Facebook's TAO and Memcached deployment caches social graph data. At peak they had 10,000+
  Memcached servers holding trillions of items. Their "lease" mechanism (similar to mutex) solves cache stampede —
  source: Nishtala et al., "Scaling Memcache at Facebook," NSDI 2013.
- **Twitter (now X) Timeline Caching:** The home timeline is assembled from up to 800 tweets from followed accounts.
  Twitter caches timelines in Redis and "fans out" new tweets to followers' caches on write (write-through). For users
  with millions of followers (celebrities), fanout is too expensive — their tweets are fetched on read instead — source:
  Twitter Engineering, "The Infrastructure Behind Twitter: Scale" (2017).
- **Cloudflare Cache:** Operates 200+ PoPs (Points of Presence) that cache HTTP responses close to users. TTLs are set
  by origin servers via `Cache-Control` headers. Cache invalidation uses a "purge" API that propagates deletions
  globally within seconds — source: Cloudflare Blog, "How Cloudflare's Global Anycast Network Works."

## Common Mistakes

- **Caching everything.** Caching data that changes frequently (e.g., a counter that updates on every request) causes
  constant cache misses or stale reads. Only cache data with a meaningful read-to-write ratio.
- **Not setting TTLs.** Without TTLs, cache entries live forever. After a DB update, reads will return stale data
  indefinitely unless explicitly invalidated. Always set a TTL as a safety net, even when using explicit invalidation.
- **Leaving Redis with the default `noeviction` policy.** Redis defaults to refusing writes when full (`noeviction`). In
  a caching use case, the correct policy is `allkeys-lru` or `allkeys-lfu`. Without this, a full cache causes write
  failures rather than graceful eviction of old entries. Always set `maxmemory` and `maxmemory-policy` explicitly.
- **Ignoring stampede and avalanche.** In low-traffic development environments, these rarely manifest. At scale they can
  take down the database within seconds of a cache flush. Design stampede and avalanche protection from the start.
- **Not accounting for the hot key problem.** A single Redis node handling millions of req/s for one key (a trending
  post, a global config) becomes a CPU and network bottleneck even with 100% hit rate. Add a local in-process L1 cache
  in front of Redis for keys with extremely skewed access.
- **Caching financial or permission data without explicit invalidation.** A user whose account is suspended should have
  their session/permission cache invalidated immediately. Relying on TTL expiry for security-critical data means they
  retain access for up to TTL seconds after revocation.
- **Deleting the cache key before the DB write.** If the process crashes after the cache delete but before the DB write,
  the cache is empty and the DB has the old value. The next read repopulates the cache with stale DB data. Always write
  the DB first, then delete the cache key.
- **Using the database as a cache.** Selecting data into a Redis set/hash and then running queries against it
  re-implements what a database already does, without the query planner, indexes, or ACID guarantees. Use Redis for
  simple key-value lookups; use the DB for complex queries.
