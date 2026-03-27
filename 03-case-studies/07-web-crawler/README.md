# Case Study: Web Crawler

**Prerequisites:** `../../01-foundations/05-partitioning-sharding/`, `../../02-advanced/09-rate-limiting-algorithms/`, `../../02-advanced/15-probabilistic-data-structures/`

---

## The Problem at Scale

A web crawler must systematically download and index the entire public web. Google's crawler (Googlebot) must keep an index of ~5 billion pages fresh, requiring continuous re-crawling on a ~2-week cycle.

| Metric | Value |
|---|---|
| Total web pages | 5 billion |
| Re-crawl cycle | 14 days |
| Pages/day required | 400 million |
| Pages/second required | ~4,600 |
| Average page size | 50 KB |
| Bandwidth required | ~230 MB/s sustained |
| URL frontier size | 5B+ URLs |
| Unique domains | ~200 million |

A naive single-threaded crawler fetching one page per second would take 158 years to crawl 5B pages. The system must be massively distributed, deduplicated, and polite.

---

## Requirements

### Functional
- Discover and download all publicly accessible web pages starting from seed URLs
- Extract links from downloaded pages and add new URLs to the frontier
- Re-crawl pages periodically based on freshness/importance
- Respect robots.txt and per-domain crawl delays
- Store raw HTML and extracted metadata for downstream indexing

### Non-Functional
- Crawl 400M pages/day without overloading target servers
- Deduplicate URLs: never fetch the same URL twice per cycle
- Scale to 5B URLs in the frontier without running out of memory
- Detect and escape crawler traps (infinite link graphs)
- Operate without human intervention for weeks at a time

### Capacity Estimation

| Metric | Calculation | Result |
|---|---|---|
| Pages/second | 400M / 86,400s | ~4,630 RPS |
| Inbound bandwidth | 4,630 × 50KB | ~225 MB/s |
| URL frontier RAM (hash set) | 5B × 64B/URL | ~320 GB |
| URL frontier RAM (Bloom filter) | 5B, 1% FPR | ~6 GB |
| Raw HTML storage | 5B × 50KB | ~250 TB |
| S3 storage cost | 250TB × $0.023/GB/month | ~$5,750/month |
| Crawler workers | 4,630 RPS / 10 RPS per worker | ~463 workers |
| Postgres write IOPS | 4,630 metadata rows/sec | ~4,630 IOPS |
| robots.txt fetches | 200M domains / 24h TTL | ~2.3 RPS (cached) |
| DNS lookups | dominated by cache hits (5-min TTL) | ~50–100 RPS |

**Overhead that candidates miss:** Every page fetch also requires a DNS lookup (cached, ~5-min TTL) and a robots.txt check (cached per domain per session). At 200M unique domains even a 24-hour cache TTL requires re-fetching 2,300+ robots.txt files per second on re-crawl cycles. The metadata database must absorb 4,630+ IOPS sustained — this pushes Postgres to its single-node write limit; Cassandra or DynamoDB is better suited for this write pattern at Google scale.

---

## High-Level Architecture

```
  Seed URLs
      │
      ▼
┌─────────────────────────────────────────────────────────────────┐
│                    URL Frontier                                   │
│  Kafka topics by priority tier:                                  │
│    high-priority  (news, trending, recently updated)             │
│    med-priority   (known sites, regular refresh cycle)           │
│    low-priority   (new/unknown domains, deep pages)              │
└──────────────────────┬──────────────────────────────────────────┘
                       │ partitioned by domain hash
          ┌────────────▼────────────┐
          │    Crawler Workers       │  (100s of instances)
          │  1. Check robots.txt     │
          │  2. Apply politeness     │
          │  3. Fetch HTML           │
          │  4. Extract links        │
          │  5. Normalize URLs       │
          └─────┬──────────┬────────┘
                │          │
     ┌──────────▼──┐  ┌────▼──────────────┐
     │ Bloom Filter │  │  Content Store     │
     │ (Redis/Spark)│  │  (S3/GCS raw HTML) │
     │ "seen this?" │  │  + metadata in DB  │
     └──────────────┘  └────────────────────┘
```

