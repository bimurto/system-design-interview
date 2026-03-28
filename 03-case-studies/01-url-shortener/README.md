# Case Study: URL Shortener

**Prerequisites:** `../../01-foundations/05-partitioning-sharding/`, `../../01-foundations/06-caching/`,
`../../01-foundations/09-indexes/`

---

## The Problem at Scale

A URL shortener like bit.ly converts long URLs into 6–8 character codes and redirects users at high speed. The
read/write asymmetry is extreme:

| Metric            | Value                                          |
|-------------------|------------------------------------------------|
| Total URLs stored | 100 million                                    |
| Daily redirects   | 10 billion                                     |
| Peak read RPS     | 115,000 (sustained); up to 580,000 at 5× burst |
| Peak write RPS    | 1,150 (sustained); up to 3,500 at 3× burst     |
| Read:write ratio  | 100:1                                          |

At 115K RPS, a single Postgres server would be saturated by reads alone (typical: 5–10K RPS with index lookups). The
solution is a read cache in Redis that absorbs 99%+ of traffic.

---

## Clarifying Requirements (3–5 min)

Before jumping to design, force agreement on scope. These are the questions an interviewer expects you to ask:

| Question                                                      | Why it matters                                                                                      |
|---------------------------------------------------------------|-----------------------------------------------------------------------------------------------------|
| Who creates short URLs — authenticated users or anonymous?    | Auth adds a user table, per-user quotas, and ownership-based deletion                               |
| Does the same long URL always produce the same short code?    | Dedup on input URL changes the write path (lookup before insert)                                    |
| What is the expected URL volume and redirect traffic?         | Drives sharding, caching tier size, and CDN strategy                                                |
| Do shortened URLs expire?                                     | Adds `expires_at` column, background cleanup job, and cache TTL logic                               |
| Is analytics required (click counts, geo, device)?            | Drives analytics pipeline complexity (Redis INCR vs. Kafka streaming)                               |
| Does the system need to support custom aliases (`/my-brand`)? | Custom codes bypass the auto-increment path and need conflict detection                             |
| What is the availability SLA?                                 | Determines whether a single Postgres region is acceptable or multi-region active-active is required |
| Should we detect malicious URLs (phishing, malware)?          | Adds a Safe Browsing API integration on the write path                                              |

**Scope for this design:** anonymous shortening, no input URL dedup, 100M stored URLs, 10B redirects/day, optional
expiry, click-count analytics, custom aliases, 99.99% read availability. No real-time geo analytics for this pass.

---

## Requirements

### Functional

- Shorten a long URL to a unique 6–8 character code
- Redirect `GET /<code>` to the original URL in <10ms
- Support custom short codes (e.g., `/sale` → marketing campaign URL)
- Track redirect counts per URL
- Expire URLs after a configurable TTL (optional)

### Non-Functional

- 99.99% availability for reads (redirects must work even if write path degrades)
- Redirect latency P99 < 10ms globally (CDN edge serving)
- Codes must be URL-safe (no special characters)
- No enumeration attacks: codes must not be sequentially guessable without mitigation

### Capacity Estimation

Back-of-envelope math for the design justification — do this explicitly in the interview.

| Metric                         | Calculation                                              | Result                    |
|--------------------------------|----------------------------------------------------------|---------------------------|
| Write RPS (sustained)          | 100M URLs / (3 years × 365d × 86400s)                    | ~1,060 RPS                |
| Write RPS (peak, 3× burst)     | 1,060 × 3                                                | ~3,200 RPS                |
| Read RPS (sustained)           | 10B redirects / 86400s                                   | ~115,750 RPS              |
| Read RPS (peak, 5× burst)      | 115,750 × 5                                              | ~580,000 RPS              |
| Read:write ratio               | —                                                        | 100:1                     |
| Storage per URL                | URL avg 200B + short_code 8B + metadata + index overhead | ~500 B                    |
| Total DB storage               | 100M × 500B                                              | ~50 GB                    |
| Storage growth rate            | 1,060 writes/s × 500B × 86400s                           | ~46 GB/year               |
| Outbound bandwidth (redirects) | 115K × 500B (302 header response)                        | ~57 MB/s                  |
| Redis working set              | Top 10M hot URLs × 300B/entry                            | ~3 GB                     |
| Cache hit rate target          | 99%+                                                     | Postgres sees < 1,150 RPS |

