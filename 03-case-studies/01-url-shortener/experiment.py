#!/usr/bin/env python3
"""
URL Shortener Lab -- experiment.py

What this demonstrates:
  1. Create 1,000 short URLs and measure throughput
  2. Time redirect with vs. without cache -- show 10-100x speedup
  3. Base62 encoding: how sequential IDs produce compact, URL-safe codes
  4. Cache hit-rate warming under Zipf traffic (cold -> warm curve)
  5. Hit counting: Redis INCR vs naive per-redirect Postgres UPDATE
  6. Hash vs. sequential vs. Snowflake-style ID generation trade-offs
  7. Birthday paradox: collision probability for hash-based IDs
  8. Thundering herd: flush Redis, fire concurrent requests, observe latency spike

Run:
  docker compose up -d
  # Wait ~20-30s for services to become healthy
  docker compose ps      # confirm web shows (healthy)
  python experiment.py
"""

import hashlib
import math
import random
import string
import time
import threading
import urllib.error
import urllib.parse
import urllib.request
import json

BASE_URL = "http://localhost:5001"
BASE62 = string.digits + string.ascii_letters


# -- Helpers ------------------------------------------------------------------

def section(title):
    print(f"\n{'=' * 66}")
    print(f"  {title}")
    print("=" * 66)


def wait_for_service(url, max_wait=60):
    print(f"  Waiting for service at {url} ...")
    for i in range(max_wait):
        try:
            urllib.request.urlopen(f"{url}/health", timeout=3)
            print(f"  Service ready after {i+1}s")
            return
        except Exception as e:
            if i % 10 == 9:
                print(f"  Still waiting... ({i+1}s elapsed, last error: {e})")
            time.sleep(1)
    raise RuntimeError(f"Service did not start within {max_wait}s")


def post_json(path, data):
    body = json.dumps(data).encode()
    req = urllib.request.Request(
        f"{BASE_URL}{path}",
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=10) as resp:
        return json.loads(resp.read())


def get_json(path):
    with urllib.request.urlopen(f"{BASE_URL}{path}", timeout=10) as resp:
        return json.loads(resp.read())


def time_redirect(code):
    """Time one redirect request. Returns elapsed ms without following the redirect."""
    url = f"{BASE_URL}/{code}"
    req = urllib.request.Request(url, method="GET")
    opener = urllib.request.build_opener(urllib.request.HTTPErrorProcessor())
    start = time.perf_counter()
    try:
        opener.open(req, timeout=10)
    except (urllib.error.HTTPError, urllib.error.URLError):
        pass
    return (time.perf_counter() - start) * 1000  # ms


def percentile(data, pct):
    sorted_data = sorted(data)
    k = (len(sorted_data) - 1) * pct / 100
    lo, hi = int(k), min(int(k) + 1, len(sorted_data) - 1)
    return sorted_data[lo] + (sorted_data[hi] - sorted_data[lo]) * (k - lo)


# -- Base62 utilities (mirror of app.py) --------------------------------------

def base62_encode(num: int) -> str:
    if num == 0:
        return BASE62[0]
    chars = []
    while num:
        chars.append(BASE62[num % 62])
        num //= 62
    return "".join(reversed(chars))


def base62_decode(s: str) -> int:
    result = 0
    for ch in s:
        result = result * 62 + BASE62.index(ch)
    return result


# -- Phase 1: Bulk URL creation -----------------------------------------------

def phase1_bulk_create():
    section("Phase 1: Create 1,000 Short URLs")
    domains = ["https://example.com", "https://github.com", "https://news.ycombinator.com"]
    codes = []
    start = time.perf_counter()
    for i in range(1000):
        url = (
            f"{random.choice(domains)}/path/{i}/"
            f"{''.join(random.choices(string.ascii_lowercase, k=6))}"
        )
        result = post_json("/shorten", {"url": url})
        codes.append(result["short_code"])
    elapsed = time.perf_counter() - start

    print(f"\n  Created 1,000 URLs in {elapsed:.2f}s  ({1000/elapsed:.0f} req/s)")
    print(f"\n  Sample codes (first 10):")
    print(f"  {'Decoded ID':>12}  {'Short Code':>12}  {'Chars':>6}")
    print(f"  {'-'*12}  {'-'*12}  {'-'*6}")
    for code in codes[:10]:
        decoded = base62_decode(code)
        print(f"  {decoded:>12}  {code:>12}  {len(code):>6}")

    print(f"\n  Base62 capacity:")
    print(f"    6 chars: {62**6:>15,} unique codes")
    print(f"    7 chars: {62**7:>15,} unique codes")
    print(f"    At 1,000 writes/day, 6-char codes last {62**6 // 1000 // 365:,} years")
    return codes