**URL Frontier:** A priority queue of URLs to crawl. Implemented as Kafka topics (one per priority tier), partitioned by domain hash so each crawler worker "owns" a set of domains and can enforce per-domain rate limiting locally.

**Bloom Filter:** A probabilistic set answering "have we seen this URL?". At 5B URLs a hash set costs ~320GB; a Bloom filter with 1% FPR costs ~6GB. False positives (occasionally skipping a valid new URL) are acceptable.

**Crawler Workers:** Stateless Python/Go processes. Each worker pulls URLs from its assigned Kafka partitions, checks robots.txt (cached per domain), waits for politeness delay, fetches, and emits links back to the frontier.

**Content Store:** Raw HTML goes to object storage (S3). Structured metadata (URL, fetch time, status, content hash) goes to Postgres/Cassandra for the indexing pipeline to consume.

---

## Deep Dives

### 1. URL Frontier: Priority Queue at Scale

The frontier is not a simple FIFO queue. Pages have different importance:

- **News articles** should be re-crawled within minutes of publication
- **Static pages** (about us, contact) can be re-crawled monthly
- **New URLs** discovered for the first time get low priority until PageRank is estimated

**Naive approach:** a single Redis sorted set keyed by priority score. Fails at 5B URLs — a sorted set of 5B members needs ~400GB RAM.

**Production approach (CommonCrawl/Google):** Kafka topics per priority tier. Each topic is partitioned by `hash(domain)`. A crawler worker consumes from one partition, so it handles all URLs for a set of domains. This gives:
1. Natural per-domain rate limiting (one worker per domain shard)
2. Persistent, replayable frontier (Kafka retains unprocessed URLs)
3. Easy re-prioritization (produce to a higher-priority topic)

URL freshness scoring:
```
priority_score = PageRank(url) × recency_factor(last_crawl) × change_frequency(url)
```

### 2. Deduplication: Bloom Filter for 5B URLs

A hash set of 5B 64-byte URL strings = 320GB RAM. Not feasible on commodity hardware.

**Bloom filter math:**
- `m` = number of bits: `m = -n × ln(p) / (ln 2)²`
- `k` = number of hash functions: `k = (m/n) × ln 2`
- For n=5B, p=1%: m ≈ 47.9B bits ≈ **5.7 GB**, k=7

A 5.7GB Bloom filter fits in a Redis instance. False positive rate of 1% means 1 in 100 new URLs is incorrectly skipped — acceptable for a web crawler (the URL will reappear from another link).

**Redis implementation:** `SETBIT bloom:{hash_i(url)} 1` for each of k hashes. `GETBIT` to check. Redis Bitset supports arbitrary bit arrays efficiently.