**Key insight for the interview:** 50 GB fits comfortably on a single Postgres node with read replicas. Sharding is not
needed until ~1B URLs (~500 GB). The bottleneck is reads (115K RPS), not storage — solved by Redis caching, not by
partitioning.

---

## High-Level Architecture

```
                    ┌──────────────────────────────────────────┐
Clients             │          Application Tier                 │
  │                 │                                          │
  ├─ POST /shorten ─►  Flask API  ──► Postgres (write)        │
  │                 │     │                                    │
  └─ GET /<code> ──►      │   ◄── Redis (cache-aside, 24h TTL)│
                    │     │                                    │
                    │     └──► Postgres (cache miss only)      │
                    └──────────────────────────────────────────┘
                              │
                    ┌─────────▼──────────┐
                    │   Postgres         │  ← source of truth
                    │   (urls table,     │
                    │    index on code)  │
                    └────────────────────┘
```

**Postgres** is the source of truth. It stores all URL mappings with a UNIQUE index on `short_code` for O(log n) lookups
and collision detection. Why Postgres and not a NoSQL store? The data is relational (URL + metadata), the volume (50 GB)
fits comfortably in a single node with read replicas, and ACID guarantees prevent duplicate code assignment.

**Redis** acts as a cache-aside layer for reads. On a redirect, the app checks Redis first (O(1) GET). On a miss, it
queries Postgres and writes the result back to Redis with a 24-hour TTL. At steady state, 99%+ of redirects are served
from Redis without touching Postgres. Why cache-aside (not write-through)? On a URL write, we only invalidate if
needed — most URLs are written once and read many times. Write-through would warm every new URL into cache, wasting
memory for URLs that are never clicked.

**Flask** is a thin routing layer. All business logic fits in ~100 lines. In production this would be multiple stateless
instances behind a load balancer.

---

## Deep Dives

### 1. ID Generation: Hash vs Sequential vs Snowflake

**The core question:** how do we turn a long URL into a short, unique code?

**Option A: Hash the URL (MD5/SHA truncated)**
Take `md5(url)[:7]` and use as the code. Pros: stateless, no DB round-trip to generate. Cons: birthday paradox. With 7
hex chars (268M possibilities), collision probability reaches 1% at ~52K URLs — unacceptable. Mitigation: detect
collision, append a counter, rehash. This adds complexity and latency.

**Option B: Sequential auto-increment + base62 encode**
Insert the URL, take the auto-increment ID, encode as base62. ID 1 → `"1"`, ID 1000 → `"g8"`, ID 1 billion → `"15FTGg"`.
Pros: zero collisions, compact, fast. Cons: predictable (sequential codes can be enumerated by crawlers). In practice,
bit.ly uses this approach but adds a private salt or epoch offset to obscure the sequence.

