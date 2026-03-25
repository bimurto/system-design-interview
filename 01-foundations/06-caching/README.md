# Caching

**Prerequisites:** `../05-partitioning-sharding/`
**Next:** `../07-load-balancing/`

---

## Concept

Caching is the practice of storing copies of frequently accessed data in a faster storage tier so that future requests can be served without recomputing or re-fetching the data from the slower source of truth. It is one of the highest-leverage techniques in systems design: a well-placed cache can reduce database load by 90%+, cut read latency from milliseconds to microseconds, and defer the need for expensive database scaling by months or years.

The fundamental trade-off in caching is between **freshness** and **speed**. The cache holds a snapshot of data at a point in time. The longer that snapshot lives (higher TTL), the more likely it is to be stale. The shorter the TTL, the more frequently the cache must be repopulated from the source of truth — and at the extreme (TTL=0), there's no cache benefit at all. Designing a caching system means choosing acceptable staleness for each type of data: user session tokens can't be stale at all; product descriptions can be 5 minutes stale; aggregated analytics can be an hour stale.

Cache effectiveness depends on the **hit rate** — the fraction of requests served from cache rather than the backing store. A 90% hit rate means 90% of requests never touch the database. A 99% hit rate means 10x fewer DB requests than a 90% hit rate. The hit rate is driven by the ratio of the hot working set (the data frequently accessed) to the cache size. If your 10GB cache holds the top 1% of accessed data, and that 1% accounts for 80% of requests, your hit rate will be high. If access is uniformly distributed across a 1TB dataset and your cache is 10GB, the hit rate will be near zero.

Caches are not just for reads. Write patterns also determine cache utility: a write-heavy application may get little benefit from caching because data changes frequently, making cached copies stale quickly. Caching is most effective for data that is read much more than it is written (high read-to-write ratio), where the data can tolerate some staleness, and where the same data is requested by many different clients (high reuse).

## How It Works

**Cache-aside (lazy loading)** is the most common pattern. The application code is responsible for all cache interactions:
1. Check the cache for the key.
2. On cache hit: return the cached value.
3. On cache miss: query the database, store the result in cache (with a TTL), return the result.

The cache is populated lazily — only when data is first requested. This means cold starts have poor hit rates. The cache fills naturally as requests arrive. The application must handle cache misses gracefully (they result in DB reads, which are slower but always correct).

**Write-through** writes data to both the cache and the database synchronously on every write. The cache is always current with the database. Reads are always fast (no misses for recently written data). The cost is that writes are slower (must write to two places) and the cache fills with data that may never be read again.

**Write-behind (write-back)** writes data to the cache and acknowledges to the client immediately. The write to the database happens asynchronously in the background. This is extremely fast for writes but introduces a durability risk: if the cache crashes before flushing to the database, the write is lost. Used in CPU L1/L2 caches (hence "write-back"), and in some application-level caches for non-critical data.

**Eviction policies** determine which keys are removed when the cache is full. LRU (Least Recently Used) evicts the key that was accessed least recently — the assumption is that recently accessed data will be accessed again soon. LFU (Least Frequently Used) evicts the key accessed fewest times — better for skewed access where a small number of keys are extremely hot. TTL eviction simply expires keys after a fixed time regardless of access frequency.

### Trade-offs

| Pattern | Read Latency | Write Latency | Consistency | Use Case |
|---------|-------------|---------------|-------------|----------|
| Cache-aside | Fast (hit) / Slow (miss) | DB speed | Eventual (TTL-based) | Most read-heavy apps |
| Write-through | Fast | Slower (2 writes) | Strong | Frequent reads after writes |
| Write-behind | Fast | Fastest | Weak (flush risk) | High-write, non-critical |
| Read-through | Fast (hit) / Slow (miss) | DB speed | Eventual | Transparent caching |

### Failure Modes

**Cache stampede (thundering herd):** a popular key expires. Simultaneously, many requests arrive for that key, all miss the cache, and all query the database. The sudden burst of DB queries can overwhelm it. Solutions: (1) distributed mutex — only the first request to miss repopulates the cache; others wait. (2) Probabilistic early expiration — randomly re-cache a key before it expires based on the remaining TTL. (3) Background refresh — a worker continuously refreshes hot keys before they expire.

**Cache penetration:** requests for keys that don't exist in the database. These always miss the cache (because the cache never stores the non-existent result) and always hit the database. An attacker or misconfigured client querying millions of non-existent keys bypasses the cache entirely. Solutions: (1) Cache null results with a short TTL. (2) Use a Bloom filter — a probabilistic data structure that can definitively answer "this key definitely does NOT exist in the database" without a DB query.

**Cache avalanche:** many keys expire at the same time, causing a wave of simultaneous cache misses and DB queries. This happens when keys are created in bulk with the same TTL (e.g., during a cache warm-up or after a deploy that flushes the cache). Solution: add random jitter to TTLs (`TTL = base_ttl + random(0, jitter)`) so expirations are spread over time.