# -- Phase 2: Cache warm-up and latency comparison ----------------------------

def phase2_cache_latency(codes):
    section("Phase 2: Redirect Latency -- Cache Cold vs. Warm")

    # Use a slice not yet warmed -- codes[30:] were not touched in Phase 1's
    # read path. Phase 1 only did POST /shorten (writes); it did not issue
    # any GET /<code> requests, so these codes are not in Redis yet.
    sample = codes[:30]

    # Cold pass: first GET for each code hits Postgres (cache miss).
    cold_times = [time_redirect(code) for code in sample]

    # Warm pass: second GET for each code hits Redis (cache hit).
    warm_times = [time_redirect(code) for code in sample]

    def fmt_stats(times):
        return (
            f"avg={sum(times)/len(times):5.1f}ms  "
            f"P50={percentile(times,50):5.1f}ms  "
            f"P95={percentile(times,95):5.1f}ms  "
            f"P99={percentile(times,99):5.1f}ms  "
            f"max={max(times):5.1f}ms"
        )

    print(f"\n  30 URLs, each requested twice (cold then warm):\n")
    print(f"  Cold (Postgres): {fmt_stats(cold_times)}")
    print(f"  Warm (Redis):    {fmt_stats(warm_times)}")
    avg_speedup = (sum(cold_times) / len(cold_times)) / (sum(warm_times) / len(warm_times))
    print(f"\n  Speedup: {avg_speedup:.1f}x  (avg cold / avg warm)")

    stats = get_json("/stats")
    print(f"\n  Global cache stats after Phase 2:")
    print(f"    Hits:     {stats['cache_hits']}")
    print(f"    Misses:   {stats['cache_misses']}")
    print(f"    Hit rate: {stats['hit_rate_pct']}%")

    print("""
  Why P95/P99 matters more than average in production:
    The average flattens the tail. At 115K RPS, even a 1% tail means
    1,150 slow requests per second felt by real users. SLOs are almost
    always expressed as P99, not average. P99 is also what drives
    timeout budgets in dependent services (CDN, mobile clients).

  The cold-vs-warm gap in this local experiment is smaller than in
  production because both Postgres and Redis are on the same host.
  In a real deployment (cross-AZ), the cold path adds ~1-5ms of network
  latency on top of the Postgres query time, widening the gap further.
""")


# -- Phase 3: Base62 encoding demo --------------------------------------------

def phase3_base62_demo():
    section("Phase 3: Base62 Encoding -- Sequential IDs to Short Codes")

    print(f"\n  Alphabet (62 chars): {BASE62}")
    print(f"\n  {'ID':>14}  {'Base62':>10}  {'Len':>4}  {'Max IDs at this length':>22}")
    print(f"  {'-'*14}  {'-'*10}  {'-'*4}  {'-'*22}")

    ids = [1, 62, 3844, 100_000, 1_000_000, 56_800_235_584, 1_000_000_000_000]
    for i in ids:
        code = base62_encode(i)
        max_at_len = 62 ** len(code)
        print(f"  {i:>14,}  {code:>10}  {len(code):>4}  {max_at_len:>22,}")

    print(f"""
  Key insight: base62 uses only alphanumeric characters [0-9A-Za-z].
  No +, /, or = like base64, so codes are safe in URLs without encoding.

  Sequential IDs give the shortest possible encoding for a given number:
    ID 1        -> "1"   (1 char,  62 possibilities)
    ID 62       -> "10"  (2 chars, boundary at 62^1)
    ID 3844     -> "100" (3 chars, boundary at 62^2)

  The predictability trade-off: sequential codes are crawlable. An attacker
  can enumerate all URLs by iterating "1", "2", ... "zzzzz". Mitigations:
    1. Add a random epoch offset to the auto-increment sequence
    2. XOR the ID with a private salt before encoding
    3. Switch to Snowflake IDs (time-sorted, not enumerable without epoch)
""")


# -- Phase 4: Cache hit rate warming ------------------------------------------