**Option C: Snowflake-style ID (Twitter's approach)**
64-bit integer: `timestamp_ms (41b) | datacenter_id (5b) | machine_id (5b) | sequence (12b)`. Encode as base62. Pros:
globally unique, no coordination, time-sortable, unpredictable without knowing the epoch. Cons: requires clock
synchronisation; clock drift on one machine can produce duplicate IDs (mitigated by refusing to generate IDs while clock
is behind last-seen timestamp).

**Production choice:** most large systems use option B (sequential) with a base62 encoding, accepting that codes are
technically guessable but adding rate limiting to prevent bulk enumeration.

| Strategy            | Collisions     | Predictable | Distributed         | Notes                       |
|---------------------|----------------|-------------|---------------------|-----------------------------|
| Hash (truncated)    | Yes (birthday) | No          | Yes                 | Needs collision retry loop  |
| Sequential + base62 | None           | Yes         | No (single counter) | Simple, fast, most common   |
| Snowflake + base62  | None           | No          | Yes                 | Best for multi-region write |

### 2. Redirect Latency: Redis Cache → CDN Edge

At 115K RPS, even Redis becomes a bottleneck if all traffic hits a single region. The solution is CDN edge caching:

1. **Redis (data center):** cache-aside, TTL 24h. Serves ~99% of traffic within a single region. P99 latency ~1ms.
2. **CDN edge (global):** popular URLs (top 0.1%) are cached at CDN PoPs (Points of Presence). Redirect responses
   include `Cache-Control: max-age=3600`. A user in Tokyo gets the redirect from Tokyo's CDN PoP without crossing the
   ocean. P99 latency ~5ms globally.

The trade-off: CDN caching means a URL can't be deleted or changed instantly (cached at edge for up to 1 hour). For "
custom URL campaigns" that need immediate updates, use `Cache-Control: no-store` or `s-maxage=0` and eat the extra
latency.

### 3. Analytics at Scale: Redis Counter + Batch Flush

Naive approach: `UPDATE urls SET hits = hits + 1 WHERE code = ?` on every redirect. At 115K RPS this is 115,000
row-locking UPDATE statements per second — Postgres will fall over.

**Production approach:**

1. `INCR hit_count:{code}` in Redis on every redirect (O(1), atomic, in-memory)
2. A background worker reads Redis counters every 60 seconds and bulk-flushes to Postgres with
   `UPDATE urls SET hits = hits + $delta WHERE code = $code`
3. Redis counter is reset (or decremented) after flush

This converts 115K writes/second (per-redirect DB writes) into ~1 batch write per minute. The trade-off: hit counts are
approximate (up to 60 seconds stale). For a URL shortener, this is perfectly acceptable. For billing or fraud detection,
you'd use a stronger consistency model.

### 4. Failure Modes and Resilience

This is often where senior/staff interview questions go deep. Know each failure scenario before the interview.

**Scenario A: Redis goes down (cache layer unavailable)**

Every redirect falls through to Postgres. With 115K sustained RPS and Postgres handling maybe 10K RPS at indexscan
speed, requests will queue and then time out. The thundering herd makes it worse: when Redis restarts with an empty
cache, all in-flight requests simultaneously miss and hammer Postgres.

Mitigations:

- **Request coalescing (mutex/singleflight):** only the first goroutine/thread for a given cache key issues a Postgres
  query; the rest wait and share the result. Reduces fan-out from N concurrent misses to 1 per key.
- **Cache warm-up on restart:** a startup script pre-populates the top N hot URLs by querying
  `SELECT short_code, original FROM urls ORDER BY hits DESC LIMIT 100000` before opening traffic.
- **Circuit breaker on Redis:** if Redis is unhealthy, route reads directly to Postgres read replicas (degraded mode),
  shedding non-critical traffic via load shedding to stay under DB capacity.
- **Redis Sentinel / Cluster:** automatic failover to a replica in <30s. Redis Cluster shards keys across nodes,
  eliminating single-node SPOF.

**Scenario B: Postgres primary goes down (write path unavailable)**

Reads continue if Redis is warm — this is why the 99.99% SLA applies to reads only. Writes fail until the replica is
promoted (streaming replication lag determines data loss window, typically <1s with synchronous_commit=on).

For zero-RPO on writes, use a synchronous replica (`synchronous_commit = remote_apply`) at the cost of ~1ms added write
latency.

**Scenario C: Hot key (viral link)**

A single short code receives 100K+ RPS (e.g., a viral tweet with a bit.ly link). Redis handles high single-key read
throughput well (~100K GET/s on a single shard), but a single Redis shard at that rate risks CPU saturation.

Mitigations:

- **CDN edge caching:** the most effective solution. A viral URL served from CDN PoPs means 0 requests reach Redis or
  Postgres.
- **Local in-process cache (L1):** each app server caches the top 1,000 codes in a local LRU dict with a 5-second TTL.
  Eliminates Redis round-trips entirely for the hottest keys.
- **Redis read replicas:** replicate the keyspace across multiple Redis read nodes; route reads via consistent hashing.

**Scenario D: Redirect loop / malicious URL**

A short URL pointing to another short URL on the same domain creates a redirect loop. Detect this at write time: before
inserting, fetch the first redirect of the submitted URL (with a 1-hop HEAD request) and reject if it resolves to the
same domain. Additionally, integrate Google Safe Browsing API: the write path submits the URL asynchronously and marks
it `is_flagged = true` if it returns a malware/phishing match.

**Scenario E: Thundering herd after schema migration or cache flush**

During a planned maintenance window (e.g., Redis FLUSHALL before upgrading), the first wave of requests post-maintenance
will miss the cache entirely and concentrate on Postgres. Mitigation: use a cache pre-warming script before cutting
traffic, and use a gradual rollout (shift 5% of traffic initially, monitor Postgres connection count, then increase).

---

## How It Actually Works

**bit.ly** uses a very similar architecture. From their engineering blog (2014): URLs are stored in a MySQL cluster
sharded by short code prefix. A Memcached layer caches the mapping (short code → long URL) with a 24-hour TTL.
Sequential IDs are base62-encoded. Analytics events are streamed to Kafka and batch-aggregated hourly into their
analytics store.

**TinyURL** (simpler, older) uses a MySQL single-master with auto-increment IDs and base36 encoding. No caching layer —
possible because TinyURL has lower traffic than bit.ly.

Key difference from this lab: production systems add **custom domains** (your-brand.co/abc), **URL expiry** (delete
after 30 days), and **abuse detection** (block URLs pointing to malware or phishing sites via Google Safe Browsing API
integration).

Source: bit.ly Engineering Blog, "Lessons Learned from Building Bit.ly" (2014); Aditi Mittal, "System Design: URL
Shortener" (High Scalability, 2023).

---

## Hands-on Lab

**Time:** ~20–25 minutes
**Services:** `db` (Postgres 15), `cache` (Redis 7), `web` (Python/Flask)

### Setup

```bash
cd system-design-interview/03-case-studies/01-url-shortener/
docker compose up -d
# Services take ~15s to become healthy
docker compose ps   # wait until web shows healthy
```

### Experiment

```bash
python experiment.py
```

The script runs eight phases automatically:

1. **Bulk create:** POST 1,000 URLs, measure throughput, inspect generated codes
2. **Cache latency:** request same URLs cold (Postgres) then warm (Redis), compare P50/P99
3. **Base62 demo:** show how sequential IDs 1–100M map to increasingly longer codes
4. **Cache warm-up curve:** 500 requests with Zipf distribution, watch hit rate climb from 0% to 80%+
5. **Hit counting:** Redis INCR vs naive per-redirect Postgres UPDATE
6. **ID strategies:** live demonstration of hash, sequential, and Snowflake-style ID generation
7. **Birthday paradox:** calculate collision probability for hash-based IDs at various URL counts
8. **Thundering herd:** flush Redis cache, fire concurrent redirects, measure latency spike and recovery

### Break It

**Force a cache miss storm:** restart Redis to evict all cached entries, then hit the service with concurrent redirects.
Observe Postgres query rate spike:

```bash
# Terminal 1: watch Postgres connections
docker compose exec db psql -U app urlshortener -c "SELECT count(*) FROM pg_stat_activity WHERE state='active';"

# Terminal 2: flush Redis cache
docker compose exec cache redis-cli FLUSHALL

# Terminal 3: rapid redirects (all go to Postgres now)
python -c "
import urllib.request, threading, time
def hit(i):
    try: urllib.request.urlopen('http://localhost:5001/1', timeout=2)
    except: pass
threads = [threading.Thread(target=hit, args=(i,)) for i in range(50)]
[t.start() for t in threads]
[t.join() for t in threads]
print('Done')
"
# Re-run Postgres query: connection count will spike during cache miss storm
```

### Observe

After the flush, the first 50 concurrent requests all hit Postgres simultaneously. Re-check cache stats:

```bash
curl http://localhost:5001/stats
```

Hit rate drops to 0%, then climbs back toward 100% as Redis repopulates. This is the "thundering herd" problem —
mitigated in production by request coalescing (only one backend request per cache key at a time, others wait) or by
pre-warming the cache on Redis restart.

### Teardown

```bash
docker compose down -v
```

---

## Interview Checklist

1. **Q: How do you estimate the storage requirements for 100M URLs?**
   A: Each URL record: ~200B URL + 50B metadata + index overhead ≈ 500B. 100M × 500B = 50GB. Fits on a single Postgres
   instance; no sharding required initially. At 1B URLs, split by hash prefix across 10 shards.

2. **Q: Why use base62 instead of UUID for short codes?**
   A: UUIDs are 36 characters — too long for a "short" URL. Base62 encodes a 64-bit integer into 6–11 URL-safe
   characters. Sequential IDs give the minimum-length encoding (ID 1 = `"1"`, ID 56 billion = `"NzGFn4"`). Base62 avoids
   `+` and `/` (base64 special chars) which would require URL-encoding.

3. **Q: How do you handle custom short codes (e.g., `/my-brand`)?**
   A: Store custom codes in the same `urls` table with a flag `is_custom = true`. The UNIQUE constraint on `short_code`
   prevents conflicts. On write, check if the requested code exists first (SELECT FOR UPDATE), then INSERT. Custom codes
   bypass the auto-increment ID path entirely.

4. **Q: What happens when a URL expires?**
   A: Add an `expires_at TIMESTAMPTZ` column. On redirect, check `NOW() < expires_at`. If expired, return 410 Gone. A
   background job (cron) `DELETEs` expired rows nightly to keep the table small. Redis TTL should be set to
   `MIN(24h, time_until_expiry)` so cache entries don't outlive the URL.

5. **Q: How do you scale the analytics pipeline?**
   A: Per-redirect `UPDATE` would saturate Postgres at high RPS. Use Redis `INCR` to count redirects in memory, then a
   background worker flushes to Postgres every 60 seconds. For real-time analytics dashboards, stream click events to
   Kafka → Flink/Spark aggregation → OLAP store (ClickHouse). The click log enables segmentation by geography, device,
   referrer.

6. **Q: What if a URL goes viral and all traffic hits the same short code?**
   A: This is the "hot key" problem. Redis handles high single-key read throughput well (~100K GET/s per shard). If
   viral traffic exceeds Redis capacity, replicate that specific key across multiple Redis nodes (key replication or
   read replicas). CDN caching is more effective: a viral URL cached at CDN PoPs means 0 requests reach the origin. For
   extreme cases, add a local in-process LRU cache (L1) per app server for the top 1,000 codes with a 5-second TTL —
   this eliminates the Redis round-trip entirely for the hottest keys.

7. **Q: How would you shard Postgres when 50GB is no longer enough?**
   A: Shard by short code prefix (first 1–2 characters, 62 values → 62 shards). Each shard owns a range of codes. A
   routing layer hashes the code to find the correct shard. Alternatively, shard by user (each user's URLs on one shard)
   for multi-tenant deployments. Range sharding on code is simpler to reason about but requires careful capacity
   planning.

8. **Q: What is your cache eviction strategy?**
   A: Redis configured with `maxmemory-policy allkeys-lru`. When memory is full, Redis evicts the least-recently-used
   key. This naturally keeps "hot" URLs (frequently accessed) in cache and evicts stale ones. Set `maxmemory` to leave
   20% headroom so Redis doesn't thrash. Monitor `evicted_keys` metric in Redis INFO — if it spikes, add more memory or
   reduce TTL.

9. **Q: How do you prevent abuse (link spam, malware)?**
   A: Rate-limit writes by IP (token bucket: 10 shortens/minute per IP, tracked in Redis). Check submitted URLs against
   Google Safe Browsing API before shortening. Scan for redirect chains (a short URL pointing to another short URL
   pointing to malware). Store a `is_flagged` boolean; flagged URLs return 451 Unavailable for Legal Reasons.

10. **Q: Walk me through the complete flow when a user clicks a short link.**
    A: (1) Browser sends `GET /abc123` to CDN edge. (2) If CDN has it cached (`Cache-Control: max-age=3600`), CDN
    responds with 302 directly — origin never sees the request. (3) On CDN miss, request reaches load balancer → Flask
    instance. (4) Flask checks Redis for `url:abc123`. (5) Redis hit: return 302 with original URL, set `Cache-Control`
    header. (6) Redis miss: query Postgres by index scan on `short_code`. (7) Populate Redis with 24h TTL. (8) Return
    302. Total latency: CDN hit <5ms, Redis hit <10ms, Postgres miss <50ms.

11. **Q: What happens to reads if Redis goes down?**
    A: All redirects fall through to Postgres. At 115K RPS, Postgres (capacity ~10K RPS) will be immediately
    overwhelmed. The thundering herd compounds this: when Redis restarts with an empty cache, all in-flight requests
    simultaneously miss. Mitigations in order of effectiveness: (1) Redis Sentinel/Cluster for automatic failover; (2)
    request coalescing so only one thread queries Postgres per cache key; (3) cache warm-up script that pre-populates
    the top 100K hot URLs from Postgres before opening traffic; (4) circuit breaker that sheds non-critical traffic to
    stay under DB capacity.

12. **Q: How do you handle the thundering herd after a planned cache flush?**
    A: Never flush during peak traffic. Use a rolling warm-up: before cutting traffic post-maintenance, run
    `SELECT short_code, original FROM urls ORDER BY hits DESC LIMIT 100000` and pre-populate Redis. Then gradually shift
    traffic (5% → 20% → 100%), watching Postgres `pg_stat_activity` for connection saturation at each step.