**Alternative at extreme scale:** Distributed Bloom filter sharded across Redis cluster, or HyperLogLog (counts unique items but can't answer "is X in set?").

### 3. Politeness: robots.txt + Crawl Delay

Being impolite causes real harm: a crawler can DDoS a small website.

**robots.txt rules:**
```
User-agent: *
Disallow: /private/
Crawl-delay: 1
```

The crawler must:
1. Fetch `/robots.txt` before first request to each domain
2. Cache robots.txt for the session (refresh periodically)
3. Never fetch disallowed paths
4. Wait `Crawl-delay` seconds between requests to the same domain

**Per-domain rate limiting implementation:**
```python
# Each worker maintains a dict: domain → last_access_time
last_access = {}
def wait_if_needed(domain):
    gap = time.time() - last_access.get(domain, 0)
    if gap < crawl_delay:
        time.sleep(crawl_delay - gap)
    last_access[domain] = time.time()
```

Since frontier is partitioned by domain, each worker handles a fixed set of domains — the rate limit state is local, no Redis coordination needed.

### 4. Distributed Crawling: Partition by Domain Hash

**Problem:** 463 crawler workers, 200M domains, each domain needs its own rate limiter.

**Solution:** Partition Kafka topics by `hash(domain) % num_partitions`. Worker 0 owns partitions 0–9, worker 1 owns 10–19, etc. Now:
- Each domain is always processed by the same worker
- Worker can maintain local `last_access` dict — no distributed coordination
- If worker crashes, Kafka rebalances partitions to other workers

**URL assignment:**
```
partition = murmur3(domain) % num_partitions
```

Workers consume from their assigned partitions with long-poll. Frontier URL processing looks like:
1. Poll frontier Kafka partition → get URL batch
2. Group by domain → apply politeness delays
3. Fetch → emit links to frontier topic (partitioned by domain)
4. Emit raw HTML to content topic (consumed by indexers)

### 5. Crawler Traps: Depth Limit + URL Normalization

**Common traps:**
- **Infinite calendars:** `/calendar/2024/1` links to `/calendar/2024/2` → `/calendar/2024/999`
- **Session IDs in URLs:** `/page?session=abc123` and `/page?session=def456` are the same page
- **Incrementing parameters:** `?page=1` → `?page=2` → `?page=99999`
- **Printer-friendly versions:** `/print/article/123` mirrors `/article/123`

**Defenses:**

1. **Depth limit:** never follow a link more than D hops from seed (D=5 is common)
2. **URL normalization:**
   - Lowercase scheme and host
   - Remove session ID parameters (`jsessionid`, `phpsessid`, `sid`)
   - Sort query parameters alphabetically
   - Remove fragment (`#section`)
   - Canonicalize trailing slashes
3. **Parameter value threshold:** if numeric parameter exceeds threshold (page > 100), skip
4. **Similarity detection:** if 90%+ of links on a page point to URLs that differ only in one incrementing parameter, flag as trap

**URL normalization example:**
```
Input:  HTTP://Example.COM/Path?b=2&a=1&sid=abc123#section
Output: http://example.com/Path?a=1&b=2
```

### 6. Content Deduplication: SimHash for Near-Duplicates

URL deduplication (Bloom filter) catches exact URL matches. But the web has massive duplicate content at distinct URLs: printer-friendly mirrors, syndicated articles, CMS pagination variants, A/B test URL variants.

**SimHash** (Charikar 2002) is a locality-sensitive hash where similar texts produce hashes with small Hamming distance:

1. Tokenise page body into 3-word shingles: `["the quick brown", "quick brown fox", ...]`
2. Hash each shingle to a 64-bit integer (MurmurHash)
3. For each bit position i, accumulate +1 if bit i is 1, -1 if 0
4. Final fingerprint: bit i = 1 iff `sum[i] > 0`

**Near-duplicate threshold:** Two pages with `Hamming(fp_A, fp_B) <= 3` are considered near-duplicates (~95%+ content similarity).

**Production implementation:**
- Compute SimHash after stripping HTML tags (content-only)
- Store 64-bit fingerprint alongside URL in Postgres metadata table
- Index pipeline: before indexing a page, query for existing fingerprints within Hamming distance 3 (via Simhash lookup tables or brute-force scan within shards)
- Keep only the canonical URL; mark near-dups as `canonical_url = <winner>`
- Impact: Google estimates ~30% of crawled pages are near-duplicates — SimHash avoids indexing redundant content

---

## Failure Modes

These are the deep-dive topics that separate senior from staff-level answers.

| Failure | Symptom | Mitigation |
|---|---|---|
| Bloom filter false positives | Valid new URLs silently skipped | Acceptable at 1% FPR; tune m/k; rebuild filter periodically from stored URLs |
| Bloom filter RAM exhaustion | Redis OOM, filter lost | Use Redis cluster; snapshot filter to disk (BGSAVE) hourly |
| Worker crash mid-crawl | Kafka partition rebalanced; pages re-fetched | Kafka consumer group rebalance; page-level idempotency (`ON CONFLICT DO NOTHING` in Postgres) |
| Kafka consumer lag spike | Frontier URLs pile up; crawl falls behind | Monitor lag; auto-scale workers; throttle link extraction on low-priority topics |
| Politeness rate exceeded | Target site responds 429/503 | Increase per-domain delay on 429 response; implement exponential backoff |
| Crawler trap slips through | Frontier grows unbounded | Depth limit + param threshold + per-domain URL count cap (e.g., max 100K URLs per domain) |
| robots.txt changes mid-crawl | Newly disallowed paths fetched | TTL-based robots.txt refresh (e.g., re-fetch every 24h); re-crawl flagged pages |
| DNS poisoning / redirect loops | Crawler follows redirect cycle indefinitely | Redirect depth limit (max 5 hops); detect cycle by tracking redirect chain per request |
| Content store (S3) write failure | Raw HTML lost; indexer starved | Retry with exponential backoff; dead-letter queue for failed writes; alert on DLQ depth |
| Clock skew across workers | Priority scores computed incorrectly | Use NTP-synchronized clocks; prefer monotonic counters for relative ordering over wall clock |

**Key failure mode for the interview:** The Bloom filter is a shared mutable structure. In a distributed crawler, you need either (a) a single Redis Bloom filter with network round-trips on every URL check, or (b) per-worker local Bloom filters that diverge (workers may fetch the same URL). Production systems typically accept (b) with a reconciliation pass after each crawl cycle, or use a distributed Bloom filter sharded by URL hash across a Redis cluster.

---

## How It Actually Works

**CommonCrawl** is the most publicly documented large-scale web crawler. Their architecture (from published datasets and documentation):
- Crawls ~3.5B pages per monthly crawl
- Uses Apache Nutch (open source crawler) with custom extensions
- URL frontier stored in distributed queues, partitioned by domain
- Raw HTML stored in WARC format on S3 (publicly accessible at commoncrawl.org)
- Bloom filter for URL deduplication across crawl runs

**Googlebot** (from Google's public documentation):
- Multiple crawler variants: Googlebot (web), Googlebot-Image, Googlebot-Video
- Politeness enforced: Googlebot is explicitly designed to avoid overwhelming servers
- robots.txt is fetched and cached per crawl cycle
- Sitemap.xml support for explicit URL discovery
- Crawl budget: each site gets a limited number of crawls/day based on quality signals

Key insight from both systems: **domain partitioning** (not URL hash partitioning) is critical for politeness. If URLs were partitioned by hash(url), the same domain's URLs would land on different workers, making per-domain rate limiting a distributed coordination problem.

Source: CommonCrawl "About" documentation; Google Search Central "How Google Search Works" (2024); Cho & Garcia-Molina, "The Evolution of the Web and Implications for an Incremental Crawler" (VLDB 2000).

---

## Hands-on Lab

**Time:** ~20 minutes
**Services:** `redis` (Redis 7), `db` (Postgres 15), `mock-server` (Flask, 50 pages), `crawler` (Python)

### Setup

```bash
cd system-design-interview/03-case-studies/07-web-crawler/
docker compose up -d redis db mock-server
# Wait ~20s for services to be healthy
docker compose ps
```

### Experiment

```bash
# Run the crawler experiment (one-shot)
docker compose --profile experiment run --rm crawler

# Or locally (faster iteration):
pip install redis psycopg2-binary mmh3 flask
# Terminal 1: start mock server
python mock_server.py
# Terminal 2: run experiment
MOCK_SERVER_URL=http://localhost:5100 python experiment.py
```

The script runs 9 phases automatically:

1. **BFS crawl:** seed 5 URLs → BFS → crawl up to 30 pages, show frontier stats
2. **Bloom filter demo:** show deduplication — URL seen twice is skipped
3. **Politeness:** show per-domain rate limiting with wait times
4. **robots.txt:** show /private/* pages are never fetched
5. **Trap detection:** show ?page=N URLs blocked above threshold
6. **URL normalization:** collapse session IDs, tracking params, fragments; verify with test cases
7. **SimHash:** compute 64-bit fingerprints; show near-duplicate detection by Hamming distance
8. **Scale math:** compute real numbers for 5B page crawl including DNS/robots.txt/IOPS overhead
9. **DB summary:** show crawled pages stored in Postgres by domain/depth

### Break It

**Disable politeness and observe rate:** modify `PolitenessTracker` delay to 0 and watch requests hit the mock server with no delay. Observe how quickly a small server could be overwhelmed.

**Remove depth limit** (`MAX_DEPTH = 999`) and start the crawl from the `/trap` URL:

```bash
# Edit experiment.py: change seed_urls to include /trap
# Watch the frontier grow indefinitely until MAX_PAGES budget is hit
```

**Fill Bloom filter to observe false positives:** use a tiny filter (`size=1000`) and add 10,000 URLs. Watch legitimate new URLs get incorrectly skipped.

### Observe

```bash
# Watch pages accumulate in Postgres
docker compose exec db psql -U app -d crawler \
  -c "SELECT domain, COUNT(*) FROM crawled_pages GROUP BY domain;"

# Check which depths were reached
docker compose exec db psql -U app -d crawler \
  -c "SELECT depth, COUNT(*) FROM crawled_pages GROUP BY depth ORDER BY depth;"
```

### Teardown

```bash
docker compose down -v
```

---

## Interview Checklist

1. **Q: How do you avoid re-crawling the same URL twice?**
   A: Bloom filter. At 5B URLs a hash set needs ~320GB RAM; a Bloom filter with 1% FPR needs ~6GB. We accept the 1% false positive rate (occasionally skipping a new URL) as it's much cheaper than storing every URL. URLs are normalized before insertion (lowercase, sorted params, no fragments) to collapse duplicates.

2. **Q: How do you ensure you're not overloading a small website?**
   A: Three layers: (1) fetch and respect robots.txt Crawl-delay, (2) per-domain rate limiting in each worker (minimum gap between requests to same domain), (3) domain partitioning assigns each domain to one worker so rate limiting is local with no coordination overhead.

3. **Q: How do you prioritize which pages to crawl first?**
   A: Separate Kafka topics per priority tier. PageRank (link-based importance), freshness (time since last crawl × expected change frequency), and content type (news vs static) all factor into a priority score. High-priority topics are consumed first by workers.

4. **Q: What is a crawler trap and how do you detect one?**
   A: A page that generates an infinite link graph — calendar URLs, session IDs in query strings, infinite pagination. Detection: depth limit (never follow beyond D hops from seed), URL normalization (remove session IDs), parameter value threshold (skip ?page=N if N > 100), and structure analysis (flag pages where 90%+ of links differ only in an incrementing parameter).

5. **Q: How do you keep 5B pages fresh with a 2-week crawl cycle?**
   A: 400M pages/day = 4,630 pages/second. Distribute across ~500 crawler workers (each fetching ~10 pages/second, limited by politeness delays). Re-crawl priority uses historical change frequency: pages that change daily (news) get recrawled daily; static pages get scheduled monthly.

6. **Q: How does partitioning the URL frontier by domain help?**
   A: Each worker owns a fixed set of domains (via hash(domain) % partitions). The worker can enforce per-domain rate limiting using local state — no distributed lock or Redis coordination. If a worker is restarted, its partition is reassigned and the rate limit resets (a brief burst that self-corrects).

7. **Q: How do you handle JavaScript-rendered pages?**
   A: Standard HTTP fetch only gets raw HTML. JS-heavy pages (SPAs) require a headless browser (Chrome via Puppeteer). This is ~100× more expensive per page. Production approach: fetch raw HTML first, detect single-page apps (empty body, large JS bundle), put them in a separate "JS render" queue processed by Chromium workers with lower throughput.

8. **Q: How do you handle duplicate content at different URLs?**
   A: URL deduplication (Bloom filter) handles exact URL matches. For near-duplicate content (same article, different URLs): compute SimHash (a locality-sensitive hash) of page content. Two pages with Hamming distance < 3 on their SimHash are near-duplicates. Store canonical URL in Postgres with `canonical_url` column.

9. **Q: What is your storage architecture for 250TB of raw HTML?**
   A: Raw HTML in WARC format (Web ARChive) on object storage (S3). 250TB at $0.023/GB/month = $5,750/month for storage. Each WARC file contains multiple pages. Metadata (URL, fetch time, content hash, HTTP headers) in Postgres for querying. Index pipeline reads from S3, not from Postgres directly.

10. **Q: Walk me through what happens when the crawler discovers a new URL.**
    A: (1) Link extractor parses href from fetched HTML. (2) URL is normalized (lowercase, sorted params, no fragment). (3) Check Bloom filter — if "seen", discard. (4) If not seen, add to Bloom filter. (5) Compute `hash(domain) % partitions` to find target Kafka partition. (6) Produce URL to frontier Kafka topic. (7) Worker assigned to that partition eventually consumes the URL, checks robots.txt cache, waits for politeness delay, fetches, and repeats.