def phase4_cache_warming(codes):
    section("Phase 4: Cache Hit Rate -- Warming Curve Under Zipf Traffic")

    # Use a slice not yet warmed -- codes[30:] were not touched in Phase 2.
    fresh_codes = codes[30:130]  # 100 codes

    print(f"  Simulating 500 requests to 100 URLs with Zipf distribution.")
    print(f"  URL[0] is ~100x more popular than URL[99] (power-law traffic).\n")
    print(f"  Reported hit rate is from the server's Redis stat counters,")
    print(f"  not a local estimate -- this reflects real cache behaviour.\n")

    # Reset server-side stats so Phase 4 numbers are isolated.
    # We do this by reading the current totals and subtracting them later.
    baseline = get_json("/stats")
    baseline_hits   = baseline["cache_hits"]
    baseline_misses = baseline["cache_misses"]

    weights = [1 / (i + 1) for i in range(len(fresh_codes))]
    total_w = sum(weights)
    weights = [w / total_w for w in weights]

    snapshots = []
    for i in range(500):
        code = random.choices(fresh_codes, weights=weights)[0]
        time_redirect(code)
        if (i + 1) % 50 == 0:
            s = get_json("/stats")
            phase_hits   = s["cache_hits"]   - baseline_hits
            phase_misses = s["cache_misses"] - baseline_misses
            phase_total  = phase_hits + phase_misses
            rate = phase_hits / phase_total * 100 if phase_total else 0
            snapshots.append((i + 1, phase_hits, phase_misses, rate))

    print(f"  {'Requests':>10}  {'Hits':>8}  {'Misses':>8}  {'Hit Rate':>10}  Bar")
    print(f"  {'-'*10}  {'-'*8}  {'-'*8}  {'-'*10}  {'-'*30}")
    for req_count, hits, misses, rate in snapshots:
        bar = "#" * int(rate / 2)
        print(f"  {req_count:>10}  {hits:>8}  {misses:>8}  {rate:>9.1f}%  {bar}")

    print(f"""
  Under Zipf distribution, ~20% of URLs receive ~80% of traffic (Pareto).
  The cache warms quickly for those hot URLs and plateau near the theoretical
  maximum hit rate for the working set size.

  Real implication: a 3 GB Redis cache for 10M "hot" URLs absorbs 99%+ of
  reads at bit.ly scale because viral links are heavily Zipf-distributed.
  The tail (cold URLs) hits Postgres, but that is a tiny fraction of volume.
""")


# -- Phase 5: Hit counting -- Redis INCR vs Postgres UPDATE -------------------

def phase5_hit_counting(codes):
    section("Phase 5: Hit Counting -- Redis INCR + Batch Flush vs. Per-Redirect UPDATE")

    print("""
  Naive approach: UPDATE urls SET hits = hits + 1 WHERE short_code = ?
  Problem: row-locking UPDATE on every redirect. At 115K RPS this means
  115,000 row locks per second -- Postgres falls over.

  Production approach (what this service implements):
    1. On each redirect: INCR hits:<code>  in Redis  (O(1), atomic)
    2. A background worker (or POST /flush_hits) bulk-updates Postgres
       once per flush interval with the accumulated delta.

  Demonstrating the pattern:
""")

    # Pick 5 codes to track
    tracked = codes[:5]
    hit_counts = {code: 0 for code in tracked}

    # Fire 20 redirects spread across tracked codes
    for _ in range(20):
        code = random.choice(tracked)
        time_redirect(code)
        hit_counts[code] += 1

    print(f"  Fired 20 redirects. Expected hit counts: {hit_counts}")
    print(f"  Checking Postgres BEFORE flush (hits column should be 0 or stale)...")

    # Before flush: Postgres hits should still be 0 for these codes
    # (redirects went to Redis and incremented only Redis counters)
    # We can't query Postgres directly from here, so we show the Redis counters.
    import urllib.request as _req
    try:
        import redis as _redis_mod
        r = _redis_mod.from_url("redis://localhost:6379", decode_responses=True)
        print(f"\n  Redis counters (hits:*) before flush:")
        for code in tracked:
            val = r.get(f"hits:{code}") or "0"
            print(f"    hits:{code:<10} = {val}")

        # Flush to Postgres
        print(f"\n  Calling POST /flush_hits ...")
        result = post_json("/flush_hits", {})
        print(f"  Flush result: {result}")

        print(f"\n  Redis counters after flush (should be gone -- GETDEL was atomic):")
        for code in tracked:
            val = r.get(f"hits:{code}") or "(deleted)"
            print(f"    hits:{code:<10} = {val}")

    except Exception as e:
        print(f"  (Redis direct check skipped -- run with Docker: {e})")
        result = post_json("/flush_hits", {})
        print(f"  Flush result: {result}")

    print(f"""
  This pattern converts 115K Postgres writes/second into one bulk UPDATE
  per flush cycle (~60s in production). The trade-off: hit counts in
  Postgres lag by up to one flush interval. For a URL shortener this is
  perfectly acceptable. For billing or fraud detection, use synchronous
  writes or a streaming pipeline (Kafka -> Flink -> OLAP store).
""")