**Stale data after invalidation:** if a key is updated in the database but the cache is not invalidated, reads return the old value until the TTL expires. For data where correctness is critical (financial balances, permissions, feature flags), explicit cache invalidation on write is required. The challenge is consistency between the DB write and the cache delete — if the process crashes between the two, the cache retains stale data. A safe pattern: delete the cache key after the DB write (not before), and retry deletion on failure.

## Interview Talking Points

- "Cache-aside (lazy loading) is the most common pattern — check cache, miss → query DB, populate cache. The app is responsible for cache management"
- "The hardest problem in caching is cache invalidation — knowing when to expire a key. TTL is simple but allows staleness; explicit invalidation is precise but adds complexity"
- "Cache stampede occurs when a hot key expires and many concurrent requests race to repopulate — use a distributed mutex or probabilistic early expiration"
- "Cache penetration means every request misses cache AND misses DB — use null caching or a Bloom filter for non-existent keys"
- "A cache hit rate below 80% often means the working set is larger than the cache — add more cache capacity or accept the DB load"
- "Write-behind is dangerous — unacknowledged data can be lost on cache failure. Only use it for non-critical data or with a persistent queue"

## Hands-on Lab

**Time:** ~20-30 minutes
**Services:** postgres (5432), redis (6379)

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

The script installs `redis` and `psycopg2-binary` if needed, then runs five phases: baseline DB-only read timing, cache warming with hit rate tracking over 200 requests with 80/20 access pattern, cache invalidation (stale value after DB update, then explicit delete), cache stampede simulation with/without mutex (showing DB query count difference), and cache penetration demonstration with null caching.

### Break It

```bash
# Simulate cache avalanche: seed the cache with many keys, all same TTL
python -c "
import redis, time
r = redis.Redis(host='localhost', port=6379, decode_responses=True)

# Populate 100 keys with the same TTL (10 seconds)
print('Setting 100 keys with identical TTL=10s...')
for i in range(100):
    r.setex(f'product:{i}', 10, f'data_{i}')

print('Waiting 10 seconds for simultaneous expiry...')
time.sleep(10)

# Now ALL keys expire at once — every request misses cache
count = 0
for i in range(100):
    if r.get(f'product:{i}') is None:
        count += 1
print(f'{count}/100 keys expired simultaneously — this is cache avalanche')
print('Fix: r.setex(key, base_ttl + random.randint(0, 30), value)')
"
```

### Observe

The hit rate table shows 0% initially (all cold misses), climbing to 60-80% as the hot set of 20 users gets cached. The stampede section should show 10 DB queries without mutex vs ~1 DB query with mutex. The penetration section shows significantly fewer DB roundtrips with null caching.

### Teardown

```bash
docker compose down -v
```

## Real-World Examples

- **Facebook Memcache:** Facebook's TAO and Memcached deployment caches social graph data. At peak they had 10,000+ Memcached servers holding trillions of items. Their "lease" mechanism (similar to mutex) solves cache stampede — source: Nishtala et al., "Scaling Memcache at Facebook," NSDI 2013.
- **Twitter (now X) Timeline Caching:** The home timeline is assembled from up to 800 tweets from followed accounts. Twitter caches timelines in Redis and "fans out" new tweets to followers' caches on write (write-through). For users with millions of followers (celebrities), fanout is too expensive — their tweets are fetched on read instead — source: Twitter Engineering, "The Infrastructure Behind Twitter: Scale" (2017).
- **Cloudflare Cache:** Operates 200+ PoPs (Points of Presence) that cache HTTP responses close to users. TTLs are set by origin servers via `Cache-Control` headers. Cache invalidation uses a "purge" API that propagates deletions globally within seconds — source: Cloudflare Blog, "How Cloudflare's Global Anycast Network Works."

## Common Mistakes

- **Caching everything.** Caching data that changes frequently (e.g., a counter that updates on every request) causes constant cache misses or stale reads. Only cache data with a meaningful read-to-write ratio.
- **Not setting TTLs.** Without TTLs, cache entries live forever. After a DB update, reads will return stale data indefinitely unless explicitly invalidated. Always set a TTL as a safety net, even when using explicit invalidation.
- **Ignoring stampede and avalanche.** In low-traffic development environments, these rarely manifest. At scale they can take down the database within seconds of a cache flush. Design stampede and avalanche protection from the start.
- **Caching financial or permission data without explicit invalidation.** A user whose account is suspended should have their session/permission cache invalidated immediately. Relying on TTL expiry for security-critical data means they retain access for up to TTL seconds after revocation.
- **Using the database as a cache.** Selecting data into a Redis set/hash and then running queries against it re-implements what a database already does, without the query planner, indexes, or ACID guarantees. Use Redis for simple key-value lookups; use the DB for complex queries.