# -- Phase 6: ID generation strategies ----------------------------------------

def phase6_id_generation():
    section("Phase 6: ID Generation Strategies -- Trade-offs")

    print(f"""
  Three approaches to generating short codes:

  +---------------------+------------------+-------------------+------------------+
  | Strategy            | Collision Risk   | Predictable?      | Distributed OK?  |
  +---------------------+------------------+-------------------+------------------+
  | Hash (MD5 truncated)| Yes (birthday)   | No (good)         | Yes              |
  | Sequential auto-inc | None             | Yes (bad for UX)  | No (single DB)   |
  | Snowflake-style     | None             | No (time-ordered) | Yes              |
  +---------------------+------------------+-------------------+------------------+
""")

    # Hash-based
    print("  Hash-based: first 7 hex chars of MD5(url)")
    sample_urls = [
        "https://example.com/article/1",
        "https://example.com/article/2",
        "https://github.com/user/repo",
    ]
    for url in sample_urls:
        h = hashlib.md5(url.encode()).hexdigest()
        code = h[:7]
        print(f"    {url:<42} -> {code}")

    # Correct Snowflake bit layout: timestamp(41b) | datacenter(5b) | machine(5b) | seq(12b)
    # This is Twitter's original Snowflake schema.
    print(f"""
  Snowflake-style: 64-bit integer composed of three fields.
  Twitter's original layout:
    Bits 63-22: timestamp_ms relative to epoch (41 bits, ~69 years of range)
    Bits 21-17: datacenter ID (5 bits, up to 32 datacenters)
    Bits 16-12: machine ID    (5 bits, up to 32 machines per datacenter)
    Bits 11-0:  sequence      (12 bits, 4096 IDs per millisecond per machine)

  Max throughput per machine: 4,096 IDs/ms = 4.1M IDs/second.
  No coordination needed -- machine/datacenter IDs assigned at startup.
""")
    EPOCH = 1_288_834_974_657  # Twitter Snowflake epoch (Nov 4 2010)
    datacenter_id = 1
    machine_id = 1
    print(f"  {'Timestamp offset':>18}  {'DC':>4}  {'Mach':>6}  {'Seq':>5}  {'Snowflake ID':>20}  {'Base62':>10}")
    print(f"  {'-'*18}  {'-'*4}  {'-'*6}  {'-'*5}  {'-'*20}  {'-'*10}")
    base_ts = int(time.time() * 1000) - EPOCH
    for seq in range(5):
        ts_offset = base_ts + seq  # one ms apart
        snow_id = (ts_offset << 22) | (datacenter_id << 17) | (machine_id << 12) | seq
        code = base62_encode(snow_id)
        print(f"  {ts_offset:>18}  {datacenter_id:>4}  {machine_id:>6}  {seq:>5}  {snow_id:>20}  {code:>10}")

    print(f"""
  Snowflake advantages:
    - No central coordination (machine_id/datacenter_id assigned at startup)
    - Time-sortable (useful for analytics: new IDs are always larger)
    - No collisions by construction within the same ms + machine
    - Used by: Twitter/X (tweet IDs), Instagram, Discord, Mastodon

  Snowflake risk: clock skew. If a machine's clock jumps backward, it can
  generate a duplicate ID. Production implementations refuse to issue IDs
  until the clock catches up past the last-seen timestamp.
""")


# -- Phase 7: Birthday paradox collision probability --------------------------

def phase7_birthday_paradox():
    section("Phase 7: Birthday Paradox -- Hash Collision Probability")

    print(f"""
  When using hash(url)[:N] as a short code, what is the probability of
  a collision after K URLs have been stored?  (Birthday problem)

  Approximation: P(collision) ~= 1 - e^(-K^2 / 2N)
  where N = total number of distinct possible codes.

  For base62 codes (62 choices per character):
""")

    print(f"  {'Chars':>6}  {'Total codes (62^N)':>20}  {'1% collision at K':>20}  {'50% collision at K':>20}")
    print(f"  {'-'*6}  {'-'*20}  {'-'*20}  {'-'*20}")

    for n_chars in [5, 6, 7, 8, 10]:
        total = 62 ** n_chars
        # Solve 0.01 = 1 - e^(-K^2/2N)  =>  K = sqrt(-2N * ln(0.99))
        k_1pct  = int(math.sqrt(-2 * total * math.log(0.99)))
        k_50pct = int(math.sqrt(-2 * total * math.log(0.5)))
        print(f"  {n_chars:>6}  {total:>20,}  {k_1pct:>20,}  {k_50pct:>20,}")

    print(f"""
  With 6 base62 chars (~56 billion codes):
    1% collision risk reached at ~1.06 billion URLs -- well beyond typical
    deployment scale. This is why base62 is strongly preferred over hex
    for short codes: same character length, 3.8x more distinct values.

  Hex (16 chars) comparison for 6-char codes:
    Hex:    16^6 = {16**6:,} codes  (1% collision at ~{int(math.sqrt(-2*16**6*math.log(0.99))):,} URLs)
    Base62: 62^6 = {62**6:,} codes  (1% collision at ~{int(math.sqrt(-2*62**6*math.log(0.99))):,} URLs)

  Production preference ranking:
    1. Sequential IDs + base62  -- no collisions, compact, simple
    2. Snowflake IDs + base62   -- no collisions, distributed, time-sorted
    3. Hash + collision retry   -- stateless generation, but retry loop adds
                                   latency and complexity at scale
""")


# -- Phase 8: Thundering herd demonstration ------------------------------------

def phase8_thundering_herd(codes):
    section("Phase 8: Thundering Herd -- Cache Flush + Concurrent Redirects")

    print("""
  The thundering herd problem: when a cache layer is flushed or restarts,
  all in-flight requests simultaneously miss and hit the backend together.
  For a URL shortener at 115K RPS, this can instantly overload Postgres.

  This phase:
    1. Measures baseline latency while Redis is warm
    2. Flushes Redis (simulating a restart or FLUSHALL)
    3. Fires N concurrent redirects at the same codes -- all miss Redis,
       all hit Postgres simultaneously
    4. Compares cold-burst latency to the warm baseline
    5. Shows how latency recovers as Redis repopulates
""")

    # Pick a set of codes that are guaranteed to be warm in Redis
    # (they were created in Phase 1 and accessed in Phase 2).
    warm_codes = codes[:20]

    # Step 1: measure warm baseline (serial, no contention)
    print("  [1/4] Measuring warm-cache baseline (serial, 20 requests)...")
    warm_times = [time_redirect(code) for code in warm_codes]
    warm_avg = sum(warm_times) / len(warm_times)
    warm_p99 = percentile(warm_times, 99)
    print(f"        avg={warm_avg:.1f}ms  P99={warm_p99:.1f}ms  (Redis warm)")

    # Step 2: flush Redis
    print("\n  [2/4] Flushing Redis cache (FLUSHALL)...")
    try:
        import redis as _redis_mod
        r = _redis_mod.from_url("redis://localhost:6379", decode_responses=True)
        r.flushall()
        print("        Redis flushed via redis-py.")
    except Exception:
        # Fallback: Docker exec
        import subprocess
        result = subprocess.run(
            ["docker", "compose", "exec", "-T", "cache", "redis-cli", "FLUSHALL"],
            capture_output=True, text=True,
            cwd="/Users/bimurto/Work/personal/system-design-content/system-design-interview/03-case-studies/01-url-shortener"
        )
        if result.returncode == 0:
            print("        Redis flushed via docker compose exec.")
        else:
            print(f"        WARNING: Could not flush Redis ({result.stderr.strip()}). Skipping herd simulation.")
            return

    # Step 3: fire concurrent requests -- simulate the thundering herd
    concurrency = 40
    print(f"\n  [3/4] Firing {concurrency} concurrent redirects (all cache misses -> Postgres)...")
    burst_times = []
    errors = []
    lock = threading.Lock()

    def concurrent_redirect(code):
        t = time_redirect(code)
        with lock:
            burst_times.append(t)

    threads = [
        threading.Thread(target=concurrent_redirect, args=(random.choice(warm_codes),))
        for _ in range(concurrency)
    ]
    burst_start = time.perf_counter()
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    burst_elapsed = time.perf_counter() - burst_start

    if burst_times:
        burst_avg = sum(burst_times) / len(burst_times)
        burst_p99 = percentile(burst_times, 99)
        slowdown = burst_avg / warm_avg if warm_avg > 0 else float("inf")
        print(f"        avg={burst_avg:.1f}ms  P99={burst_p99:.1f}ms  "
              f"slowdown={slowdown:.1f}x vs warm baseline")
        print(f"        ({len(burst_times)} requests completed in {burst_elapsed:.2f}s)")

    # Step 4: measure recovery -- Redis is now re-warming
    print("\n  [4/4] Measuring recovery (same codes, now repopulated in Redis)...")
    recovery_times = [time_redirect(code) for code in warm_codes]
    recovery_avg = sum(recovery_times) / len(recovery_times)
    recovery_p99 = percentile(recovery_times, 99)
    print(f"        avg={recovery_avg:.1f}ms  P99={recovery_p99:.1f}ms  (Redis warm again)")

    # Summary table
    print(f"""
  Summary:
    Phase              avg latency   P99 latency   vs baseline
    -----------------  -----------   -----------   -----------
    Warm (baseline)    {warm_avg:>7.1f}ms    {warm_p99:>7.1f}ms    1.0x
    Cold burst (herd)  {burst_avg:>7.1f}ms    {burst_p99:>7.1f}ms    {burst_avg/warm_avg:.1f}x
    Recovery           {recovery_avg:>7.1f}ms    {recovery_p99:>7.1f}ms    {recovery_avg/warm_avg:.1f}x

  Observations:
    - The cold burst latency spike reflects {concurrency} threads all hitting
      Postgres simultaneously. In production at 115K RPS, this spike would be
      orders of magnitude larger and would likely cause a cascade failure.
    - Recovery is immediate once Redis repopulates: the first miss per code
      re-warms the cache, and all subsequent requests are fast again.

  Production mitigations for the thundering herd:
    1. Request coalescing: only one goroutine/thread queries the DB per cache
       key; all others wait and share the result. Reduces fan-out from N to 1.
    2. Cache warm-up script: before reopening traffic after a cache restart,
       pre-populate the top 100K hot URLs via SELECT ... ORDER BY hits DESC.
    3. Staggered TTL jitter: instead of all keys expiring at the same time
       (e.g., all set to TTL=86400s at startup), add random(0, 3600) jitter
       to spread expirations across the hour.
    4. Probabilistic early expiration (PER): re-fetch a cache entry slightly
       before it expires using a probability proportional to how close the
       TTL is to zero. Prevents synchronised expiry of hot keys.
""")


# -- Main ---------------------------------------------------------------------

def main():
    section("URL SHORTENER LAB")
    print("""
  Architecture:
    Client -> Flask (base62 encode) -> Postgres (source of truth)
                  |
                  +-> Redis cache-aside (TTL 24h, allkeys-lru eviction)

  Cache-aside read path:
    1. Check Redis  -> hit: return 302, INCR hits:<code> in Redis
    2. Miss: query Postgres, populate Redis, INCR hits:<code> in Redis
    3. Background flush (/flush_hits): GETDEL counters, bulk UPDATE Postgres

  This experiment walks through eight phases covering the key design
  decisions a senior engineer would discuss in a FAANG interview.
""")

    wait_for_service(BASE_URL)

    codes = phase1_bulk_create()
    phase2_cache_latency(codes)
    phase3_base62_demo()
    phase4_cache_warming(codes)
    phase5_hit_counting(codes)
    phase6_id_generation()
    phase7_birthday_paradox()
    phase8_thundering_herd(codes)

    section("Lab Complete")
    print("""
  Key takeaways:
    Cache-aside cuts redirect latency 10-100x (Postgres -> Redis)
    Base62 vs hex: same code length, 3.8x more distinct values per char
    Sequential IDs: zero collisions, compact, but sequentially enumerable
    Hash IDs: stateless generation, but birthday paradox causes collisions
    Snowflake IDs: distributed, no collisions, time-sorted -- best at scale
    Hit counting: Redis INCR + batch flush eliminates Postgres UPDATE bottleneck
    Thundering herd: cache flush under load causes latency spikes -- mitigate
      with request coalescing, pre-warming, TTL jitter, and probabilistic expiry

  Try this:
    docker compose exec cache redis-cli info stats | grep evicted_keys
    # Evictions show when Redis has to drop LRU keys to make room.
    # In production, monitor this metric and add memory before it spikes.

    docker compose exec db psql -U app urlshortener -c \\
      "SELECT short_code, hits FROM urls ORDER BY hits DESC LIMIT 10;"
    # After running Phase 5, verify that hits were flushed to Postgres.

  Next: 02-twitter-timeline/ -- fan-out on write vs. pull at read time
""")


if __name__ == "__main__":
    main()
